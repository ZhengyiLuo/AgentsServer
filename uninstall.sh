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

usage() {
  cat <<USAGE
Usage: ./uninstall.sh [--yes] [--purge-state]

Stops and removes the AgentsServer user service, versioned release runtime,
and generated configuration (including the access token). Chat history, jobs,
files, and terminals under $STATE_ROOT are preserved by default so a later
./install.sh picks them back up.

  --yes           Do not prompt before removing the service, releases, and
                   configuration.
  --purge-state   Also delete $STATE_ROOT (chat history, jobs, files, and
                   tokens). This cannot be undone. Prompted for separately
                   even with --yes, unless both flags are given together.
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
      launchctl bootout "$SERVICE_TARGET" >/dev/null 2>&1 || true
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
    if systemctl --user list-unit-files "$SERVICE_NAME.service" >/dev/null 2>&1; then
      echo "Stopping $SERVICE_NAME.service"
      systemctl --user disable --now "$SERVICE_NAME.service" >/dev/null 2>&1 || true
    fi
    if [[ -f "$SERVICE_FILE" ]]; then
      echo "Removing $SERVICE_FILE"
      rm -f "$SERVICE_FILE"
      systemctl --user daemon-reload || true
    fi
    ;;
  *)
    echo "Unsupported host OS: $OS_NAME; skipping service removal." >&2
    ;;
esac

if [[ -d "$INSTALL_ROOT" ]]; then
  echo "Removing release runtime at $INSTALL_ROOT"
  rm -rf "$INSTALL_ROOT"
fi

if [[ -d "$CONFIG_ROOT" ]]; then
  echo "Removing configuration at $CONFIG_ROOT"
  rm -rf "$CONFIG_ROOT"
fi

if [[ "$PURGE_STATE" == "true" ]]; then
  if [[ -e "$STATE_ROOT" || -L "$LEGACY_STATE_ROOT" ]]; then
    if confirm "Also permanently delete chat history, jobs, files, and tokens at $STATE_ROOT?"; then
      [[ ! -L "$LEGACY_STATE_ROOT" ]] || rm -f "$LEGACY_STATE_ROOT"
      [[ ! -e "$STATE_ROOT" ]] || rm -rf "$STATE_ROOT"
      echo "Deleted $STATE_ROOT"
    else
      echo "Kept $STATE_ROOT" >&2
    fi
  fi
elif [[ -e "$STATE_ROOT" ]]; then
  echo "Preserved chat history, jobs, files, and tokens at $STATE_ROOT."
  echo "Re-running ./install.sh will pick this state back up. Pass --purge-state to also delete it."
fi

echo "AgentsServer service, release runtime, and configuration removed."
echo "Note: any persistent chat terminals (tmux sessions named zd_*) keep running"
echo "independently and are not touched by this script. List them with: tmux ls"
