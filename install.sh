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
ROLLBACK_HEALTH_CHECK_MAX_ATTEMPTS=45
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
TEAM_HUB_MODE_OVERRIDE=""
TEAM_HUB_MODE="disabled"
TEAM_HUB_TRANSPORT_OVERRIDE=""
TEAM_HUB_TRANSPORT="loopback"
TEAM_HUB_URL_OVERRIDE=""
TEAM_HUB_URL=""
TEAM_HUB_DIRECT_IP_URL_OVERRIDE=""
TEAM_HUB_DIRECT_IP_URL=""
EXPECTED_SERVER_IDENTITY=""
EXPECTED_TEAM_HUB_ID=""
EXPECTED_TEAM_HUB_TRANSPORT=""
EXPECTED_TEAM_HUB_TRANSPORT_SET="false"
EXPECTED_TEAM_HUB_URL=""
EXPECTED_TEAM_HUB_URL_SET="false"
EXPECTED_TEAM_HUB_DIRECT_IP_URL=""
EXPECTED_TEAM_HUB_DIRECT_IP_URL_SET="false"
TEAM_HUB_SNAPSHOT=""
TEAM_HUB_DATA_DIR=""
TEAM_HUB_OPERATION_ID=""

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
Usage: ./install.sh [--port PORT] [--bind ADDRESS] [--release-version VERSION] [--team-hub-host|--no-team-hub-host] [--team-hub-tailscale-serve-url URL] [--team-hub-direct-ip-url URL] [--non-interactive] [--allow-port-fallback|--no-port-fallback]

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

--team-hub-host designates this server as the one Team Hub host. It defaults
to host-local access. For remote private-tailnet access, also pass the exact
Tailscale Serve URL ending in /api/team-hub.
--team-hub-tailscale-serve-url selects private Tailscale Serve HTTPS transport
for this host. It implies --team-hub-host and rejects Funnel-capable ports.
--team-hub-direct-ip-url adds an advanced, unencrypted raw IPv4 route on the
same AgentsServer origin. It implies --team-hub-host. IP shape is not identity
or Tailscale attestation; credentials and messages are plaintext on this route.
--no-team-hub-host stops Team Hub hosting while preserving its state. This beta
does not support direct reactivation of preserved Hub state; recovery requires
a signed managed operation. Without either option, an existing host/disabled
setting is preserved; new installs default to disabled.

--show-token prints the current access token for an already-installed
AgentsServer and exits immediately; it makes no other changes.
USAGE
}

SHOW_TOKEN="false"

while (($#)); do
  case "$1" in
    --port) PORT="${2:-}"; PORT_EXPLICIT="true"; shift 2 ;;
    --bind) BIND_ADDRESS="${2:-}"; shift 2 ;;
    --release-version) RELEASE_VERSION="${2:-}"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE="true"; shift ;;
    --allow-port-fallback) PORT_FALLBACK="true"; shift ;;
    --no-port-fallback) PORT_FALLBACK="false"; shift ;;
    --team-hub-host)
      if [[ "$TEAM_HUB_MODE_OVERRIDE" == "disabled" ]]; then
        echo "--team-hub-host and --no-team-hub-host cannot be combined." >&2
        exit 2
      fi
      TEAM_HUB_MODE_OVERRIDE="host"
      shift
      ;;
    --team-hub-tailscale-serve-url)
      if [[ "$TEAM_HUB_MODE_OVERRIDE" == "disabled" ]]; then
        echo "--team-hub-tailscale-serve-url and --no-team-hub-host cannot be combined." >&2
        exit 2
      fi
      if (($# < 2)) || [[ -z "${2:-}" || "${2:-}" == --* ]]; then
        echo "--team-hub-tailscale-serve-url requires a URL." >&2
        exit 2
      fi
      TEAM_HUB_URL_OVERRIDE="${2:-}"
      TEAM_HUB_TRANSPORT_OVERRIDE="tailscale_serve"
      TEAM_HUB_MODE_OVERRIDE="host"
      shift 2
      ;;
    --team-hub-direct-ip-url)
      if [[ "$TEAM_HUB_MODE_OVERRIDE" == "disabled" ]]; then
        echo "--team-hub-direct-ip-url and --no-team-hub-host cannot be combined." >&2
        exit 2
      fi
      if (($# < 2)) || [[ -z "${2:-}" || "${2:-}" == --* ]]; then
        echo "--team-hub-direct-ip-url requires a URL." >&2
        exit 2
      fi
      TEAM_HUB_DIRECT_IP_URL_OVERRIDE="${2:-}"
      TEAM_HUB_MODE_OVERRIDE="host"
      shift 2
      ;;
    --no-team-hub-host)
      if [[ "$TEAM_HUB_MODE_OVERRIDE" == "host" ]]; then
        echo "--team-hub-host and --no-team-hub-host cannot be combined." >&2
        exit 2
      fi
      TEAM_HUB_MODE_OVERRIDE="disabled"
      shift
      ;;
    # Internal, fail-closed continuity assertions passed only by the
    # authenticated managed updater.
    --expected-server-identity) EXPECTED_SERVER_IDENTITY="${2:-}"; shift 2 ;;
    --expected-team-hub-id) EXPECTED_TEAM_HUB_ID="${2:-}"; shift 2 ;;
    --expected-team-hub-transport)
      if (($# < 2)); then
        echo "--expected-team-hub-transport requires a value." >&2
        exit 2
      fi
      EXPECTED_TEAM_HUB_TRANSPORT="${2:-}"
      EXPECTED_TEAM_HUB_TRANSPORT_SET="true"
      shift 2
      ;;
    --expected-team-hub-url)
      if (($# < 2)); then
        echo "--expected-team-hub-url requires a value (empty for loopback)." >&2
        exit 2
      fi
      EXPECTED_TEAM_HUB_URL="${2:-}"
      EXPECTED_TEAM_HUB_URL_SET="true"
      shift 2
      ;;
    --expected-team-hub-direct-ip-url)
      if (($# < 2)); then
        echo "--expected-team-hub-direct-ip-url requires a value (empty when absent)." >&2
        exit 2
      fi
      EXPECTED_TEAM_HUB_DIRECT_IP_URL="${2:-}"
      EXPECTED_TEAM_HUB_DIRECT_IP_URL_SET="true"
      shift 2
      ;;
    --team-hub-snapshot) TEAM_HUB_SNAPSHOT="${2:-}"; shift 2 ;;
    --team-hub-data-dir) TEAM_HUB_DATA_DIR="${2:-}"; shift 2 ;;
    --team-hub-operation-id) TEAM_HUB_OPERATION_ID="${2:-}"; shift 2 ;;
    --show-token) SHOW_TOKEN="true"; shift ;;
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

if [[ -n "$EXPECTED_SERVER_IDENTITY" ]] && [[ ! "$EXPECTED_SERVER_IDENTITY" =~ ^[A-Za-z0-9_.:-]{8,240}$ ]]; then
  echo "Expected server identity is invalid." >&2
  exit 2
fi
if [[ -n "$EXPECTED_TEAM_HUB_ID" ]] && [[ ! "$EXPECTED_TEAM_HUB_ID" =~ ^[A-Za-z0-9_.:-]{8,240}$ ]]; then
  echo "Expected Team Hub identity is invalid." >&2
  exit 2
