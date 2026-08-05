#!/usr/bin/env bash
set -euo pipefail

PORT="7850"
BIND_ADDRESS="0.0.0.0"
RELEASE_VERSION=""
UV_VERSION="${AGENTS_SERVER_UV_VERSION:-0.10.10}"
UV_DOWNLOAD_CONNECT_TIMEOUT_SECONDS="${AGENTS_SERVER_DOWNLOAD_CONNECT_TIMEOUT_SECONDS:-15}"
UV_DOWNLOAD_TIMEOUT_SECONDS="${AGENTS_SERVER_DOWNLOAD_TIMEOUT_SECONDS:-180}"
UV_DOWNLOAD_RETRIES="${AGENTS_SERVER_DOWNLOAD_RETRIES:-3}"
UV_INSTALL_TIMEOUT_SECONDS="${AGENTS_SERVER_UV_INSTALL_TIMEOUT_SECONDS:-300}"
DEPENDENCY_SYNC_TIMEOUT_SECONDS="${AGENTS_SERVER_DEPENDENCY_TIMEOUT_SECONDS:-1200}"
INSTALL_HEARTBEAT_SECONDS="${AGENTS_SERVER_INSTALL_HEARTBEAT_SECONDS:-15}"
HEALTH_CHECK_ATTEMPTS="${AGENTS_SERVER_HEALTH_CHECK_ATTEMPTS:-45}"
INSTALL_ROOT="${AGENTS_SERVER_INSTALL_DIR:-$HOME/.local/share/agents-server}"
CONFIG_ROOT="${AGENTS_SERVER_CONFIG_DIR:-$HOME/.config/agents-server}"
LEGACY_STATE_ROOT="$HOME/.zenithbot-agent"
if [[ -n "${AGENTSDOCK_STATE_DIR:-}" ]]; then
  STATE_ROOT="$AGENTSDOCK_STATE_DIR"
elif [[ -n "${AGENTS_SERVER_STATE_DIR:-}" ]]; then
  STATE_ROOT="$AGENTS_SERVER_STATE_DIR"
elif [[ -n "${ZENITHBOT_AGENT_DIR:-}" ]]; then
  STATE_ROOT="$ZENITHBOT_AGENT_DIR"
else
  STATE_ROOT="$HOME/.agentsdock"
fi
SERVICE_NAME="agents-server"
LEGACY_SERVICE_NAME="zenithbot-agent"
LAUNCHCTL_STOP_ATTEMPTS=50
LAUNCHCTL_STOP_DELAY=0.1
LAUNCHCTL_BOOTSTRAP_ATTEMPTS=3
NON_INTERACTIVE="false"
PORT_EXPLICIT="false"
PORT_FALLBACK="auto"
PORT_FALLBACK_ATTEMPTS=5

if [[ -t 1 ]] && [[ "${TERM:-}" != "dumb" ]] && [[ -z "${NO_COLOR:-}" ]]; then
  COLOR_GREEN=$'\033[32m'
  COLOR_RED=$'\033[31m'
  COLOR_YELLOW=$'\033[33m'
  COLOR_BOLD=$'\033[1m'
  COLOR_RESET=$'\033[0m'
else
  COLOR_GREEN=""
  COLOR_RED=""
  COLOR_YELLOW=""
  COLOR_BOLD=""
  COLOR_RESET=""
fi
CHECK_MARK="${COLOR_GREEN}✓${COLOR_RESET}"
CROSS_MARK="${COLOR_RED}✗${COLOR_RESET}"
DOT_MARK="${COLOR_YELLOW}○${COLOR_RESET}"

usage() {
  cat <<'USAGE'
Usage: ./install.sh [--port PORT] [--bind ADDRESS] [--release-version VERSION] [--non-interactive] [--allow-port-fallback|--no-port-fallback]

Installs or updates AgentsServer for the current user. Releases and Python
runtimes are versioned, the previous healthy release is retained for rollback,
and existing chat state and generated tokens are preserved. No sudo privileges
are required.

--non-interactive skips the optional tmux install prompt on macOS instead of
asking; use it for unattended/SSH-driven runs.

--port pins the exact requested port unless --allow-port-fallback is also set.
Without --port, setup may select one of the next 5 ports when the default is
already occupied by a service that does not authenticate as AgentsServer.

--allow-port-fallback enables nearby-port selection even with an explicit
--port value.

--no-port-fallback disables automatically retrying on the next free port when
the default port is already held by another process.
USAGE
}

while (($#)); do
  case "$1" in
    --port) PORT="${2:-}"; PORT_EXPLICIT="true"; shift 2 ;;
    --bind) BIND_ADDRESS="${2:-}"; shift 2 ;;
    --release-version) RELEASE_VERSION="${2:-}"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE="true"; shift ;;
    --allow-port-fallback) PORT_FALLBACK="true"; shift ;;
    --no-port-fallback) PORT_FALLBACK="false"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$PORT_FALLBACK" == "auto" ]]; then
  if [[ "$PORT_EXPLICIT" == "true" ]]; then
    PORT_FALLBACK="false"
  else
    PORT_FALLBACK="true"
  fi
fi

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

validate_positive_integer() {
  local name="$1"
  local value="$2"
  if [[ ! "$value" =~ ^[0-9]+$ ]] || ((value < 1)); then
    echo "$name must be a positive integer, got: $value" >&2
    exit 2
  fi
}

