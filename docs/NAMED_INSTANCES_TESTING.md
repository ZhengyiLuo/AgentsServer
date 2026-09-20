# Named-instance local acceptance

Branch: `feature/named-server-instances`, based on main `7aaa74b`.

No existing installation must be replaced to try this feature. In particular,
the original server on 7850 can remain running throughout the trial.

From this feature checkout:

```bash
./instances.sh list
./instances.sh new --name local-test
./instances.sh info local-test
./install.sh --instance local-test --show-token
```

Add the printed **local-test** URL and token as another connection in AgentsDock.
Verify that it starts with no AgentsDock chats or paired connections. Native
provider login/history may still be visible: same-user CLI profiles are shared,
not copied or isolated by this feature. Do not import a provider session that
is actively running in the default server during this first trial.
Leave optional Team Network hosting disabled for the trial. Enabling it later
requires a separate unused secure-peer port as well as the main HTTP port.

Test a new chat, then `./instances.sh restart local-test`: its own history should
remain. The default server should stay running on 7850 and its token should
still work. `./instances.sh update local-test` reinstalls the code from the
checkout containing that command; it does not fetch a newer release. The normal
in-app signed updater is still the route for published-version upgrades.

## Import exclusion across instances

Open Import Chat in the new instance. Conversations already present in another
installed instance on this machine should be absent, including archived chats
and chats in stopped services. No other instance's transcripts, configuration,
or history are modified. A fresh chat without a provider ID needs no cross-
instance scan. This only covers installed/discoverable instances for this OS user.

For a safe two-way test, use two disposable named instances running this branch,
not the default server. Import one unused provider conversation into test A,
then refresh Import Chat on test B: that conversation should disappear. A stale
selection or manually pasting its ID on B must be rejected as already in use.
Restart or stop A: its conversation must remain excluded on B. Delete the test
wrapper on A (not its native transcript) or uninstall A while preserving history:
the conversation becomes available after refreshing B.

The old default server does not need restarting for a new server to exclude its
saved chats. However, an old server's own picker will not gain the new behavior
until updated. Simultaneous-import protection requires both servers to run the
new code. Existing duplicates are left untouched.

To remove **only the new test service**, preserving its history:

```bash
./uninstall.sh --instance local-test
```

The preview must name only `local-test`; type `uninstall local-test` only if it does.
After the service is removed, answer **No/Enter** to the release-name question to
keep its history. Answer **Yes** only if you want to permanently delete that
instance's saved AgentsDock data and free its name. The default instance, original
provider chats, project files and earlier backups must remain untouched.
Do not approve the bare `./uninstall.sh` or `--all` preview during this trial:
those intentionally include the default instance. For bulk management without
the original server, use `--all --exclude default`.

To start fresh using that removed test name, run:

```bash
./instances.sh new --name local-test --port 7851
```

Answer `y` or `yes` only if you want an empty AgentsDock instance. The old
instance's data is moved to a private backup whose path is printed; original
provider chats/project files are untouched. Enter cancels without moving history.
To retain the existing AgentsDock history instead, use
`./install.sh --instance local-test --port 7851`. Never use `default` for this test.
Normal Python bytecode caches in this checkout no longer block installation.

## Automated coverage

`tests/test_server_instances.py` uses temporary homes and fake services for:

- Names, fixed disjoint roots, private registry and concurrent-operation guards.
- Port allocation, conflicts, manifests and partial failures.
- Default discovery without migration; no credentials in listing output.
- Instance-bound launchd/systemd operations, runtime config and update context.
- Named install and uninstall preserving a synthetic default-on-7850 fixture.
- Separate terminal names/sockets and exclusive state-directory ownership.
- Exact-name confirmation, color warning, cancellation and guarded history purge.
- Refusal to update a named server with an old default-only release.

The existing activation, service-state, health-security, stage-cleanup, update,
release-manifest and terminal suites provide backward-compatibility checks.
The complete existing installer suite includes longer Team Hub recovery cases;
passing the focused tests is not a claim that the entire repository suite ran.

`tests/test_cross_instance_import.py` covers ownership filtering, stale/manual
imports, parked IDs, stopped/removed instances, unsafe indexes and cancellation
using synthetic homes only.

An opt-in real-process smoke test starts two servers with temporary HOME/config/
state and ephemeral loopback ports. It verifies distinct identities, token
rejection across servers, state-lock rejection, independent shutdown, two-way
import exclusion and concurrent imports committing to only one server:

```bash
AGENTSDOCK_RUN_INSTANCE_SMOKE=1 PYTHONDONTWRITEBYTECODE=1 \
  python -m unittest tests.test_server_instances_smoke
```

It never calls an installer/service manager or targets 7850. Normal CI skips
this opt-in test. Real launchd/systemd installation and a phone connection still
need operator acceptance; mocked service tests are not a substitute for those.
