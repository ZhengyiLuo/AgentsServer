# AgentsServer 1.0.0

## What this release includes

This is the stable upgrade from **AgentsServer 0.1.25**, the previous published
stable release. It brings together the subsequent 0.1.26 and 1.0 beta work:
durable chat-to-chat messaging, expanded Team Network collaboration, browser
chat sharing, native goal steering, more accurate history, and safer recovery
and updates. Existing capabilities such as Team Hub hosting, secure pairing,
scheduled jobs, provider history import and Codex goals have been extended and
hardened; they are not all new features of this release.

**Already on 1.0.0-beta.8?** This stable replacement additionally repairs
source-proven cross-chat delivery wrappers imported as user messages, and keeps
ordinary Codex parents supervised while their subagents finish. History repair
uses the saved delivery receipt and exact native provider-turn identity,
preserving the original agent message and genuine user input. There is no additional API,
dependency, signing-key or storage-schema change from beta.8.

**Replacing the original 1.0.0:** the version number is intentionally unchanged.
Already-installed 1.0.0 servers will not discover this as a newer version;
explicitly reinstall the verified replacement package to receive this repair.
Installing only the desktop replacement does not apply a server history fix.

- [Previous stable release: 0.1.25](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v0.1.25)
- [Full implementation comparison through beta.8](https://github.com/ZhengyiLuo/AgentsServer/compare/v0.1.25...v1.0.0-beta.8)
- [Changes included in beta.8](https://github.com/ZhengyiLuo/AgentsServer/blob/v1.0.0/docs/RELEASE_1.0.0-beta.8.md)

## Chat-to-chat messaging that survives reconnects

- Explicit, accepted structured `@Chat` references can establish an exact
  bidirectional permission pair. Later authorized runs can discover and use
  that pair without another mention. Permissions do not spread to other chats,
  the recipient's other routes, or forks.
- Negotiated asynchronous pair messages use a durable per-chat mailbox. Send
  returns a message identity after acceptance; a reply is a separate explicit
  message, not an automatically forwarded final answer or an obligation to reply.
- Agents can discover unread senders and read ordered, paged batches with exact
  message IDs, original timestamps and reply relationships. Stable read keys
  survive provider/server reconnects. Exact retries return the saved receipt;
  they do not create another message or consume a different batch.
- Busy recipients are not interrupted or steered by mailbox arrivals. Bounded,
  body-free availability hints use existing provider checkpoints. An eligible
  idle recipient can wake once for a new unread batch; queued user work, Stop,
  deletion and current permissions remain authoritative.
- Revocation, cancellation and current route revisions are rechecked through
  final admission and read retries. Already-delivered mail remains readable
  after its sender is archived, without making that sender available for new
  sends or automatic work.
- Remove arbitrary stored-route and hourly-message quotas for configured
  asynchronous pair messaging. Discovery and reads are paginated; message-size
  limits, explicit permission checks and legacy exchange limits remain.
- Fix provider-tool message bodies supplied through stdin, including replies;
  preserve exact acceptance/cancellation receipts rather than guessing that a
  failed or disconnected request should be submitted again.

Legacy exchange modes retain their negotiated behavior. “Accepted,” “read by
agent” and “replied” are distinct states. Opening a message in the desktop is
not an agent read receipt. See [the mailbox contract](https://github.com/ZhengyiLuo/AgentsServer/blob/v1.0.0/docs/CHAT_MAILBOX.md) and
[paired-route semantics](https://github.com/ZhengyiLuo/AgentsServer/blob/v1.0.0/docs/ASYNC_CHAT_ROUTES.md).

## Expanded Team Network collaboration — still beta

- Add server-addressed Team Mail and separate it from shared Bulletin content.
  Mail supports optional subjects, exact-parent threads, scoped replies,
  attachments, recipient dismissal and versioned read/unread attention state.
  Marking mail unread does not erase its historical delivery/read receipts.
- Add durable, revocable chat-scoped mail grants tied to the exact recipient
  server identity. Read access, message text and imported metadata do not grant
  permission to send. Agent replies require the appropriate existing route.
- Add author-versioned Bulletin edits and supported deletion/moderation flows,
  preserving revision and authorization history. Editing a post does not create
  a replacement mail item or silently change its recipients or attachments.
- Deliver negotiated, coalesced Mail/Bulletin availability hints through the
  existing stream. These do not poll message bodies or automatically dispatch
  a remote agent.
- Expand authenticated Host controls, invitation/operator management and
  server-bound Teamspace access. Opt-in secure-peer Join requests can complete
  after approval while preserving the original consent, deadline and identity;
  cancellation, expiry and a later connection choice prevent stale activation.
- Harden secure-peer connection recovery, certificate rotation, attachment
  streaming and reclamation, and snapshot/restore continuity. Host maintenance
  drains admitted writes, and verified current-schema reads avoid unnecessary
  migration writer locks.

Team Network remains a beta feature inside this stable server release.
Cross-server Team Mail goes to a **server inbox**; it is not a request to start
or steer a remote chat. Same-server chat-mailbox wake behavior is separate.

## Share one chat in a browser

- **View only** creates an explicitly requested, immutable, paged snapshot of
  the readable conversation. Large histories are streamed into bounded pages
  rather than silently truncated to a small transcript prefix.
- **Interactive** shares trusted control of one live chat, including its
  supported queue, goal, approval, settings and scheduled-job actions. It uses
  the packaged AgentsDock chat renderer and preserves native event identities
  and chronology across paging and reconnects.
- Normal links are token-free; recipients enter a separately supplied reusable
  access token. View-only snapshots also support an optional token-in-link URL.
  Expiry/revocation remain effective across browser sessions and reconnects.
- Share creation can return a different reachable HTTP/HTTPS origin for the
  same server, such as its LAN address, without changing the authenticated
  management connection. Interactive shares retain their exact origin binding;
  create a new share to use another address.
- Both modes can play and seek registered videos actually attached to sent
  messages or published in the shared chat. Media uses scoped authorization and
  byte-range responses; unused uploads and arbitrary workspace paths are not
  exposed. Codec support still depends on the browser.
- Durable request receipts prevent reconnects or uncertain submissions from
  automatically repeating prompts or controls. Shared APIs do not expose native
  administration, tmux or general filesystem browsing/downloads.

Interactive sharing is **trusted collaboration, not a provider sandbox**: a
guest can ask the existing agent to use its normal tools and may create work
that outlives the share. Revocation cannot undo accepted work, remove scheduled
jobs already created, or retract text/video bytes a recipient saved. Sharing
does not configure ingress or make a private address reachable. HTTP is
unencrypted; use HTTPS on untrusted networks. See
[View only](https://github.com/ZhengyiLuo/AgentsServer/blob/v1.0.0/docs/PUBLIC_CHAT_SHARES.md) and [Interactive](https://github.com/ZhengyiLuo/AgentsServer/blob/v1.0.0/docs/INTERACTIVE_CHAT_SHARES.md).

## Native goals, steering and subagents

- Keep an ordinary Codex run owned when its native parent turn ends before its
  children. Once the currently owned children finish, collect their native
  notifications using an empty-input continuation on the same thread. No goal,
  synthetic user prompt, repeated reminder or polling loop is required.
- Preserve that parent's runtime authority, chronological answers and controls
  through collection. A spontaneous native continuation wins over a queued
  continuation request; Stop, stale owners and uncertain delivery cannot
  silently replay the original prompt or submit a duplicate continuation.
- Preserve a Codex goal's local owner across the ordinary first turn that
  creates it and later native continuation turns. An intermediate final answer
  no longer prematurely ends the surrounding goal operation.
- Send now can steer compatible text and attachments into that same native
  goal without pausing it, replacing its run authority or starting a substitute
  user turn. Between continuation turns, one accepted follow-up waits on the
  existing stream for the next ready turn.
- Fix plain follow-ups rejected solely because the client automatically
  attached saved route metadata. The goal lane leaves those snapshots unused
  and retains its existing authority. Explicit new references, provider commands,
  changed runtime settings and stale-owner requests remain fenced.
- Recheck exact goal/run/turn ownership, process generation, budgets and
  Pause/Stop immediately before delivery. Uncertain sends are not automatically
  replayed, and accepted follow-ups produce one chronological user boundary.
- Keep public commentary, final answers, retry notices and compaction/runtime
  activity distinct. Reconnecting progress is not automatically a terminal
  failure, and later goal progress remains below the appropriate answer/input.
- Preserve native Codex child-thread titles separately from nicknames and
  paths, including live renames and explicit title clears. Recover stale names
  for known completed children with one durable identity correction that keeps
  the original lifecycle time, status and run; reopening does not append it again.
- Expose an authenticated server setting for Codex subagent concurrency through
  `GET/PUT /api/admin/codex/subagents`. A positive integer sets the provider's
  per-thread child limit; `null` removes the override. Saving preserves other
  Codex settings and never reloads or interrupts running providers.
- Remove the former default ten-active-chat admission cap. Explicit operator
  limits and resource guardrails still apply. Server chat concurrency is
  separate from Codex child-thread slots: an unset subagent limit means the
  provider's default, **not unlimited**.

The concurrency setting applies to new or reloaded native Codex threads;
already-loaded chats need Reload provider while idle. Chat-specific overrides
take precedence, and legacy exec transport reports this setting unavailable.
Name presentation and the settings form require a matching desktop release.
See [native goal behavior](https://github.com/ZhengyiLuo/AgentsServer/blob/v1.0.0/docs/NATIVE_GOAL_PROGRESS.md) and
[subagent identity](https://github.com/ZhengyiLuo/AgentsServer/blob/v1.0.0/docs/SUBAGENT_IDENTITY.md).

## More accurate history, schedules and lifecycle recovery

- Catch up provider-authored work when reopening a chat without repeatedly
  importing the same tail. Preserve validated provider message identities,
  public commentary/final phases and original timestamps; imported history
  cannot acquire live run ownership.
- Repair source-proven duplicates of native scheduled runs, mailbox wake
  instructions, control notices and assistant answers on recent and historical
  pages. Native scheduled events retain their job identity; real user messages
  and ambiguous quotations remain visible. No text-prefix heuristic hides them.
- Correlate legacy async cross-chat wrappers with their saved prepared-message
  receipts and exact completed provider turn. A provider's full delivery
  wrapper differs from the clean message body in the timeline; importing it
  must not create another green user message. Apply the same proof to old
  imported records and new history catch-up, including partial history pages.
- Handle tool-heavy, large and forked Codex histories with streamed, bounded
  proof instead of rejecting them solely at the former aggregate-size limits.
  Incomplete or changed evidence defers the batch without advancing its durable
  cursor. Repair does not rewrite the original provider transcript.
- Match decorated Claude/Codex assistant replays to their verified native
  message/item identity, so cleaned emoji/shortcodes do not create duplicate
  answers. Distinct IDs, changed bodies and uncertain evidence stay distinct.
- Fork live Claude and Codex chats at verified completed-turn checkpoints;
  preserve source cutoffs without inheriting unrelated route permission.
- Keep scheduled jobs chronological and defer manual runs while a chat is busy.
  Queue reorder/promotion/retry controls retain exact identities and durable
  ordering instead of losing pending work or replaying uncertain delivery.
- Reconcile interrupted Claude background work without injecting fake user
  prompts. Bound stuck-provider readiness, Stop cleanup and child finalization,
  while preventing stale child records from becoming permanent activity blockers.
- Improve macOS process metrics, thread-bloat diagnostics, state-write
  coalescing and log rotation. Storage failures retire exact run/capability
  ownership without claiming an unsaved completion or automatically executing
  the next queued turn.
- Retain an explicit duplicate-history maintenance operation with dry-run as
  the default and idle, atomic rewriting only when requested. Routine
  provenance-aware read repair is not that destructive maintenance operation.

## Runtime, security and update improvements

- Add the installed Cursor CLI as another backend, including streaming
  reasoning/tool output and file delivery. Runtime authentication, models,
  permissions and advanced controls remain backend-specific.
- Improve Claude catalog discovery, native provider-command discovery and
  runtime-setting application for subsequent work. Provider tools still need
  to be installed and authenticated for the service's operating-system user.
- Move helper authority out of chat prompts into private, run-bound runtime
  credentials and enforce live ownership/revocation at use. Harden history and
  sharing projections so provider-control data is not treated as user content.
- Strengthen privileged native-admin authentication, browser/ambiguous-header
  rejection, bounded request bodies and identity-bound update/restart controls.
  Refresh the dependency lock, including the patched cryptography line and
  pinned build dependencies.
- Make when-idle updates durable and keep a pending reservation passive until
  admission is safe. Improve progress/status responsiveness, provider/child
  quiescence, scheduler fairness and launchd/systemd process handling.
- Harden signed, versioned release verification, exact responder identity and
  Team Hub backup/restore continuity. Add an explicitly confirmed,
  schedule-bound force-update path without silently turning an ordinary pending
  update into an interruption.

## Compatibility, limits and rollout

- **API contract:** 15 in 0.1.25 → **28**. **Team Hub schema:** 5 → **22**.
  Compared with beta.8, both are unchanged. Older clients retain negotiated
  compatibility paths, but new controls and projections require supporting
  app/server capabilities; an app update alone cannot repair old server history.
- Back up before upgrading from older releases. Hub and snapshot-store
  migrations are not reversed by swapping the binary: restore a compatible
  backup for a schema-crossing downgrade. Snapshot storage uses schema v3;
  older binaries cannot serve new video-bearing pages. Older text-only shares
  must be recreated to include videos. Video bytes are not copied into snapshot
  storage, so deleting/changing source media or rotating its signing token can
  make those videos unavailable.
- Supported server hosts are **Linux and Apple silicon macOS** with trusted
  `uv` and authenticated provider CLIs. Intel macOS is rejected before install
  mutation because the patched cryptography runtime is unsupported there.
  `tmux` remains optional for ordinary chat/files/jobs, but is required for the
  persistent terminal and in-app managed updater. Release automation runs its
  full checks on Linux; this is not certification of every provider, browser
  codec or network topology.
- Explicit idle native Codex Goal Resume still lacks fresh provider-tool
  authority for agent mailbox reads. Mail remains stored; this path must not
  reuse a stale proof. An ordinary authorized turn continuing into its own goal
  retains its authority and is a different, supported path.
- “No arbitrary quota” does not mean unbounded memory, messages, uploads or
  execution. Paging, payload, concurrency, timeout, permission and provider
  resource protections remain. Team Network retains its beta designation.
- Stable installations discover 1.0.0 through the **Stable** channel. Beta
  installations must select Stable to discover it; publication does not change
  a saved channel. Install through the signed managed updater and wait for idle
  unless the operator explicitly authorizes interruption.

Publishing a release does not install it, restart servers, change active goals,
run scheduled jobs, open network access or alter permissions. A queued update is
not an installed update. Stable publication requires its own complete release
gate and verification of the downloaded signed manifest, archive checksum and
exact source; these notes do not themselves assert that deployment has occurred.
