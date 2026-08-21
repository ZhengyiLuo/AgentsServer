#!/usr/bin/env bash
set -euo pipefail

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
OS_NAME="$(uname -s)"

ASSUME_YES="false"
PURGE_STATE="false"

normalize_managed_path() {
  local label="$1"
  local candidate="$2"
  local parent leaf physical_parent normalized home_physical

  if [[ -z "$candidate" || "$candidate" != /* || "$candidate" == *$'\n'* || "$candidate" == *$'\r'* ]]; then
    echo "$label must be a non-empty absolute path without control characters." >&2
    return 1
  fi
  while [[ "$candidate" != "/" && "$candidate" == */ ]]; do
    candidate="${candidate%/}"
  done
  case "$candidate/" in
    *"/../"*|*"/./"*|*"//"*)
      echo "$label must not contain '.', '..', or repeated-slash path components: $candidate" >&2
      return 1
      ;;
  esac

  # Resolve existing parent directories without following the final component.
  # Removing a final symlink must remove only that link, while an intermediate
  # symlink still needs to be accounted for by the safety checks below.
  parent="$(dirname -- "$candidate")"
  leaf="$(basename -- "$candidate")"
  while [[ ! -d "$parent" && "$parent" != "/" ]]; do
    leaf="$(basename -- "$parent")/$leaf"
    parent="$(dirname -- "$parent")"
  done
  physical_parent="$(cd -P -- "$parent" 2>/dev/null && pwd -P)" || {
    echo "Could not resolve the parent of $label: $candidate" >&2
    return 1
  }
  if [[ "$physical_parent" == "/" ]]; then
    normalized="/$leaf"
  else
    normalized="$physical_parent/$leaf"
  fi
  home_physical="$(cd -P -- "$HOME" 2>/dev/null && pwd -P)" || {
    echo "Could not resolve HOME safely: $HOME" >&2
    return 1
  }

  case "$normalized" in
    "/"|"/Applications"|"/Library"|"/System"|"/Users"|"/Volumes"|"/bin"|"/etc"|"/home"|"/opt"|"/private"|"/sbin"|"/tmp"|"/usr"|"/var"|\
    "$home_physical"|"$home_physical/.config"|"$home_physical/.local"|"$home_physical/.local/share"|"$home_physical/Library"|"$home_physical/Library/LaunchAgents")
      echo "Refusing unsafe $label target: $normalized" >&2
      return 1
      ;;
  esac
  printf '%s\n' "$normalized"
}

paths_overlap() {
  local first="$1"
  local second="$2"
  [[ "$first" == "$second" || "$first" == "$second/"* || "$second" == "$first/"* ]]
}

INSTALL_ROOT="$(normalize_managed_path AGENTS_SERVER_INSTALL_DIR "$INSTALL_ROOT")" || exit 2
CONFIG_ROOT="$(normalize_managed_path AGENTS_SERVER_CONFIG_DIR "$CONFIG_ROOT")" || exit 2
STATE_ROOT="$(normalize_managed_path AGENTSDOCK_STATE_DIR "$STATE_ROOT")" || exit 2
for managed_root in "$INSTALL_ROOT" "$CONFIG_ROOT" "$STATE_ROOT"; do
  if [[ -L "$managed_root" ]]; then
    echo "Refusing symbolic-link managed root: $managed_root" >&2
    exit 2
  fi
done
if paths_overlap "$INSTALL_ROOT" "$CONFIG_ROOT" || \
  paths_overlap "$INSTALL_ROOT" "$STATE_ROOT" || \
  paths_overlap "$CONFIG_ROOT" "$STATE_ROOT"; then
  echo "Refusing overlapping install, configuration, and state roots; each must be a separate directory." >&2
  exit 2
fi

usage() {
  cat <<USAGE
Usage: ./uninstall.sh [--yes] [--purge-state]

Stops and removes the AgentsServer user service, versioned release runtime,
and generated configuration (including the access token). Chat history, jobs,
files, and terminals under $STATE_ROOT are preserved by default so a later
./install.sh picks ordinary AgentsServer state back up. Preserved Team Hub state
is not auto-reactivated in this beta; it requires signed managed recovery or
support-assisted restoration.

  --yes           Do not prompt before removing the service, releases, and
                   configuration.
  --purge-state   Also delete $STATE_ROOT (chat history, jobs, files, and
                   tokens). This cannot be undone and always requires typing
                   the exact state path interactively; --yes never bypasses it.
USAGE
}

