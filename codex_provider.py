"""Explicit custom Responses endpoints for the native Codex runtime."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import uuid
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request as URLRequest, build_opener

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from codex_auth import validate_api_key
from codex_app_server import CodexAppServerManager, decline_server_request
from codex_side_question import isolated_config, _verify_protocol
from side_questions import isolated_environment, run_isolated_command


PROVIDER_ID = "agentsdock_custom"
ENV_KEY = "AGENTSDOCK_CODEX_PROVIDER_API_KEY"
MAX_BODY_BYTES = 16 * 1024
MAX_SUMMARY_CAPABILITIES_BYTES = 192 * 1024
MAX_MODEL_CATALOG_BYTES = 384 * 1024
TEST_TIMEOUT_SECONDS = 45
TEST_TOOL = "agentsdock_compatibility_ping"
TEST_INSTRUCTIONS = (
    "You are checking basic Codex compatibility. Follow only the current check prompt. "
    "The only permitted tools are update_plan and agentsdock_compatibility_ping. "
    "Do not access files, execute commands, browse, or use integrations."
)
TEST_PROMPT = (
    "Perform this basic compatibility check only. First call update_plan with one "
    "completed step named Compatibility check. Then call agentsdock_compatibility_ping "
    "exactly once with an empty object. Reply with exactly the token returned by that "
    "tool, without formatting. Do not use any other tools."
)
SUMMARY_TEST_PROMPT = (
    "Without using any tools, compare 17 times 23 with 19 times 21. "
    "Work out which product is larger, then answer in one short sentence."
)
EFFORT_OPTIONS = [{"value": value, "label": label} for value, label in (
    ("none", "None"), ("minimal", "Minimal"), ("low", "Low"), ("medium", "Medium"),
    ("high", "High"), ("xhigh", "Extra high"), ("max", "Max"), ("ultra", "Ultra"))]


def model_capability(value: dict | None = None) -> dict:
    """Bound public capability metadata without inferring endpoint support."""
    value = value or {}
    supported = value.get("reasoning_supported")
    supported = supported if isinstance(supported, bool) else None
    efforts = value.get("reasoning_efforts")
    allowed = {option["value"] for option in EFFORT_OPTIONS}
    efforts = [effort.get("effort") if isinstance(effort, dict) else effort for effort in efforts] if isinstance(efforts, list) else []
    efforts = list(dict.fromkeys(effort for effort in efforts if isinstance(effort, str) and effort in allowed)) if isinstance(efforts, list) else []
    if supported is False:
        efforts = []
    summary_supported = value.get("reasoning_summary_supported")
    return {"kind": "chat" if value.get("kind") == "chat" else "unknown",
        "compatibility": value.get("compatibility") if value.get("compatibility") in {"verified", "unsupported"} else "unverified",
        "reasoning_efforts": efforts, "reasoning_supported": True if efforts else supported,
        "reasoning_summary_supported": summary_supported if isinstance(summary_supported, bool) else None}


def runtime_effort(selected: dict, catalog: dict, requested: object) -> str:
    """Never inherit normal-account defaults or invent endpoint capabilities."""
    capability = model_capability(catalog.get("model_capabilities", {}).get(selected.get("model")))
    return requested if isinstance(requested, str) and requested in capability["reasoning_efforts"] else ""


def runtime_summary(selected: dict, catalog: dict) -> str:
    """Request summaries only with separate, explicit endpoint/model evidence."""
    capability = model_capability(catalog.get("model_capabilities", {}).get(selected.get("model")))
    return "auto" if capability["reasoning_summary_supported"] is True else "none"


def turn_overrides(model: str, effort: str = "", *, summary: str = "none") -> dict:
    # Null turn.effort means inherit. Replacing the native collaboration settings
    # explicitly clears both a configured effort and an earlier turn's effort.
    # Native default mode preserves the thread's base/developer instructions.
    return {"model": model, "summary": "auto" if summary == "auto" else "none", "collaborationMode": {"mode": "default", "settings": {
        "model": model, "reasoning_effort": effort or None, "developer_instructions": None}},
        **({"effort": effort} if effort else {})}


def discovered_model_capability(entry: dict, model: str, native_models: dict | None = None) -> dict | None:
    """Exclude affirmative non-chat evidence; unfamiliar IDs remain usable."""
    non_chat = {"embedding", "embeddings", "rerank", "reranking", "moderation", "image-generation",
        "text-to-image", "image", "text-to-speech", "speech-to-text", "transcription", "tts", "video-generation"}
    chat = {"chat", "chat-completion", "chat-completions", "text-generation", "responses", "conversation", "llm"}
    declared = {str(entry.get(key) or "").lower().replace("_", "-") for key in ("type", "task", "kind")}
    if declared & non_chat and not declared & chat:
        return None
    name = model.lower().rsplit("/", 1)[-1]
    if not declared & chat and re.match(r"^(?:text-embedding(?:-|$)|(?:omni-|text-)?moderation(?:-|$)|dall-e(?:-|$)|gpt-image(?:-|$)|whisper(?:-|$)|tts(?:-|$)|(?:text-)?rerank(?:-|$)|sora(?:-|$))", name):
        return None
    capabilities = entry.get("capabilities") if isinstance(entry.get("capabilities"), dict) else {}
    architecture = entry.get("architecture") if isinstance(entry.get("architecture"), dict) else {}
    outputs = entry.get("output_modalities", architecture.get("output_modalities"))
    if not declared & chat and isinstance(outputs, list) and outputs and all(
        isinstance(output, str) and output in {"image", "audio", "video", "embedding", "embeddings"}
        for output in outputs
    ):
        return None
    reasoning = entry.get("reasoning") if isinstance(entry.get("reasoning"), dict) else {}
    supported = entry.get("reasoning_supported", entry.get("supports_reasoning", capabilities.get("reasoning", reasoning.get("supported"))))
    effort_fields = [(source, key) for source, keys in (
        (entry, ("supported_reasoning_efforts", "reasoning_efforts", "supported_reasoning_levels")),
        (capabilities, ("supported_reasoning_efforts", "reasoning_efforts", "supported_reasoning_levels")),
        (reasoning, ("efforts", "supported_reasoning_levels")),
    ) for key in keys]
    efforts = next((source[key] for source, key in effort_fields if key in source), [])
    explicit_reasoning = (any(key in source for source, key in effort_fields)
        or any(key in entry for key in ("reasoning_supported", "supports_reasoning"))
        or "reasoning" in capabilities or "supported" in reasoning)
    if not explicit_reasoning and entry.get("owned_by") == "openai" and entry.get("mode") == "responses":
        # An explicitly OpenAI Responses model may use its installed native
        # model's effort choices. This does not verify gateway compatibility.
        native_entries = native_models.get("models") if isinstance(native_models, dict) else None
        native_name = model.rsplit("/", 1)[-1]
        native_model = next((item for item in native_entries
            if isinstance(item, dict) and item.get("slug") == native_name), None) if isinstance(native_entries, list) else None
        if native_model:
            efforts = native_model.get("supported_reasoning_levels", [])
    summary_supported = entry.get("reasoning_summary_supported", entry.get("supports_reasoning_summary_parameter",
        capabilities.get("reasoning_summary", reasoning.get("summary_supported"))))
    return model_capability({"kind": "chat" if declared & chat else "unknown",
        "reasoning_supported": supported, "reasoning_efforts": efforts,
        "reasoning_summary_supported": summary_supported})


def validate_model(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 256 or any(ord(char) < 33 or ord(char) > 126 for char in value.strip()):
        raise HTTPException(400, "Enter a model ID without whitespace or control characters.")
    return value.strip()


def validate_selection(value: object, *, require_key: bool = True) -> dict:
    fields = {"base_url", "api_key"} if require_key else {"base_url"}
    if not isinstance(value, dict) or not fields.issubset(value) or set(value) - fields - {"model"}:
        raise HTTPException(400, "Provide the endpoint and a fresh provider API key.")
    base, model = value.get("base_url"), value.get("model")
    if not isinstance(base, str) or not 1 <= len(base.strip()) <= 2048:
        raise HTTPException(400, "Enter a valid provider base URL.")
    base = base.strip().rstrip("/")
    if any(ord(char) < 33 or ord(char) > 126 for char in base) or "\\" in base:
        raise HTTPException(400, "Enter a valid provider base URL.")
    try:
        url = urlsplit(base)
        port = url.port
        valid = bool(url.hostname) and url.scheme in {"https", "http"} and url.username is None and url.password is None and not url.query and not url.fragment
        valid = valid and (url.scheme == "https" or url.hostname in {"localhost", "127.0.0.1", "::1"})
        valid = valid and not url.path.lower().endswith(("/responses", "/chat/completions"))
        if port is not None and not 1 <= port <= 65535:
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(400, "Use an HTTPS base URL, or HTTP on loopback, without credentials, query, fragment or an API operation suffix.")
    result = {"base_url": urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path, "", ""))}
    if model is not None:
        result["model"] = validate_model(model)
    if require_key:
        result["api_key"] = validate_api_key({"api_key": value["api_key"]})
    return result


def binding(selection: dict) -> str:
    return hashlib.sha256(json.dumps([selection["base_url"]], separators=(",", ":")).encode()).hexdigest()


def legacy_binding(selection: dict) -> str | None:
    model = selection.get("legacy_model") or selection.get("model")
    return hashlib.sha256(json.dumps([selection["base_url"], model], separators=(",", ":")).encode()).hexdigest() if model else None


def catalog_key(selected: dict) -> str:
    return hashlib.sha256((selected["base_url"] + "\0" + selected["api_key"]).encode()).hexdigest()


def session_choice(value) -> str:
    choice = "default" if value is None else value
    if not isinstance(choice, str) or choice not in {"default", "custom"}:
        raise HTTPException(400, "Codex provider must be default or custom.")
    return choice


class ProviderStore:
    """Atomic metadata pointer plus private, endpoint-bound credential records."""
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self._catalogs: dict[str, dict] = {}
        self._saved_catalogs: dict[str, dict] = {}
        self._model_capabilities: dict[str, dict[str, dict]] = {}
        self._summary_capabilities: dict[str, dict[str, bool]] = {}
        self._public_selections: dict[str, dict] = {}
        self._revision_catalog_keys: dict[str, str] = {}

    def _directory(self, *, create=False):
        if self.root.is_symlink() or self.root.parent.is_symlink():
            raise HTTPException(503, "Provider credential storage is unavailable.")
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.exists():
            info = self.root.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise HTTPException(503, "Provider credential storage must be private to the server user.")

    def _read(self, name: str, *, max_bytes=MAX_BODY_BYTES) -> dict | None:
        self._directory()
        try:
            fd = os.open(self.root / name, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise HTTPException(503, "Provider credential storage is unavailable.") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > max_bytes:
                raise ValueError("unsafe credential file")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                value = json.load(stream)
            if not isinstance(value, dict):
                raise ValueError("invalid record")
            return value
        finally:
            os.close(fd)

    def _atomic(self, name: str, value: dict):
        self._directory(create=True)
        target = self.root / name
        if target.is_symlink():
            raise HTTPException(503, "Provider credential storage is unavailable.")
        fd, temporary = tempfile.mkstemp(prefix=".write-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            # replace is the commit point; durability failure must not make
            # save delete the credential now selected by the metadata pointer.
            durable = False
            with suppress(OSError):
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                    durable = True
                finally:
                    os.close(directory)
            return durable
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    def selection(self, *, include_key=False, revision: str | None = None, include_revision=False) -> dict | None:
        with self.lock:
            try:
                metadata = self._read("settings.json") or {}
                identifier = revision or metadata.get("credential_id")
                if not identifier and not metadata:
                    return None
                if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
                    raise ValueError("invalid credential identity")
                credential = self._read("credential-" + identifier + ".json")
                if not credential:
                    raise ValueError("missing credential")
                source = metadata if identifier == metadata.get("credential_id") else credential
                selected = validate_selection({name: source[name] for name in ("base_url", "model") if name in source}, require_key=False)
                legacy_model = credential.get("legacy_model") or source.get("model")
                if legacy_model:
                    selected["legacy_model"] = validate_model(legacy_model)
                if not credential.get("binding") or credential.get("binding") not in (binding(selected), legacy_binding(selected)):
                    raise ValueError("credential binding mismatch")
                key = validate_api_key({"api_key": credential.get("api_key")})
                self._public_selections[identifier] = {**selected, "credential_id": identifier}
                self._revision_catalog_keys[identifier] = catalog_key({**selected, "api_key": key})
                if include_key:
                    selected["api_key"] = key
                if include_revision or revision:
                    selected["credential_id"] = identifier
                if not include_revision and revision is None:
                    selected.pop("legacy_model", None)
                return selected
            except Exception:
                raise HTTPException(503, "Saved provider settings or credentials are unavailable.") from None

    def status(self, *, available=True) -> dict:
        selected = self.selection(include_revision=True)
        return {"available": available, "configured": selected is not None,
            "base_url": selected["base_url"] if selected else None,
            "model": selected.get("model") if selected else None,
            "credential_id": selected.get("credential_id") if selected else None,
            "has_api_key": selected is not None, "wire_api": "responses"}

    def for_session(self, session: dict, *, include_key=False) -> dict | None:
        if session_choice(session.get("codex_provider")) == "default":
            return None
        revision = session.get("codex_provider_revision")
        selected = self.selection(include_key=include_key, revision=revision, include_revision=True)
        if selected is None:
            raise HTTPException(409, "Configure the custom Codex endpoint before using this chat.")
        if session.get("codex_provider_binding") not in (None, binding(selected), legacy_binding(selected)):
            raise HTTPException(409, "This conversation belongs to another Codex endpoint. Start a new Codex chat, or restore its original endpoint.")
        model = session.get("model") or selected.get("model") or self.cached_catalog(selected).get("default_model")
        if model:
            selected["model"] = validate_model(model)
        return selected

    def registration(self, *, include_key=False) -> dict | None:
        # A damaged optional endpoint must not prevent normal Codex startup.
        # Custom chat admission and the admin status route still fail closed.
        try:
            return self.selection(include_key=include_key, include_revision=True)
        except HTTPException:
            return None

    def catalog(self, *, available: bool, session: dict | None = None, summary=False) -> dict:
        try:
            # Immutable revisions permit public projections to reuse validated
            # metadata without reopening credential files for every event.
            selected = self._public_selections.get(session.get("codex_provider_revision")) if session else None
            selected = selected or (self.for_session(session) if session is not None else self.registration())
        except HTTPException:
            selected = None
        result = {"configured": selected is not None, "available": available and selected is not None,
            "model": selected.get("model") if selected else None,
            "base_url": selected["base_url"] if selected else None,
            "credential_id": selected.get("credential_id") if selected else None,
            **(self.cached_catalog(selected) if selected else {"models": [], "efforts": [],
                "model_efforts": {}, "model_capabilities": {}, "default_model": "", "default_effort": ""})}
        if summary:
            result.pop("models", None)
            result.pop("model_efforts", None)
            result.pop("model_capabilities", None)
        return result

    def cached_catalog(self, selected: dict | None = None) -> dict:
        with self.lock:
            if selected is None:
                selected = self.registration(include_key=True)
            identifier = (selected or {}).get("credential_id")
            if selected and "api_key" not in selected and identifier not in self._revision_catalog_keys:
                selected = self.selection(include_key=True, revision=selected.get("credential_id"))
            key = (catalog_key(selected) if "api_key" in selected else self._revision_catalog_keys.get(identifier)) if selected else None
            cached = self._catalogs.get(key)
            if cached is None:
                cached = self._saved_model_catalog(selected)
            capabilities = {model["value"]: model_capability(cached.get("model_capabilities", {}).get(model["value"]))
                for model in cached.get("models", [])}
            for model, evidence in self._model_capabilities.get(key, {}).items():
                capabilities[model] = model_capability({**capabilities.get(model, {}), **evidence})
            for model, supported in self._saved_summary_capabilities(selected).items():
                capabilities[model] = model_capability({**capabilities.get(model, {}), "reasoning_summary_supported": supported})
            if selected and selected.get("model"):
                capabilities.setdefault(selected["model"], model_capability())
            return {"models": cached.get("models", []), "efforts": [],
                "model_efforts": {model: [option for option in EFFORT_OPTIONS if option["value"] in capability["reasoning_efforts"]]
                    for model, capability in capabilities.items()},
                "model_capabilities": capabilities,
                "default_model": cached.get("default_model") or (selected or {}).get("model") or "",
                "default_effort": ""}

    def cache_catalog(self, selected: dict, catalog: dict):
        with self.lock:
            models = catalog.get("models", [])
            self._catalogs[catalog_key(selected)] = {"models": models,
                "model_capabilities": {model["value"]: model_capability(catalog.get("model_capabilities", {}).get(model["value"])) for model in models},
                "default_model": catalog.get("default_model", "")}
            self._save_model_catalog(selected, self._catalogs[catalog_key(selected)])
            # Apply fresh explicit denials when discovery completes, rather
            # than letting old metadata veto a later successful model check.
            denials = {model["value"]: False for model in models
                if catalog.get("model_capabilities", {}).get(model["value"], {}).get("reasoning_summary_supported") is False}
            if denials:
                self._cache_summary_capabilities(selected, denials)

    def _save_model_catalog(self, selected: dict, catalog: dict) -> None:
        identifier = selected.get("credential_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            return
        saved = self.selection(include_key=True, revision=identifier)
        if not saved or catalog_key(saved) != catalog_key(selected):
            raise HTTPException(409, "The saved endpoint changed. Refresh before discovering models.")
        # Basic-check status and summary observations keep their independent
        # lifetimes. Only the discovered model list and effort choices reload.
        metadata = {**catalog, "model_capabilities": {model: {
            name: value for name, value in capability.items()
            if name not in {"compatibility", "reasoning_summary_supported"}}
            for model, capability in catalog["model_capabilities"].items()}}
        record = {"version": 1, "credential_id": identifier, "binding": binding(saved),
            "catalog_key": catalog_key(saved), "catalog": metadata}
        if len(json.dumps(record, separators=(",", ":")).encode()) > MAX_MODEL_CATALOG_BYTES:
            return
        try:
            self._atomic("model-catalog-" + identifier + ".json", record)
        except (OSError, HTTPException):
            pass
        self._saved_catalogs[identifier] = metadata

    def _saved_model_catalog(self, selected: dict | None) -> dict:
        identifier = (selected or {}).get("credential_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            return {}
        if identifier in self._saved_catalogs:
            return self._saved_catalogs[identifier]
        try:
            if identifier not in self._revision_catalog_keys:
                self.selection(revision=identifier)
            expected_key = self._revision_catalog_keys.get(identifier)
            if "api_key" in selected and catalog_key(selected) != expected_key:
                return {}
            record = self._read("model-catalog-" + identifier + ".json", max_bytes=MAX_MODEL_CATALOG_BYTES)
            if record is None:
                self._saved_catalogs[identifier] = {}
                return {}
            catalog = record.get("catalog")
            if (record.get("version") != 1 or record.get("credential_id") != identifier
                    or record.get("binding") != binding(selected)
                    or record.get("catalog_key") != expected_key
                    or not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list)
                    or len(catalog["models"]) > 512 or not isinstance(catalog.get("model_capabilities"), dict)):
                raise ValueError("Invalid saved model catalog")
            models = [{"value": validate_model(model.get("value")), "label": validate_model(model.get("value"))}
                for model in catalog["models"]]
            if len({model["value"] for model in models}) != len(models):
                raise ValueError("Duplicate saved model identities")
            capabilities = {model["value"]: model_capability({
                **catalog["model_capabilities"].get(model["value"], {}),
                "compatibility": "unverified", "reasoning_summary_supported": None}) for model in models}
            default_model = catalog.get("default_model") or ""
            if default_model and default_model not in capabilities:
                raise ValueError("Invalid saved default model")
            result = {"models": models, "model_capabilities": capabilities, "default_model": default_model}
            self._saved_catalogs[identifier] = result
            return result
        except Exception:
            # Optional catalog metadata never blocks opening a custom chat.
            return {}

    def cache_model_capability(self, selected: dict, capability: dict):
        """Retain only probe evidence for this exact endpoint/key/model tuple."""
        model = validate_model(selected.get("model"))
        with self.lock:
            records = self._model_capabilities.setdefault(catalog_key(selected), {})
            previous = records.get(model, {})
            cleaned = model_capability({**previous, **capability})
            records[model] = {name: cleaned[name] for name in cleaned
                if name != "reasoning_summary_supported" and (name in previous or name in capability)}
            supported = capability.get("reasoning_summary_supported")
            if isinstance(supported, bool):
                self._cache_summary_capabilities(selected, {model: supported})

    def _cache_summary_capabilities(self, selected: dict, observations: dict[str, bool]) -> None:
        identifier = selected.get("credential_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            return
        # Entered, unsaved credentials never create durable evidence.
        # Validate the claimed immutable revision before using its name.
        saved = self.selection(include_key=True, revision=identifier)
        if not saved or catalog_key(saved) != catalog_key(selected):
            raise HTTPException(409, "The saved endpoint changed. Refresh before checking this model.")
        summaries = dict(self._saved_summary_capabilities(saved))
        for model, supported in observations.items():
            summaries.pop(model, None)
            summaries[model] = supported
        while len(summaries) > 512:
            summaries.pop(next(iter(summaries)))
        try:
            self._atomic("summary-capabilities-" + identifier + ".json", {
                "version": 1, "credential_id": identifier, "binding": binding(saved),
                "catalog_key": catalog_key(saved), "models": summaries})
        except (OSError, HTTPException):
            # A successful optional check remains useful for this
            # process even when display metadata cannot be persisted.
            pass
        self._summary_capabilities[identifier] = summaries

    def _saved_summary_capabilities(self, selected: dict | None) -> dict[str, bool]:
        """Private summary evidence belongs to one saved credential revision."""
        identifier = (selected or {}).get("credential_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            return {}
        if identifier in self._summary_capabilities:
            return self._summary_capabilities[identifier]
        try:
            record = self._read("summary-capabilities-" + identifier + ".json", max_bytes=MAX_SUMMARY_CAPABILITIES_BYTES)
            if record is None:
                models = {}
            else:
                models = record.get("models")
                if (record.get("version") != 1 or record.get("credential_id") != identifier
                        or record.get("binding") != binding(selected)
                        or record.get("catalog_key") != self._revision_catalog_keys.get(identifier)
                        or not isinstance(models, dict) or len(models) > 512
                        or any(validate_model(model) != model or not isinstance(supported, bool) for model, supported in models.items())):
                    raise ValueError("Invalid summary capability evidence")
            self._summary_capabilities[identifier] = models
            return models
        except Exception:
            # Optional display capability evidence must never prevent a chat or
            # catalog from opening. Credential files retain strict validation.
            return {}

    def revision(self):
        with self.lock:
            return (self._read("settings.json") or {}).get("credential_id")

    def _binding_name(self, thread_id: str) -> str:
        return "thread-" + hashlib.sha256(thread_id.encode()).hexdigest() + ".json"

    def require_thread(self, thread_id: str, selected: dict | None):
        if not thread_id:
            return
        with self.lock:
            previous = self._read(self._binding_name(thread_id))
            expected = binding(selected) if selected else None
            accepted = {expected}
            if selected:
                accepted.add(legacy_binding(selected))
                accepted.discard(None)
            if (previous or {}).get("binding") not in accepted or (
                previous and previous.get("credential_id") and
                previous["credential_id"] != (selected or {}).get("credential_id")
            ):
                raise HTTPException(409, "This conversation belongs to another Codex endpoint. Start a new Codex chat, or restore its original endpoint.")

    def record_thread(self, thread_id: str, selected: dict | None):
        with self.lock:
            if selected:
                self._atomic(self._binding_name(thread_id), {"binding": binding(selected),
                    "credential_id": selected.get("credential_id")})

    def for_thread(self, thread_id: str, *, include_key=False) -> dict | None:
        with self.lock:
            record = self._read(self._binding_name(thread_id)) or {}
            revision = record.get("credential_id")
            if revision:
                return self.selection(revision=revision, include_key=include_key, include_revision=True)
            if record.get("binding"):
                selected = self.registration(include_key=include_key)
                if selected and record["binding"] in (binding(selected), legacy_binding(selected)):
                    return selected
                raise HTTPException(409, "This conversation's original Codex endpoint is unavailable.")
            return None

    def retain_current(self) -> dict | None:
        """Make the current generation independently readable before replacement."""
        selected = self.selection(include_key=True, include_revision=True)
        if selected:
            self._atomic("credential-" + selected["credential_id"] + ".json",
                {**selected, "binding": binding(selected)})
            for path in self.root.glob("thread-*.json"):
                previous = self._read(path.name) or {}
                if not previous.get("credential_id") and previous.get("binding") and previous["binding"] in (binding(selected), legacy_binding(selected)):
                    self._atomic(path.name, {"binding": binding(selected), "credential_id": selected["credential_id"]})
        return selected

    def save(self, selected: dict):
        with self.lock:
            try:
                self.retain_current()
            except HTTPException:
                # A missing old credential must not prevent an operator from
                # repairing the current selection with a fresh credential.
                pass
            identifier = uuid.uuid4().hex
            credential_name = "credential-" + identifier + ".json"
            self._atomic(credential_name, {**selected, "binding": binding(selected)})
            try:
                self._atomic("settings.json", {name: value for name, value in
                    {**selected, "credential_id": identifier}.items() if name != "api_key"})
            except BaseException:
                with suppress(OSError):
                    os.unlink(self.root / credential_name)
                raise

    def reset(self):
        with self.lock:
            try:
                self.retain_current()
            except (HTTPException, ValueError, UnicodeError):
                pass
            self._atomic("settings.json", {})


def native_config(selected: dict) -> dict:
    return {"model_provider": PROVIDER_ID, **({"model": selected["model"]} if selected.get("model") else {}),
        "model_reasoning_summary": "auto" if selected.get("reasoning_summary") == "auto" else "none",
        "model_providers": {PROVIDER_ID: {"name": "AgentsDock custom endpoint",
            "base_url": selected["base_url"], "env_key": ENV_KEY, "requires_openai_auth": False,
            "wire_api": "responses",
            "supports_websockets": False}},
        "shell_environment_policy.exclude": [ENV_KEY, "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_AUTH_TOKEN"]}


def native_args(selected: dict) -> tuple[str, ...]:
    return config_args(native_config(selected))


def registration_args(selected: dict) -> tuple[str, ...]:
    config = native_config(selected)
    config.pop("model_provider")
    config.pop("model", None)
    # The client merges the dedicated secret exclusion per thread after
    # resolving native profile/project layers for that thread's cwd.
    config.pop("shell_environment_policy.exclude")
    return config_args(config)


def registration_environment(environment: dict, selected: dict) -> dict:
    return native_environment(environment, selected)


def config_args(config: dict) -> tuple[str, ...]:
    # CLI overrides consume TOML, not JSON objects. Primitive and array JSON
    # values are TOML-compatible; maps require separate dotted keys.
    result = []
    def add(name, value):
        if isinstance(value, dict):
            for key, child in value.items():
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
                    raise ValueError("unsupported native configuration key")
                # Codex's override parser splits paths itself and retains
                # quotes literally; our fixed schema uses bare safe keys.
                add(name + "." + key, child)
        else:
            result.extend(("-c", name + "=" + json.dumps(value)))
    for name, value in config.items():
        add(name, value)
    return tuple(result)


def native_catalog_without_reasoning_defaults(payload: object) -> dict:
    """Retain native tools/instructions without assuming gateway reasoning support."""
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list) or not 1 <= len(models) <= 512:
        raise ValueError("Native model catalog is unavailable")
    corrected, seen = [], set()
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("Native model catalog is invalid")
        slug = validate_model(model.get("slug"))
        if slug in seen:
            raise ValueError("Native model catalog has duplicate identities")
        seen.add(slug)
        corrected.append({**model,
            "default_reasoning_level": None,
            "default_reasoning_summary": "none",
            # Permit explicit per-turn requests. Endpoint/model evidence gates
            # them in runtime_summary; native or inherited defaults stay off.
            "supports_reasoning_summary_parameter": True,
            "supports_experimental_context": False,
            "multi_agent_reasoning_effort": None,
            # The internal Lite serializer adds reasoning.context regardless
            # of supports_experimental_context. Custom endpoints use Responses.
            "use_responses_lite": False,
        })
    return {"models": corrected}


async def prepare_native_catalog(executable: str, environment: dict, target_path: Path) -> str:
    """Build an owned custom-runtime catalog from this executable's offline metadata."""
    target = Path(target_path).absolute()
    storage = ProviderStore(target.parent)
    try:
        storage._directory(create=True)
        with tempfile.TemporaryDirectory(prefix=".catalog-source-", dir=target.parent) as temporary:
            env = {key: value for key, value in environment.items()
                if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "PATHEXT"}}
            env.update({"HOME": temporary, "CODEX_HOME": temporary, "TMPDIR": temporary,
                "TMP": temporary, "TEMP": temporary, "RUST_LOG": "off", "OTEL_SDK_DISABLED": "true"})
            config = {**isolated_config(), "cli_auth_credentials_store": "ephemeral",
                "history.persistence": "none", "check_for_update_on_startup": False,
                "analytics.enabled": False, "otel.exporter": "none", "otel.trace_exporter": "none",
                "model_provider": "agentsdock_catalog",
                "model_providers": {"agentsdock_catalog": {"name": "Offline native catalog",
                    "base_url": "http://127.0.0.1:9/v1", "requires_openai_auth": False,
                    "wire_api": "responses", "request_max_retries": 0, "stream_max_retries": 0}}}
            # This existing owned-process helper caps each output stream at
            # 1 MiB and joins cancellation/timeout cleanup before returning.
            raw = await run_isolated_command(
                [executable, *config_args(config), "debug", "models"],
                prompt="", cwd=temporary, env=env, timeout=15,
            )
            catalog = native_catalog_without_reasoning_defaults(json.loads(raw))
            # Reuse the private atomic writer; the catalog contains metadata,
            # never endpoint credentials or the user's native configuration.
            storage._atomic(target.name, catalog)
            return str(target)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise HTTPException(503, "The native Codex model catalog could not be prepared. Update or repair Codex and retry.") from None


