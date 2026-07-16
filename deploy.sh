#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_HOST="${ZENITHDOCK_REMOTE_HOST:-${1:-}}"
REMOTE_APP_DIR="${AGENTSDOCK_REMOTE_APP_DIR:-${ZENITHDOCK_REMOTE_APP_DIR:-.local/share/agents-server/current}}"
REMOTE_SERVER_PATH="$REMOTE_APP_DIR/agent_server.py"
REMOTE_SERVER_DIR="$(dirname "$REMOTE_SERVER_PATH")"
REMOTE_PYTHON="$REMOTE_APP_DIR/.venv/bin/python"
SERVICE_NAME="${AGENTSDOCK_SERVER_SERVICE:-${ZENITHDOCK_AGENT_SERVICE:-agents-server.service}}"
HEALTH_ATTEMPTS="${ZENITHDOCK_HEALTH_ATTEMPTS:-45}"
HEALTH_TOKEN="${AGENTSDOCK_AGENT_TOKEN:-${ZENITHDOCK_AGENT_TOKEN:-}}"
RUNTIME_FILES=(
  "$SCRIPT_DIR/agent_server.py"
  "$SCRIPT_DIR/update_runner.py"
  "$SCRIPT_DIR/release-public-key.pem"
  "$SCRIPT_DIR/VERSION"
)

if [[ -z "$REMOTE_HOST" ]]; then
  cat >&2 <<'USAGE'
Usage:
  ZENITHDOCK_REMOTE_HOST=<ssh-host> ./server/deploy.sh
  ./server/deploy.sh <ssh-host>

Optional:
  AGENTSDOCK_REMOTE_APP_DIR=<remote-app-dir>
  AGENTSDOCK_SERVER_SERVICE=<systemd-user-service>
  AGENTSDOCK_AGENT_TOKEN=<health-check-token>
  ZENITHDOCK_HEALTH_ATTEMPTS=<startup-health-attempts>
USAGE
  exit 2
fi

echo "Deploying AgentsServer runtime to $REMOTE_HOST:$REMOTE_SERVER_DIR"
ssh "$REMOTE_HOST" "mkdir -p '$REMOTE_SERVER_DIR'"
scp "${RUNTIME_FILES[@]}" "$REMOTE_HOST:$REMOTE_SERVER_DIR/"

echo "Checking server runtime dependencies"
ssh "$REMOTE_HOST" "
  if ! '$REMOTE_PYTHON' -c 'import cryptography' >/dev/null 2>&1; then
    if [[ -x \"\$HOME/.local/bin/uv\" ]]; then
      \"\$HOME/.local/bin/uv\" pip install --python '$REMOTE_PYTHON' cryptography
    else
      '$REMOTE_PYTHON' -m pip install cryptography
    fi
  fi
"

echo "Compiling server on $REMOTE_HOST"
ssh "$REMOTE_HOST" "'$REMOTE_PYTHON' -m py_compile '$REMOTE_SERVER_PATH' '$REMOTE_SERVER_DIR/update_runner.py'"

echo "Restarting $SERVICE_NAME"
ssh "$REMOTE_HOST" "systemctl --user restart '$SERVICE_NAME'"

echo "Checking health"
REMOTE_HEALTH_BODY="/tmp/zenithdock-agent-health.json"
HEALTH_OK=0
STATUS="000"
for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
  if [[ -n "$HEALTH_TOKEN" ]]; then
    if ssh "$REMOTE_HOST" "curl -fsS -H 'Authorization: Bearer ${HEALTH_TOKEN}' http://127.0.0.1:7850/api/health"; then
      HEALTH_OK=1
      break
    fi
  else
    STATUS="$(ssh "$REMOTE_HOST" "curl -sS -o '$REMOTE_HEALTH_BODY' -w '%{http_code}' http://127.0.0.1:7850/api/health || true")"
    if [[ "$STATUS" == "200" ]]; then
      ssh "$REMOTE_HOST" "cat '$REMOTE_HEALTH_BODY'"
      HEALTH_OK=1
      break
    elif [[ "$STATUS" == "401" ]]; then
      echo "Health endpoint requires a token; service is responding. Set AGENTSDOCK_AGENT_TOKEN to verify authenticated health."
      HEALTH_OK=1
      break
    fi
  fi
  sleep 1
done
if [[ "$HEALTH_OK" != "1" ]]; then
  ssh "$REMOTE_HOST" "cat '$REMOTE_HEALTH_BODY'" || true
  echo "Health check failed with HTTP $STATUS" >&2
  exit 1
fi
echo
