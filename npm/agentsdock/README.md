# agentsdock

Install and manage your AgentsDock server with one command name. This is the
public CLI entry point; the desktop app remains a separate signed download.
The same-version `@agentsdock/server` dependency supplies the runtime and the
existing signed update protocol. Existing scoped-package installations remain
supported.

## Install the command

Run these commands on the **server machine**, which can be the same computer as
your desktop app. Use Apple silicon macOS with a desktop login, or Linux with a
working user systemd session. Install [Node.js 22.14 or later with npm](https://nodejs.org/en/download)
and [uv](https://docs.astral.sh/uv/getting-started/installation/) for the Python
environment. Install **tmux** for persistent terminals and in-app managed
updates; basic chats work without it. On macOS with Homebrew, use
`brew install node uv tmux`. Run npm as the server owner without `sudo`.
Client-only devices do not need these prerequisites. The npm server installer
does not run natively on Windows.

```sh
npm install -g agentsdock
agentsdock list
agentsdock token default
```

On a fresh machine, the global install automatically sets up and starts the
default server using the existing installer and its defaults. There is no
mandatory second `setup` command. It first checks for existing installation or
state, including named instances; repeat installs leave these unchanged and do
not restart, overwrite or upgrade a running service. The runtime lives in its
managed installation directory, independently of the npm package/cache.

Wait for installation to finish, then confirm `default` is `running` in
`agentsdock list`. No npm account is needed to install the public package.
Add its URL and the token from `agentsdock token default` in the separately
downloaded [AgentsDock app](https://agentsdock.net/#downloads). Keep the token
private. Use the listed local URL on the same computer, a LAN URL on the same
network, or a Tailscale URL on your private tailnet. `127.0.0.1` cannot reach
the server from another device.

Install and sign in to your preferred provider CLI on the server before
starting a chat. npm setup does not install provider CLIs or authenticate them.

For a beta, select `npm install -g agentsdock@beta`. Stable and beta packages are
published on npm. Features explicitly marked as source-only below may require a
later package release.

`-g` makes the command available outside the current project when npm's global
bin directory is on your PATH. A local `npm install agentsdock` is also supported;
invoke it using `npx agentsdock`, not a bare shell command. Local/dependency
installs, linked source checkouts, CI installs and npx do not automatically start
a service. Use `npx agentsdock setup` when you deliberately want a server from a
local install. No installation changes shell configuration or logs into a provider.

`setup` (also `install`) refuses to overwrite an existing default installation
or history. Global auto setup uses the same prerequisites; it does not install
uv, Homebrew, provider CLIs or Tailscale automatically. If a prerequisite is
missing, install it and check it in the same terminal before retrying
`agentsdock setup`; if the CLI was not installed, rerun the npm install command.

The connection-output improvements in the next paragraph are source-only and
are not included in the published 1.0.10 or 1.0.11-beta.1 packages. With those
packages, use `agentsdock list` and `agentsdock token default` even if npm's
installation output does not show connection details.

With the improved output, a successful first setup shows the default server URL,
access token and a scannable terminal QR together, even when npm hides successful
lifecycle output. The QR includes reachable LAN/Tailscale addresses after
authenticated health checks. Keep the token and QR private. Raw installer output and access tokens
are not copied into npm's captured output/debug logs. Headless installations
without a controlling terminal show only a non-secret summary; use
`agentsdock token default` explicitly to retrieve a token later. Reinstalling
over existing server state remains a no-op, not a setup or restart.
`--foreground-scripts` can still help display non-secret lifecycle diagnostics.
A failed auto setup makes npm report failure instead of claiming the server is
ready. To obtain interactive diagnostics or choose a custom port/bind:

```sh
AGENTSDOCK_SKIP_SETUP=1 npm install -g agentsdock
agentsdock setup --port 7854 --bind 127.0.0.1
```

`--ignore-scripts` also disables auto setup, as required by npm; this package does
not bypass that setting. `setup` remains the explicit retry/custom-install path.

To show the connection details again, run `agentsdock token`: choose the server
and its URLs, token and QR appear together. Use `agentsdock token default` to skip
the chooser, or `agentsdock pair default` for just the addresses and QR. These
commands are read-only and require no source-checkout path. Interactive
`agentsdock setup` and `agentsdock new work` also show the connection details on
success. If the server is local-only or not reachable yet, setup still succeeds
and explains how to generate the QR after networking is ready.

## Manage servers

```sh
agentsdock list
agentsdock status
agentsdock new work --port 7854 --bind 127.0.0.1
agentsdock status work
agentsdock logs work
agentsdock token
agentsdock token work
agentsdock pair work
agentsdock restart work
agentsdock remove work
```

Flat commands are the primary interface. `servers ACTION` and `instances ACTION`
remain aliases, as do `new --name NAME` and `token/status --instance NAME`.
Start/stop/restart/remove require an
explicit instance or the native helper's explicit `--all` selector. Stop/restart
interrupt running work; wait for chats to finish first. Remove retains the
existing confirmation and history-preservation behavior. Tokens are private;
`token` only reads an existing token and does not reinstall or restart a server.
Without a name, `agentsdock token` lists this OS user's existing servers on the
machine (including stopped instances). Use Up/Down arrows to highlight a server
and Enter to select it; Esc, Ctrl+C or Ctrl+D cancels without showing a token.
The menu scrolls when needed and restores terminal input before showing a token.
It displays the selected server's connection URLs alongside its token, retaining
the optional clipboard prompt. Verified Tailscale addresses appear first;
local-only bindings are never advertised as reachable from another device.
Basic terminals without cursor control retain the numbered/name prompt, where
empty input cancels. Even a single server requires confirmation. In a script or with
redirected output, select explicitly: `agentsdock token default` or
`agentsdock token work`. Loopback binding is local-only; choose a reachable bind
explicitly if another device needs access.

`agentsdock status` prints a separate readable block for every instance: status,
addresses, installed version and port. In an interactive terminal it then asks
whether to view a token. Answer `y`, use Up/Down and Enter to select a server,
and its URL/token appear together. Declining or cancelling shows no token.
`agentsdock status work` limits both the summary and picker to that instance.
Redirected/non-interactive status never prompts or prints tokens; explicitly
named `agentsdock token work` still outputs only the token when redirected.
The status is the native service-manager state, not an authenticated health probe;
the version comes from that instance's installed runtime, not the npm CLI package.
`agentsdock info work` retains JSON output. If `setup` finds an existing default
installation, it leaves it unchanged and points to `agentsdock new` for another
instance (or `agentsdock new work --port 7854` for an explicit name and port).

Current acceptance gap: the native instance manager still validates the older
single-service layout. `remove default --yes` was observed refusing a current
split gateway/execution installation before making changes. Start/stop/restart
and removal for split layouts need guarded lifecycle integration and native
acceptance before this CLI can be advertised as complete instance management.
Do not bypass the binding check or stop only one process. Fresh auto setup and
safe repeated npm installation have been verified independently on Linux;
macOS first-service creation still needs disposable native acceptance.

The packaged CLI has also been installed and reinstalled on Apple silicon
macOS with existing services. Disposable named launchd instances passed real
creation (explicit and automatic name/port), token/authenticated-health,
start/stop/restart, uninstall cancellation and removal/name-release checks.
Saved test data remained in its private backup. This does not close the
fresh-default or split-service acceptance gaps above.

## Read service logs

This command is new in source and is not included in the published 1.0.10 or
1.0.11-beta.1 packages yet.

```sh
agentsdock logs                       # Default server: recent entries, then exit
agentsdock logs work                  # One named server; find names with agentsdock list
agentsdock logs work --lines 20       # -n 20 also works
agentsdock logs work --follow         # -f also works; Ctrl+C stops only this viewer
agentsdock logs work -n 0 -f           # Only entries arriving after viewing starts
```

Run on the server machine as its OS user; this is not a remote log-download API.
The default is 100 lines, with a range of 0–10,000. macOS shows separate stdout
and stderr sections (the last N lines per file, with a 1 MiB initial-read cap).
Split default installations include both gateway and worker logs. Linux uses
the current user's journal and shows the last N entries across the selected
instance's units, without a pager. Log sources follow the installed layout,
not the CLI's version. Missing/empty Mac log files are reported and watched if
following; unsafe paths fail rather than being followed. Nothing is restarted
or installed, and no token/configuration file is printed. Service log entries
can still contain private data; review them before sharing. macOS control
characters are escaped for terminal safety, not credential redaction.

## Connect a phone by QR

```sh
agentsdock pair                      # default instance
agentsdock pair work                 # terminal QR with Tailscale and LAN candidates
agentsdock pair work --network lan   # same Wi-Fi/LAN
agentsdock pair work --output connection.png
```

For URLs, the token and a terminal QR in one interactive display, use
`agentsdock token work`. Redirected token output remains token-only.

In a mobile build with pairing support, open **Servers > Add server > Scan QR code**,
allow camera access, and scan the terminal. Review the scanned server name and URL;
authenticated health and the QR's server identity must match before **Add** is
enabled. Tap **Add** to save and switch. A saved identity reuses the existing
profile. Manual entry remains available; image import and connection deep links
are not enabled on mobile.

Interactive setup and token display also show a connection QR when a reachable
address passes authenticated health. Fresh global npm installation writes the QR
only to the controlling terminal, never to captured npm logs. Headless setup emits
no QR. A missing camera-reachable address or failed QR generation does not fail
installation; use `agentsdock pair` once networking is ready. This requires the
updated server/CLI package and does not change existing published versions.

Automatic mode includes up to six discovered LAN/Tailscale addresses that pass
authenticated health with the same identity. Mobile tries these addresses and
retains alternatives for reconnect. `--url http://192.168.1.20:7851` restricts the
QR to one listed address; arbitrary URLs are rejected. Conflicting identities
fail closed. Older single-address QRs remain supported by updated mobile apps.

`qrcode` is an alias for `pair`. `--link` explicitly prints the credential-bearing
`agentsdock://connect#...` URL instead of a QR. Treat the link/image like the
server's access token. The command never contacts an online QR service and does
not rotate tokens, reinstall, restart, or change network settings. Saved PNGs are
owner-only and existing files are never overwritten.

Both devices must be on a reachable LAN or private Tailscale network. A server
bound only to localhost cannot connect to a physical phone; automatic selection
refuses that case. `--network local` is an explicit same-computer/simulator test
option. `--network tailscale` requires a confirmed Tailscale address and binding.
The QR includes the identity returned by authenticated health at the included URLs.

## Complete public command comparison

`work` is an example instance name; `default` selects the original instance.
These are server installation/administration commands, not the run-bound
mailbox/jobs/team tools supplied to agents inside an authorized chat.

| Purpose | Previous entry point | Short entry point |
| --- | --- | --- |
| Install the command and create a fresh default server | `npm install -g @agentsdock/server` then `agentsdock-server install` | `npm install -g agentsdock` (automatic first setup) |
| Explicit retry/custom default setup | `agentsdock-server install` / first `./install.sh` | `agentsdock setup` or `agentsdock install` |
| All instances and connection addresses | `./instances.sh list` | `agentsdock list` |
| All instance statuses, addresses, versions and ports | `./instances.sh status` | `agentsdock status` (separate blocks) |
| One instance's readable status | `./instances.sh status work` | `agentsdock status work` |
| One instance's details as JSON | `./instances.sh info work` | `agentsdock info work` |
| Recent service logs | Platform-specific `tail` / `journalctl` | `agentsdock logs [NAME]` |
| New automatic name/free port | `./instances.sh new` | `agentsdock new` |
| New named instance | `./instances.sh new --name work --port 7854` | `agentsdock new work --port 7854` |
| Start one | `./instances.sh start work` | `agentsdock start work` |
| Stop one | `./instances.sh stop work` | `agentsdock stop work` |
| Restart one | `./instances.sh restart work` | `agentsdock restart work` |
| Choose an existing server and read its token | `./instances.sh token` | `agentsdock token` |
| Scan a server connection | No previous short command | `agentsdock pair [NAME]` / `agentsdock qrcode [NAME]` |
| Read default token directly | `./install.sh --show-token` | `agentsdock token default` |
| Read named token | `./install.sh --instance work --show-token` | `agentsdock token work` |
| Uninstall one, preserve history | `./uninstall.sh --instance work` / `./instances.sh remove work` | `agentsdock remove work` or `agentsdock uninstall work` |
| Explicitly purge instance state | `./uninstall.sh --instance work --purge-state` | `agentsdock remove work --purge-state` |
| Start/stop/restart all | `./instances.sh restart --all` (or start/stop) | `agentsdock restart --all` (or start/stop) |
| Exclude an instance from a bulk action | `./instances.sh restart --all --exclude default` | `agentsdock restart --all --exclude default` |
| Uninstall all with confirmation | `./instances.sh remove --all` / bare `./uninstall.sh` | `agentsdock remove --all` |
| Submit a signed managed update | `agentsdock-server update` plus required descriptor/identity arguments | `agentsdock update` with the same arguments |
| Guarded pre-activation recovery | `agentsdock-server recover` | `agentsdock recover` |
| Installed CLI/package version | `agentsdock-server --version` | `agentsdock version` or `agentsdock --version` |
| CLI help | `agentsdock-server --help` | `agentsdock help` / `agentsdock --help` / `agentsdock -h` |

Supported options retain their native meanings:

- `setup`/`install`: `--port`, `--bind`, `--non-interactive`, `--dry-run`.
- `new`: optional positional name or `--name`, `--port`, `--bind`.
- `start`/`stop`/`restart`/`remove`: positional name or `--instance NAME`, or
  explicit `--all`; repeat `--exclude NAME` only with `--all`.
- `remove`/`uninstall`: also `--yes` and `--purge-state`. Purge retains the native
  interactive confirmation and cannot be made unattended with `--yes`.
- `token`/`status`: positional name or `--instance NAME`, never both.
- `logs`: optional name or `--instance NAME` (default: `default`), `-n`/`--lines`
  and `-f`/`--follow`. No bulk selector or arbitrary log path.
- `update`: the six required arguments in the example below. Bare `update` or
  `update work` does not select a registry tag or silently restart a server.

Differences from the old source scripts:

- `setup`/`install` is fresh-default-only. Re-running `./install.sh` on an
  installed service could reinstall it; the public CLI instead refuses that
  path and keeps signed managed updates separate.
- Bare `remove`/`uninstall` refuses an omitted target. Unlike bare
  `./uninstall.sh`, it never implies removing all instances.
- `version` is the installed CLI/package version, not a remote or already-running
  server's health/version result. `info` reads the selected instance's metadata.
- Source-only operations are **not yet exposed** as short commands: rebinding or
  changing the port of an existing installation; manifest-based bulk creation
  (`./instances.sh install --manifest ...`); Team Hub configuration/reactivation;
  foreground development `python agent_server.py serve`. There are no implemented
  `agentsdock configure`, `agentsdock login`, or `agentsdock serve`
  commands in this package.
- Direct checkout reinstalls (`./instances.sh update`, installer's version or
  custom-root overrides) are not aliases for the signed `update` command. Split
  runtime preparation/activation and internal recovery flags remain under the
  managed updater, rather than arbitrary public pass-through arguments.
- Provider login remains in each provider's native CLI. Existing source scripts
  and the `agentsdock-server` command remain available for their original uses.

## Updates and recovery

Prefer the app's update control. `agentsdock update` accepts the same signed
descriptor, identity and token-file arguments as `agentsdock-server update`:

```sh
agentsdock update --server-url https://server.example \
  --server-identity ID --server-instance-id INSTANCE \
  --token-file /private/path/server-token \
  --manifest agents-server-npm-manifest.json \
  --signature agents-server-npm-manifest.sig
```

`agentsdock recover` retains the narrowly scoped existing pre-activation recovery
checks. The CLI does not turn `servers update` or arbitrary installer arguments
into an unsigned update bypass. Updating this npm command does not itself update
an already-running server; managed activation remains independent of npm caches.

## Preparing this package

From the canonical repository root:

```sh
python3 server/scripts/package_agentsdock_cli.py --output dist/agentsdock-cli
```

This stamps the same committed `server/VERSION`, pins the exact scoped dependency,
and produces a tarball and checksum receipt without publishing or installing.
Test the tarball with that exact server package before publication. Release
automation must publish/verify the scoped runtime first, then this CLI, with the
same stable/beta channel and appropriate trusted-publisher ownership. Existing
server manifests, signing keys and previously frozen candidates are unchanged.

The canonical `server-npm-publish.yml` now prepares both packages and publishes
the verified runtime before this CLI, with exact source/version/hash checks and
explicit stable-tag preservation. See the
[matched release procedure](../../../docs/NPM_CLI_RELEASES.md) for private staging,
first-publication authentication and required native acceptance. A first beta
uses `agentsdock@beta`; it does not create a stable unscoped `latest` tag.