def native_environment(environment: dict, selected: dict) -> dict:
    clean = {name: value for name, value in environment.items() if name.upper() not in {
        "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_AUTH_TOKEN", "OPENAI_BASE_URL", "OPENAI_API_BASE",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", ENV_KEY}}
    clean[ENV_KEY] = selected["api_key"]
    clean["RUST_LOG"] = "off"
    return clean


class _NoModelRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def discover_models(selected: dict, native_models: dict | None = None) -> dict:
    """One bounded operator-requested listing; never forward a key on redirect."""
    request = URLRequest(selected["base_url"] + "/models", headers={
        "Authorization": "Bearer " + selected["api_key"], "Accept": "application/json"})
    try:
        with build_opener(ProxyHandler({}), _NoModelRedirects()).open(request, timeout=15) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("model response too large")
            payload = json.loads(raw)
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise ValueError("invalid model list")
        models = []
        capabilities = {}
        seen = set()
        for entry in entries[:2000]:
            if not isinstance(entry, dict):
                continue
            try:
                value = validate_model(entry.get("id"))
            except HTTPException:
                continue
            capability = discovered_model_capability(entry, value, native_models)
            if capability is None:
                continue
            if value not in seen:
                seen.add(value)
                models.append({"value": value, "label": value})
                capabilities[value] = capability
            if len(models) == 512:
                break
        return {"ok": True, "status": "ready",
            "message": "The endpoint returned its model list. Model compatibility remains unverified until tested.",
            "models": models, "efforts": [],
            "model_efforts": {model: [option for option in EFFORT_OPTIONS if option["value"] in capability["reasoning_efforts"]]
                for model, capability in capabilities.items()},
            "model_capabilities": capabilities,
            "default_model": models[0]["value"] if models else "", "default_effort": ""}
    except HTTPError as exc:
        if exc.code in (401, 403):
            return test_result("authentication_failed")
        if exc.code in (404, 405):
            return {**test_result("unsupported"),
                "message": "The endpoint does not provide model discovery. Save it and enter a model ID in the chat."}
        return test_result("failed")
    except (URLError, TimeoutError, ConnectionError):
        return test_result("connection_failed")
    except Exception:
        return test_result("failed")


def test_result(status: str) -> dict:
    messages = {"ready": "Basic compatibility checked: native isolated tools and a follow-up response completed. Workspace tools, integrations and reasoning effort settings were not checked.",
        "unsupported": "The endpoint did not accept the Responses protocol required by Codex.",
        "unsupported_parameter": "The endpoint rejected a parameter required by this native Codex request. Check this model's Responses compatibility and supported settings.",
        "authentication_failed": "The endpoint rejected the provider API key.",
        "model_unavailable": "The endpoint did not accept this model ID.",
        "connection_failed": "The connection test could not reach the endpoint or timed out.",
        "inconclusive": "The model did not complete the isolated tool check and follow-up. Compatibility remains unverified.",
        "failed": "The native Codex connection test did not complete. Check the endpoint, model and provider access."}
    return {"ok": status == "ready", "status": status, "message": messages[status]}


def classify_failure(value) -> str:
    # Inspect in memory only; upstream error messages may contain credentials.
    if isinstance(value, (TimeoutError, ConnectionError)):
        return "connection_failed"
    text = str(value).lower()
    if any(marker in text for marker in ("401", "403", "invalid_api_key", "unauthorized", "forbidden", "incorrect api key")):
        return "authentication_failed"
    if any(marker in text for marker in ("model_not_found", "model not found", "unknown model", "model does not exist")):
        return "model_unavailable"
    if any(marker in text for marker in ("unsupported_parameter", "unsupported parameter", "unsupported_value", "unknown parameter", "unrecognized request argument", "unrecognized parameter")) or (
        any(marker in text for marker in ("reasoning.effort", "reasoning effort", "reasoning_effort", "reasoning.summary", "reasoning summary", "reasoning_summary", "tool_choice", "tools[", "tools.", "tool type", "tool format", "custom tool", "freeform"))
        and any(marker in text for marker in ("not supported", "unsupported", "invalid parameter", "invalid value", "unknown field"))
    ):
        return "unsupported_parameter"
    if any(marker in text for marker in ("405", "unsupported protocol", "/responses is not supported", "cannot post /v1/responses")):
        return "unsupported"
    if any(marker in text for marker in ("connection refused", "dns error", "failed to lookup address", "connection timed out", "request timed out", "connection failed:", "error sending request")):
        return "connection_failed"
    return "failed"


async def test_connection(selected: dict, *, executable: str, environment: dict,
                          manager_factory=CodexAppServerManager, verify_protocol=_verify_protocol) -> dict:
    native = None
    opening = None
    checks = {"native_tool_call": False, "tool_roundtrip": False, "continuation": False}
    token = "COMPATIBILITY_" + uuid.uuid4().hex
    ping_count = 0
    unexpected_request = False
    thread = None
    basic_ready = False
    summary_supported = None
    summary_check = "not_checked"

    def observe_summary_failure(error):
        nonlocal summary_supported, summary_check
        failure = str(error).lower()
        if (classify_failure(error) == "unsupported_parameter"
                and any(marker in failure for marker in ("reasoning.summary", "reasoning_summary", "reasoning summary"))):
            summary_supported, summary_check = False, "unsupported"

    def result(status):
        compatibility = ("verified" if status == "ready" else "unsupported"
            if status in {"unsupported", "unsupported_parameter", "model_unavailable"} else "unverified")
        return {**test_result(status), "model": selected["model"], "compatibility": compatibility,
            "scope": "isolated_native_tools_and_continuation", "checks": dict(checks),
            "reasoning_summary_supported": summary_supported, "summary_check": summary_check}

    async def handle_request(request_id, method, params):
        nonlocal ping_count, unexpected_request
        if method == "item/tool/call":
            if (params.get("threadId") == thread and params.get("tool") == TEST_TOOL
                    and params.get("arguments") == {} and ping_count == 0):
                ping_count += 1
                return {"success": True, "contentItems": [{"type": "inputText", "text": token}]}
            unexpected_request = True
            return {"success": False, "contentItems": [{"type": "inputText", "text": "Only the compatibility ping is available."}]}
        unexpected_request = True
        return await decline_server_request(request_id, method, params)

    with tempfile.TemporaryDirectory(prefix="agentsdock-provider-test-") as temporary:
        config = {**isolated_config(), **native_config(selected), "cli_auth_credentials_store": "ephemeral",
            "tools.update_plan.enabled": True, "developer_instructions": TEST_INSTRUCTIONS,
            "log_dir": str(Path(temporary) / "logs"), "sqlite_home": str(Path(temporary) / "db"),
            "history.persistence": "none", "check_for_update_on_startup": False,
            "analytics.enabled": False, "otel.exporter": "none", "otel.trace_exporter": "none"}
        # Connection checks are one explicit attempt; normal chats retain
        # native bounded HTTP/stream recovery for transient failures.
        config["model_providers"][PROVIDER_ID].update(request_max_retries=0, stream_max_retries=0)
        env = native_environment(isolated_environment(environment), selected)
        # A fresh SQLite database paired with the user's Codex home triggers
        # a full history reindex before native initialization can complete.
        # The probe needs only its explicit endpoint/model/key, so isolate its
        # configuration, auth and history together without changing HOME.
        env.update({"CODEX_HOME": temporary, "TMPDIR": temporary, "OTEL_SDK_DISABLED": "true"})
        try:
            async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
                await verify_protocol(executable, temporary, {name: value for name, value in env.items() if name != ENV_KEY})
                config["model_catalog_json"] = await prepare_native_catalog(
                    executable, env, Path(temporary) / "models.json",
                )
                args = config_args(config)
                native = manager_factory(executable, cwd=temporary, env_factory=lambda: env,
                    app_server_args=args, request_timeout=15, lifecycle_timeout=15,
                    server_request_handler=handle_request,
                    sensitive_values=(selected["api_key"],))
                # Env-carried credentials deserve the same diagnostic suppression
                # as native account/login/start, including startup diagnostics.
                native.client._authentication_submitted = True
                # Retain ownership if cancellation races subprocess spawn.
                opening = asyncio.create_task(native.start())
                await asyncio.shield(opening)
                effective = await native.request("config/read", {"includeLayers": False})
                settings = effective.get("config", {})
                provider = settings.get("model_providers", {}).get(PROVIDER_ID, {})
                if settings.get("cli_auth_credentials_store") != "ephemeral" or settings.get("model_provider") != PROVIDER_ID or provider.get("requires_openai_auth") is not False or provider.get("env_key") != ENV_KEY or provider.get("base_url") != selected["base_url"]:
                    return result("failed")
                servers = settings.get("mcp_servers", {})
                if not isinstance(servers, dict):
                    return result("failed")
                config["mcp_servers"] = {name: {"enabled": False} for name in servers}
                thread = await native.start_thread({"ephemeral": True, "cwd": temporary, "model": selected["model"],
                    "modelProvider": PROVIDER_ID, "approvalPolicy": "never", "sandbox": "read-only",
                    "baseInstructions": TEST_INSTRUCTIONS, "developerInstructions": TEST_INSTRUCTIONS,
                    "config": config, "dynamicTools": [{"name": TEST_TOOL,
                        "description": "Harmless in-memory compatibility check. Returns a random token; performs no external action.",
                        "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}],
                    "environments": []})
                metadata = await native.read_thread(thread, include_turns=False)
                if metadata.get("ephemeral") is not True or metadata.get("path") is not None:
                    return result("failed")
                for index, prompt in enumerate((TEST_PROMPT,
                    "Repeat exactly the token returned by the compatibility ping in the preceding turn. Do not call any tools.")):
                    turn = await native.start_turn(thread, [{"type": "text", "text": prompt}],
                        overrides={**turn_overrides(selected["model"]), "environments": []})
                    answer = ""
                    try:
                        while True:
                            packet = await turn.next_notification()
                            data = packet.get("params", {})
                            if packet.get("method") == "error":
                                # One explicit attempt; never silently retry a probe.
                                return result(classify_failure(data.get("error") or data.get("message")))
                            if packet.get("method") == "turn/plan/updated":
                                checks["native_tool_call"] = True
                            if packet.get("method") == "item/completed" and data.get("item", {}).get("type") == "agentMessage":
                                answer = str(data["item"].get("text") or "").strip()
                            if packet.get("method") == "turn/completed":
                                completed = data.get("turn", {})
                                if completed.get("status") != "completed" or completed.get("error"):
                                    return result(classify_failure(completed.get("error")))
                                if unexpected_request or ping_count != 1 or answer != token or not checks["native_tool_call"]:
                                    return result("inconclusive")
                                checks["tool_roundtrip" if index == 0 else "continuation"] = True
                                break
                    finally:
                        await turn.close()
                basic_ready = True
                # Optional evidence from this explicit operator check only.
                # Missing summaries or an unrelated failure never invalidate
                # the completed basic tool/continuation check.
                summary_check = "inconclusive"
                try:
                    async with asyncio.timeout(10):
                        turn = await native.start_turn(thread, [{"type": "text", "text": SUMMARY_TEST_PROMPT}],
                            overrides={**turn_overrides(selected["model"], summary="auto"), "environments": []})
                        observed_summary = False
                        try:
                            while True:
                                packet = await turn.next_notification()
                                data = packet.get("params", {})
                                method = packet.get("method")
                                if method == "item/reasoning/summaryTextDelta" and isinstance(data.get("delta"), str) and data["delta"].strip():
                                    observed_summary = True
                                if method == "item/completed" and data.get("item", {}).get("type") == "reasoning":
                                    summaries = data["item"].get("summary")
                                    if isinstance(summaries, list) and any(isinstance(text, str) and text.strip() for text in summaries):
                                        observed_summary = True
                                error = (data.get("error") or data.get("message")) if method == "error" else None
                                if method == "turn/completed":
                                    completed = data.get("turn", {})
                                    error = completed.get("error")
                                    if completed.get("status") == "completed" and not error and observed_summary and not unexpected_request:
                                        summary_supported, summary_check = True, "supported"
                                if error:
                                    observe_summary_failure(error)
                                if error or method == "turn/completed":
                                    break
                        finally:
                            await turn.close()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    observe_summary_failure(exc)
                return result("ready")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return result("ready" if basic_ready else classify_failure(exc))
        finally:
            async def cleanup():
                if opening is not None:
                    with suppress(BaseException):
                        await opening
                if native is not None:
                    await native.close()
            closing = asyncio.create_task(cleanup())
            cancelled = False
            try:
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError:
                        cancelled = True
                closing.result()
            finally:
                env.clear()
            if cancelled:
                raise asyncio.CancelledError


def create_router(*, authorize, store: ProviderStore, mutate, probe, available, discover=discover_models, session_lookup=None, native_models=None) -> APIRouter:
    router = APIRouter()

    async def body(request, *, saved=False):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY_BYTES:
                raise HTTPException(413, "Provider request is too large.")
        try:
            parsed = json.loads(raw)
            if saved and isinstance(parsed, dict) and "base_url" not in parsed and "api_key" not in parsed:
                if set(parsed) - {"model", "session_id", "credential_id"}:
                    raise HTTPException(400, "Provide a model and the saved endpoint identity.")
                selected_model = validate_model(parsed.get("model"))
                session_id, revision = parsed.get("session_id"), parsed.get("credential_id")
                if session_id is not None and (not isinstance(session_id, str) or not 1 <= len(session_id) <= 256):
                    raise HTTPException(400, "Provide a valid chat identity.")
                if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{32}", revision)):
                    raise HTTPException(400, "Provide a valid saved endpoint identity.")
                if session_id is not None:
                    session = session_lookup(session_id) if session_lookup else None
                    if not session:
                        raise HTTPException(404, "Chat not found.")
                    selected = await asyncio.to_thread(store.for_session, session, include_key=True)
                else:
                    selected = await asyncio.to_thread(store.selection, include_key=True, include_revision=True)
                if selected is None:
                    raise HTTPException(409, "Save a custom endpoint before checking a model.")
                if revision is not None and selected.get("credential_id") != revision:
                    selected.clear()
                    raise HTTPException(409, "The saved endpoint changed. Refresh before checking this model.")
                return {**selected, "model": selected_model}
            return validate_selection(parsed)
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "Provide a valid provider request.") from None
        finally:
            raw.clear()

    def access(request):
        authorize(request)
        if not available():
            raise HTTPException(503, "Custom providers require native Codex app-server transport.")

    async def catalog(selected):
        # Native metadata is local and credential-free; endpoint discovery stays
        # explicit and supplies the identity/ownership fence for any fallback.
        if native_models is not None:
            try:
                native = await native_models()
            except (HTTPException, OSError, ValueError):
                native = None
            result = await asyncio.to_thread(discover, selected, native_models=native)
        else:
            result = await asyncio.to_thread(discover, selected)
        if result.get("ok") is True:
            store.cache_catalog(selected, result)
            return {**result, **store.cached_catalog(selected)}
        return result

    @router.get("/api/admin/codex/provider")
    async def status(request: Request):
        authorize(request)
        return JSONResponse(await asyncio.to_thread(store.status, available=available()), headers={"Cache-Control": "no-store"})

    @router.post("/api/admin/codex/provider/test")
    async def test(request: Request):
        access(request)
        selected = await body(request, saved=True)
        try:
            result = await probe(selected) if selected.get("model") else await catalog(selected)
            if selected.get("model") and result.get("compatibility") in {"verified", "unsupported", "unverified"}:
                evidence = {"compatibility": result["compatibility"]}
                if isinstance(result.get("reasoning_summary_supported"), bool):
                    evidence["reasoning_summary_supported"] = result["reasoning_summary_supported"]
                store.cache_model_capability(selected, evidence)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Provider test is unavailable.") from None
        finally:
            selected.clear()

    @router.get("/api/admin/codex/provider/models")
    async def models(request: Request, session_id: str | None = None):
        access(request)
        if session_id is not None:
            session = session_lookup(session_id) if session_lookup else None
            if not session:
                raise HTTPException(404, "Chat not found.")
            selected = await asyncio.to_thread(store.for_session, session, include_key=True)
        else:
            selected = await asyncio.to_thread(store.selection, include_key=True, include_revision=True)
        if selected is None:
            raise HTTPException(409, "Save a custom endpoint before refreshing its models.")
        try:
            return JSONResponse(await catalog(selected), headers={"Cache-Control": "no-store"})
        finally:
            selected.clear()

    @router.post("/api/admin/codex/provider/models")
    async def entered_models(request: Request):
        access(request)
        selected = await body(request)
        try:
            return JSONResponse(await catalog(selected), headers={"Cache-Control": "no-store"})
        finally:
            selected.clear()

    @router.put("/api/admin/codex/provider")
    async def save(request: Request):
        access(request)
        selected = await body(request)
        try:
            await mutate(selected)
            return JSONResponse(await asyncio.to_thread(store.status), headers={"Cache-Control": "no-store"})
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Provider settings could not be saved.") from None
        finally:
            selected.clear()

    @router.delete("/api/admin/codex/provider")
    async def reset(request: Request):
        access(request)
        try:
            await mutate(None)
            return JSONResponse(await asyncio.to_thread(store.status), headers={"Cache-Control": "no-store"})
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Provider settings could not be reset.") from None

    return router
