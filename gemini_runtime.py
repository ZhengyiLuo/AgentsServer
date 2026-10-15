"""Gemini runtime resolution and explicit per-process credential isolation.

No login, settings mutation or model request takes place during discovery.
Custom API profiles never reuse the native Google credential directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import selectors
import signal
import subprocess
import sys
import tempfile
import time

VERSION = "0.64.0"
AUTH_ENV = (
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_ACCESS_TOKEN", "GOOGLE_GENAI_USE_GCA", "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL", "GEMINI_CLI_AUTH_OVERRIDE",
    "GEMINI_API_KEY_AUTH_MECHANISM", "GEMINI_CLI_USE_COMPUTE_ADC",
    "GOOGLE_GENAI_API_VERSION", "GEMINI_CLI_CUSTOM_HEADERS", "CLOUD_SHELL",
)


class GeminiConfigurationError(ValueError):
    pass


def read_settings(path: Path) -> dict:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise GeminiConfigurationError("Gemini settings exceed the supported size.")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise GeminiConfigurationError("Gemini settings must be a JSON object.")
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise GeminiConfigurationError("Gemini settings could not be read safely.") from exc


def native_home(env: dict[str, str]) -> Path:
    # Unlike the wrapper's memory heuristic, core Storage treats this as a HOME
    # prefix and appends .gemini itself (verified in the pinned CLI package).
    return Path(env.get("GEMINI_CLI_HOME") or env.get("HOME") or Path.home())


def native_credentials_present(env: dict[str, str]) -> bool:
    try:
        directory = native_home(env) / ".gemini"
        settings = read_settings(directory / "settings.json")
        selected = settings.get("security", {}).get("auth", {}).get("selectedType")
        # This is evidence of cached credentials, not a remote validity check.
        return selected == "oauth-personal" and (directory / "oauth_creds.json").is_file()
    except (GeminiConfigurationError, AttributeError):
        return False


def resolve_executable(env: dict[str, str]) -> str | None:
    override = env.get("AGENTSDOCK_GEMINI_BIN")
    candidates = [override] if override else ["gemini", str(Path.home() / ".local/bin/gemini"),
        "/opt/homebrew/bin/gemini", "/usr/local/bin/gemini"]
    for candidate in candidates:
        if candidate and (value := shutil.which(candidate, path=env.get("PATH"))):
            return str(Path(value).absolute())
    return None


def probe(env: dict[str, str], *, timeout: float = 8) -> dict:
    executable = resolve_executable(env)
    base = {"backend": "gemini", "available": False, "authenticated": None,
            "installed": bool(executable), "version": None, "_executable": executable}
    if not executable:
        return {**base, "status": "missing", "message": "Gemini CLI is not installed.",
                "action": "Install @google/gemini-cli on the server, then run gemini to sign in."}
    try:
        version = version_output(executable, env, timeout).strip()
    except (OSError, subprocess.SubprocessError):
        return {**base, "status": "error", "message": "Gemini CLI version check failed.",
                "action": "Check gemini --version on the server, then retry."}
    if version != VERSION:
        return {**base, "status": "error", "version": version[:64],
                "message": f"This Gemini integration requires CLI {VERSION}.",
                "action": f"Install @google/gemini-cli@{VERSION} on the server."}
    return {**base, "version": version, "status": "ready", "available": True,
            "message": "Gemini checks the selected credentials when a message is sent.", "action": None}


def version_output(executable: str, env: dict[str, str], timeout: float) -> str:
    """A broken wrapper cannot fill memory or keep catalog discovery alive."""
    process = subprocess.Popen([executable, "--version"], env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise subprocess.TimeoutExpired("gemini --version", timeout)
                chunk = os.read(process.stdout.fileno(), 4097)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 4096:
                    raise subprocess.SubprocessError("Gemini version output exceeded its limit.")
        if process.wait(timeout=max(.01, deadline - time.monotonic())) != 0:
            raise subprocess.SubprocessError("Gemini version check failed.")
        return output.decode("utf-8", errors="replace")
    finally:
        # Kill only this probe's process group, including pipe-holding children.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        process.stdout.close()


def build_environment(base: dict[str, str], *, cwd: str, api_home: Path | None = None,
                      api_key: str | None = None, base_url: str | None = None) -> dict[str, str]:
    """Explicit auth selection; no fallback between API and Google accounts.

    API home is server-owned and durable for this connection's native history.
    Never point it at an ordinary user's home or reset/delete it on Forget.
    """
    env = {key: value for key, value in base.items() if key not in AUTH_ENV}
    env.update({"GEMINI_CLI_NO_RELAUNCH": "1", "NO_BROWSER": "true",
                "GEMINI_TELEMETRY_ENABLED": "false"})
    expected_auth = "gemini-api-key" if api_home is not None else "oauth-personal"
    system_path = Path(env.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH") or (
        "/Library/Application Support/GeminiCli/settings.json" if sys.platform == "darwin" else "/etc/gemini-cli/settings.json"))
    # System-selected auth outranks our private profile. Do not bypass an
    # organization's enforcement, or accidentally use another account type.
    try:
        system_auth = read_settings(system_path).get("security", {}).get("auth", {})
        if any(system_auth.get(key) not in {None, expected_auth} for key in ("selectedType", "enforcedType")):
            raise GeminiConfigurationError("Server-managed Gemini authentication conflicts with this connection.")
    except AttributeError as exc:
        raise GeminiConfigurationError("Invalid managed Gemini authentication settings.") from exc
    project = read_settings(Path(cwd) / ".gemini/settings.json")
    try:
        project_auth = project.get("security", {}).get("auth", {}).get("selectedType")
    except AttributeError as exc:
        raise GeminiConfigurationError("Invalid project Gemini authentication settings.") from exc
    if api_home is None:
        if project_auth not in {None, "oauth-personal"}:
            raise GeminiConfigurationError("This workspace selects a different Gemini authentication method. Use a matching connection.")
        if not native_credentials_present(env):
            raise GeminiConfigurationError("Sign in with Google by running gemini on the server, or configure a Gemini API connection.")
        return env
    if not api_key or not base_url:
        raise GeminiConfigurationError("Configure the Gemini API connection before sending a message.")
    if project_auth not in {None, "gemini-api-key"}:
        raise GeminiConfigurationError("The workspace overrides Gemini API authentication. Its settings must match this connection.")
    api_home = api_home.absolute()
    original = native_home(base).resolve()
    if api_home.resolve() == original or api_home.is_symlink():
        raise GeminiConfigurationError("The Gemini API profile must be separate from the native account.")
    if api_home.is_symlink() or api_home.parent.is_symlink():
        raise GeminiConfigurationError("The Gemini API profile cannot use linked directories.")
    api_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = api_home.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise GeminiConfigurationError("The Gemini API profile is not private.")
    directory = api_home / ".gemini"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise GeminiConfigurationError("The Gemini API profile is not private.")
    settings_path = directory / "settings.json"
    settings = read_settings(settings_path)
    # Only this private profile is changed; project and global settings remain
    # untouched. Preserve existing skills, policies and user choices here.
    settings.setdefault("security", {}).setdefault("auth", {})["selectedType"] = "gemini-api-key"
    descriptor, temporary = tempfile.mkstemp(prefix=".settings-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(settings, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, settings_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    normalized_base = base_url.rstrip("/")
    for version in ("v1beta", "v1"):
        if normalized_base.endswith("/" + version):
            normalized_base = normalized_base[:-(len(version) + 1)]
            env["GOOGLE_GENAI_API_VERSION"] = version
            break
    env.update({"GEMINI_CLI_HOME": str(api_home), "GEMINI_API_KEY": api_key,
                "GOOGLE_GEMINI_BASE_URL": normalized_base})
    return env
