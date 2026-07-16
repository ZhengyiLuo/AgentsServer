#!/usr/bin/env bash
set -euo pipefail

PORT="7850"
BIND_ADDRESS="0.0.0.0"
RELEASE_VERSION=""
UV_VERSION="${AGENTS_SERVER_UV_VERSION:-0.10.10}"
INSTALL_ROOT="${AGENTS_SERVER_INSTALL_DIR:-$HOME/.local/share/agents-server}"
CONFIG_ROOT="${AGENTS_SERVER_CONFIG_DIR:-$HOME/.config/agents-server}"
STATE_ROOT="${ZENITHBOT_AGENT_DIR:-$HOME/.zenithbot-agent}"
SERVICE_NAME="agents-server"

usage() {
  cat <<'USAGE'
Usage: ./install.sh [--port PORT] [--bind ADDRESS] [--release-version VERSION]

Installs or updates AgentsServer for the current user. Releases and Python
runtimes are versioned, the previous healthy release is retained for rollback,
and existing chat state and generated tokens are preserved. No sudo privileges
are required.
USAGE
}

while (($#)); do
  case "$1" in
    --port) PORT="${2:-}"; shift 2 ;;
    --bind) BIND_ADDRESS="${2:-}"; shift 2 ;;
    --release-version) RELEASE_VERSION="${2:-}"; shift 2 ;;
    --non-interactive) shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || ((PORT < 1 || PORT > 65535)); then
  echo "Port must be an integer between 1 and 65535." >&2
  exit 2
fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$RELEASE_VERSION" && -f "$SOURCE_DIR/VERSION" ]]; then
  RELEASE_VERSION="$(tr -d '[:space:]' < "$SOURCE_DIR/VERSION")"