fi
if [[ -n "$TEAM_HUB_OPERATION_ID" ]] && [[ ! "$TEAM_HUB_OPERATION_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]]; then
  echo "Managed Team Hub operation ID is invalid." >&2
  exit 2
fi
if [[ -n "$EXPECTED_TEAM_HUB_ID" ]]; then
  if [[ "$EXPECTED_TEAM_HUB_TRANSPORT_SET" == "false" && "$EXPECTED_TEAM_HUB_URL_SET" == "false" ]]; then
    # A beta.2 managed updater cannot pass these additive continuity fields.
    # Its only supported Team Hub transport was loopback, so bind that legacy
    # operation to loopback explicitly rather than consulting mutable env.
    EXPECTED_TEAM_HUB_TRANSPORT="loopback"
    EXPECTED_TEAM_HUB_TRANSPORT_SET="true"
    EXPECTED_TEAM_HUB_URL=""
    EXPECTED_TEAM_HUB_URL_SET="true"
  elif [[ "$EXPECTED_TEAM_HUB_TRANSPORT_SET" != "$EXPECTED_TEAM_HUB_URL_SET" ]]; then
    echo "Managed Team Hub transport and URL assertions must be supplied together." >&2
    exit 2
  fi
  if [[ "$EXPECTED_TEAM_HUB_DIRECT_IP_URL_SET" != "true" ]]; then
    # Runners predating the Direct IP route contract could only have accepted
    # a route set without Direct IP. Preserve that exact absence rather than
    # adopting a previously ignored environment value during the update.
    EXPECTED_TEAM_HUB_DIRECT_IP_URL=""
    EXPECTED_TEAM_HUB_DIRECT_IP_URL_SET="true"
  fi
fi
if [[ "$EXPECTED_TEAM_HUB_TRANSPORT_SET" == "true" && "$EXPECTED_TEAM_HUB_TRANSPORT" != "loopback" && "$EXPECTED_TEAM_HUB_TRANSPORT" != "tailscale_serve" && "$EXPECTED_TEAM_HUB_TRANSPORT" != "direct_ip" ]]; then
  echo "Expected Team Hub transport is invalid." >&2
  exit 2
fi
if [[ -n "$EXPECTED_TEAM_HUB_ID" || "$EXPECTED_TEAM_HUB_TRANSPORT_SET" == "true" || "$EXPECTED_TEAM_HUB_URL_SET" == "true" || "$EXPECTED_TEAM_HUB_DIRECT_IP_URL_SET" == "true" || -n "$TEAM_HUB_SNAPSHOT" || -n "$TEAM_HUB_DATA_DIR" || -n "$TEAM_HUB_OPERATION_ID" ]]; then
  if [[ -z "$EXPECTED_SERVER_IDENTITY" || -z "$EXPECTED_TEAM_HUB_ID" || "$EXPECTED_TEAM_HUB_TRANSPORT_SET" != "true" || "$EXPECTED_TEAM_HUB_URL_SET" != "true" || -z "$TEAM_HUB_SNAPSHOT" || -z "$TEAM_HUB_DATA_DIR" || -z "$TEAM_HUB_OPERATION_ID" ]]; then
    echo "Managed Team Hub rollback arguments must be supplied together with the expected server identity." >&2
    exit 2
  fi
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
OLD_TARGET=""
RELEASE_ACTIVATED="false"
CANDIDATE_SERVICE_MAY_HAVE_STARTED="false"
TEAM_HUB_RECOVERY_ATTEMPTED="false"
TEAM_HUB_OPERATION_FINALIZED="false"
TEAM_HUB_OPERATION_PENDING="false"
[[ -z "$EXPECTED_TEAM_HUB_ID" ]] || TEAM_HUB_OPERATION_PENDING="true"
IN_EXIT_CLEANUP="false"
ENV_CONFIG_BACKUP=""
ENV_CONFIG_EXISTED="false"
ENV_CONFIG_CAPTURED="false"
SERVICE_CONFIG_BACKUP=""
SERVICE_CONFIG_EXISTED="false"
SERVICE_CONFIG_CAPTURED="false"

scrub_staged_process_environment() {
  unset \
    AGENTSDOCK_AGENT_TOKEN \
    AGENTSDOCK_PROVIDER_AUTHORITY_FILE \
    AGENTSDOCK_PUBLISH_TOKEN \
    ZENITHBOT_AGENT_TOKEN \
    ZENITHDOCK_AGENT_TOKEN
}

run_without_server_secrets() (
  scrub_staged_process_environment
  "$@"
)

team_hub_control_runtime() {
  local preferred_root="${1:-}"
  local candidate=""
  for candidate in \
    "$preferred_root" \
    "${OLD_TARGET:-}" \
    "$CURRENT_LINK" \
    "$RELEASE_DIR" \
    "$STAGE_DIR" \
    "$SOURCE_DIR"; do
    [[ -n "$candidate" ]] || continue
    if [[ -f "$candidate/agentsdock_team_hub/store.py" && -x "$candidate/.venv/bin/python" ]]; then
      printf '%s\n%s\n' "$candidate/.venv/bin/python" "$candidate"
      return 0
    fi
  done
  return 1
}

clear_team_hub_operation_fence() {
  [[ "$TEAM_HUB_OPERATION_PENDING" == "true" ]] || return 0
  local runtime=""
  local python_path=""
  local source_root=""
  runtime="$(team_hub_control_runtime "${1:-}")" || {
    echo "No installed runtime can verify the Team Hub maintenance fence." >&2
    return 1
  }
  python_path="${runtime%%$'\n'*}"
  source_root="${runtime#*$'\n'}"
  run_without_server_secrets env PYTHONPATH="$source_root" "$python_path" -c '
from pathlib import Path
import sys
from agentsdock_team_hub.store import HubStore

cleared = HubStore.clear_maintenance_fence_control(
    Path(sys.argv[1]),
    expected_hub_id=sys.argv[2],
    expected_host_identity=sys.argv[3],
    expected_reason="server-update",
    expected_operation_id=sys.argv[4],
    expected_snapshot=Path(sys.argv[5]),
)
if not cleared:
    raise RuntimeError("the exact Team Hub maintenance fence is missing")
' \
    "$TEAM_HUB_DATA_DIR" \
    "$EXPECTED_TEAM_HUB_ID" \
    "$EXPECTED_SERVER_IDENTITY" \
    "$TEAM_HUB_OPERATION_ID" \
    "$TEAM_HUB_SNAPSHOT"
}

mask_install_signals() {
  trap '' HUP INT TERM
}

resume_install_signals() {
  trap 'exit 130' HUP INT TERM
}

verify_team_hub_operation_fence() {
  [[ "$TEAM_HUB_OPERATION_PENDING" == "true" ]] || return 0
  local runtime=""
  local python_path=""
  local source_root=""
  runtime="$(team_hub_control_runtime "${1:-}")" || {
    echo "No installed runtime can verify the Team Hub maintenance fence." >&2
    return 1
  }
  python_path="${runtime%%$'\n'*}"
  source_root="${runtime#*$'\n'}"
  run_without_server_secrets env PYTHONPATH="$source_root" "$python_path" -c '
from pathlib import Path
import sys
from agentsdock_team_hub.store import HubStore

matched = HubStore.maintenance_fence_matches_control(
    Path(sys.argv[1]),
    expected_hub_id=sys.argv[2],
    expected_host_identity=sys.argv[3],
    expected_reason="server-update",
    expected_operation_id=sys.argv[4],
    expected_snapshot=Path(sys.argv[5]),
)
if not matched:
    raise RuntimeError("the exact Team Hub maintenance fence is missing")
' \
    "$TEAM_HUB_DATA_DIR" \
    "$EXPECTED_TEAM_HUB_ID" \
    "$EXPECTED_SERVER_IDENTITY" \
    "$TEAM_HUB_OPERATION_ID" \
    "$TEAM_HUB_SNAPSHOT"
}

verify_team_hub_rollback_snapshot() {
  [[ "$TEAM_HUB_OPERATION_PENDING" == "true" ]] || return 0
  local runtime=""
  local python_path=""
  local source_root=""
  runtime="$(team_hub_control_runtime "${1:-}")" || {
    echo "No installed runtime can verify the Team Hub rollback snapshot." >&2
    return 1
  }
  python_path="${runtime%%$'\n'*}"
  source_root="${runtime#*$'\n'}"
  run_without_server_secrets env PYTHONPATH="$source_root" "$python_path" -m agentsdock_team_hub.cli \
    verify-snapshot \
    --data-dir "$TEAM_HUB_DATA_DIR" \
    --snapshot "$TEAM_HUB_SNAPSHOT" \
    --expected-host-identity "$EXPECTED_SERVER_IDENTITY" \
    --expected-hub-id "$EXPECTED_TEAM_HUB_ID" \
    --expected-operation-id "$TEAM_HUB_OPERATION_ID"
}

early_operation_cleanup() {
  local exit_status=$?
  trap - EXIT
  mask_install_signals
  set +e
  if [[ "$exit_status" != "0" && "$TEAM_HUB_OPERATION_PENDING" == "true" && "$TEAM_HUB_OPERATION_FINALIZED" != "true" ]]; then
    if clear_team_hub_operation_fence "$CURRENT_LINK"; then
      TEAM_HUB_OPERATION_FINALIZED="true"
    else
      echo "AgentsServer install failed before takeover and could not clear the exact Team Hub maintenance fence." >&2
    fi
  fi
  exit "$exit_status"
}

trap early_operation_cleanup EXIT
trap 'exit 130' HUP INT TERM

read_env_value() {
  local file="$1"
  local name="$2"
  [[ -f "$file" ]] || return 0
  sed -n "s/^${name}=//p" "$file" | tail -n 1
}

env_file_has_key() {
  local file="$1"
  local name="$2"
  [[ -n "$file" && -f "$file" ]] || return 1
  grep -q "^${name}=" "$file"
}

read_persisted_team_hub_config() {
  RESOLVED_TEAM_HUB_MODE=""
  if env_file_has_key "$ENV_FILE" AGENTSDOCK_TEAM_HUB_MODE; then
    RESOLVED_TEAM_HUB_MODE="$(read_env_value "$ENV_FILE" AGENTSDOCK_TEAM_HUB_MODE)"
  elif env_file_has_key "$LEGACY_ENV_FILE" AGENTSDOCK_TEAM_HUB_MODE; then
    RESOLVED_TEAM_HUB_MODE="$(read_env_value "$LEGACY_ENV_FILE" AGENTSDOCK_TEAM_HUB_MODE)"
  else
    RESOLVED_TEAM_HUB_MODE="${AGENTSDOCK_TEAM_HUB_MODE:-}"
  fi

  RESOLVED_TEAM_HUB_TRANSPORT=""
  RESOLVED_TEAM_HUB_TRANSPORT_SET="false"
  if env_file_has_key "$ENV_FILE" AGENTSDOCK_TEAM_HUB_TRANSPORT; then
    RESOLVED_TEAM_HUB_TRANSPORT="$(read_env_value "$ENV_FILE" AGENTSDOCK_TEAM_HUB_TRANSPORT)"
    RESOLVED_TEAM_HUB_TRANSPORT_SET="true"
  elif env_file_has_key "$LEGACY_ENV_FILE" AGENTSDOCK_TEAM_HUB_TRANSPORT; then
    RESOLVED_TEAM_HUB_TRANSPORT="$(read_env_value "$LEGACY_ENV_FILE" AGENTSDOCK_TEAM_HUB_TRANSPORT)"
    RESOLVED_TEAM_HUB_TRANSPORT_SET="true"
  elif [[ "${AGENTSDOCK_TEAM_HUB_TRANSPORT+x}" == "x" ]]; then
    RESOLVED_TEAM_HUB_TRANSPORT="$AGENTSDOCK_TEAM_HUB_TRANSPORT"
    RESOLVED_TEAM_HUB_TRANSPORT_SET="true"
  fi

  RESOLVED_TEAM_HUB_URL=""
  if env_file_has_key "$ENV_FILE" AGENTSDOCK_TEAM_HUB_URL; then
    RESOLVED_TEAM_HUB_URL="$(read_env_value "$ENV_FILE" AGENTSDOCK_TEAM_HUB_URL)"
  elif env_file_has_key "$LEGACY_ENV_FILE" AGENTSDOCK_TEAM_HUB_URL; then
    RESOLVED_TEAM_HUB_URL="$(read_env_value "$LEGACY_ENV_FILE" AGENTSDOCK_TEAM_HUB_URL)"
  else
    RESOLVED_TEAM_HUB_URL="${AGENTSDOCK_TEAM_HUB_URL:-}"
  fi

  RESOLVED_TEAM_HUB_DIRECT_IP_URL=""
  if env_file_has_key "$ENV_FILE" AGENTSDOCK_TEAM_HUB_DIRECT_IP_URL; then
    RESOLVED_TEAM_HUB_DIRECT_IP_URL="$(read_env_value "$ENV_FILE" AGENTSDOCK_TEAM_HUB_DIRECT_IP_URL)"
  elif env_file_has_key "$LEGACY_ENV_FILE" AGENTSDOCK_TEAM_HUB_DIRECT_IP_URL; then
    RESOLVED_TEAM_HUB_DIRECT_IP_URL="$(read_env_value "$LEGACY_ENV_FILE" AGENTSDOCK_TEAM_HUB_DIRECT_IP_URL)"
  else
    RESOLVED_TEAM_HUB_DIRECT_IP_URL="${AGENTSDOCK_TEAM_HUB_DIRECT_IP_URL:-}"
  fi
}

canonical_team_hub_tailnet_hostname() {
  local hostname="$1"
  local label=""
  local -a labels=()
  [[ "$hostname" != *. ]] || return 1
  IFS='.' read -r -a labels <<< "$hostname"
  ((${#labels[@]} >= 4)) || return 1
  [[ "${labels[${#labels[@]} - 2]}" == "ts" && "${labels[${#labels[@]} - 1]}" == "net" ]] || return 1
  for label in "${labels[@]}"; do
    (( ${#label} >= 1 && ${#label} <= 63 )) || return 1
    [[ "$label" != xn--* ]] || return 1
    if [[ ! "$label" =~ ^[a-z0-9]$ && ! "$label" =~ ^[a-z0-9][a-z0-9-]*[a-z0-9]$ ]]; then
      return 1
    fi
  done
}

canonical_team_hub_direct_ipv4_url() {
  local value="$1"
  local expected_port="$2"
  local octet=""
  local -a octets=()
  [[ "$value" =~ ^http://([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}):([0-9]{1,5})/api/team-hub$ ]] || return 1
  [[ "${BASH_REMATCH[2]}" == "$expected_port" ]] || return 1
  IFS='.' read -r -a octets <<< "${BASH_REMATCH[1]}"
  ((${#octets[@]} == 4)) || return 1
  for octet in "${octets[@]}"; do
    [[ "$octet" =~ ^0$|^[1-9][0-9]{0,2}$ ]] || return 1
    ((10#$octet <= 255)) || return 1
  done
  ((10#${octets[0]} != 0 && 10#${octets[0]} != 127 && 10#${octets[0]} < 224)) || return 1
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

find_existing_token() {
  local candidate found_token
  for candidate in "$ENV_FILE" "$LEGACY_ENV_FILE"; do
    [[ -n "$candidate" && -f "$candidate" ]] || continue
    found_token="$(read_env_value "$candidate" AGENTSDOCK_AGENT_TOKEN)"
    [[ -n "$found_token" ]] || found_token="$(read_env_value "$candidate" ZENITHDOCK_AGENT_TOKEN)"
    [[ -n "$found_token" ]] || found_token="$(read_env_value "$candidate" ZENITHBOT_AGENT_TOKEN)"
    [[ -z "$found_token" ]] || { printf '%s' "$found_token"; return 0; }
  done
  if [[ -f "$LEGACY_SERVICE_FILE" ]]; then
    found_token="$(grep -E '^Environment="?ZENITHDOCK_AGENT_TOKEN=' "$LEGACY_SERVICE_FILE" | tail -n 1 || true)"
    found_token="${found_token#*ZENITHDOCK_AGENT_TOKEN=}"
    found_token="${found_token%\"}"
    [[ -z "$found_token" ]] || { printf '%s' "$found_token"; return 0; }
  fi
  return 1
}

if [[ "$SHOW_TOKEN" == "true" ]]; then
  if TOKEN_TO_SHOW="$(find_existing_token)"; then
    printf '%s\n' "$TOKEN_TO_SHOW"
    exit 0
  fi
  echo "No AgentsServer access token found at $ENV_FILE. Run install.sh first." >&2
  exit 1
fi

OS_NAME="$(uname -s)"
SYSTEMD_SERVICE_FILE="$HOME/.config/systemd/user/$SERVICE_NAME.service"
LABEL="com.agentsdock.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
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

read_persisted_team_hub_config
EXISTING_TEAM_HUB_MODE="$RESOLVED_TEAM_HUB_MODE"
TEAM_HUB_MODE="${TEAM_HUB_MODE_OVERRIDE:-${EXISTING_TEAM_HUB_MODE:-disabled}}"
if [[ "$TEAM_HUB_MODE" != "host" && "$TEAM_HUB_MODE" != "disabled" ]]; then
  echo "AGENTSDOCK_TEAM_HUB_MODE must be host or disabled." >&2
  exit 2
fi
EXISTING_TEAM_HUB_TRANSPORT="$RESOLVED_TEAM_HUB_TRANSPORT"
EXISTING_TEAM_HUB_TRANSPORT_SET="$RESOLVED_TEAM_HUB_TRANSPORT_SET"
EXISTING_TEAM_HUB_URL="$RESOLVED_TEAM_HUB_URL"
EXISTING_TEAM_HUB_DIRECT_IP_URL="$RESOLVED_TEAM_HUB_DIRECT_IP_URL"
PREVIOUS_TEAM_HUB_TRANSPORT="loopback"
if [[ "$EXISTING_TEAM_HUB_TRANSPORT_SET" == "true" ]]; then
  PREVIOUS_TEAM_HUB_TRANSPORT="$EXISTING_TEAM_HUB_TRANSPORT"
fi
PREVIOUS_TEAM_HUB_URL="$EXISTING_TEAM_HUB_URL"
PREVIOUS_TEAM_HUB_DIRECT_IP_URL="$EXISTING_TEAM_HUB_DIRECT_IP_URL"
if [[ "$TEAM_HUB_MODE" == "disabled" ]]; then
  TEAM_HUB_TRANSPORT="loopback"
  TEAM_HUB_URL=""
else
  if [[ -n "$TEAM_HUB_TRANSPORT_OVERRIDE" ]]; then
    TEAM_HUB_TRANSPORT="$TEAM_HUB_TRANSPORT_OVERRIDE"
  elif [[ "$EXISTING_TEAM_HUB_TRANSPORT_SET" == "true" ]]; then
    TEAM_HUB_TRANSPORT="$EXISTING_TEAM_HUB_TRANSPORT"
  else
    TEAM_HUB_TRANSPORT="loopback"
  fi
  TEAM_HUB_URL="${TEAM_HUB_URL_OVERRIDE:-$EXISTING_TEAM_HUB_URL}"
  TEAM_HUB_DIRECT_IP_URL="${TEAM_HUB_DIRECT_IP_URL_OVERRIDE:-$EXISTING_TEAM_HUB_DIRECT_IP_URL}"
  if [[ -n "$TEAM_HUB_DIRECT_IP_URL_OVERRIDE" && -z "$TEAM_HUB_TRANSPORT_OVERRIDE" ]] && {
    [[ "$EXISTING_TEAM_HUB_TRANSPORT_SET" != "true" ]] \
      || [[ "$EXISTING_TEAM_HUB_MODE" != "host" ]]
  }; then
    TEAM_HUB_TRANSPORT="direct_ip"
    TEAM_HUB_URL="$TEAM_HUB_DIRECT_IP_URL"
  fi
fi
case "$TEAM_HUB_TRANSPORT" in
  loopback)
    if [[ -n "$TEAM_HUB_URL" ]]; then
      echo "Loopback Team Hub transport does not accept an external Hub URL." >&2
      exit 2
    fi
    ;;
  tailscale_serve)
    if [[ "$TEAM_HUB_MODE" != "host" ]]; then
      echo "Tailscale Serve Team Hub transport requires host mode." >&2
      exit 2
    fi
    TEAM_HUB_URL_LOWER="$(printf '%s' "$TEAM_HUB_URL" | tr '[:upper:]' '[:lower:]')"
    if [[ "$TEAM_HUB_URL" != "$TEAM_HUB_URL_LOWER" ]]; then
      echo "Team Hub Tailscale Serve URL must be canonical HTTPS on an explicit *.ts.net port and end in /api/team-hub." >&2
      exit 2
    fi
    if [[ "$TEAM_HUB_URL" =~ ^https://([a-z0-9][a-z0-9.-]*\.ts\.net):([0-9]{1,5})/api/team-hub$ ]]; then
      TEAM_HUB_SERVE_HOST="${BASH_REMATCH[1]}"
      TEAM_HUB_SERVE_PORT="${BASH_REMATCH[2]}"
    else
      echo "Team Hub Tailscale Serve URL must be canonical HTTPS on an explicit *.ts.net port and end in /api/team-hub." >&2
      exit 2
    fi
    if [[ "$TEAM_HUB_SERVE_PORT" == "443" || "$TEAM_HUB_SERVE_PORT" == "8443" || "$TEAM_HUB_SERVE_PORT" == "10000" ]]; then
      echo "Team Hub Tailscale Serve URL is invalid or uses a Funnel-capable port." >&2
      exit 2
    fi
    if [[ "$TEAM_HUB_SERVE_PORT" != "8444" ]]; then
      echo "Team Hub Tailscale Serve URL must use the private beta port 8444." >&2
      exit 2
    fi
    if ! canonical_team_hub_tailnet_hostname "$TEAM_HUB_SERVE_HOST"; then
      echo "Team Hub Tailscale Serve URL has a noncanonical tailnet hostname." >&2
      exit 2
    fi
    ;;
  direct_ip)
    if [[ "$TEAM_HUB_MODE" != "host" || -z "$TEAM_HUB_URL" || "$TEAM_HUB_URL" != "$TEAM_HUB_DIRECT_IP_URL" ]]; then
      echo "Direct-IP Team Hub transport requires the exact configured direct-IP URL in host mode." >&2
      exit 2
    fi
    ;;
  *)
    echo "AGENTSDOCK_TEAM_HUB_TRANSPORT must be loopback, tailscale_serve, or direct_ip." >&2
    exit 2
    ;;
esac
if [[ -n "$TEAM_HUB_DIRECT_IP_URL" ]] && ! canonical_team_hub_direct_ipv4_url "$TEAM_HUB_DIRECT_IP_URL" "$PORT"; then
  echo "Team Hub Direct IP URL must be exact http://<literal-ip>:$PORT/api/team-hub on the AgentsServer port." >&2
  exit 2
fi
if [[ "$EXPECTED_TEAM_HUB_DIRECT_IP_URL_SET" == "true" ]] && [[ "$TEAM_HUB_DIRECT_IP_URL" != "$EXPECTED_TEAM_HUB_DIRECT_IP_URL" ]]; then
  echo "Managed Team Hub Direct IP route changed after update acceptance." >&2
  exit 2
fi
if [[ -n "$EXPECTED_TEAM_HUB_ID" ]] && {
  [[ "$TEAM_HUB_TRANSPORT" != "$EXPECTED_TEAM_HUB_TRANSPORT" ]] \
    || [[ "$TEAM_HUB_URL" != "$EXPECTED_TEAM_HUB_URL" ]]
}; then
  echo "Managed Team Hub transport or URL changed after update acceptance." >&2
  exit 2
fi
if [[ "$EXISTING_TEAM_HUB_MODE" == "host" && "$TEAM_HUB_MODE" == "host" ]] && {
  [[ "$PREVIOUS_TEAM_HUB_TRANSPORT" != "$TEAM_HUB_TRANSPORT" ]] \
    || [[ "$PREVIOUS_TEAM_HUB_URL" != "$TEAM_HUB_URL" ]]
}; then
  echo "Changing an existing Team Hub origin is not supported by this beta." >&2
  exit 2
fi
if [[ "$EXISTING_TEAM_HUB_MODE" == "host" && "$TEAM_HUB_MODE" == "host" && "$PREVIOUS_TEAM_HUB_DIRECT_IP_URL" != "$TEAM_HUB_DIRECT_IP_URL" ]]; then
  echo "Changing an existing Team Hub Direct IP origin is not supported by this beta." >&2
  exit 2
fi
if [[ -n "$EXPECTED_TEAM_HUB_ID" && "$TEAM_HUB_MODE" != "host" ]]; then
  echo "Managed Team Hub continuity requires AGENTSDOCK_TEAM_HUB_MODE=host." >&2
  exit 2
fi
TEAM_HUB_CANONICAL_DATA_DIR="$STATE_ROOT/team-hub"
if [[ "$TEAM_HUB_OPERATION_PENDING" == "true" && "$TEAM_HUB_DATA_DIR" != "$TEAM_HUB_CANONICAL_DATA_DIR" ]]; then
  echo "Managed Team Hub data must use the configured AgentsServer state directory." >&2
  exit 2
fi
if [[ "$TEAM_HUB_MODE" == "host" && "$TEAM_HUB_OPERATION_PENDING" != "true" ]]; then
  TEAM_HUB_EXISTING_STATE="false"
  for candidate in \
    "$TEAM_HUB_CANONICAL_DATA_DIR/team-hub.sqlite3" \
    "$TEAM_HUB_CANONICAL_DATA_DIR/team-hub.sqlite3-wal" \
    "$TEAM_HUB_CANONICAL_DATA_DIR/team-hub.sqlite3-shm" \
    "$TEAM_HUB_CANONICAL_DATA_DIR/access-token-signing.key" \
    "$TEAM_HUB_CANONICAL_DATA_DIR/maintenance-fence.json"; do
    if [[ -e "$candidate" || -L "$candidate" ]]; then
      TEAM_HUB_EXISTING_STATE="true"
      break
    fi
  done
  if [[ "$TEAM_HUB_EXISTING_STATE" != "true" && -d "$TEAM_HUB_CANONICAL_DATA_DIR" ]]; then
    if find "$TEAM_HUB_CANONICAL_DATA_DIR" -maxdepth 1 -type f -name '*.proof' -print -quit | grep -q .; then
      TEAM_HUB_EXISTING_STATE="true"
    fi
  fi
  if [[ "$TEAM_HUB_EXISTING_STATE" == "true" ]]; then
    echo "Existing Team Hub state requires a signed managed update with an exact maintenance snapshot." >&2
    echo "  Direct adoption or update of an existing Team Hub database is not supported by this beta." >&2
    exit 1
  fi
fi

RELEASE_FILES=(agent_server.py team_hub_host.py secure_peer_runtime.py secure_peer_delivery.py agentsdock_jobs.py agentsdock_chats.py agentsdock_publish.py claude_sdk_client.py codex_app_server.py install.sh uninstall.sh update_runner.py pyproject.toml uv.lock VERSION release-public-key.pem)
RELEASE_DIRECTORIES=(agentsdock_team_hub)
TEAM_HUB_RELEASE_FILES=(
  __init__.py
  auth.py
  cli.py
  database.py
  security.py
  secure_peer.py
  secure_peer_hub.py
  service.py
  store.py
  migrations/__init__.py
  migrations/0001_identity_auth.sql
  migrations/0002_teamspace_ledger.sql
  migrations/0003_service_runtime.sql
  migrations/0004_managed_host_binding.sql
  migrations/0005_tailnet_bootstrap_delegations.sql
)

for name in "${RELEASE_FILES[@]}"; do
  if [[ ! -f "$SOURCE_DIR/$name" || -L "$SOURCE_DIR/$name" ]]; then
    echo "$name is missing beside install.sh or is not a regular release file." >&2
    exit 1
  fi
done
for name in "${RELEASE_DIRECTORIES[@]}"; do
  if [[ ! -d "$SOURCE_DIR/$name" || -L "$SOURCE_DIR/$name" ]]; then
    echo "$name is missing beside install.sh or is not a real directory." >&2
    exit 1
  fi
  if find "$SOURCE_DIR/$name" \( -type l -o -type f \( -name '*.pyc' -o -name '*.pyo' \) -o -type d -name '__pycache__' -o \( ! -type d ! -type f \) \) -print -quit | grep -q .; then
    echo "$name contains linked or generated entries and cannot be installed." >&2
    exit 1
  fi
done
for name in "${TEAM_HUB_RELEASE_FILES[@]}"; do
  if [[ ! -f "$SOURCE_DIR/agentsdock_team_hub/$name" || -L "$SOURCE_DIR/agentsdock_team_hub/$name" ]]; then
    echo "agentsdock_team_hub/$name is missing or is not a regular release file." >&2
    exit 1
  fi
done
TEAM_HUB_RELEASE_FILE_COUNT="$(find "$SOURCE_DIR/agentsdock_team_hub" -type f | wc -l)"
TEAM_HUB_RELEASE_FILE_COUNT="${TEAM_HUB_RELEASE_FILE_COUNT//[[:space:]]/}"
if [[ "$TEAM_HUB_RELEASE_FILE_COUNT" != "${#TEAM_HUB_RELEASE_FILES[@]}" ]]; then
  echo "agentsdock_team_hub contains unexpected release files." >&2
  exit 1
fi
TEAM_HUB_RELEASE_DIRECTORY_COUNT="$(find "$SOURCE_DIR/agentsdock_team_hub" -type d | wc -l)"
TEAM_HUB_RELEASE_DIRECTORY_COUNT="${TEAM_HUB_RELEASE_DIRECTORY_COUNT//[[:space:]]/}"
if [[ "$TEAM_HUB_RELEASE_DIRECTORY_COUNT" != "2" ]]; then
  echo "agentsdock_team_hub contains unexpected release directories." >&2
  exit 1
fi

current_release_binding() {
  if [[ -L "$CURRENT_LINK" ]]; then
    printf 'symlink:%s' "$(readlink "$CURRENT_LINK")"
  elif [[ -d "$CURRENT_LINK" ]]; then
    printf 'directory'
  elif [[ -e "$CURRENT_LINK" ]]; then
    return 1
  else
    printf 'missing'
  fi
}

assert_team_hub_config_unchanged() {
  read_persisted_team_hub_config
  if [[ "$RESOLVED_TEAM_HUB_MODE" != "$EXISTING_TEAM_HUB_MODE" \
    || "$RESOLVED_TEAM_HUB_TRANSPORT_SET" != "$EXISTING_TEAM_HUB_TRANSPORT_SET" \
    || "$RESOLVED_TEAM_HUB_TRANSPORT" != "$EXISTING_TEAM_HUB_TRANSPORT" \
    || "$RESOLVED_TEAM_HUB_URL" != "$EXISTING_TEAM_HUB_URL" \
    || "$RESOLVED_TEAM_HUB_DIRECT_IP_URL" != "$EXISTING_TEAM_HUB_DIRECT_IP_URL" ]]; then
    echo "Team Hub configuration changed while the installer was preparing." >&2
    return 1
  fi
}

validate_managed_team_hub_inputs() {
  local runtime_root="$1"
  local current_binding=""
  local candidate=""
  [[ "$TEAM_HUB_OPERATION_PENDING" == "true" ]] || return 0
  assert_team_hub_config_unchanged || return
  current_binding="$(current_release_binding)" || {
    echo "The current release changed to an unsafe path while the installer was preparing." >&2
    return 1
  }
  if [[ "$current_binding" != "$INITIAL_CURRENT_LINK_BINDING" ]]; then
    echo "The current release changed while the installer was preparing." >&2
    return 1
  fi
  if [[ ! -d "$TEAM_HUB_DATA_DIR" || -L "$TEAM_HUB_DATA_DIR" || ! -d "$TEAM_HUB_SNAPSHOT" || -L "$TEAM_HUB_SNAPSHOT" ]]; then
    echo "Managed Team Hub data or snapshot directory changed or became unsafe." >&2
    return 1
  fi
  for candidate in manifest.json team-hub.sqlite3 access-token-signing.key; do
    if [[ ! -f "$TEAM_HUB_SNAPSHOT/$candidate" || -L "$TEAM_HUB_SNAPSHOT/$candidate" ]]; then
      echo "Managed Team Hub snapshot changed or has an unsafe $candidate file." >&2
      return 1
    fi
  done
  if ! verify_team_hub_operation_fence "$runtime_root"; then
    echo "Managed Team Hub installer no longer owns the exact live maintenance fence." >&2
    return 1
  fi
  if ! verify_team_hub_rollback_snapshot "$runtime_root"; then
    echo "Managed Team Hub rollback snapshot no longer passes full read-only verification." >&2
    return 1
  fi
}

if [[ "$TEAM_HUB_OPERATION_PENDING" == "true" ]]; then
  INITIAL_CURRENT_LINK_BINDING="$(current_release_binding)" || {
    echo "Managed Team Hub update requires a safe current release path." >&2
    exit 1
  }
  TEAM_HUB_SNAPSHOT_PARENT="${TEAM_HUB_SNAPSHOT%/*}"
  TEAM_HUB_SNAPSHOT_NAME="${TEAM_HUB_SNAPSHOT##*/}"
  if [[ "$TEAM_HUB_SNAPSHOT_PARENT" != "$TEAM_HUB_DATA_DIR/maintenance-backups" || ! "$TEAM_HUB_SNAPSHOT_NAME" =~ ^snapshot_[A-Za-z0-9_]+$ ]]; then
    echo "Managed Team Hub snapshot is not an exact maintenance generation." >&2
    exit 2
  fi
  if [[ ! -d "$TEAM_HUB_DATA_DIR" || -L "$TEAM_HUB_DATA_DIR" || ! -d "$TEAM_HUB_SNAPSHOT" || -L "$TEAM_HUB_SNAPSHOT" ]]; then
    echo "Managed Team Hub data or snapshot directory is unavailable or unsafe." >&2
    exit 1
  fi
  for candidate in manifest.json team-hub.sqlite3 access-token-signing.key; do
    if [[ ! -f "$TEAM_HUB_SNAPSHOT/$candidate" || -L "$TEAM_HUB_SNAPSHOT/$candidate" ]]; then
      echo "Managed Team Hub snapshot is missing a safe $candidate file." >&2
      exit 1
    fi
  done
  if ! verify_team_hub_operation_fence "$CURRENT_LINK"; then
    echo "Managed Team Hub installer takeover does not own the exact live maintenance fence." >&2
    exit 1
  fi
  if ! verify_team_hub_rollback_snapshot "$CURRENT_LINK"; then
    echo "Managed Team Hub rollback snapshot failed full read-only verification before candidate activation." >&2
    exit 1
  fi
fi

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

UV_INSTALLER=""
ACTIVE_STAGE_PID=""
ACTIVE_STAGE_PGID=""
INSTALL_LOCK_DIR="$INSTALL_ROOT/.install-lock"
INSTALL_LOCK_HELD="false"
PREVIOUS_LINK_WAS_SYMLINK="false"
PREVIOUS_LINK_TARGET=""
PREVIOUS_LINK_STATE_CAPTURED="false"
CURRENT_LINK_STATE_CAPTURED="false"
CURRENT_LINK_WAS_SYMLINK="false"
CURRENT_LINK_WAS_DIRECTORY="false"
CURRENT_LINK_TARGET=""

backup_runtime_configuration() {
  if [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]]; then
    ENV_CONFIG_BACKUP="$(mktemp "${TMPDIR:-/tmp}/agents-server-env.XXXXXX")"
    cp "$ENV_FILE" "$ENV_CONFIG_BACKUP"
    chmod 600 "$ENV_CONFIG_BACKUP"
    ENV_CONFIG_EXISTED="true"
  elif [[ -e "$ENV_FILE" || -L "$ENV_FILE" ]]; then
    echo "$ENV_FILE is not a regular configuration file." >&2
    return 1
  fi
  ENV_CONFIG_CAPTURED="true"

  local service_file="$SYSTEMD_SERVICE_FILE"
  [[ "$OS_NAME" != "Darwin" ]] || service_file="$PLIST"
  if [[ -f "$service_file" && ! -L "$service_file" ]]; then
    SERVICE_CONFIG_BACKUP="$(mktemp "${TMPDIR:-/tmp}/agents-server-service.XXXXXX")"
    cp "$service_file" "$SERVICE_CONFIG_BACKUP"
    chmod 600 "$SERVICE_CONFIG_BACKUP"
    SERVICE_CONFIG_EXISTED="true"
  elif [[ -e "$service_file" || -L "$service_file" ]]; then
    echo "$service_file is not a regular service configuration file." >&2
    return 1
  fi
  SERVICE_CONFIG_CAPTURED="true"

  if [[ -L "$CURRENT_LINK" ]]; then
    CURRENT_LINK_WAS_SYMLINK="true"
    CURRENT_LINK_TARGET="$(readlink "$CURRENT_LINK")"
  elif [[ -d "$CURRENT_LINK" ]]; then
    CURRENT_LINK_WAS_DIRECTORY="true"
  elif [[ -e "$CURRENT_LINK" ]]; then
    echo "$CURRENT_LINK is not a supported release link or directory." >&2
    return 1
  fi
  CURRENT_LINK_STATE_CAPTURED="true"

  if [[ -L "$PREVIOUS_LINK" ]]; then
    PREVIOUS_LINK_WAS_SYMLINK="true"
    PREVIOUS_LINK_TARGET="$(readlink "$PREVIOUS_LINK")"
  elif [[ -e "$PREVIOUS_LINK" ]]; then
    echo "$PREVIOUS_LINK is not a symbolic link." >&2
    return 1
  fi
  PREVIOUS_LINK_STATE_CAPTURED="true"
}

restore_regular_configuration() {
  local target="$1"
  local backup="$2"
  local existed="$3"
  if [[ "$existed" == "true" ]]; then
    [[ -n "$backup" && -f "$backup" ]] || return 1
    cp -p "$backup" "$target"
  else
    if [[ -d "$target" && ! -L "$target" ]]; then
      echo "Refusing to replace unexpected configuration directory $target." >&2
      return 1
    fi
    if [[ -e "$target" || -L "$target" ]]; then
      rm -f "$target"
    fi
  fi
}

restore_runtime_configuration() {
  [[ "$ENV_CONFIG_CAPTURED" != "true" ]] || \
    restore_regular_configuration "$ENV_FILE" "$ENV_CONFIG_BACKUP" "$ENV_CONFIG_EXISTED" || return
  local service_file="$SYSTEMD_SERVICE_FILE"
  [[ "$OS_NAME" != "Darwin" ]] || service_file="$PLIST"
  [[ "$SERVICE_CONFIG_CAPTURED" != "true" ]] || \
    restore_regular_configuration "$service_file" "$SERVICE_CONFIG_BACKUP" "$SERVICE_CONFIG_EXISTED" || return
}

restore_release_links() {
  [[ "$CURRENT_LINK_STATE_CAPTURED" == "true" ]] || return 0
  if [[ "$CURRENT_LINK_WAS_DIRECTORY" == "true" ]]; then
    if [[ -n "$OLD_TARGET" && -d "$OLD_TARGET" ]]; then
      if [[ -e "$CURRENT_LINK" && ! -L "$CURRENT_LINK" ]]; then
        echo "Refusing to replace unexpected current release path $CURRENT_LINK." >&2
        return 1
      fi
      ln -sfn "$OLD_TARGET" "$CURRENT_LINK" || return
    elif [[ ! -d "$CURRENT_LINK" || -L "$CURRENT_LINK" ]]; then
      echo "The original current release directory cannot be recovered." >&2
      return 1
    fi
  elif [[ "$CURRENT_LINK_WAS_SYMLINK" == "true" ]]; then
    if [[ -n "$OLD_TARGET" && -e "$OLD_TARGET" ]]; then
      ln -sfn "$OLD_TARGET" "$CURRENT_LINK" || return
    elif [[ -L "$CURRENT_LINK" && "$(readlink "$CURRENT_LINK")" == "$CURRENT_LINK_TARGET" ]]; then
      :
    else
      echo "The original current release link cannot be recovered." >&2
      return 1
    fi
  elif [[ -L "$CURRENT_LINK" ]]; then
    rm -f "$CURRENT_LINK" || return
  elif [[ -e "$CURRENT_LINK" ]]; then
    echo "Refusing to replace unexpected current release path $CURRENT_LINK." >&2
    return 1
  fi

  if [[ "$PREVIOUS_LINK_STATE_CAPTURED" == "true" ]]; then
    if [[ "$PREVIOUS_LINK_WAS_SYMLINK" == "true" ]]; then
      ln -sfn "$PREVIOUS_LINK_TARGET" "$PREVIOUS_LINK" || return
    elif [[ -L "$PREVIOUS_LINK" ]]; then
      rm -f "$PREVIOUS_LINK" || return
    elif [[ -e "$PREVIOUS_LINK" ]]; then
      echo "Refusing to replace unexpected previous release path $PREVIOUS_LINK." >&2
      return 1
    fi
  fi
}

restore_pre_candidate_changes() {
  restore_release_links || return
  restore_runtime_configuration || return
}

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
  local exit_status=$?
  trap - EXIT
  mask_install_signals
  IN_EXIT_CLEANUP="true"
  set +e
  stop_active_stage
  if [[ "$exit_status" != "0" && "$TEAM_HUB_RECOVERY_ATTEMPTED" != "true" && ( "$TEAM_HUB_OPERATION_PENDING" != "true" || "$TEAM_HUB_OPERATION_FINALIZED" != "true" ) ]]; then
    TEAM_HUB_RECOVERY_ATTEMPTED="true"
    if [[ "$CANDIDATE_SERVICE_MAY_HAVE_STARTED" == "true" ]]; then
      if declare -F restore_previous_release >/dev/null && restore_previous_release; then
        [[ "$TEAM_HUB_OPERATION_PENDING" != "true" ]] || TEAM_HUB_OPERATION_FINALIZED="true"
      elif [[ "$TEAM_HUB_OPERATION_PENDING" == "true" ]]; then
        echo "Managed Team Hub rollback is incomplete; the exact maintenance fence remains fail-closed when present." >&2
      fi
    elif restore_pre_candidate_changes; then
      if [[ "$TEAM_HUB_OPERATION_PENDING" == "true" ]]; then
        if clear_team_hub_operation_fence "$OLD_TARGET"; then
          TEAM_HUB_OPERATION_FINALIZED="true"
        else
          echo "AgentsServer install failed before candidate health and could not safely release Team Hub maintenance." >&2
        fi
      fi
    elif [[ "$TEAM_HUB_OPERATION_PENDING" == "true" ]]; then
      echo "AgentsServer install failed before candidate health and could not restore local configuration; Team Hub remains fail-closed." >&2
    fi
  fi
  [[ -z "$UV_INSTALLER" ]] || rm -f "$UV_INSTALLER"
  rm -rf "$STAGE_DIR"
  [[ -z "$ENV_CONFIG_BACKUP" ]] || rm -f "$ENV_CONFIG_BACKUP"
  [[ -z "$SERVICE_CONFIG_BACKUP" ]] || rm -f "$SERVICE_CONFIG_BACKUP"
  release_install_lock
  exit "$exit_status"
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
if ! validate_managed_team_hub_inputs "$CURRENT_LINK"; then
  echo "Managed Team Hub inputs changed before the installer acquired exclusive ownership." >&2
  exit 1
fi

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

install_uv_runtime() (
  scrub_staged_process_environment
  sh "$UV_INSTALLER"
)

sync_release_dependencies() (
  # Managed updates may be launched from a long-lived tmux server. Never let
  # project/virtual-environment selectors inherited by that server redirect
  # this release sync into an unrelated workspace.
  scrub_staged_process_environment
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

validate_staged_release_runtime() (
  scrub_staged_process_environment
  "$STAGE_DIR/.venv/bin/python" -c 'import websockets' >/dev/null
  "$STAGE_DIR/.venv/bin/python" -c 'from importlib.metadata import version; import claude_agent_sdk; sdk_version = version("claude-agent-sdk"); raise SystemExit(0 if sdk_version == "0.2.130" else f"expected claude-agent-sdk 0.2.130, got {sdk_version}")'
  "$STAGE_DIR/.venv/bin/python" -c 'import croniter, dateutil; from zoneinfo import ZoneInfo; ZoneInfo("America/Los_Angeles")' >/dev/null
  "$STAGE_DIR/.venv/bin/python" -m py_compile \
    "$STAGE_DIR/agent_server.py" \
    "$STAGE_DIR/team_hub_host.py" \
    "$STAGE_DIR/secure_peer_runtime.py" \
    "$STAGE_DIR/secure_peer_delivery.py" \
    "$STAGE_DIR/agentsdock_jobs.py" \
    "$STAGE_DIR/agentsdock_chats.py" \
    "$STAGE_DIR/agentsdock_publish.py" \
    "$STAGE_DIR/claude_sdk_client.py" \
    "$STAGE_DIR/codex_app_server.py" \
    "$STAGE_DIR/update_runner.py"
  "$STAGE_DIR/.venv/bin/python" -m compileall -q "$STAGE_DIR/agentsdock_team_hub"
  PYTHONPATH="$STAGE_DIR" "$STAGE_DIR/.venv/bin/python" -c 'import agentsdock_team_hub, secure_peer_delivery, secure_peer_runtime, team_hub_host; from agentsdock_team_hub import secure_peer, secure_peer_hub' >/dev/null
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
    install_uv_runtime; then
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
mkdir -p "$STAGE_DIR/agentsdock_team_hub/migrations"
chmod 755 "$STAGE_DIR/agentsdock_team_hub" "$STAGE_DIR/agentsdock_team_hub/migrations"
for name in "${TEAM_HUB_RELEASE_FILES[@]}"; do
  install -m 644 \
    "$SOURCE_DIR/agentsdock_team_hub/$name" \
    "$STAGE_DIR/agentsdock_team_hub/$name"
done
chmod 755 "$STAGE_DIR/agent_server.py" "$STAGE_DIR/agentsdock_jobs.py" "$STAGE_DIR/agentsdock_chats.py" "$STAGE_DIR/agentsdock_publish.py" "$STAGE_DIR/install.sh" "$STAGE_DIR/uninstall.sh" "$STAGE_DIR/update_runner.py"

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
validate_staged_release_runtime

TOKEN="$(find_existing_token || true)"
generate_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    run_without_server_secrets "$STAGE_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))'
  fi
}
[[ "$TOKEN" =~ ^[A-Za-z0-9_-]{32,}$ ]] || TOKEN="$(generate_token)"

PRESERVE_SOURCE=""
[[ ! -f "$LEGACY_ENV_FILE" ]] || PRESERVE_SOURCE="$LEGACY_ENV_FILE"
[[ ! -f "$ENV_FILE" ]] || PRESERVE_SOURCE="$ENV_FILE"

write_runtime_env() {
  local env_temp="$CONFIG_ROOT/.env.$$"
  if [[ -n "$PRESERVE_SOURCE" ]]; then
    grep -Ev '^(AGENTSDOCK_(STATE_DIR|AGENT_CWD|AGENT_BIND|AGENT_PORT|AGENT_TOKEN|TEAM_HUB_MODE|TEAM_HUB_TRANSPORT|TEAM_HUB_URL|TEAM_HUB_DIRECT_IP_URL)|AGENTS_SERVER_(STATE_DIR|INSTALL_DIR)|ZENITHBOT_AGENT_(DIR|CWD|BIND|PORT|TOKEN)|ZENITHDOCK_AGENT_TOKEN|PATH)=' \
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
AGENTSDOCK_TEAM_HUB_MODE=$TEAM_HUB_MODE
AGENTSDOCK_TEAM_HUB_TRANSPORT=$TEAM_HUB_TRANSPORT
AGENTSDOCK_TEAM_HUB_URL=$TEAM_HUB_URL
AGENTSDOCK_TEAM_HUB_DIRECT_IP_URL=$TEAM_HUB_DIRECT_IP_URL
AGENTS_SERVER_INSTALL_DIR=$INSTALL_ROOT
PATH=$SERVER_PATH
EOF
  chmod 600 "$env_temp"
  mv "$env_temp" "$ENV_FILE"
  PRESERVE_SOURCE="$ENV_FILE"
}

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

restore_previous_release_transaction() {
  [[ -n "$OLD_TARGET" && -e "$OLD_TARGET" ]] || return 1
  if ! stop_service; then
    echo "The candidate service could not be stopped, so rollback was not attempted." >&2
    return 1
  fi
  if ! restore_team_hub_snapshot; then
    echo "The verified Team Hub snapshot could not be restored; the previous release was not started." >&2
    return 1
  fi
  if ! restore_release_links || ! restore_runtime_configuration; then
    echo "The Team Hub snapshot was restored, but the previous release configuration could not be restored." >&2
    return 1
  fi
  if ! restart_service; then
    echo "The previous release link and configuration were restored, but its service could not be restarted." >&2
    return 1
  fi
  if [[ "$TEAM_HUB_OPERATION_PENDING" == "true" ]]; then
    if ! wait_for_previous_release_health; then
      echo "The previous service restarted, but its exact server and Team Hub identities were not healthy; rollback is incomplete." >&2
      return 1
    fi
    # A verified offline restore consumes the exact operation fence together
    # with the candidate-mutated state.  Mark final only after the old process
    # proves its stable server and Hub identities.
    TEAM_HUB_OPERATION_FINALIZED="true"
  elif ! wait_for_health; then
    echo "The previous release restarted but did not become healthy." >&2
    return 1
  fi
  return 0
}

restore_previous_release() {
  TEAM_HUB_RECOVERY_ATTEMPTED="true"
  mask_install_signals
  local status=0
  if restore_previous_release_transaction; then
    status=0
  else
    status=$?
  fi
  if [[ "$IN_EXIT_CLEANUP" != "true" && ( "$TEAM_HUB_OPERATION_PENDING" != "true" || "$TEAM_HUB_OPERATION_FINALIZED" != "true" ) ]]; then
    resume_install_signals
  fi
  return "$status"
}

stop_service() {
  if [[ "$OS_NAME" == "Linux" ]]; then
    systemctl --user stop "$SERVICE_NAME.service"
    return
  fi
  local domain="gui/$(id -u)"
  local service_target="$domain/$LABEL"
  local output=""
  local status=0
  if ! launchctl print "$service_target" >/dev/null 2>&1; then
    return 0
  fi
  if output="$(launchctl bootout "$service_target" 2>&1)"; then
    [[ -z "$output" ]] || printf '%s\n' "$output"
  else
    status=$?
    if launchctl print "$service_target" >/dev/null 2>&1; then
      [[ -z "$output" ]] || printf '%s\n' "$output" >&2
      return "$status"
    fi
  fi
  wait_for_launch_agent_removal "$service_target"
}

restore_team_hub_snapshot() {
  [[ -n "$EXPECTED_TEAM_HUB_ID" ]] || return 0
  local restore_python="$RELEASE_DIR/.venv/bin/python"
  if [[ ! -x "$restore_python" ]]; then
    echo "The candidate runtime cannot verify the Team Hub rollback snapshot." >&2
    return 1
  fi
  echo "      Restoring the verified Team Hub maintenance snapshot"
  run_without_server_secrets env PYTHONPATH="$RELEASE_DIR" "$restore_python" -m agentsdock_team_hub.cli \
    restore-snapshot \
    --data-dir "$TEAM_HUB_DATA_DIR" \
    --snapshot "$TEAM_HUB_SNAPSHOT" \
    --expected-host-identity "$EXPECTED_SERVER_IDENTITY" \
    --expected-hub-id "$EXPECTED_TEAM_HUB_ID" \
    --expected-operation-id "$TEAM_HUB_OPERATION_ID"
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
    cat > "$SYSTEMD_SERVICE_FILE" <<EOF
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
    LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
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
    <key>AGENTSDOCK_STATE_DIR</key><string>$STATE_ROOT</string>
    <key>AGENTSDOCK_AGENT_CWD</key><string>$HOME</string>
    <key>AGENTSDOCK_AGENT_BIND</key><string>$BIND_ADDRESS</string>
    <key>AGENTSDOCK_AGENT_PORT</key><string>$PORT</string>
    <key>AGENTSDOCK_AGENT_TOKEN</key><string>$TOKEN</string>
    <key>AGENTSDOCK_TEAM_HUB_MODE</key><string>$TEAM_HUB_MODE</string>
    <key>AGENTSDOCK_TEAM_HUB_TRANSPORT</key><string>$TEAM_HUB_TRANSPORT</string>
    <key>AGENTSDOCK_TEAM_HUB_URL</key><string>$TEAM_HUB_URL</string>
    <key>AGENTSDOCK_TEAM_HUB_DIRECT_IP_URL</key><string>$TEAM_HUB_DIRECT_IP_URL</string>
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

release_health_check_once() {
  local port="$1"
  local runtime_root="$2"
  local expected_version="$3"
  local expected_server="$4"
  local expected_hub_mode="$5"
  local expected_hub="$6"
  local expected_hub_transport="$7"
  local expected_hub_url="$8"
  local allow_legacy_transport="${9:-false}"
  local response_file=""
  response_file="$(mktemp "$STATE_ROOT/admin/.install-health.XXXXXX")" || return 1
  chmod 600 "$response_file"
  if command -v curl >/dev/null 2>&1 && curl --version >/dev/null 2>&1; then
    if ! curl --fail --silent --show-error --connect-timeout 1 --max-time 2 --max-filesize 1048576 \
      -H "Authorization: Bearer $TOKEN" \
      --output "$response_file" \
      "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then
      rm -f "$response_file"
      return 1
    fi
  elif ! wget \
    --quiet \
    --timeout=2 \
    --tries=1 \
    --header="Authorization: Bearer $TOKEN" \
    --output-document="$response_file" \
    "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then
    rm -f "$response_file"
    return 1
  fi
  local response_size=""
  response_size="$(wc -c < "$response_file" 2>/dev/null || true)"
  response_size="${response_size//[[:space:]]/}"
  if [[ ! "$response_size" =~ ^[0-9]+$ ]] || ((response_size > 1048576)); then
    rm -f "$response_file"
    return 1
  fi
  local result=1
  if run_without_server_secrets "$runtime_root/.venv/bin/python" - \
    "$response_file" \
    "$expected_version" \
    "$expected_server" \
    "$expected_hub_mode" \
    "$expected_hub" \
    "$expected_hub_transport" \
    "$expected_hub_url" \
    "$TEAM_HUB_DIRECT_IP_URL" \
    "$allow_legacy_transport" <<'PY'
import json
import re
import sys

(
    path,
    expected_version,
    expected_server,
    hub_mode,
    expected_hub,
    expected_transport,
    expected_hub_url,
    expected_direct_ip_url,
    allow_legacy_transport,
) = sys.argv[1:]
try:
    with open(path, "rb") as stream:
        health = json.load(stream)
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit(1)
if not isinstance(health, dict) or health.get("ok") is not True:
    raise SystemExit(1)
if health.get("server_version") != expected_version:
    raise SystemExit(1)
server_identity = health.get("server_identity")
if not isinstance(server_identity, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{8,240}", server_identity) is None:
    raise SystemExit(1)
if expected_server and server_identity != expected_server:
    raise SystemExit(1)
capabilities = health.get("capabilities")
capability = capabilities.get("team_hub_v1") if isinstance(capabilities, dict) else None
if not isinstance(capability, dict):
    raise SystemExit(1)
if hub_mode == "host":
    required = {
        "available": True,
        "designated_host": True,
        "version": 1,
        "base_path": "/api/team-hub",
        "host_server_identity": server_identity,
    }
    if any(capability.get(key) != value for key, value in required.items()):
        raise SystemExit(1)
    hub_id = capability.get("hub_id")
    if not isinstance(hub_id, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{8,240}", hub_id) is None:
        raise SystemExit(1)
    if expected_hub and hub_id != expected_hub:
        raise SystemExit(1)
    transport = capability.get("transport")
    hub_url = capability.get("hub_url")
    if (
        allow_legacy_transport == "true"
        and transport is None
        and expected_transport == "loopback"
        and not expected_hub_url
    ):
        transport = "loopback"
    if transport != expected_transport or hub_url != (expected_hub_url or None):
        raise SystemExit(1)
    routes = capability.get("routes")
    expected_routes = [{
        "transport": expected_transport,
        "hub_url": expected_hub_url or None,
    }]
    if expected_direct_ip_url and expected_transport != "direct_ip":
        expected_routes.append({
            "transport": "direct_ip",
            "hub_url": expected_direct_ip_url,
        })
    if routes != expected_routes:
        if not (allow_legacy_transport == "true" and routes is None):
            raise SystemExit(1)
elif hub_mode == "disabled":
    required = {
        "available": False,
        "designated_host": False,
        "version": 1,
        "base_path": None,
        "hub_id": None,
        "host_server_identity": None,
        "transport": None,
        "hub_url": None,
    }
    if any(capability.get(key) != value for key, value in required.items()):
        raise SystemExit(1)
else:
    raise SystemExit(1)
secure_capability = capabilities.get("secure_peer_v1")
if secure_capability is None and allow_legacy_transport == "true":
    pass
else:
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
PY
  then
    result=0
  fi
  rm -f "$response_file"
  return "$result"
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

wait_for_exact_release_health() {
  local runtime_root="$1"
  local expected_version="$2"
  local expected_server="$3"
  local expected_hub_mode="$4"
  local expected_hub="$5"
  local expected_hub_transport="$6"
  local expected_hub_url="$7"
  local health_label="$8"
  local attempt_limit="${9:-$HEALTH_CHECK_ATTEMPTS}"
  local allow_legacy_transport="${10:-false}"
  local attempt
  for ((attempt = 1; attempt <= attempt_limit; attempt++)); do
    if release_health_check_once \
      "$PORT" \
      "$runtime_root" \
      "$expected_version" \
      "$expected_server" \
      "$expected_hub_mode" \
      "$expected_hub" \
      "$expected_hub_transport" \
      "$expected_hub_url" \
      "$allow_legacy_transport"; then
      return 0
    fi
    if ((attempt < attempt_limit)) && ((attempt % HEALTH_CHECK_HEARTBEAT_ATTEMPTS == 0)); then
      echo "      Still waiting for exact $health_label health (${attempt}s elapsed, timeout ${attempt_limit}s)"
    fi
    ((attempt == attempt_limit)) || sleep 1
  done
  return 1
}

wait_for_release_health() {
  wait_for_exact_release_health \
    "$RELEASE_DIR" \
    "$RELEASE_VERSION" \
    "$EXPECTED_SERVER_IDENTITY" \
    "$TEAM_HUB_MODE" \
    "$EXPECTED_TEAM_HUB_ID" \
    "$TEAM_HUB_TRANSPORT" \
    "$TEAM_HUB_URL" \
    "candidate release"
}

wait_for_previous_release_health() {
  local previous_version=""
  local rollback_attempts="$HEALTH_CHECK_ATTEMPTS"
  [[ -n "$OLD_TARGET" && -f "$OLD_TARGET/VERSION" ]] || {
    echo "The previous release version cannot be verified." >&2
    return 1
  }
  previous_version="$(tr -d '[:space:]' < "$OLD_TARGET/VERSION")"
  if [[ ! "$previous_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][A-Za-z0-9.-]+)?$ ]]; then
    echo "The previous release version is invalid." >&2
    return 1
  fi
  if ((rollback_attempts > ROLLBACK_HEALTH_CHECK_MAX_ATTEMPTS)); then
    rollback_attempts="$ROLLBACK_HEALTH_CHECK_MAX_ATTEMPTS"
  fi
  wait_for_exact_release_health \
    "$OLD_TARGET" \
    "$previous_version" \
    "$EXPECTED_SERVER_IDENTITY" \
    "host" \
    "$EXPECTED_TEAM_HUB_ID" \
    "$PREVIOUS_TEAM_HUB_TRANSPORT" \
    "$PREVIOUS_TEAM_HUB_URL" \
    "restored release" \
    "$rollback_attempts" \
    "true"
}

if [[ "$TEAM_HUB_OPERATION_PENDING" == "true" ]]; then
  if ! verify_team_hub_rollback_snapshot "$STAGE_DIR"; then
    echo "Candidate Team Hub rollback verifier rejected the exact snapshot before activation." >&2
    exit 1
  fi
fi

echo "[3/7] Activating release $RELEASE_VERSION"
CURRENT_LINK_WAS_DIRECTORY="false"
if [[ -L "$CURRENT_LINK" ]]; then
  OLD_TARGET="$(readlink "$CURRENT_LINK")"
  [[ "$OLD_TARGET" == /* ]] || OLD_TARGET="$INSTALL_ROOT/$OLD_TARGET"
elif [[ -d "$CURRENT_LINK" ]]; then
  CURRENT_LINK_WAS_DIRECTORY="true"
  OLD_TARGET="$RELEASES_ROOT/legacy-$(date -u +%Y%m%d%H%M%S)"
elif [[ -e "$CURRENT_LINK" ]]; then
  echo "$CURRENT_LINK is not a supported release link or directory." >&2
  exit 1
fi
if [[ "$TEAM_HUB_OPERATION_PENDING" == "true" && "$CURRENT_LINK_WAS_DIRECTORY" == "true" ]]; then
  echo "Managed Team Hub update requires a versioned current-release symlink; legacy directory takeover is not supported." >&2
  exit 1
fi
if [[ "$TEAM_HUB_OPERATION_PENDING" == "true" && ( -z "$OLD_TARGET" || ! -d "$OLD_TARGET" ) ]]; then
  echo "Managed Team Hub update requires an existing installed release for verified rollback." >&2
  exit 1
fi
if ! validate_managed_team_hub_inputs "$CURRENT_LINK"; then
  echo "Managed Team Hub inputs changed before candidate activation." >&2
  exit 1
fi

backup_runtime_configuration
RELEASE_ACTIVATED="true"
if [[ "$CURRENT_LINK_WAS_DIRECTORY" == "true" ]]; then
  mv "$CURRENT_LINK" "$OLD_TARGET"
fi
if [[ -d "$RELEASE_DIR" ]]; then
  if [[ -n "$OLD_TARGET" && "$OLD_TARGET" == "$RELEASE_DIR" ]]; then
    REPLACED_DIR="$RELEASES_ROOT/$RELEASE_VERSION-replaced-$(date -u +%Y%m%d%H%M%S)-$$"
    OLD_TARGET="$REPLACED_DIR"
    mv "$RELEASE_DIR" "$OLD_TARGET"
  else
    rm -rf "$RELEASE_DIR"
  fi
fi
mv "$STAGE_DIR" "$RELEASE_DIR"
if [[ -n "$OLD_TARGET" && -e "$OLD_TARGET" ]]; then
  ln -sfn "$OLD_TARGET" "$PREVIOUS_LINK"
fi
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"
write_runtime_env

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
  CANDIDATE_SERVICE_MAY_HAVE_STARTED="true"
  if ! restart_service; then
    echo "AgentsServer $RELEASE_VERSION could not start; restoring the previous service when possible." >&2
    if restore_previous_release; then
      echo "The previous release and service were restored." >&2
    fi
    exit 1
  fi

  echo "[5/7] Waiting for authenticated health"
  if wait_for_release_health; then
    if [[ "$TEAM_HUB_OPERATION_PENDING" == "true" ]]; then
      mask_install_signals
      if clear_team_hub_operation_fence "$RELEASE_DIR"; then
        TEAM_HUB_OPERATION_FINALIZED="true"
      else
        resume_install_signals
        echo "AgentsServer $RELEASE_VERSION became healthy but could not clear its exact Team Hub maintenance fence; rolling back." >&2
        if restore_previous_release; then
          echo "The previous release and service were restored." >&2
        fi
        exit 1
      fi
    fi
    break
  fi

  echo "AgentsServer $RELEASE_VERSION did not become healthy; rolling back." >&2
  PORT="$ORIGINAL_PORT"
  if restore_previous_release; then
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
if [[ "$TEAM_HUB_MODE" == "host" ]]; then
  echo
  if [[ "$TEAM_HUB_TRANSPORT" == "tailscale_serve" ]]; then
    echo "  ${COLOR_BOLD}Teamspace host${COLOR_RESET} $TEAM_HUB_URL"
    echo "  $CHECK_MARK server bound to the expected private Tailscale Serve URL"
    echo "  $DOT_MARK verify the separately managed Serve listener with: tailscale serve status --json"
  fi
  if [[ -n "$TEAM_HUB_DIRECT_IP_URL" ]]; then
    echo "  ${COLOR_BOLD}Teamspace Direct IP (unencrypted, advanced)${COLOR_RESET} $TEAM_HUB_DIRECT_IP_URL"
    echo "  $CROSS_MARK plaintext route: IP address is routing only, not identity or Tailscale attestation"
  fi
  echo "  ${COLOR_BOLD}Team Hub host operator commands${COLOR_RESET}"
  printf '  Bootstrap proof: PYTHONPATH='
  printf '%q ' "$CURRENT_LINK"
  printf '%q ' "$CURRENT_LINK/.venv/bin/python"
  printf '%s ' -m agentsdock_team_hub.cli bootstrap-proof --data-dir
  printf '%q\n' "$STATE_ROOT/team-hub"
  printf '  Device recovery: PYTHONPATH='
  printf '%q ' "$CURRENT_LINK"
  printf '%q ' "$CURRENT_LINK/.venv/bin/python"
  printf '%s ' -m agentsdock_team_hub.cli device-recovery --data-dir
  printf '%q ' "$STATE_ROOT/team-hub"
  printf '%s\n' '--email EMAIL --device-label LABEL'
fi
echo
if [[ -z "$EXPECTED_SERVER_IDENTITY" ]]; then
  printf 'AGENTSDOCK_SETUP_RESULT={"server_url":"%s","access_token":"%s","service":"%s","tailscale_ip":"%s","server_version":"%s"}\n' \
    "$SERVER_URL" "$TOKEN" "$SERVICE_KIND" "$TAILSCALE_IP" "$RELEASE_VERSION"
fi