validate_positive_integer "AGENTS_SERVER_DOWNLOAD_CONNECT_TIMEOUT_SECONDS" "$UV_DOWNLOAD_CONNECT_TIMEOUT_SECONDS"
validate_positive_integer "AGENTS_SERVER_DOWNLOAD_TIMEOUT_SECONDS" "$UV_DOWNLOAD_TIMEOUT_SECONDS"
validate_positive_integer "AGENTS_SERVER_DOWNLOAD_RETRIES" "$UV_DOWNLOAD_RETRIES"
validate_positive_integer "AGENTS_SERVER_UV_INSTALL_TIMEOUT_SECONDS" "$UV_INSTALL_TIMEOUT_SECONDS"
validate_positive_integer "AGENTS_SERVER_DEPENDENCY_TIMEOUT_SECONDS" "$DEPENDENCY_SYNC_TIMEOUT_SECONDS"
validate_positive_integer "AGENTS_SERVER_INSTALL_HEARTBEAT_SECONDS" "$INSTALL_HEARTBEAT_SECONDS"
validate_positive_integer "AGENTS_SERVER_HEALTH_CHECK_ATTEMPTS" "$HEALTH_CHECK_ATTEMPTS"

RELEASES_ROOT="$INSTALL_ROOT/releases"
RELEASE_DIR="$RELEASES_ROOT/$RELEASE_VERSION"
STAGE_DIR="$RELEASES_ROOT/.staging-$RELEASE_VERSION-$$"
CURRENT_LINK="$INSTALL_ROOT/current"
PREVIOUS_LINK="$INSTALL_ROOT/previous"
ENV_FILE="$CONFIG_ROOT/env"
LEGACY_SERVICE_FILE="$HOME/.config/systemd/user/$LEGACY_SERVICE_NAME.service"

read_env_value() {
  local file="$1"
  local name="$2"
  [[ -f "$file" ]] || return 0
  sed -n "s/^${name}=//p" "$file" | tail -n 1
}

LEGACY_ENV_FILE=""
if [[ -f "$LEGACY_SERVICE_FILE" ]]; then
  LEGACY_ENV_FILE="$(sed -n 's/^EnvironmentFile=//p' "$LEGACY_SERVICE_FILE" | tail -n 1)"
  LEGACY_ENV_FILE="${LEGACY_ENV_FILE#-}"
  LEGACY_ENV_FILE="${LEGACY_ENV_FILE#\"}"
  LEGACY_ENV_FILE="${LEGACY_ENV_FILE%\"}"
  LEGACY_ENV_FILE="${LEGACY_ENV_FILE//%h/$HOME}"
fi
if [[ ! -f "$LEGACY_ENV_FILE" && -f "$HOME/Zenithbot/.env" ]]; then
  LEGACY_ENV_FILE="$HOME/Zenithbot/.env"
fi

OS_NAME="$(uname -s)"
SERVER_PATH=""
append_server_path() {
  local candidate="$1"
  [[ -n "$candidate" ]] || return 0
  case ":$SERVER_PATH:" in
    *":$candidate:"*) ;;
    *) SERVER_PATH="${SERVER_PATH:+$SERVER_PATH:}$candidate" ;;
  esac
}

append_server_path_list() {
  local path_list="$1"
  local candidate
  local old_ifs="$IFS"
  IFS=":"
  for candidate in $path_list; do
    append_server_path "$candidate"
  done
  IFS="$old_ifs"
}

EXISTING_PATH="$(read_env_value "$ENV_FILE" PATH)"
[[ -n "$EXISTING_PATH" ]] || EXISTING_PATH="$(read_env_value "$LEGACY_ENV_FILE" PATH)"
# Prefer the previously saved runtime PATH when present, otherwise retain the
# launcher's PATH. Add standard user and Homebrew locations without allowing
# repeated installs to grow the saved value indefinitely.
append_server_path_list "${EXISTING_PATH:-${PATH:-}}"
append_server_path "$HOME/.local/bin"
append_server_path "$HOME/.cargo/bin"
append_server_path "/opt/homebrew/bin"
append_server_path "/usr/local/bin"
append_server_path "/usr/bin"
append_server_path "/bin"
export PATH="$SERVER_PATH"
RELEASE_FILES=(agent_server.py agentsdock_jobs.py agentsdock_publish.py codex_app_server.py install.sh uninstall.sh update_runner.py pyproject.toml uv.lock VERSION release-public-key.pem)

for name in "${RELEASE_FILES[@]}"; do
  if [[ ! -f "$SOURCE_DIR/$name" ]]; then
    echo "$name is missing beside install.sh." >&2
    exit 1
  fi
done

PREFLIGHT_FAILED="false"
MISSING_PREREQUISITE_NAMES=()
MISSING_PREREQUISITE_GUIDANCE=()
record_prerequisite_failure() {
  local prerequisite_name="$1"
  local guidance="$2"
  PREFLIGHT_FAILED="true"
  MISSING_PREREQUISITE_NAMES+=("$prerequisite_name")
  MISSING_PREREQUISITE_GUIDANCE+=("$guidance")
  echo "Unavailable prerequisite: $prerequisite_name" >&2
  echo "  $guidance" >&2
}

