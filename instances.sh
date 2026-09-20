#!/usr/bin/env bash
set -euo pipefail

# No downloads or package installation just to list/manage existing instances.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for instance_python in "$SCRIPT_DIR/.venv/bin/python" \
  "$HOME/.local/share/agents-server/current/.venv/bin/python" \
  "$(command -v python3 || true)"; do
  if [[ -n "$instance_python" && -x "$instance_python" ]] \
    && "$instance_python" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    exec "$instance_python" "$SCRIPT_DIR/server_instances.py" "$@"
  fi
done
echo "Instance management needs Python 3.10+; run ./install.sh first or install Python." >&2
exit 1
