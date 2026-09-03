#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_HOST="${AGENTSDOCK_REMOTE_HOST:-${ZENITHDOCK_REMOTE_HOST:-${1:-}}}"
REMOTE_APP_DIR="${AGENTSDOCK_REMOTE_APP_DIR:-${ZENITHDOCK_REMOTE_APP_DIR:-.local/share/agents-server/current}}"
REMOTE_SERVER_PATH="$REMOTE_APP_DIR/agent_server.py"
REMOTE_SERVER_DIR="$(dirname "$REMOTE_SERVER_PATH")"
REMOTE_PYTHON="$REMOTE_APP_DIR/.venv/bin/python"
SERVICE_NAME="${AGENTSDOCK_SERVER_SERVICE:-${ZENITHDOCK_AGENT_SERVICE:-agents-server.service}}"
HEALTH_ATTEMPTS="${AGENTSDOCK_HEALTH_ATTEMPTS:-${ZENITHDOCK_HEALTH_ATTEMPTS:-45}}"
HEALTH_TOKEN="${AGENTSDOCK_AGENT_TOKEN:-${ZENITHDOCK_AGENT_TOKEN:-}}"
RUNTIME_FILES=(
  "$SCRIPT_DIR/agent_server.py"
  "$SCRIPT_DIR/team_hub_host.py"
  "$SCRIPT_DIR/secure_peer_runtime.py"
  "$SCRIPT_DIR/secure_peer_delivery.py"
  "$SCRIPT_DIR/agentsdock_jobs.py"
  "$SCRIPT_DIR/agentsdock_chats.py"
  "$SCRIPT_DIR/agentsdock_emergency.py"
  "$SCRIPT_DIR/agentsdock_publish.py"
  "$SCRIPT_DIR/agentsdock_mail.py"
  "$SCRIPT_DIR/claude_sdk_client.py"
  "$SCRIPT_DIR/codex_app_server.py"
  "$SCRIPT_DIR/cursor_agent_client.py"
  "$SCRIPT_DIR/cursor_process_guard.py"
  "$SCRIPT_DIR/update_runner.py"
  "$SCRIPT_DIR/release-public-key.pem"
  "$SCRIPT_DIR/VERSION"
)

if [[ -z "$REMOTE_HOST" ]]; then
  cat >&2 <<'USAGE'
Usage:
  AGENTSDOCK_REMOTE_HOST=<ssh-host> ./deploy.sh
  ./deploy.sh <ssh-host>

Optional:
  AGENTSDOCK_REMOTE_APP_DIR=<remote-app-dir>
  AGENTSDOCK_SERVER_SERVICE=<systemd-user-service>
  AGENTSDOCK_AGENT_TOKEN=<health-check-token>
  AGENTSDOCK_HEALTH_ATTEMPTS=<startup-health-attempts>

Direct in-place deployment is refused when the target hosts Team Hub or has a
paired Teamspace client. Use the signed managed update so its versioned
rollback and Teamspace continuity checks remain in force.
USAGE
  exit 2
fi

if [[ -n "$HEALTH_TOKEN" ]] && [[ ! "$HEALTH_TOKEN" =~ ^[A-Za-z0-9_-]{32,}$ ]]; then
  echo "AGENTSDOCK_AGENT_TOKEN is invalid." >&2
  exit 2
fi
if [[ -z "$HEALTH_TOKEN" ]]; then
  echo "AGENTSDOCK_AGENT_TOKEN is required to prove exact post-deploy version and preserve Teamspace state." >&2
  echo "  Use the signed managed update if authenticated in-place health is unavailable." >&2
  exit 2
fi

REMOTE_HAS_HUB_RUNTIME="$(
  ssh "$REMOTE_HOST" \
    "if [[ -f '$REMOTE_SERVER_DIR/team_hub_host.py' || -d '$REMOTE_SERVER_DIR/agentsdock_team_hub' ]]; then printf present; else printf absent; fi"
)"
if [[ "$REMOTE_HAS_HUB_RUNTIME" != "present" && "$REMOTE_HAS_HUB_RUNTIME" != "absent" ]]; then
  echo "Could not determine whether the target already contains Team Hub runtime files." >&2
  exit 1