while (($#)); do
  case "$1" in
    --yes) ASSUME_YES="true"; shift ;;
    --purge-state) PURGE_STATE="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

INSTALL_LOCK_DIR="$INSTALL_ROOT/.install-lock"
UNINSTALL_LOCK_HELD="false"
INSTALL_ROOT_CREATED_FOR_LOCK="false"

release_operation_lock() {
  [[ "$UNINSTALL_LOCK_HELD" == "true" ]] || return 0
  if [[ "$(cat "$INSTALL_LOCK_DIR/pid" 2>/dev/null || true)" == "$$" ]]; then
    rm -rf "$INSTALL_LOCK_DIR"
  fi
  UNINSTALL_LOCK_HELD="false"
  if [[ "$INSTALL_ROOT_CREATED_FOR_LOCK" == "true" ]]; then
    rmdir "$INSTALL_ROOT" 2>/dev/null || true
  fi
}

acquire_operation_lock() {
  if [[ ! -d "$INSTALL_ROOT" ]]; then
    mkdir -p "$INSTALL_ROOT"
    INSTALL_ROOT_CREATED_FOR_LOCK="true"
  fi
  if ! mkdir "$INSTALL_LOCK_DIR" 2>/dev/null; then
    local owner_pid=""
    owner_pid="$(cat "$INSTALL_LOCK_DIR/pid" 2>/dev/null || true)"
    if [[ "$owner_pid" =~ ^[0-9]+$ ]] && kill -0 "$owner_pid" >/dev/null 2>&1; then
      echo "Refusing to uninstall while an AgentsServer install/update is active (PID $owner_pid)." >&2
      return 1
    fi
    local stale_lock="$INSTALL_ROOT/.uninstall-stale-lock-$$"
    if ! mv "$INSTALL_LOCK_DIR" "$stale_lock" 2>/dev/null; then
      echo "An AgentsServer install/update started while uninstall was checking the operation lock." >&2
      return 1
    fi
    rm -rf "$stale_lock"
    if ! mkdir "$INSTALL_LOCK_DIR" 2>/dev/null; then
      echo "An AgentsServer install/update is already running." >&2
      return 1
    fi
  fi
  printf '%s\n' "$$" > "$INSTALL_LOCK_DIR/pid"
  chmod 700 "$INSTALL_LOCK_DIR"
  UNINSTALL_LOCK_HELD="true"
}

acquire_operation_lock || exit 1
trap release_operation_lock EXIT
trap 'exit 130' HUP INT TERM

