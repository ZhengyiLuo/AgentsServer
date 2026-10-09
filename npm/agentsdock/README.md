# agentsdock

Install and manage your AgentsDock server with one command name. This is the
public CLI entry point; the desktop app remains a separate signed download.
The same-version `@agentsdock/server` dependency supplies the runtime and the
existing signed update protocol. Existing scoped-package installations remain
supported.

## Install the command

```sh
npm install -g agentsdock@beta
agentsdock list
```

On a fresh machine, the global install automatically sets up and starts the
default server using the existing installer and its defaults. There is no
mandatory second `setup` command. It first checks for existing installation or
state, including named instances; repeat installs leave these unchanged and do
not restart, overwrite or upgrade a running service. The runtime lives in its
managed installation directory, independently of the npm package/cache.

The first short-command release is on Beta. Use the explicit `@beta` above;
untagged `npm install -g agentsdock` is for a future approved stable CLI release.
This documentation describes the package being prepared; commands are available
from the registry only after that package/channel has been published. Do not
infer name ownership from an npm 404 response.

`-g` makes the command available outside the current project when npm's global
bin directory is on your PATH. A local `npm install agentsdock` is also supported;
invoke it using `npx agentsdock`, not a bare shell command. Local/dependency
installs, linked source checkouts, CI installs and npx do not automatically start
a service. Use `npx agentsdock setup` when you deliberately want a server from a
local install. No installation changes shell configuration or logs into a provider.

Use Node >=22.14, `uv`, and Apple silicon macOS with a desktop login or Linux with
a working user systemd session. Run as the server owner without `sudo`. Install
and authenticate your preferred provider CLI separately. `setup` (also `install`)
refuses to overwrite an existing default installation or history. Global auto
setup uses the same `uv` and native service prerequisites; it does not install
Homebrew, provider CLIs or Tailscale without a separate user action.

The npm lifecycle may hide successful script output. Add `--foreground-scripts`
to the npm command if you want to see its status messages. Automatic setup never
copies raw installer output or access tokens into npm logs. Use `agentsdock list`
for connection addresses and `agentsdock token` to view/copy the token afterward.
A failed auto setup makes npm report failure instead of claiming the server is
ready. To obtain interactive diagnostics or choose a custom port/bind:

```sh
AGENTSDOCK_SKIP_SETUP=1 npm install -g agentsdock@beta
agentsdock setup --port 7854 --bind 127.0.0.1
```

`--ignore-scripts` also disables auto setup, as required by npm; this package does
not bypass that setting. `setup` remains the explicit retry/custom-install path.

Before a new setup or local instance-management helper runs, npm lifecycle
`node_modules/.bin` paths, npm's disposable `_npx` cache paths and npm node-gyp
shims are removed from the inherited PATH. Ordered absolute user, provider and
Node locations remain available; empty/relative entries are not persisted.
This lexical filter does not resolve symlink aliases or authenticate executables.
It prevents new temporary npm-path dependencies; it does not rewrite the saved
PATH of an already-installed server or change managed-update policy.

## Manage servers

```sh
agentsdock list
agentsdock status
agentsdock new work --port 7854 --bind 127.0.0.1
agentsdock status work
agentsdock token
agentsdock token work
agentsdock restart work
agentsdock remove work
```

Flat commands are the primary interface. `servers ACTION` and `instances ACTION`
remain aliases, as do `new --name NAME` and `token/status --instance NAME`.
Start/stop/restart/remove require an
explicit instance or the native helper's explicit `--all` selector. Legacy and
named-instance stop/restart can interrupt running work; wait for chats to finish
first. The split default server requires verified idle admission before stopping
either component. Remove retains the
existing confirmation and history-preservation behavior. Tokens are private;
`token` only reads an existing token and does not reinstall or restart a server.
Without a name, `agentsdock token` lists this OS user's existing servers on the
machine (including stopped instances). Use Up/Down arrows to highlight a server
and Enter to select it; Esc, Ctrl+C or Ctrl+D cancels without showing a token.
The menu scrolls when needed and restores terminal input before showing a token.
It displays only the selected token, retaining the optional clipboard prompt.
Basic terminals without cursor control retain the numbered/name prompt, where
empty input cancels. Even a single server requires confirmation. In a script or with
redirected output, select explicitly: `agentsdock token default` or
`agentsdock token work`. Loopback binding is local-only; choose a reachable bind
explicitly if another device needs access.

`agentsdock status` prints a separate readable block for every instance: status,
addresses, installed version and port. `agentsdock status work` selects just one.
The status is the native service-manager state, not an authenticated health probe;
the version comes from that instance's installed runtime, not the npm CLI package.
`agentsdock info work` retains JSON output. If `setup` finds an existing default
installation, it leaves it unchanged and points to `agentsdock new` for another
instance (or `agentsdock new work --port 7854` for an explicit name and port).

Beta.5 preparation adds guarded control of both default-server components.
Stop/restart requires an installed worker that supports the paired lifecycle
protocol; installing a newer CLI alone does not update an older server. If the
command reports that requirement, update the installed server through the app's
signed managed-update path first. It leaves unsupported running services intact.
Busy work, pending updates, changed native registrations or uncertain ownership
prevent a forced stop. Interrupted operations retain an owned retry journal;
follow the command's retry guidance rather than deleting it manually.

Exact-package native setup, lifecycle and removal acceptance for beta.5 remains
pending. Source/fixture tests are not proof of fresh macOS or Linux service
operation. Earlier Linux auto-setup results do not substitute for the new pair.

The packaged CLI has also been installed and reinstalled on Apple silicon
macOS with existing services. Disposable named launchd instances passed real
creation (explicit and automatic name/port), token/authenticated-health,
start/stop/restart, uninstall cancellation and removal/name-release checks.
Saved test data remained in its private backup. This does not close the
fresh-default or split-service acceptance gaps above.

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
| New automatic name/free port | `./instances.sh new` | `agentsdock new` |
| New named instance | `./instances.sh new --name work --port 7854` | `agentsdock new work --port 7854` |
| Start one | `./instances.sh start work` | `agentsdock start work` |
| Stop one | `./instances.sh stop work` | `agentsdock stop work` |
| Restart one | `./instances.sh restart work` | `agentsdock restart work` |
| Choose an existing server and read its token | `./instances.sh token` | `agentsdock token` |
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
  `agentsdock configure`, `agentsdock logs`, `agentsdock login`, or `agentsdock serve`
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