fi
if [[ ! "$RELEASE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][A-Za-z0-9.-]+)?$ ]]; then
  echo "Release version is missing or invalid." >&2
  exit 2
fi

RELEASES_ROOT="$INSTALL_ROOT/releases"
RELEASE_DIR="$RELEASES_ROOT/$RELEASE_VERSION"
STAGE_DIR="$RELEASES_ROOT/.staging-$RELEASE_VERSION-$$"
CURRENT_LINK="$INSTALL_ROOT/current"
PREVIOUS_LINK="$INSTALL_ROOT/previous"
ENV_FILE="$CONFIG_ROOT/env"
SERVER_PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
RELEASE_FILES=(agent_server.py install.sh update_runner.py pyproject.toml uv.lock VERSION release-public-key.pem)

for name in "${RELEASE_FILES[@]}"; do
  if [[ ! -f "$SOURCE_DIR/$name" ]]; then
    echo "$name is missing beside install.sh." >&2
    exit 1
  fi
done

cleanup() { rm -rf "$STAGE_DIR"; }
trap cleanup EXIT

echo "[1/7] Preparing the versioned AgentsServer runtime"
mkdir -p "$RELEASES_ROOT" "$CONFIG_ROOT" "$STATE_ROOT" "$STATE_ROOT/admin"
chmod 700 "$CONFIG_ROOT" "$STATE_ROOT" "$STATE_ROOT/admin"

if ! command -v uv >/dev/null 2>&1; then
  echo "      Installing uv $UV_VERSION for the current user"
  UV_INSTALLER="$(mktemp "${TMPDIR:-/tmp}/agents-server-uv.XXXXXX")"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" -o "$UV_INSTALLER"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$UV_INSTALLER" "https://astral.sh/uv/$UV_VERSION/install.sh"
  else
    echo "Install uv first: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
  fi
  sh "$UV_INSTALLER"
  rm -f "$UV_INSTALLER"
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { echo "uv is not available on PATH." >&2; exit 1; }

rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"
for name in "${RELEASE_FILES[@]}"; do
  install -m 644 "$SOURCE_DIR/$name" "$STAGE_DIR/$name"
done
chmod 755 "$STAGE_DIR/agent_server.py" "$STAGE_DIR/install.sh" "$STAGE_DIR/update_runner.py"

echo "[2/7] Resolving the release dependencies with uv"
uv sync --project "$STAGE_DIR" --python '>=3.10' --no-dev --frozen >/dev/null

TOKEN=""
ADMIN_TOKEN=""
if [[ -f "$ENV_FILE" ]]; then
  TOKEN="$(sed -n 's/^ZENITHDOCK_AGENT_TOKEN=//p' "$ENV_FILE" | tail -n 1)"
  ADMIN_TOKEN="$(sed -n 's/^AGENTS_SERVER_ADMIN_TOKEN=//p' "$ENV_FILE" | tail -n 1)"
fi
generate_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    "$STAGE_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))'
  fi
}
[[ "$TOKEN" =~ ^[A-Za-z0-9_-]{32,}$ ]] || TOKEN="$(generate_token)"
[[ "$ADMIN_TOKEN" =~ ^[A-Za-z0-9_-]{32,}$ ]] || ADMIN_TOKEN="$(generate_token)"

ENV_TEMP="$CONFIG_ROOT/.env.$$"
cat > "$ENV_TEMP" <<EOF
ZENITHBOT_AGENT_DIR=$STATE_ROOT
ZENITHBOT_AGENT_CWD=$HOME
ZENITHBOT_AGENT_BIND=$BIND_ADDRESS
ZENITHBOT_AGENT_PORT=$PORT
ZENITHDOCK_AGENT_TOKEN=$TOKEN
AGENTS_SERVER_ADMIN_TOKEN=$ADMIN_TOKEN
AGENTS_SERVER_INSTALL_DIR=$INSTALL_ROOT
PATH=$SERVER_PATH
EOF
chmod 600 "$ENV_TEMP"
mv "$ENV_TEMP" "$ENV_FILE"

echo "[3/7] Activating release $RELEASE_VERSION"
OLD_TARGET=""
if [[ -L "$CURRENT_LINK" ]]; then
  OLD_TARGET="$(readlink "$CURRENT_LINK")"
  [[ "$OLD_TARGET" == /* ]] || OLD_TARGET="$INSTALL_ROOT/$OLD_TARGET"
elif [[ -d "$CURRENT_LINK" ]]; then
  LEGACY_DIR="$RELEASES_ROOT/legacy-$(date -u +%Y%m%d%H%M%S)"
  mv "$CURRENT_LINK" "$LEGACY_DIR"
  OLD_TARGET="$LEGACY_DIR"
fi

if [[ -d "$RELEASE_DIR" ]]; then
  if [[ -n "$OLD_TARGET" && "$OLD_TARGET" == "$RELEASE_DIR" ]]; then
    REPLACED_DIR="$RELEASES_ROOT/$RELEASE_VERSION-replaced-$(date -u +%Y%m%d%H%M%S)-$$"
    mv "$RELEASE_DIR" "$REPLACED_DIR"
    OLD_TARGET="$REPLACED_DIR"
  else
    rm -rf "$RELEASE_DIR"
  fi
fi
mv "$STAGE_DIR" "$RELEASE_DIR"

if [[ -n "$OLD_TARGET" && -e "$OLD_TARGET" ]]; then
  ln -sfn "$OLD_TARGET" "$PREVIOUS_LINK"
fi
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"

restart_service() {
  if [[ "$OS_NAME" == "Linux" ]]; then
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME.service" >/dev/null
    systemctl --user restart "$SERVICE_NAME.service"
  else
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
  fi
}

echo "[4/7] Installing the user service"
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
WorkingDirectory=$CURRENT_LINK
EnvironmentFile=$ENV_FILE
ExecStart=$CURRENT_LINK/.venv/bin/python $CURRENT_LINK/agent_server.py serve --bind $BIND_ADDRESS --port $PORT
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
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
    <string>$CURRENT_LINK/.venv/bin/python</string>
    <string>$CURRENT_LINK/agent_server.py</string>
    <string>serve</string><string>--bind</string><string>$BIND_ADDRESS</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$CURRENT_LINK</string>
  <key>EnvironmentVariables</key><dict>
    <key>ZENITHBOT_AGENT_DIR</key><string>$STATE_ROOT</string>
    <key>ZENITHBOT_AGENT_CWD</key><string>$HOME</string>
    <key>ZENITHBOT_AGENT_BIND</key><string>$BIND_ADDRESS</string>
    <key>ZENITHBOT_AGENT_PORT</key><string>$PORT</string>
    <key>ZENITHDOCK_AGENT_TOKEN</key><string>$TOKEN</string>
    <key>AGENTS_SERVER_ADMIN_TOKEN</key><string>$ADMIN_TOKEN</string>
    <key>AGENTS_SERVER_INSTALL_DIR</key><string>$INSTALL_ROOT</string>
    <key>PATH</key><string>$SERVER_PATH</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/AgentsServer/server.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/AgentsServer/server-error.log</string>
</dict></plist>
EOF
  chmod 600 "$PLIST"
  SERVICE_KIND="launch-agent"
else
  echo "Unsupported host OS: $OS_NAME" >&2
  exit 1
fi
restart_service

wait_for_health() {
  for _attempt in $(seq 1 45); do
    if curl -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

echo "[5/7] Waiting for authenticated health"
if ! wait_for_health; then
  echo "AgentsServer $RELEASE_VERSION did not become healthy; rolling back." >&2
  if [[ -n "$OLD_TARGET" && -e "$OLD_TARGET" ]]; then
    ln -sfn "$OLD_TARGET" "$CURRENT_LINK"
    restart_service
    wait_for_health || true
    echo "The previous release was restored." >&2
  fi
  if [[ "$OS_NAME" == "Linux" ]]; then
    systemctl --user status "$SERVICE_NAME.service" --no-pager -l >&2 || true
  fi
  exit 1
fi

echo "[6/7] Checking optional agent runtimes"
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
[[ -z "$TAILSCALE_IP" ]] || SERVER_URL="http://$TAILSCALE_IP:$PORT"

echo "[7/7] AgentsServer $RELEASE_VERSION is ready"
printf 'AGENTSDOCK_SETUP_RESULT={"server_url":"%s","access_token":"%s","admin_token":"%s","service":"%s","tailscale_ip":"%s","server_version":"%s"}\n' \
  "$SERVER_URL" "$TOKEN" "$ADMIN_TOKEN" "$SERVICE_KIND" "$TAILSCALE_IP" "$RELEASE_VERSION"