confirm() {
  local prompt="$1"
  [[ "$ASSUME_YES" != "true" ]] || return 0
  if [[ ! -t 0 ]]; then
    echo "Refusing to $prompt without --yes on a non-interactive run." >&2
    return 1
  fi
  local reply=""
  read -r -p "$prompt [y/N] " reply || return 1
  case "$reply" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

confirm_state_purge() {
  local reply=""
  if [[ ! -t 0 ]]; then
    echo "Refusing to purge state without an interactive terminal; --yes never bypasses the state guard." >&2
    return 1
  fi
  echo "Permanent deletion requested for chat history, jobs, files, and tokens."
  read -r -p "Type the exact state path to confirm ($STATE_ROOT): " reply || return 1
  if [[ "$reply" != "$STATE_ROOT" ]]; then
    echo "State path did not match; nothing was changed." >&2
    return 1
  fi
}

if [[ "$PURGE_STATE" == "true" ]] && ! confirm_state_purge; then
  exit 1
fi

echo "This will stop and remove the AgentsServer service, release runtime at"
echo "$INSTALL_ROOT, and configuration at $CONFIG_ROOT (including the access token)."
if confirm "Proceed?"; then
  :
else
  echo "Aborted; nothing was changed." >&2
  exit 1
fi

case "$OS_NAME" in
  Darwin)
    LABEL="com.agentsdock.server"
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    SERVICE_TARGET="gui/$(id -u)/$LABEL"
    if launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
      echo "Stopping $LABEL"
      if ! launchctl bootout "$SERVICE_TARGET" >/dev/null 2>&1 && \
        launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
        echo "Could not stop $LABEL; no files were removed." >&2
        exit 1
      fi
      stopped="false"
      for ((_attempt = 1; _attempt <= 50; _attempt++)); do
        if ! launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
          stopped="true"
          break
        fi
        sleep 0.1
      done
      if [[ "$stopped" != "true" ]]; then
        echo "$LABEL did not stop; no files were removed." >&2
        exit 1
      fi
    fi
    if [[ -f "$PLIST" ]]; then
      echo "Removing $PLIST"
      rm -f "$PLIST"
    fi
    LOG_DIR="$HOME/Library/Logs/AgentsServer"
    if [[ -d "$LOG_DIR" ]]; then
      echo "Removing $LOG_DIR"
      rm -rf "$LOG_DIR"
    fi
    ;;
  Linux)
    SERVICE_FILE="$HOME/.config/systemd/user/$SERVICE_NAME.service"
    if ! systemctl --user show-environment >/dev/null 2>&1; then
      echo "The systemd user service manager is unavailable; no files were removed." >&2
      exit 1
    fi
    if [[ -f "$SERVICE_FILE" ]] || systemctl --user is-active --quiet "$SERVICE_NAME.service"; then
      echo "Stopping $SERVICE_NAME.service"
      if ! systemctl --user disable --now "$SERVICE_NAME.service" >/dev/null 2>&1 && \
        systemctl --user is-active --quiet "$SERVICE_NAME.service"; then
        echo "Could not stop $SERVICE_NAME.service; no files were removed." >&2
        exit 1
      fi
      stopped="false"
      for ((_attempt = 1; _attempt <= 50; _attempt++)); do
        if ! systemctl --user is-active --quiet "$SERVICE_NAME.service"; then
          stopped="true"
          break
        fi
        sleep 0.1
      done
      if [[ "$stopped" != "true" ]]; then
        echo "$SERVICE_NAME.service did not stop; no files were removed." >&2
        exit 1
      fi
    fi
    if [[ -f "$SERVICE_FILE" ]]; then
      echo "Removing $SERVICE_FILE"
      rm -f "$SERVICE_FILE"
      systemctl --user daemon-reload || true
    fi
    ;;
  *)
    echo "Unsupported host OS: $OS_NAME; no files were removed." >&2
    exit 1
    ;;
esac

if [[ -d "$INSTALL_ROOT" ]]; then
  echo "Removing release runtime at $INSTALL_ROOT"
  shopt -s dotglob nullglob
  for install_entry in "$INSTALL_ROOT"/*; do
    [[ "$install_entry" == "$INSTALL_LOCK_DIR" ]] || rm -rf -- "$install_entry"
  done
  shopt -u dotglob nullglob
fi

if [[ -d "$CONFIG_ROOT" ]]; then
  echo "Removing configuration at $CONFIG_ROOT"
  rm -rf "$CONFIG_ROOT"
fi

if [[ "$PURGE_STATE" == "true" ]]; then
  if [[ -e "$STATE_ROOT" || -L "$LEGACY_STATE_ROOT" ]]; then
    [[ ! -L "$LEGACY_STATE_ROOT" ]] || rm -f "$LEGACY_STATE_ROOT"
    [[ ! -e "$STATE_ROOT" ]] || rm -rf "$STATE_ROOT"
    echo "Deleted $STATE_ROOT"
  fi
elif [[ -e "$STATE_ROOT" ]]; then
  echo "Preserved chat history, jobs, files, and tokens at $STATE_ROOT."
  echo "Re-running ./install.sh will pick ordinary AgentsServer state back up. Pass --purge-state to also delete it."
  if [[ -e "$STATE_ROOT/team-hub/team-hub.sqlite3" || -L "$STATE_ROOT/team-hub/team-hub.sqlite3" ]]; then
    echo "Preserved Team Hub state is not auto-reactivated in this beta; use a signed managed recovery or support-assisted restoration."
  fi
fi

# Keep the shared install/update lock alive until every requested removal has
# completed. Removing the install root while the lock still lives inside it
# would let a new installer race with configuration or state deletion.
release_operation_lock
if [[ -d "$INSTALL_ROOT" ]] && ! rmdir "$INSTALL_ROOT"; then
  echo "Could not remove the now-unlocked install directory at $INSTALL_ROOT." >&2
  exit 1
fi

echo "AgentsServer service, release runtime, and configuration removed."
echo "Note: any persistent chat terminals (tmux sessions named zd_*) keep running"
echo "independently and are not touched by this script. List them with: tmux ls"
