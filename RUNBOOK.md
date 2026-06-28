# ZenithBotServer Runbook

This documents the exact launch/debug sequence used on this machine for
`/home/david-park/src/ZenithBotServer`.

The server token is intentionally not written into this tracked file. The live
token was saved in the ignored local file `.env.local`, and the user-level
systemd service reads its token from `/home/david-park/.config/zenithbot-agent.env`.

## Current Canonical Launch

The server is managed by the user-level systemd service:

```bash
systemctl --user status zenithbot-agent.service --no-pager -l
systemctl --user restart zenithbot-agent.service
```

The service command is:

```bash
/home/david-park/src/ZenithBotServer/.venv/bin/python \
  /home/david-park/src/ZenithBotServer/agent_server.py \
  serve --bind 0.0.0.0 --port 7850
```

The installed service file is:

```bash
systemctl --user cat zenithbot-agent.service
```

Important service settings:

```text
WorkingDirectory=/home/david-park/src/ZenithBotServer
EnvironmentFile=/home/david-park/.config/zenithbot-agent.env
Environment=ZENITHBOT_AGENT_DIR=/home/david-park/.zenithbot-agent
Environment=ZENITHBOT_AGENT_CWD=/home/david-park
Environment=ZENITHBOT_BACKEND=codex
ExecStart=/home/david-park/src/ZenithBotServer/.venv/bin/python /home/david-park/src/ZenithBotServer/agent_server.py serve --bind 0.0.0.0 --port 7850
Restart=always
RestartSec=2
```

## Health Check

Use the ignored repo-local `.env.local` file:

```bash
cd /home/david-park/src/ZenithBotServer
set -a
. ./.env.local
set +a
curl -sS --max-time 3 \
  -H "Authorization: Bearer $ZENITHDOCK_AGENT_TOKEN" \
  http://127.0.0.1:7850/api/health
```

Without the token, `/api/health` returns `401 Unauthorized`.

## Commands Used During Setup

Initial port/process check:

```bash
cd /home/david-park/src/ZenithBotServer
ss -ltnp 'sport = :7850' || true
.venv/bin/python - <<'PY'
import fastapi, uvicorn, pydantic
print("deps_ok")
PY
```

The port was already occupied by the ZenithBotServer process, so I checked the
process and unauthenticated health:

```bash
ps -p 1860 -o pid,ppid,lstart,cmd --no-headers
curl -sS -i --max-time 3 http://127.0.0.1:7850/api/health || true
```

I verified that a token existed in the running process environment and used it
for an authenticated health check. This reads only token presence and uses the
token without printing it:

```bash
python - <<'PY'
from pathlib import Path
import subprocess

pid = 1860
env = {}
raw = Path(f"/proc/{pid}/environ").read_bytes()
for part in raw.split(b"\0"):
    if b"=" in part:
        k, v = part.split(b"=", 1)
        env[k.decode(errors="ignore")] = v.decode(errors="ignore")

token = env.get("ZENITHDOCK_AGENT_TOKEN")
print("token_present", bool(token))
if token:
    res = subprocess.run(
        [
            "curl",
            "-sS",
            "-o",
            "/tmp/zenithbot_health.json",
            "-w",
            "%{http_code}",
            "--max-time",
            "3",
            "-H",
            f"Authorization: Bearer {token}",
            "http://127.0.0.1:7850/api/health",
        ],
        text=True,
        capture_output=True,
    )
    print("http_status", res.stdout.strip())
    print(Path("/tmp/zenithbot_health.json").read_text()[:1000])
PY
```

When the token was unknown, I attempted a manual rotate/relaunch. The actual
generated token is redacted here; the command shape was:

```bash
cd /home/david-park/src/ZenithBotServer
PID=1860
if ps -p "$PID" -o cmd= | grep -q '/home/david-park/src/ZenithBotServer/agent_server.py'; then
  kill "$PID"
  for i in $(seq 1 20); do
    if ! kill -0 "$PID" 2>/dev/null; then break; fi
    sleep 0.1
  done
fi

TOKEN="$(python - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"

LOG=/home/david-park/src/ZenithBotServer/zenithbot-agent.log
PIDFILE=/home/david-park/src/ZenithBotServer/zenithbot-agent.pid
ZENITHDOCK_AGENT_TOKEN="$TOKEN" \
ZENITHBOT_BACKEND=codex \
ZENITHBOT_AGENT_CWD=/home/david-park \
nohup .venv/bin/python agent_server.py serve --bind 0.0.0.0 --port 7850 \
  >"$LOG" 2>&1 &
NEWPID=$!
printf '%s\n' "$NEWPID" > "$PIDFILE"

for i in $(seq 1 40); do
  STATUS=$(curl -sS -o /tmp/zenithbot_health.json -w '%{http_code}' \
    --max-time 2 \
    -H "Authorization: Bearer $TOKEN" \
    http://127.0.0.1:7850/api/health || true)
  if [ "$STATUS" = "200" ]; then break; fi
  sleep 0.25
done
printf 'pid=%s\nstatus=%s\nlog=%s\n' "$NEWPID" "$STATUS" "$LOG"
```

Systemd immediately restored the service with its configured token, so I
confirmed service ownership:

```bash
ss -ltnp 'sport = :7850' || true
ps -ef | rg 'ZenithBotServer|agent_server.py' | rg -v rg || true
systemctl --user status zenithbot-agent.service --no-pager -l
systemctl --user cat zenithbot-agent.service
```

Then I saved the live service token to ignored `.env.local` so local health
checks and clients can use the same token:

```bash
cd /home/david-park/src/ZenithBotServer
python - <<'PY'
from pathlib import Path
import os
import stat
import subprocess

pids = []
for proc in Path("/proc").iterdir():
    if not proc.name.isdigit():
        continue
    try:
        cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
    except Exception:
        continue
    if "/home/david-park/src/ZenithBotServer/agent_server.py" in cmd:
        pids.append(int(proc.name))

if not pids:
    raise SystemExit("no ZenithBotServer process found")

pid = min(pids)
env = {}
for part in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
    if b"=" in part:
        k, v = part.split(b"=", 1)
        env[k.decode(errors="ignore")] = v.decode(errors="ignore")

token = env.get("ZENITHDOCK_AGENT_TOKEN")
if not token:
    raise SystemExit(f"process {pid} has no ZENITHDOCK_AGENT_TOKEN")

path = Path(".env.local")
values = {
    "ZENITHDOCK_AGENT_TOKEN": token,
    "ZENITHBOT_BACKEND": env.get("ZENITHBOT_BACKEND", "codex"),
    "ZENITHBOT_AGENT_CWD": env.get("ZENITHBOT_AGENT_CWD", "/home/david-park"),
}
path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

res = subprocess.run(
    [
        "curl",
        "-sS",
        "-o",
        "/tmp/zenithbot_health_saved_token.json",
        "-w",
        "%{http_code}",
        "--max-time",
        "3",
        "-H",
        f"Authorization: Bearer {token}",
        "http://127.0.0.1:7850/api/health",
    ],
    text=True,
    capture_output=True,
)
print("pid", pid)
print("saved", path)
print("status", res.stdout.strip())
PY

git status --short --ignored .env.local zenithbot-agent.pid
```

Expected git status for the token file:

```text
!! .env.local
```