require_command() {
  local command_name="$1"
  local guidance="$2"
  command -v "$command_name" >/dev/null 2>&1 || record_prerequisite_failure "$command_name" "$guidance"
}

require_download_client() {
  local guidance="$1"
  if command -v curl >/dev/null 2>&1 && curl --version >/dev/null 2>&1; then
    return 0
  fi
  if command -v wget >/dev/null 2>&1 && wget --version >/dev/null 2>&1; then
    return 0
  fi
  record_prerequisite_failure "curl or wget" "$guidance"
}

probe_service_manager() {
  local output=""
  if [[ "$OS_NAME" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1; then
    if ! output="$(launchctl print "gui/$UID" 2>&1)"; then
      [[ -z "$output" ]] || echo "  launchctl: ${output//$'\n'/ }" >&2
      record_prerequisite_failure \
        "macOS launchd user domain gui/$UID" \
        "Log into a macOS GUI user session and verify: launchctl print gui/$UID"
    fi
  elif [[ "$OS_NAME" == "Linux" ]] && command -v systemctl >/dev/null 2>&1; then
    if ! output="$(systemctl --user show-environment 2>&1)"; then
      [[ -z "$output" ]] || echo "  systemctl: ${output//$'\n'/ }" >&2
      record_prerequisite_failure \
        "systemctl --user session" \
        "Log into a systemd user session and verify: systemctl --user show-environment"
    fi
  fi
}

TMUX_WARNING=""

tmux_working() {
  command -v tmux >/dev/null 2>&1 && tmux -V >/dev/null 2>&1
}

offer_brew_tmux_install() {
  # Only ever offered on macOS with Homebrew present; never attempted over a
  # non-interactive/SSH-driven run, where there is no one to answer a prompt.
  [[ "$NON_INTERACTIVE" != "true" && -t 0 ]] || return 1
  command -v brew >/dev/null 2>&1 || return 1
  local reply=""
  read -r -p "      tmux was not found. Install it now with Homebrew (brew install tmux)? [y/N] " reply || return 1
  case "$reply" in
    y|Y|yes|YES) ;;
    *) return 1 ;;
  esac
  echo "      Installing tmux with Homebrew"
  brew install tmux
}

check_tmux_prerequisite() {
  # tmux is optional: it only backs the persistent chat terminal, tmux-pane
  # inspection, and in-app managed updates. The rest of AgentsServer (chats,
  # turns, jobs, files) runs without it, so a missing tmux is a warning, not
  # a preflight failure.
  local guidance=""
  tmux_working && return 0
  if [[ "$OS_NAME" == "Darwin" ]]; then
    if offer_brew_tmux_install && tmux_working; then
      return 0
    fi
    guidance="Install tmux with Homebrew: brew install tmux"
  else
    guidance="Install tmux with your package manager, for example: sudo apt install tmux, sudo dnf install tmux, or sudo pacman -S tmux."
  fi
  TMUX_WARNING="tmux is unavailable, so the persistent chat terminal, tmux-pane inspection, and in-app managed updates will not work. $guidance Then rerun install.sh to enable them; everything else about AgentsServer works without it."
  echo "Optional prerequisite unavailable: tmux" >&2
  echo "  $TMUX_WARNING" >&2
}