fi

TARGET_HUB_STATE="unknown"
EXPECTED_SERVER_IDENTITY=""
if [[ -n "$HEALTH_TOKEN" ]]; then
  if TARGET_HEALTH="$(
    ssh "$REMOTE_HOST" \
      "curl -fsS --connect-timeout 2 --max-time 5 --max-filesize 1048576 -H 'Authorization: Bearer ${HEALTH_TOKEN}' http://127.0.0.1:7850/api/health"
  )"; then
    if ((${#TARGET_HEALTH} > 1048576)); then
      echo "Target health response exceeds its safety limit." >&2
      exit 1
    fi
    if ! TARGET_HUB_RESULT="$(
      printf '%s' "$TARGET_HEALTH" | python3 -c '
import json
import re
import sys


def valid_identifier(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_.:-]{8,240}", value) is not None
    )


def secure_peer_available(capabilities):
    capability = capabilities.get("secure_peer_v1")
    required = {
        "available": True,
        "state_available": True,
        "state_error_code": None,
        "required": False,
        "version": 1,
        "control_path": "/api/admin/secure-peers/v1/status",
        "proxy_prefix": "/api/team-hub-secure",
    }
    return isinstance(capability, dict) and all(
        capability.get(key) == expected for key, expected in required.items()
    )


try:
    health = json.load(sys.stdin)
except (UnicodeError, json.JSONDecodeError):
    raise SystemExit(2)
if not isinstance(health, dict) or health.get("ok") is not True:
    raise SystemExit(2)
server_identity = health.get("server_identity")
if not valid_identifier(server_identity):
    raise SystemExit(2)
capabilities = health.get("capabilities")
if not isinstance(capabilities, dict) or "team_hub_v1" not in capabilities:
    print(f"legacy\t{server_identity}")
    raise SystemExit(0)
capability = capabilities.get("team_hub_v1")
if not isinstance(capability, dict):
    raise SystemExit(2)
if capability.get("designated_host") is True:
    print(f"host\t{server_identity}")
elif not secure_peer_available(capabilities):
    raise SystemExit(2)
elif (
    capability.get("designated_host") is False
    and capability.get("available") is False
    and capability.get("version") == 1
    and capability.get("base_path") is None
    and capability.get("hub_id") is None
    and capability.get("host_server_identity") is None
):
    print(f"disabled\t{server_identity}")
elif (
    capability.get("designated_host") is False
    and capability.get("available") is True
    and capability.get("version") == 1
    and capability.get("transport") == "secure_peer"
    and capability.get("hub_url") is None
    and secure_peer_available(capabilities)
):
    connection_id = capability.get("connection_id")
    hub_id = capability.get("hub_id")
    host_server_identity = capability.get("host_server_identity")
    if not all(valid_identifier(value) for value in (
        connection_id,
        hub_id,
        host_server_identity,
    )):
        raise SystemExit(2)
    base_path = f"/api/team-hub-secure/{connection_id}"
    route = {
        "transport": "secure_peer",
        "hub_url": None,
        "base_path": base_path,
        "connection_id": connection_id,
        "host_server_identity": host_server_identity,
        "hub_id": hub_id,
    }
    if capability.get("base_path") != base_path or capability.get("routes") != [route]:
        raise SystemExit(2)
    print(f"client\t{server_identity}")
else:
    raise SystemExit(2)
'
    )"; then
      echo "Target returned a malformed or quarantined Teamspace capability; refusing in-place deployment." >&2
      exit 1
    fi
    TARGET_HUB_STATE="${TARGET_HUB_RESULT%%$'\t'*}"
    EXPECTED_SERVER_IDENTITY="${TARGET_HUB_RESULT#*$'\t'}"
  elif [[ "$REMOTE_HAS_HUB_RUNTIME" == "absent" ]]; then
    TARGET_HUB_STATE="legacy"
  fi
elif [[ "$REMOTE_HAS_HUB_RUNTIME" == "absent" ]]; then
  TARGET_HUB_STATE="legacy"
fi

if [[ "$TARGET_HUB_STATE" == "host" ]]; then
  echo "Target is the designated Team Hub host; use the signed managed update instead of deploy.sh." >&2
  exit 1
fi
if [[ "$TARGET_HUB_STATE" == "legacy" ]]; then
  echo "Target does not report the exact disabled Team Hub capability; refusing in-place deployment." >&2
  echo "  Use the signed installer/update so existing configuration and state cannot be migrated without rollback." >&2
  exit 1
fi
if [[ "$TARGET_HUB_STATE" == "unknown" ]]; then
  echo "Could not prove that the existing Team Hub runtime is disabled; set AGENTSDOCK_AGENT_TOKEN or use the signed managed update." >&2
  exit 1
fi
if [[ ( "$TARGET_HUB_STATE" == "disabled" || "$TARGET_HUB_STATE" == "client" ) && -z "$EXPECTED_SERVER_IDENTITY" ]]; then
  echo "Target did not provide a stable server identity; refusing in-place deployment." >&2
  exit 1
fi

# team_hub_v1 is intentionally a live route capability: an offline paired
# client can temporarily look disabled there. Bind the deployment decision to
# durable secure-peer control state before touching the active release.
if ! TARGET_SECURE_PEER_STATUS="$(
  ssh "$REMOTE_HOST" \
    "curl -fsS --connect-timeout 2 --max-time 5 --max-filesize 1048576 -H 'X-AgentsDock-Token: ${HEALTH_TOKEN}' http://127.0.0.1:7850/api/admin/secure-peers/v1/status"
)"; then
  echo "Could not verify durable secure-peer pairing state; use the signed managed update." >&2
  exit 1
