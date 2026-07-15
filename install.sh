#!/usr/bin/env bash
set -euo pipefail

PORT="7850"
BIND_ADDRESS="0.0.0.0"
UV_VERSION="${AGENTS_SERVER_UV_VERSION:-0.10.10}"
INSTALL_ROOT="${AGENTS_SERVER_INSTALL_DIR:-$HOME/.local/share/agents-server}"
CONFIG_ROOT="${AGENTS_SERVER_CONFIG_DIR:-$HOME/.config/agents-server}"
STATE_ROOT="${ZENITHBOT_AGENT_DIR:-$HOME/.zenithbot-agent}"
SERVICE_NAME="agents-server"

usage() {
  cat <<'USAGE'
Usage: ./install.sh [--port PORT] [--bind ADDRESS]

Installs or updates AgentsServer for the current user. Existing chat state and
the generated access token are preserved. No sudo privileges are required.
USAGE
}

while (($#)); do
  case "$1" in
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    --bind)
      BIND_ADDRESS="${2:-}"
      shift 2
      ;;
    --non-interactive)
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || ((PORT < 1 || PORT > 65535)); then
  echo "Port must be an integer between 1 and 65535." >&2
  exit 2
fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVER_SOURCE="$SOURCE_DIR/agent_server.py"
APP_DIR="$INSTALL_ROOT/current"
VENV_DIR="$INSTALL_ROOT/.venv"
ENV_FILE="$CONFIG_ROOT/env"

if [[ ! -f "$SERVER_SOURCE" ]]; then
  echo "agent_server.py is missing beside install.sh." >&2
  exit 1
fi

echo "[1/6] Preparing the AgentsServer runtime"
mkdir -p "$APP_DIR" "$CONFIG_ROOT" "$STATE_ROOT"
chmod 700 "$CONFIG_ROOT" "$STATE_ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "      Installing uv $UV_VERSION for the current user"
  UV_INSTALLER="$(mktemp "${TMPDIR:-/tmp}/agents-server-uv.XXXXXX")"
  trap 'rm -f "$UV_INSTALLER"' EXIT
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" -o "$UV_INSTALLER"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$UV_INSTALLER" "https://astral.sh/uv/$UV_VERSION/install.sh"
  else
    echo "Install uv first: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
  fi
  sh "$UV_INSTALLER"
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv was installed but is not available on PATH." >&2
  exit 1
fi

echo "[2/6] Installing Python dependencies with uv"
uv venv --python '>=3.10' "$VENV_DIR" >/dev/null
uv pip install --python "$VENV_DIR/bin/python" \
  fastapi uvicorn python-multipart pydantic >/dev/null
install -m 755 "$SERVER_SOURCE" "$APP_DIR/agent_server.py"

TOKEN=""
if [[ -f "$ENV_FILE" ]]; then
  TOKEN="$(sed -n 's/^ZENITHDOCK_AGENT_TOKEN=//p' "$ENV_FILE" | tail -n 1)"
fi
if [[ ! "$TOKEN" =~ ^[A-Za-z0-9_-]{32,}$ ]]; then
  if command -v openssl >/dev/null 2>&1; then
    TOKEN="$(openssl rand -hex 32)"
  else
    TOKEN="$($VENV_DIR/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
  fi
fi

cat > "$ENV_FILE" <<EOF
ZENITHBOT_AGENT_DIR=$STATE_ROOT
ZENITHBOT_AGENT_CWD=$HOME
ZENITHBOT_AGENT_BIND=$BIND_ADDRESS
ZENITHBOT_AGENT_PORT=$PORT
ZENITHDOCK_AGENT_TOKEN=$TOKEN
EOF
chmod 600 "$ENV_FILE"

echo "[3/6] Installing the user service"
OS_NAME="$(uname -s)"
if [[ "$OS_NAME" == "Linux" ]]; then
  USER_SERVICE_DIR="$HOME/.config/systemd/user"
  mkdir -p "$USER_SERVICE_DIR"
  cat > "$USER_SERVICE_DIR/$SERVICE_NAME.service" <<EOF
[Unit]
Description=AgentsServer
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python $APP_DIR/agent_server.py serve --bind $BIND_ADDRESS --port $PORT
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable "$SERVICE_NAME.service" >/dev/null
  systemctl --user restart "$SERVICE_NAME.service"
  SERVICE_KIND="systemd-user"
elif [[ "$OS_NAME" == "Darwin" ]]; then
  LABEL="com.agentsdock.server"
  LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
  PLIST="$LAUNCH_AGENTS/$LABEL.plist"
  mkdir -p "$LAUNCH_AGENTS" "$HOME/Library/Logs/AgentsServer"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$VENV_DIR/bin/python</string>
    <string>$APP_DIR/agent_server.py</string>
    <string>serve</string><string>--bind</string><string>$BIND_ADDRESS</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>ZENITHBOT_AGENT_DIR</key><string>$STATE_ROOT</string>
    <key>ZENITHBOT_AGENT_CWD</key><string>$HOME</string>
    <key>ZENITHDOCK_AGENT_TOKEN</key><string>$TOKEN</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/AgentsServer/server.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/AgentsServer/server-error.log</string>
</dict></plist>
EOF
  chmod 600 "$PLIST"
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  SERVICE_KIND="launch-agent"
else
  echo "Unsupported host OS: $OS_NAME" >&2
  exit 1
fi

echo "[4/6] Waiting for authenticated health"
HEALTH_OK=0
for _attempt in $(seq 1 45); do
  if curl -fsS -H "Authorization: Bearer $TOKEN" \
    "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    HEALTH_OK=1
    break
  fi
  sleep 1
done
if [[ "$HEALTH_OK" != "1" ]]; then
  echo "AgentsServer did not become healthy on port $PORT." >&2
  if [[ "$OS_NAME" == "Linux" ]]; then
    systemctl --user status "$SERVICE_NAME.service" --no-pager -l >&2 || true
  fi
  exit 1
fi

echo "[5/6] Checking optional agent runtimes"
RUNTIMES=()
command -v claude >/dev/null 2>&1 && RUNTIMES+=(claude)
command -v codex >/dev/null 2>&1 && RUNTIMES+=(codex)
if ((${#RUNTIMES[@]} == 0)); then
  echo "      Server is ready; install and sign in to Claude Code or Codex before starting a chat."
else
  echo "      Found: ${RUNTIMES[*]}"
fi

TAILSCALE_IP=""
if command -v tailscale >/dev/null 2>&1; then
  TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
fi
SERVER_URL="http://127.0.0.1:$PORT"
if [[ -n "$TAILSCALE_IP" ]]; then
  SERVER_URL="http://$TAILSCALE_IP:$PORT"
fi

echo "[6/6] AgentsServer is ready"
printf 'AGENTSDOCK_SETUP_RESULT={"server_url":"%s","access_token":"%s","service":"%s","tailscale_ip":"%s"}\n' \
  "$SERVER_URL" "$TOKEN" "$SERVICE_KIND" "$TAILSCALE_IP"
