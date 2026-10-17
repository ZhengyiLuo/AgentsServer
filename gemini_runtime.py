"""Gemini runtime resolution and explicit per-process credential isolation.

No login, settings mutation or model request takes place during discovery.
New API chats use private profiles. Explicitly connected native imports keep
their original history root, selecting API auth only in the child process.
"""
from __future__ import annotations

import json
import copy
from contextlib import suppress
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
API_ONLY_MESSAGE = ("Gemini in AgentsDock supports API connections only. Configure a Gemini API key "
                    "in My Agents, then connect this imported chat to that API or create a Gemini API chat. Existing history is preserved.")
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
                "action": "Install @google/gemini-cli on the server, then configure a Gemini API key in My Agents."}
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
                      api_key: str | None = None, base_url: str | None = None,
                      native_history: bool = False) -> dict[str, str]:
    """Explicit auth selection; no fallback between API and Google accounts.

    API home is server-owned and durable for this connection's native history.
    Never point it at an ordinary user's home or reset/delete it on Forget.
    """
    if api_home is None:
        raise GeminiConfigurationError(API_ONLY_MESSAGE)
    env = {key: value for key, value in base.items() if key not in AUTH_ENV}
    env.update({"GEMINI_CLI_NO_RELAUNCH": "1", "NO_BROWSER": "true",
                "GEMINI_TELEMETRY_ENABLED": "false"})
    expected_auth = "gemini-api-key"
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
        auth = project.get("security", {}).get("auth", {})
        project_auth = auth.get("selectedType")
        if auth.get("enforcedType") not in {None, expected_auth}:
            raise GeminiConfigurationError("The workspace enforces a different Gemini authentication method.")
    except AttributeError as exc:
        raise GeminiConfigurationError("Invalid project Gemini authentication settings.") from exc
    if not api_key or not base_url:
        raise GeminiConfigurationError("Configure the Gemini API connection before sending a message.")
    if project_auth not in {None, "gemini-api-key"}:
        raise GeminiConfigurationError("The workspace overrides Gemini API authentication. Its settings must match this connection.")
    if native_history:
        # Pinned 0.64's in-memory user auth override preserves normal settings
        # precedence, unlike ACP authenticate (which writes settings/credentials).
        # Keep the ORIGINAL history root: no transcript copy, symlink or new ID.
        original = native_home(base)
        try:
            auth = read_settings(original / ".gemini/settings.json").get("security", {}).get("auth", {})
            if auth.get("enforcedType") not in {None, expected_auth}:
                raise GeminiConfigurationError("Native Gemini settings enforce a different authentication method.")
        except AttributeError as exc:
            raise GeminiConfigurationError("Invalid native Gemini authentication settings.") from exc
        env.update({"GEMINI_CLI_HOME": str(original), "GEMINI_CLI_AUTH_OVERRIDE": expected_auth})
        return api_environment(env, api_key, base_url)
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
    inherit_skill_configuration(original, api_home, settings)
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
    env["GEMINI_CLI_HOME"] = str(api_home)
    return api_environment(env, api_key, base_url)


def api_environment(env: dict[str, str], api_key: str, base_url: str) -> dict[str, str]:
    normalized_base = base_url.rstrip("/")
    for version in ("v1beta", "v1"):
        if normalized_base.endswith("/" + version):
            normalized_base = normalized_base[:-(len(version) + 1)]
            env["GOOGLE_GENAI_API_VERSION"] = version
            break
    env.update({"GEMINI_API_KEY": api_key,
                "GOOGLE_GEMINI_BASE_URL": normalized_base})
    return env


def history_environment(base: dict, session: dict, connection: dict, root: Path) -> dict:
    env = dict(base)
    if connection and not session.get("gemini_native_history"):
        env["GEMINI_CLI_HOME"] = str(root / ("gemini-runtime-" + connection["credential_id"]))
    return env


def inherit_skill_configuration(original: Path, profile: Path, settings: dict) -> None:
    """Reference installed skills/extensions, never copy or reinstall them.

    Share only named non-login assets, NOT the Gemini home, settings or credential
    files. Native trust/skill disables and tool policies remain authoritative.
    Auth and history stay in the private API profile. Existing private assets
    are not overwritten silently.
    """
    native = read_settings(original / ".gemini/settings.json")
    try:
        if native.get("security", {}).get("auth", {}).get("enforcedType") not in {None, "gemini-api-key"}:
            raise GeminiConfigurationError("Native Gemini settings enforce a different authentication method.")
    except AttributeError:
        raise GeminiConfigurationError("Invalid Gemini authentication settings.") from None
    for key in ("skills", "tools", "policy", "extensions", "admin"):
        if key in native:
            settings[key] = copy.deepcopy(native[key])
        else:
            settings.pop(key, None)
    security = copy.deepcopy(native.get("security") or {})
    if not isinstance(security, dict):
        raise GeminiConfigurationError("Invalid Gemini security settings.")
    security.pop("auth", None)
    settings["security"] = security
    # Plan availability is native configuration, not a different login type.
    # Copy only this non-auth subtree, not global default approval preferences.
    general = native.get("general") or {}
    if not isinstance(general, dict):
        raise GeminiConfigurationError("Invalid Gemini general settings.")
    settings.setdefault("general", {}).pop("plan", None)
    if "plan" in general:
        settings["general"]["plan"] = copy.deepcopy(general["plan"])
    for relative in (".gemini/skills", ".agents/skills", ".gemini/extensions",
                     ".gemini/policies", ".gemini/trustedFolders.json"):
        source, target = original / relative, profile / relative
        if target.is_symlink():
            if os.readlink(target) != str(source):
                raise GeminiConfigurationError("Gemini API skill configuration changed; inspect the private profile before retrying.")
            continue
        if not source.exists():
            continue
        if target.exists():
            raise GeminiConfigurationError("Gemini API profile has separate skill configuration; reconcile it before retrying.")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.parent.is_symlink():
            raise GeminiConfigurationError("The Gemini API profile is not private.")
        try:
            target.symlink_to(source, target_is_directory=source.is_dir())
        except FileExistsError:
            # Concurrent discovery and a turn may initialize the same profile.
            if not target.is_symlink() or os.readlink(target) != str(source):
                raise GeminiConfigurationError("Gemini API skill configuration changed.") from None


def list_native_skills(executable: str, env: dict[str, str], cwd: str, timeout: float = 15) -> str:
    """Pinned native discovery, including enabled extension skills and trust.

    Include built-ins with the native --all flag; it does not enable disabled
    skills or override workspace trust. This is local discovery, not an ACP
    model prompt/authenticate.
    Kill the owned group on timeout/output overflow; never publish stderr.
    """
    process = subprocess.Popen([executable, "skills", "list", "--all"], cwd=cwd,
        env={**env, "NO_COLOR": "1", "FORCE_COLOR": "0"}, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    data = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise GeminiConfigurationError("Gemini skill discovery timed out.")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise GeminiConfigurationError("Gemini skill discovery exceeded its output limit.")
        if process.wait(timeout=max(.01, deadline - time.monotonic())) != 0:
            raise GeminiConfigurationError("Gemini skill discovery failed.")
        return data.decode("utf-8", errors="strict")
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise GeminiConfigurationError("Gemini skill discovery failed.") from None
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
        process.stdout.close()
