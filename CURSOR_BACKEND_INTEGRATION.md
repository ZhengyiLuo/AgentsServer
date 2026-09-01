# Cursor backend contract

This document describes the production Cursor backend contract introduced in
the AgentsServer `0.1.26-beta.12` release line. Cursor is an optional third
backend alongside Claude and Codex. The server contract is versioned as
`capabilities.cursor_backend.version = 2`.

## Availability and executable discovery

Clients must require both signals before showing Cursor as selectable:

- `GET /api/health`: `capabilities.cursor_backend.available == true` and
  `version == 2` mean this server understands the hardened Cursor contract.
- `GET /api/runtime/catalog`: `backends.cursor.available == true` means a
  compatible, authenticated CLI is ready on this host.

The server prefers `CURSOR_BIN` when explicitly configured. Otherwise it
checks `cursor-agent` and then `agent`, choosing the first *compatible*
candidate rather than the first installed file. Compatibility requires print
mode, `stream-json`, resume, model selection, `--trust`, `--force`, plan mode,
and model listing. A legacy installed binary is reported as incompatible and
cannot shadow a later compatible candidate. The compatibility-probed absolute
path is fenced into the admitted turn and reused for catalog/spawn.

No CLI is installed or upgraded automatically. Runtime diagnostics name the
resolved executable in login/update actions while keeping the internal path
out of public payloads.

## Session and permission contract

The existing create, update, list, detail, turn, queue, stop, and fork APIs
accept `backend = "cursor"`. Session detail responses include
`cursor_session_id` and the generic active `session_id`; list summaries omit
provider IDs and include `backend_locked`.

`cursor_permission_mode` has exactly three accepted values:

| Value | CLI flags | User-visible meaning |
| --- | --- | --- |
| `default` | `--trust` | Workspace reads/edits; shell commands are rejected |
| `full_access` | `--trust --force` | Full command access |
| `plan` | `--mode plan` | Plan only |

`auto_review` is intentionally neither advertised nor accepted: Cursor can
request interactive approval in that mode, while this headless runner has no
approval bridge. Persisted legacy/unknown values are normalized and saved as
`default` during store load; runtime evaluation also fails safe to `default`.
Permission labels describe behavior captured against Cursor CLI
`2026.08.11-e8db854`; they are not an AgentsServer sandbox or security
boundary. Help-text compatibility proves flag availability, while Cursor
continues to own the enforcement semantics.

## Runtime behavior

Each turn launches the exact admitted executable in print mode with
`--output-format stream-json`. The runner:

- drains stderr concurrently with a bounded retained tail;
- enforces startup, idle, absolute wall-clock, and post-terminal-exit limits;
- treats unknown schema, approval requests, missing init/result events,
  logical `is_error`, nonzero exits, and timeout/stop conditions explicitly;
- bounds assistant/reasoning/tool payloads and aggregate in-memory tool/text
  state;
- emits an empty final result plus `is_error = true` on logical failure so
  jobs, digests, and cross-chat finalizers cannot misclassify partial output;
- preserves normal AgentsDock stop, queue, artifact, diff, and lifecycle
  events.

Contract v2 additionally launches Cursor through a small process guard. The
guard isolates the CLI in its own process group, forwards stop signals, closes
stdin, and ensures descendant processes are reaped. The server also emits a
bounded idle warning before enforcing the idle timeout, so a genuinely stalled
CLI cannot leave a chat permanently busy.

The provider session identifier is bounded/validated before persistence or
resume. It is stored when the CLI emits a valid initialization event, even if
that turn later fails, so a retry can resume the provider-created session.
Instruction hashes and fork-memory consumption still wait for a successful
terminal result. Later turns use `--resume`.

### Prompt instruction limitation

Cursor print mode has no independently verified system/developer instruction
channel in the compatible CLI build used for this beta. AgentsServer therefore
places its stable provider prelude, session system prompt, optional fork
memory, and current user prompt in one same-role prompt envelope. Health
advertises `instruction_channel = "prompt_envelope"` and
`trusted_system_instruction_channel = false`.

The exact compatible CLI build was verified with a positional prompt, resume,
and `stream-json`; it was not available on the release host to prove equivalent
stdin semantics. For this beta the composed prompt is therefore present in
the spawned process argv (persisted/broadcast argv is redacted). This is a
documented beta limitation; do not replace it with an unverified stdin or
temporary-file transport. Sensitive helper actions remain constrained by the
server-issued per-turn authority file and ACLs.

Instruction hashes and fork-memory consumption are committed only after a
recognized successful terminal event. A stopped/failed turn reinjects them on
retry. Stable instructions are not duplicated on an ordinary successful
resume.

## Forks, resume, and scheduled jobs

- Resume by `provider_session_id`, `session_id`, or `cursor_session_id` is
  supported and uses `--resume`. Cursor local transcript import into the
  AgentsDock timeline is not supported; clients should use
  `import_history = false` and describe this as provider-context resume.
- A Cursor fork never inherits the provider ID. It preserves the permission
  mode and receives bounded memory once on its first successful turn.
- `provider_context_mode = "standalone"` is supported for scheduled/manual
  jobs, including a one-off Cursor backend override from a Claude/Codex parent.
  Provider identity, instruction hash, and memory-consumption state remain
  isolated from the parent chat.

## Cross-chat and digest compatibility

Cursor can be a route-granted *source* and can use server ACL helper commands
to contact supported Claude/Codex targets. Cursor is not advertised in
`cross_chat_handoffs_v1.supported_target_backends` for direct handoff delivery
because it lacks a trusted instruction channel.

The older context-digest workflow is provider-neutral and supports Cursor as
source or target: generation and delivery are ordinary turns in the selected
chats. Logical Cursor failures leave the digest job failed rather than sent.

## Packaging and validation

`cursor_agent_client.py` and `cursor_process_guard.py` are required runtime
files in all three transports:

- `scripts/package_release.py` archive packaging;
- `install.sh` archive/staging install and import/compile validation;
- `deploy.sh` direct deploy and remote import/compile validation.

Regression tests cover the parser/command contract, executable diagnostics,
packaging parity, runner lifecycle/failure/bounds, resume/fork/standalone
isolation, and scheduled-job failure projection. Run tests with
`PYTHONDONTWRITEBYTECODE=1` so installer source-tree checks are not polluted by
test-created `__pycache__` directories.