preflight_prerequisites() {
  case "$OS_NAME" in
    Darwin)
      require_download_client "Restore the curl included with macOS, or install curl or wget with Homebrew: brew install curl."
      require_command "launchctl" "launchctl is included with macOS; run this installer from a supported macOS user session."
      ;;
    Linux)
      require_download_client "Install curl or wget with your package manager, for example: sudo apt install curl, sudo dnf install curl, or sudo pacman -S curl."
      require_command "systemctl" "AgentsServer's Linux installer requires systemd and a working systemctl --user session."
      ;;
    *)
      echo "Unsupported host OS: $OS_NAME" >&2
      PREFLIGHT_FAILED="true"
      ;;
  esac
  case "$OS_NAME" in
    Darwin|Linux) check_tmux_prerequisite ;;
  esac
  probe_service_manager
  if [[ "$PREFLIGHT_FAILED" == "true" ]]; then
    local names=""
    local actions=""
    local index
    for ((index = 0; index < ${#MISSING_PREREQUISITE_NAMES[@]}; index++)); do
      [[ -z "$names" ]] || names+=", "
      names+="${MISSING_PREREQUISITE_NAMES[$index]}"
      [[ -z "$actions" ]] || actions+=" "
      actions+="${MISSING_PREREQUISITE_GUIDANCE[$index]}"
    done
    if [[ -n "$names" ]]; then
      echo "Missing prerequisites: $names. $actions Then run install.sh again; no state, release, configuration, or service changes were made." >&2
    else
      echo "Prerequisite check failed for unsupported host OS $OS_NAME; no state, release, configuration, or service changes were made." >&2
    fi
    return 1
  fi
}

# This deliberately runs before the cleanup trap, directory creation, state
# migration, release staging, or service changes.
preflight_prerequisites || exit 1

SERVICE_CONFIG_BACKUP=""
UV_INSTALLER=""
ACTIVE_STAGE_PID=""
ACTIVE_STAGE_PGID=""
INSTALL_LOCK_DIR="$INSTALL_ROOT/.install-lock"
INSTALL_LOCK_HELD="false"

signal_active_stage() {
  local signal_name="$1"
  if [[ -n "$ACTIVE_STAGE_PGID" ]] && kill "-$signal_name" -- "-$ACTIVE_STAGE_PGID" >/dev/null 2>&1; then
    return 0
  fi
  [[ -z "$ACTIVE_STAGE_PID" ]] || kill "-$signal_name" "$ACTIVE_STAGE_PID" >/dev/null 2>&1 || true
}

stop_active_stage() {
  [[ -n "$ACTIVE_STAGE_PID" ]] || return 0
  signal_active_stage TERM
  local attempt
  for attempt in $(seq 1 20); do
    kill -0 "$ACTIVE_STAGE_PID" >/dev/null 2>&1 || break
    sleep 0.05
  done
  if kill -0 "$ACTIVE_STAGE_PID" >/dev/null 2>&1; then
    signal_active_stage KILL
  fi
  wait "$ACTIVE_STAGE_PID" 2>/dev/null || true
  ACTIVE_STAGE_PID=""
  ACTIVE_STAGE_PGID=""
}

release_install_lock() {
  [[ "$INSTALL_LOCK_HELD" == "true" ]] || return 0
  if [[ "$(cat "$INSTALL_LOCK_DIR/pid" 2>/dev/null || true)" == "$$" ]]; then
    rm -rf "$INSTALL_LOCK_DIR"
  fi
  INSTALL_LOCK_HELD="false"
}

cleanup() {
  stop_active_stage
  [[ -z "$UV_INSTALLER" ]] || rm -f "$UV_INSTALLER"
  rm -rf "$STAGE_DIR"
  [[ -z "$SERVICE_CONFIG_BACKUP" ]] || rm -f "$SERVICE_CONFIG_BACKUP"
  release_install_lock
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

acquire_install_lock() {
  mkdir -p "$INSTALL_ROOT"
  if ! mkdir "$INSTALL_LOCK_DIR" 2>/dev/null; then
    local owner_pid=""
    owner_pid="$(cat "$INSTALL_LOCK_DIR/pid" 2>/dev/null || true)"
    if [[ "$owner_pid" =~ ^[0-9]+$ ]] && kill -0 "$owner_pid" >/dev/null 2>&1; then
      echo "Another AgentsServer installation is already running (PID $owner_pid)." >&2
      echo "  Wait for it to finish, or cancel it from AgentsDock before retrying." >&2
      return 1
    fi
    local stale_lock="$INSTALL_ROOT/.install-lock-stale-$$"
    if ! mv "$INSTALL_LOCK_DIR" "$stale_lock" 2>/dev/null; then
      echo "Another AgentsServer installation started while setup was retrying." >&2
      echo "  Wait for it to finish, then run install.sh again." >&2
      return 1
    fi
    rm -rf "$stale_lock"
    if ! mkdir "$INSTALL_LOCK_DIR" 2>/dev/null; then
      echo "Another AgentsServer installation is already running." >&2
      echo "  Wait for it to finish, then run install.sh again." >&2
      return 1
    fi
  fi
  printf '%s\n' "$$" > "$INSTALL_LOCK_DIR/pid"
  chmod 700 "$INSTALL_LOCK_DIR"
  INSTALL_LOCK_HELD="true"
}

acquire_install_lock || exit 1

run_timed_stage() {
  local label="$1"
  local timeout_seconds="$2"
  local guidance="$3"
  shift 3
  local started_at="$SECONDS"
  local next_heartbeat=$((SECONDS + INSTALL_HEARTBEAT_SECONDS))
  local elapsed=0
  local status=0

  echo "      $label (timeout: ${timeout_seconds}s)"
  # A separate process group lets timeout/cancellation terminate workers
  # spawned by uv or by the uv bootstrap script, not only their parent shell.
  set -m
  "$@" &
  ACTIVE_STAGE_PID="$!"
  ACTIVE_STAGE_PGID="$ACTIVE_STAGE_PID"
  set +m
  while kill -0 "$ACTIVE_STAGE_PID" >/dev/null 2>&1; do
    elapsed=$((SECONDS - started_at))
    if ((SECONDS >= next_heartbeat)); then
      echo "      Still working on $label (${elapsed}s elapsed)"
      next_heartbeat=$((SECONDS + INSTALL_HEARTBEAT_SECONDS))
    fi
    if ((elapsed >= timeout_seconds)); then
      echo "$label timed out after ${timeout_seconds}s." >&2
      echo "  $guidance" >&2
      stop_active_stage
      return 124
    fi
    sleep 1
  done

  if wait "$ACTIVE_STAGE_PID"; then
    ACTIVE_STAGE_PID=""
    ACTIVE_STAGE_PGID=""
    return 0
  else
    status=$?
  fi
  ACTIVE_STAGE_PID=""
  ACTIVE_STAGE_PGID=""
  echo "$label failed with exit code $status." >&2
  echo "  $guidance" >&2
  return "$status"
}

download_uv_installer() {
  local source_url="$1"
  local destination="$2"
  local attempts=$((UV_DOWNLOAD_RETRIES + 1))
  if command -v curl >/dev/null 2>&1 && curl --version >/dev/null 2>&1; then
    if curl \
      --fail \
      --location \
      --silent \
      --show-error \
      --connect-timeout "$UV_DOWNLOAD_CONNECT_TIMEOUT_SECONDS" \
      --max-time "$UV_DOWNLOAD_TIMEOUT_SECONDS" \
      --retry "$UV_DOWNLOAD_RETRIES" \
      --retry-delay 1 \
      --retry-connrefused \
      --output "$destination" \
      "$source_url"; then
      return 0
    fi
  elif command -v wget >/dev/null 2>&1 && wget --version >/dev/null 2>&1; then
    if wget \
      --timeout="$UV_DOWNLOAD_TIMEOUT_SECONDS" \
      --tries="$attempts" \
      --waitretry=1 \
      --output-document="$destination" \
      "$source_url"; then
      return 0
    fi
  fi
  echo "Could not download uv $UV_VERSION after up to $attempts attempts." >&2
  echo "  Check DNS, proxy, firewall, and outbound HTTPS access to astral.sh, then run install.sh again." >&2
  return 1
}

sync_release_dependencies() (
  # Managed updates may be launched from a long-lived tmux server. Never let
  # project/virtual-environment selectors inherited by that server redirect
  # this release sync into an unrelated workspace.
  unset \
    CONDA_PREFIX \
    PYTHONHOME \
    PYTHONPATH \
    UV_CONFIG_FILE \
    UV_NO_PROJECT \
    UV_PROJECT \
    UV_PROJECT_ENVIRONMENT \
    UV_PYTHON \
    UV_WORKING_DIR \
    VIRTUAL_ENV
  export UV_PROJECT_ENVIRONMENT="$STAGE_DIR/.venv"
  uv sync --project "$STAGE_DIR" --python '>=3.10' --no-dev --frozen
)

migrate_legacy_state() {
  [[ "$STATE_ROOT" == "$HOME/.agentsdock" ]] || return 0
  if [[ -L "$LEGACY_STATE_ROOT" ]]; then
    return 0
  fi
  if [[ -e "$LEGACY_STATE_ROOT" && ! -e "$STATE_ROOT" ]]; then
    echo "      Migrating existing AgentsDock history to $STATE_ROOT"
    mv "$LEGACY_STATE_ROOT" "$STATE_ROOT"
    ln -s "$STATE_ROOT" "$LEGACY_STATE_ROOT"
  elif [[ -e "$LEGACY_STATE_ROOT" && -e "$STATE_ROOT" ]]; then
    echo "Both $LEGACY_STATE_ROOT and $STATE_ROOT exist; refusing to guess which history is canonical." >&2
    exit 1
  elif [[ -d "$STATE_ROOT" && ! -e "$LEGACY_STATE_ROOT" ]]; then
    ln -s "$STATE_ROOT" "$LEGACY_STATE_ROOT"
  fi
}

echo "[1/7] Preparing the versioned AgentsServer runtime"
migrate_legacy_state
mkdir -p "$RELEASES_ROOT" "$CONFIG_ROOT" "$STATE_ROOT" "$STATE_ROOT/admin"
chmod 700 "$CONFIG_ROOT" "$STATE_ROOT" "$STATE_ROOT/admin"

if ! command -v uv >/dev/null 2>&1; then
  echo "      Installing uv $UV_VERSION for the current user"
  UV_INSTALLER="$(mktemp "${TMPDIR:-/tmp}/agents-server-uv.XXXXXX")"
  if ! download_uv_installer "https://astral.sh/uv/$UV_VERSION/install.sh" "$UV_INSTALLER"; then
    exit 1
  fi
  if run_timed_stage \
    "uv installation" \
    "$UV_INSTALL_TIMEOUT_SECONDS" \
    "Install uv manually from https://docs.astral.sh/uv/getting-started/installation/, then run install.sh again." \
    sh "$UV_INSTALLER"; then
    :
  else
    stage_status=$?
    exit "$stage_status"
  fi
  rm -f "$UV_INSTALLER"
  UV_INSTALLER=""
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { echo "uv is not available on PATH." >&2; exit 1; }

rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"
for name in "${RELEASE_FILES[@]}"; do
  install -m 644 "$SOURCE_DIR/$name" "$STAGE_DIR/$name"
done
chmod 755 "$STAGE_DIR/agent_server.py" "$STAGE_DIR/agentsdock_jobs.py" "$STAGE_DIR/agentsdock_publish.py" "$STAGE_DIR/install.sh" "$STAGE_DIR/uninstall.sh" "$STAGE_DIR/update_runner.py"

echo "[2/7] Resolving the release dependencies with uv"
if run_timed_stage \
  "dependency resolution" \
  "$DEPENDENCY_SYNC_TIMEOUT_SECONDS" \
  "Review the uv output above. Verify disk space and outbound HTTPS access, then run install.sh again; the active release was not changed." \
  sync_release_dependencies; then
  :
else
  stage_status=$?
  exit "$stage_status"
fi
if [[ ! -d "$STAGE_DIR/.venv" || -L "$STAGE_DIR/.venv" || ! -x "$STAGE_DIR/.venv/bin/python" ]]; then
  echo "Dependency resolution did not create the isolated release runtime at $STAGE_DIR/.venv." >&2
  echo "  The active release was not changed. Review the uv output above, then run install.sh again." >&2
  exit 1
fi
"$STAGE_DIR/.venv/bin/python" -c 'import websockets' >/dev/null
"$STAGE_DIR/.venv/bin/python" -c 'import croniter, dateutil; from zoneinfo import ZoneInfo; ZoneInfo("America/Los_Angeles")' >/dev/null
"$STAGE_DIR/.venv/bin/python" -m py_compile \
  "$STAGE_DIR/agent_server.py" \
  "$STAGE_DIR/agentsdock_jobs.py" \
  "$STAGE_DIR/agentsdock_publish.py" \
  "$STAGE_DIR/codex_app_server.py" \
  "$STAGE_DIR/update_runner.py"

TOKEN=""
for candidate in "$ENV_FILE" "$LEGACY_ENV_FILE"; do
  [[ -n "$candidate" && -f "$candidate" ]] || continue
  TOKEN="$(read_env_value "$candidate" AGENTSDOCK_AGENT_TOKEN)"
  [[ -n "$TOKEN" ]] || TOKEN="$(read_env_value "$candidate" ZENITHDOCK_AGENT_TOKEN)"
  [[ -n "$TOKEN" ]] || TOKEN="$(read_env_value "$candidate" ZENITHBOT_AGENT_TOKEN)"
  [[ -z "$TOKEN" ]] || break
done
if [[ -z "$TOKEN" ]]; then
  if [[ -f "$LEGACY_SERVICE_FILE" ]]; then
    TOKEN="$(grep -E '^Environment="?ZENITHDOCK_AGENT_TOKEN=' "$LEGACY_SERVICE_FILE" | tail -n 1 || true)"
    TOKEN="${TOKEN#*ZENITHDOCK_AGENT_TOKEN=}"
    TOKEN="${TOKEN%\"}"
  fi
fi
generate_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    "$STAGE_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))'
  fi
}
[[ "$TOKEN" =~ ^[A-Za-z0-9_-]{32,}$ ]] || TOKEN="$(generate_token)"

PRESERVE_SOURCE=""
[[ ! -f "$LEGACY_ENV_FILE" ]] || PRESERVE_SOURCE="$LEGACY_ENV_FILE"
[[ ! -f "$ENV_FILE" ]] || PRESERVE_SOURCE="$ENV_FILE"

write_runtime_env() {
  local env_temp="$CONFIG_ROOT/.env.$$"
  if [[ -n "$PRESERVE_SOURCE" ]]; then
    grep -Ev '^(AGENTSDOCK_(STATE_DIR|AGENT_CWD|AGENT_BIND|AGENT_PORT|AGENT_TOKEN)|AGENTS_SERVER_(STATE_DIR|INSTALL_DIR)|ZENITHBOT_AGENT_(DIR|CWD|BIND|PORT|TOKEN)|ZENITHDOCK_AGENT_TOKEN|PATH)=' \
      "$PRESERVE_SOURCE" > "$env_temp" || true
  else
    : > "$env_temp"
  fi
  cat >> "$env_temp" <<EOF
AGENTSDOCK_STATE_DIR=$STATE_ROOT
AGENTSDOCK_AGENT_CWD=$HOME
AGENTSDOCK_AGENT_BIND=$BIND_ADDRESS
AGENTSDOCK_AGENT_PORT=$PORT
AGENTSDOCK_AGENT_TOKEN=$TOKEN
AGENTS_SERVER_INSTALL_DIR=$INSTALL_ROOT
PATH=$SERVER_PATH
EOF
  chmod 600 "$env_temp"
  mv "$env_temp" "$ENV_FILE"
  PRESERVE_SOURCE="$ENV_FILE"
}

write_runtime_env

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

wait_for_launch_agent_removal() {
  local service_target="$1"
  local attempt
  for ((attempt = 1; attempt <= LAUNCHCTL_STOP_ATTEMPTS; attempt++)); do
    if ! launchctl print "$service_target" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$LAUNCHCTL_STOP_DELAY"
  done
  echo "Timed out waiting for $LABEL to stop." >&2
  return 1
}

transient_launchctl_bootstrap_error() {
  local status="$1"
  local output="$2"
  # launchctl collapses launchd's EALREADY into status 5 and this generic EIO text.
  [[ "$output" == *"Operation already in progress"* ]] || \
    { ((status == 5)) && [[ "$output" == *"Bootstrap failed: 5: Input/output error"* ]]; }
}

bootstrap_launch_agent() {
  local domain="$1"
  local service_target="$2"
  local allow_transient_retry="$3"
  local attempt=1
  local output=""
  local status=0

  while ((attempt <= LAUNCHCTL_BOOTSTRAP_ATTEMPTS)); do
    if output="$(launchctl bootstrap "$domain" "$PLIST" 2>&1)"; then
      [[ -z "$output" ]] || printf '%s\n' "$output"
      return 0
    else
      status=$?
    fi
    if [[ "$allow_transient_retry" != "true" ]] || \
      ! transient_launchctl_bootstrap_error "$status" "$output" || \
      ((attempt == LAUNCHCTL_BOOTSTRAP_ATTEMPTS)); then
      [[ -z "$output" ]] || printf '%s\n' "$output" >&2
      return "$status"
    fi
    wait_for_launch_agent_removal "$service_target" || return 1
    sleep "$LAUNCHCTL_STOP_DELAY"
    ((attempt += 1))
  done
  return "$status"
}

restart_service() {
  if [[ "$OS_NAME" == "Linux" ]]; then
    systemctl --user disable --now "$LEGACY_SERVICE_NAME.service" >/dev/null 2>&1 || true
    systemctl --user daemon-reload || return
    systemctl --user enable "$SERVICE_NAME.service" >/dev/null || return
    systemctl --user restart "$SERVICE_NAME.service"
  else
    local domain="gui/$(id -u)"
    local service_target="$domain/$LABEL"
    local had_service="false"
    local output=""
    local status=0
    if launchctl print "$service_target" >/dev/null 2>&1; then
      had_service="true"
      # bootout acknowledges the request before launchd has removed the job.
      if output="$(launchctl bootout "$service_target" 2>&1)"; then
        [[ -z "$output" ]] || printf '%s\n' "$output"
      else
        status=$?
        if launchctl print "$service_target" >/dev/null 2>&1; then
          [[ -z "$output" ]] || printf '%s\n' "$output" >&2
          return "$status"
        fi
      fi
      wait_for_launch_agent_removal "$service_target" || return 1
    fi
    bootstrap_launch_agent "$domain" "$service_target" "$had_service"
  fi
}

restore_previous_release() {
  local restore_service_config="${1:-false}"
  [[ -n "$OLD_TARGET" && -e "$OLD_TARGET" ]] || return 1
  ln -sfn "$OLD_TARGET" "$CURRENT_LINK" || return
  if [[ "$restore_service_config" == "true" && "$OS_NAME" == "Darwin" && -n "$SERVICE_CONFIG_BACKUP" ]]; then
    cp -p "$SERVICE_CONFIG_BACKUP" "$PLIST" || return
  fi
  if restart_service; then
    return 0
  fi
  echo "The previous release link was restored, but its service could not be restarted." >&2
  return 1
}

port_has_listener() {
  local port="$1"
  # Keep the probe in a subshell so both the socket descriptor and diagnostic
  # redirection are scoped to this check. A bare `exec ... 2>/dev/null` here
  # would permanently silence the installer's stderr after the first probe.
  (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null || return 1
  return 0
}

describe_port_listener() {
  local port="$1"
  command -v lsof >/dev/null 2>&1 || return 0
  lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print "  " $1 " (pid " $2 ")"}' | sort -u
}

write_service_files() {
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
# Keep the coordinator alive long enough to record/recover agent failures
# when systemd-oomd must choose among pressured user services. Provider
# subprocesses remain in this cgroup and are stopped with the service.
ManagedOOMPreference=avoid

[Install]
WantedBy=default.target
EOF
    SERVICE_KIND="systemd-user"
  elif [[ "$OS_NAME" == "Darwin" ]]; then
    LABEL="com.agentsdock.server"
    LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
    PLIST="$LAUNCH_AGENTS/$LABEL.plist"
    mkdir -p "$LAUNCH_AGENTS" "$HOME/Library/Logs/AgentsServer"
    if [[ -f "$PLIST" && -z "$SERVICE_CONFIG_BACKUP" ]]; then
      SERVICE_CONFIG_BACKUP="$(mktemp "${TMPDIR:-/tmp}/agents-server-launch-agent.XXXXXX")"
      cp -p "$PLIST" "$SERVICE_CONFIG_BACKUP"
    fi
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
    <key>AGENTSDOCK_STATE_DIR</key><string>$STATE_ROOT</string>
    <key>AGENTSDOCK_AGENT_CWD</key><string>$HOME</string>
    <key>AGENTSDOCK_AGENT_BIND</key><string>$BIND_ADDRESS</string>
    <key>AGENTSDOCK_AGENT_PORT</key><string>$PORT</string>
    <key>AGENTSDOCK_AGENT_TOKEN</key><string>$TOKEN</string>
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
}

HEALTH_CHECK_HEARTBEAT_ATTEMPTS=5

health_check_once() {
  local port="$1"
  if command -v curl >/dev/null 2>&1 && curl --version >/dev/null 2>&1; then
    if curl --fail --silent --show-error --connect-timeout 1 --max-time 2 \
      -H "Authorization: Bearer $TOKEN" \
      "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then
      return 0
    fi
  elif wget \
    --quiet \
    --timeout=2 \
    --tries=1 \
    --header="Authorization: Bearer $TOKEN" \
    --output-document=/dev/null \
    "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

wait_for_health() {
  local attempt
  for ((attempt = 1; attempt <= HEALTH_CHECK_ATTEMPTS; attempt++)); do
    if health_check_once "$PORT"; then
      return 0
    fi
    if ((attempt < HEALTH_CHECK_ATTEMPTS)) && ((attempt % HEALTH_CHECK_HEARTBEAT_ATTEMPTS == 0)); then
      echo "      Still waiting for health (${attempt}s elapsed, timeout ${HEALTH_CHECK_ATTEMPTS}s)"
    fi
    ((attempt == HEALTH_CHECK_ATTEMPTS)) || sleep 1
  done
  return 1
}

# A requested port held by an authenticated AgentsServer is the service being
# replaced and will be released by restart_service. A listener that is present
# before restart and rejects the preserved token is treated as a conflict.
ORIGINAL_PORT="$PORT"
PORT_AUTO_SELECTED="false"
port_fallback_attempt=0

while true; do
  # Select a fallback only for a listener that was present before this service
  # is restarted and does not authenticate with the preserved AgentsServer
  # token. A newly started but unhealthy AgentsServer must be rolled back, not
  # mistaken for a port conflict and silently moved to another port.
  if [[ "$PORT_FALLBACK" == "true" ]] && port_has_listener "$PORT" && ! health_check_once "$PORT"; then
    echo "Port $PORT is already held by another process:" >&2
    describe_port_listener "$PORT" >&2
    port_fallback_attempt=$((port_fallback_attempt + 1))
    candidate=$((ORIGINAL_PORT + port_fallback_attempt))
    while ((port_fallback_attempt <= PORT_FALLBACK_ATTEMPTS)) && ((candidate <= 65535)) && port_has_listener "$candidate"; do
      port_fallback_attempt=$((port_fallback_attempt + 1))
      candidate=$((ORIGINAL_PORT + port_fallback_attempt))
    done
    if ((port_fallback_attempt <= PORT_FALLBACK_ATTEMPTS)) && ((candidate <= 65535)); then
      PORT="$candidate"
      PORT_AUTO_SELECTED="true"
      write_runtime_env
      echo "Selecting port $PORT instead (attempt $port_fallback_attempt/$PORT_FALLBACK_ATTEMPTS)." >&2
      continue
    fi
    echo "Ran out of nearby free ports to try." >&2
  fi

  echo "[4/7] Installing the user service (port $PORT)"
  write_service_files
  if ! restart_service; then
    echo "AgentsServer $RELEASE_VERSION could not start; restoring the previous service when possible." >&2
    if restore_previous_release true; then
      echo "The previous release and service were restored." >&2
    fi
    exit 1
  fi

  echo "[5/7] Waiting for authenticated health"
  if wait_for_health; then
    break
  fi

  echo "AgentsServer $RELEASE_VERSION did not become healthy; rolling back." >&2
  PORT="$ORIGINAL_PORT"
  write_service_files
  if restore_previous_release false; then
    wait_for_health || true
    echo "The previous release was restored." >&2
  fi
  if [[ "$OS_NAME" == "Linux" ]]; then
    if [[ -z "$OLD_TARGET" && -f "$HOME/.config/systemd/user/$LEGACY_SERVICE_NAME.service" ]]; then
      systemctl --user enable --now "$LEGACY_SERVICE_NAME.service" >/dev/null 2>&1 || true
    fi
    systemctl --user status "$SERVICE_NAME.service" --no-pager -l >&2 || true
  fi
  exit 1
done

echo "[6/7] Checking optional agent runtimes"
check_runtime_cli() {
  local name="$1"
  local install_hint="$2"
  if command -v "$name" >/dev/null 2>&1; then
    echo "      $CHECK_MARK $name found"
    return 0
  fi
  echo "      $CROSS_MARK $name not found - $install_hint"
  return 1
}
CLAUDE_READY="false"
CODEX_READY="false"
check_runtime_cli claude "npm install -g @anthropic-ai/claude-code, then run: claude" && CLAUDE_READY="true"
check_runtime_cli codex "npm install -g @openai/codex, then run: codex login" && CODEX_READY="true"
if [[ "$CLAUDE_READY" == "false" && "$CODEX_READY" == "false" ]]; then
  echo "      Sign in to at least one before starting a chat."
fi

TAILSCALE_IP=""
if command -v tailscale >/dev/null 2>&1; then
  TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
fi
SERVER_URL="http://127.0.0.1:$PORT"
[[ -z "$TAILSCALE_IP" ]] || SERVER_URL="http://$TAILSCALE_IP:$PORT"

echo "[7/7] AgentsServer $RELEASE_VERSION is ready"
echo
echo "  ${COLOR_BOLD}Server URL${COLOR_RESET}    $SERVER_URL"
echo
echo "  ${COLOR_BOLD}Next steps${COLOR_RESET}"
if [[ "$PORT_AUTO_SELECTED" == "true" ]]; then
  echo "  $DOT_MARK port $ORIGINAL_PORT was already in use, installed on $PORT instead (--port pins an exact port unless --allow-port-fallback is supplied)"
fi
if [[ "$CLAUDE_READY" == "false" && "$CODEX_READY" == "false" ]]; then
  echo "  $CROSS_MARK install and sign in to Claude Code or Codex (see [6/7] above) before starting a chat"
fi
if [[ -n "$TMUX_WARNING" ]]; then
  echo "  $CROSS_MARK tmux unavailable: persistent terminal, pane inspection, and in-app updates won't work - $TMUX_WARNING"
else
  echo "  $CHECK_MARK tmux available"
fi
if [[ -n "$TAILSCALE_IP" ]]; then
  echo "  $CHECK_MARK reachable via Tailscale at $TAILSCALE_IP"
else
  echo "  $DOT_MARK optional: install and connect Tailscale to reach this server from another device or WiFi network: https://tailscale.com/download"
fi
echo
printf 'AGENTSDOCK_SETUP_RESULT={"server_url":"%s","access_token":"%s","service":"%s","tailscale_ip":"%s","server_version":"%s"}\n' \
  "$SERVER_URL" "$TOKEN" "$SERVICE_KIND" "$TAILSCALE_IP" "$RELEASE_VERSION"