fi
if ((${#TARGET_SECURE_PEER_STATUS} > 1048576)); then
  echo "Target secure-peer status response exceeds its safety limit." >&2
  exit 1
fi
if ! TARGET_PEER_STATE="$(
  printf '%s' "$TARGET_SECURE_PEER_STATUS" | python3 -c '
import json
import re
import sys


def valid_identifier(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_.:-]{8,240}", value) is not None
    )


expected_server_identity = sys.argv[1]
try:
    status = json.load(sys.stdin)
except (UnicodeError, json.JSONDecodeError):
    raise SystemExit(2)
if (
    not isinstance(status, dict)
    or status.get("version") not in {1, 2}
    or "active_connection_id" not in status
):
    raise SystemExit(2)
if status.get("server_identity") != expected_server_identity:
    raise SystemExit(2)
status_version = status["version"]
active_connection_id = status.get("active_connection_id")
if active_connection_id is not None and not valid_identifier(active_connection_id):
    raise SystemExit(2)
pairings = status.get("pairings")
if not isinstance(pairings, list):
    raise SystemExit(2)
paired_client = active_connection_id is not None
active_connection_seen = active_connection_id is None
known_statuses = {
    "requesting",
    "pending_approval",
    "approved",
    "connected",
    "rejected",
    "revoked",
    "expired",
    "error",
}
trust_statuses = {
    "pending": {"requesting", "pending_approval"},
    "approved": {"approved", "connected"},
    "rejected": {"rejected"},
    "cancelled": {"rejected"},
    "revoked": {"revoked"},
    "expired": {"expired"},
    "error": {"error"},
}
known_transport_states = {"online", "reconnecting", "offline", "disconnected", "revoked"}
for pairing in pairings:
    if not isinstance(pairing, dict):
        raise SystemExit(2)
    direction = pairing.get("direction")
    if direction not in {"incoming", "outgoing"}:
        raise SystemExit(2)
    if direction != "outgoing":
        continue
    connection_id = pairing.get("connection_id")
    pairing_status = pairing.get("status")
    if not valid_identifier(connection_id) or pairing_status not in known_statuses:
        raise SystemExit(2)
    if connection_id == active_connection_id:
        active_connection_seen = True
        paired_client = True
    if status_version == 1:
        actionable = pairing_status in {
            "requesting",
            "pending_approval",
            "approved",
            "connected",
        }
    else:
        trust_state = pairing.get("trust_state")
        transport_state = pairing.get("transport_state")
        if (
            trust_state not in trust_statuses
            or pairing_status not in trust_statuses[trust_state]
            or transport_state not in known_transport_states
        ):
            raise SystemExit(2)
        actionable = trust_state in {"pending", "approved"}
    # Pending or approved outgoing relationships can change while the process
    # is down. Only the managed updater can preserve or reject them under an
    # exact snapshot and rollback transaction.
    if actionable:
        paired_client = True
if not active_connection_seen:
    raise SystemExit(2)
print("client" if paired_client else "unpaired")
' "$EXPECTED_SERVER_IDENTITY"
)"; then
  echo "Target returned malformed or quarantined secure-peer status; refusing in-place deployment." >&2
  exit 1
fi
if [[ "$TARGET_HUB_STATE" == "client" || "$TARGET_PEER_STATE" == "client" ]]; then
  echo "Target is a paired secure-peer Teamspace client; use the signed managed update instead of deploy.sh." >&2
  exit 1
fi

echo "Deploying AgentsServer runtime to $REMOTE_HOST:$REMOTE_SERVER_DIR"
ssh "$REMOTE_HOST" "mkdir -p '$REMOTE_SERVER_DIR'"
scp "${RUNTIME_FILES[@]}" "$REMOTE_HOST:$REMOTE_SERVER_DIR/"
rsync -a --delete --delete-excluded \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '*.pyo' \
  "$SCRIPT_DIR/agentsdock_team_hub/" \
  "$REMOTE_HOST:$REMOTE_SERVER_DIR/agentsdock_team_hub/"

echo "Checking server runtime dependencies"
ssh "$REMOTE_HOST" "
  if ! '$REMOTE_PYTHON' -c 'from importlib.metadata import version; import claude_agent_sdk, croniter, cryptography, dateutil, tzdata; sdk_version = version(\"claude-agent-sdk\"); raise SystemExit(0 if sdk_version == \"0.2.130\" else f\"expected claude-agent-sdk 0.2.130, got {sdk_version}\")' >/dev/null 2>&1; then
    if [[ -x \"\$HOME/.local/bin/uv\" ]]; then
      \"\$HOME/.local/bin/uv\" pip install --python '$REMOTE_PYTHON' \
        --no-binary claude-agent-sdk \
        'claude-agent-sdk==0.2.130' 'croniter>=6,<7' 'cryptography>=44,<47' 'python-dateutil>=2.9,<3' 'tzdata>=2025.2'
    else
      '$REMOTE_PYTHON' -m pip install \
        --no-binary claude-agent-sdk \
        'claude-agent-sdk==0.2.130' 'croniter>=6,<7' 'cryptography>=44,<47' 'python-dateutil>=2.9,<3' 'tzdata>=2025.2'
    fi
  fi
  PYTHONPATH='$REMOTE_SERVER_DIR' '$REMOTE_PYTHON' -c 'from importlib.metadata import version; import agentsdock_team_hub, claude_agent_sdk, cursor_agent_client, cursor_process_guard, secure_peer_delivery, secure_peer_runtime, team_hub_host, agentsdock_mail; from agentsdock_team_hub import secure_peer, secure_peer_hub; sdk_version = version(\"claude-agent-sdk\"); raise SystemExit(0 if sdk_version == \"0.2.130\" else f\"expected claude-agent-sdk 0.2.130, got {sdk_version}\")'
"

echo "Compiling server on $REMOTE_HOST"
ssh "$REMOTE_HOST" "chmod 755 '$REMOTE_SERVER_DIR/agentsdock_jobs.py' '$REMOTE_SERVER_DIR/agentsdock_chats.py' '$REMOTE_SERVER_DIR/agentsdock_emergency.py' '$REMOTE_SERVER_DIR/agentsdock_publish.py' '$REMOTE_SERVER_DIR/agentsdock_mail.py' && '$REMOTE_PYTHON' -m compileall -q '$REMOTE_SERVER_DIR/agentsdock_team_hub' && '$REMOTE_PYTHON' -m py_compile '$REMOTE_SERVER_PATH' '$REMOTE_SERVER_DIR/team_hub_host.py' '$REMOTE_SERVER_DIR/secure_peer_runtime.py' '$REMOTE_SERVER_DIR/secure_peer_delivery.py' '$REMOTE_SERVER_DIR/agentsdock_jobs.py' '$REMOTE_SERVER_DIR/agentsdock_chats.py' '$REMOTE_SERVER_DIR/agentsdock_emergency.py' '$REMOTE_SERVER_DIR/agentsdock_publish.py' '$REMOTE_SERVER_DIR/agentsdock_mail.py' '$REMOTE_SERVER_DIR/claude_sdk_client.py' '$REMOTE_SERVER_DIR/codex_app_server.py' '$REMOTE_SERVER_DIR/cursor_agent_client.py' '$REMOTE_SERVER_DIR/cursor_process_guard.py' '$REMOTE_SERVER_DIR/update_runner.py'"

echo "Restarting $SERVICE_NAME"
ssh "$REMOTE_HOST" "systemctl --user restart '$SERVICE_NAME'"

echo "Checking health"
HEALTH_OK=0
EXPECTED_VERSION="$(tr -d '[:space:]' < "$SCRIPT_DIR/VERSION")"
for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
  if REMOTE_HEALTH="$(
    ssh "$REMOTE_HOST" \
      "curl -fsS --connect-timeout 2 --max-time 5 --max-filesize 1048576 -H 'Authorization: Bearer ${HEALTH_TOKEN}' http://127.0.0.1:7850/api/health"
  )"; then
    if ((${#REMOTE_HEALTH} <= 1048576)) && \
      printf '%s' "$REMOTE_HEALTH" | python3 -c '
import json
import re
import sys

expected_version = sys.argv[1]
expected_server_identity = sys.argv[2]
try:
    health = json.load(sys.stdin)
except (UnicodeError, json.JSONDecodeError):
    raise SystemExit(1)
if not isinstance(health, dict) or health.get("ok") is not True:
    raise SystemExit(1)
if health.get("server_version") != expected_version:
    raise SystemExit(1)
server_identity = health.get("server_identity")
if not isinstance(server_identity, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{8,240}", server_identity) is None:
    raise SystemExit(1)
if server_identity != expected_server_identity:
    raise SystemExit(1)
capabilities = health.get("capabilities")
capability = capabilities.get("team_hub_v1") if isinstance(capabilities, dict) else None
if not isinstance(capability, dict):
    raise SystemExit(1)
required = {
    "available": False,
    "designated_host": False,
    "version": 1,
    "base_path": None,
    "hub_id": None,
    "host_server_identity": None,
    "transport": None,
    "hub_url": None,
    "routes": [],
}
if any(capability.get(key) != value for key, value in required.items()):
    raise SystemExit(1)
secure_capability = capabilities.get("secure_peer_v1")
secure_required = {
    "available": True,
    "state_available": True,
    "state_error_code": None,
    "required": False,
    "version": 1,
    "control_path": "/api/admin/secure-peers/v1/status",
    "proxy_prefix": "/api/team-hub-secure",
}
if not isinstance(secure_capability, dict) or any(
    secure_capability.get(key) != value
    for key, value in secure_required.items()
):
    raise SystemExit(1)
' "$EXPECTED_VERSION" "$EXPECTED_SERVER_IDENTITY"; then
      printf '%s\n' "$REMOTE_HEALTH"
      HEALTH_OK=1
      break
    fi
  fi
  sleep 1
done
if [[ "$HEALTH_OK" != "1" ]]; then
  echo "Authenticated health did not prove exact version $EXPECTED_VERSION with Teamspace unpaired and secure-peer state available." >&2
  exit 1
fi
echo
