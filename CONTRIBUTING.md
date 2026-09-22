# Contributing to AgentsServer

This repository contains the deployable server. Client changes belong in
[AgentsDock](https://github.com/ZhengyiLuo/AgentsDock).

## Development and tests

Use Python 3.13, matching the release and pull-request workflows. Deployable
modules live at the repository root. Python tests are moving into `tests/`;
some test modules temporarily remain at the root. The shard runner covers both
locations. Use one shard to run the complete suite locally:

```bash
uv sync --locked --python 3.13
ulimit -s "$(ulimit -Hs)"
PYTHONDONTWRITEBYTECODE=1 uv run --python 3.13 python scripts/run_test_shard.py --index 0 --count 1
```

Run one test module with its package name:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --python 3.13 python -m unittest tests.test_provider_history_sync -v
```

Tests that import the server must use temporary state/configuration and a
synthetic home; do not point them at installed services or real chat history.
The named-instance [acceptance guide](docs/NAMED_INSTANCES_TESTING.md) describes
the opt-in isolated two-server smoke test and safe manual verification.

Pull requests to `main` run the lock check, test-file compilation, and full Python
suite in [Server CI](.github/workflows/server-ci.yml). This workflow does not
publish releases. Tests are not included in release archives.

## Documentation

Keep the README focused on installation and basic use. Detailed API contracts,
security invariants, compatibility notes, and verification records belong in
versioned `docs/` files. Existing notes describe their stated version or test
scope; they are not a guarantee that a feature is in every published release.

Useful references include [chat mailbox](docs/CHAT_MAILBOX.md),
[async routes](docs/ASYNC_CHAT_ROUTES.md), and
[OpenCode verification](docs/OPENCODE_READINESS_2026-09-11.md).

## Before opening a pull request

- Run relevant tests and explain any skipped or environment-dependent checks.
- Keep changes focused; do not deploy or restart an existing service as part of
  a test run.
- Never commit chat state, uploads, tokens, `.env` files, private hostnames,
  personal machine paths, or compiled caches.
- Review `git diff --check` and include user-facing behavior and compatibility
  notes in the PR description.
