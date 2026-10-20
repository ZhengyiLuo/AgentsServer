"""Gemini native approval modes, not Codex sandbox or authorization grants."""
from gemini_acp_client import GeminiProtocolError

MODES = ("default", "autoEdit", "yolo", "plan")
# Plan is offered by the UI only after this workspace's native handshake has
# advertised it. Every turn revalidates the native list before any prompt.
INITIAL_MODES = MODES[:3]


def effective_mode(session: dict) -> str:
    value = session.get("gemini_permission_mode") or "default"
    if value not in MODES:
        raise GeminiProtocolError("Invalid Gemini permission mode. Choose a native Gemini mode.")
    return value


def startup_mode(mode: str) -> str:
    # Explicit argv prevents inheriting a user's default YOLO setting. Starting
    # YOLO through the CLI also enforces native disableYoloMode/secureModeEnabled;
    # pinned ACP set_mode alone does not check those administrator restrictions.
    # Plan is selected over ACP: CLI --approval-mode plan silently falls back to
    # default when Plan is disabled, which must never masquerade as read-only.
    return {"default": "default", "autoEdit": "auto_edit", "yolo": "yolo", "plan": "default"}[mode]


def available_modes(opened: dict) -> list[str]:
    modes = opened.get("modes")
    options = modes.get("availableModes") if isinstance(modes, dict) else None
    if not isinstance(options, list) or len(options) > 32:
        raise GeminiProtocolError("Gemini did not return its native permission modes. Check the supported CLI version.")
    ids = []
    for option in options:
        if not isinstance(option, dict) or not isinstance(option.get("id"), str):
            raise GeminiProtocolError("Gemini returned an invalid permission mode.")
        if option["id"] in MODES and option["id"] not in ids:
            ids.append(option["id"])
    if "default" not in ids:
        raise GeminiProtocolError("Gemini did not advertise normal tool approvals.")
    return ids


async def apply_mode(client, opened: dict, mode: str) -> list[str]:
    modes = available_modes(opened)
    if mode not in modes:
        raise GeminiProtocolError(f"Gemini permission mode '{mode}' is not enabled in this workspace. Choose an available mode; no prompt was sent.")
    await client.request("session/set_mode", {"sessionId": client.session_id, "modeId": mode})
    return modes
