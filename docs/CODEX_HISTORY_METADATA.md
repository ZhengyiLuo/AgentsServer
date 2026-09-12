# Forward Codex assistant history metadata

Newly parsed Codex rollout `event_msg/agent_message` and
`response_item/message` assistant records retain only the explicit provider
`payload.phase` values `commentary` and `final_answer`, plus a valid timezone-aware
ISO timestamp from the outer record's `timestamp`. The original timestamp string
is preserved; malformed or timezone-free values are omitted independently.
No phase is inferred from message text, and private reasoning records remain
outside the public assistant history parser. Accepted user messages also retain
their validated outer timestamp, never an assistant phase; user/goal provenance
classification is unchanged.

Both normal and staged import paths emit commentary as `reasoning_summary` with
`phase: commentary`, and final answers as `assistant_text` with
`phase: final_answer`. Original times use the existing event `ts` field. These
events remain explicitly imported history, not live turn ownership. The synthetic
import terminal uses the last validated source-message timestamp when available,
so importing September 1 work on September 10 does not imply nine days of work.
This terminal behavior is shared with Claude, using its already-validated
`provider_origin.timestamp`; raw item timestamps are not trusted for Claude.
Missing source timestamps keep the existing fallback; no dates are inferred. Public
Codex commentary also participates in timeline ownership matching, while generic
reasoning does not.

Adjacent duplicate provider representations enrich missing metadata on the
retained, not-yet-persisted item. Known metadata is never downgraded, and explicit
different phases remain separate even when the text is identical. The existing
kind/text cursor digest and timeline text-matching keys are unchanged. An
optional allowlisted `codex_last_item_phase` cursor field preserves known phase
distinctions across future bounded delta passes and checkpoint recovery.

This is forward-only. Already persisted imports are not rewritten. A duplicate
straddling an old or metadata-free committed cursor keeps legacy conservative
deduplication: no missing historical phase or timestamp is guessed, and a richer
later copy cannot enrich that already persisted row. Unknown phases retain the
existing phase-less assistant presentation. A consumed duplicate with an explicit
phase can initialize missing cursor phase for subsequent raw records without
changing the old row. No filesystem work was added to
event projection or request hot paths.

Validation uses `test_codex_history_metadata_isolated.py` and the existing Codex
goal/Claude isolated suites. They compile explicitly allowlisted AST helpers,
mock persistence or use temporary files, and never import/start the server or
contact a provider.

## Typed runtime inputs

Provider user-role records explicitly typed `multi_agent.subagent_notification`
or `generic.turn_aborted` are runtime metadata, not human input. Classification
requires the exclusive provider kind, the complete matching wrapper, valid
provider identity, and no positive human/client provenance. Ordinary quotations
and unknown or mixed kinds remain visible. Silent imported boundaries preserve
following answers without creating a new human message or changing live state.

Subagent notifications cover completed results (including null), errors,
shutdown and not-found terminal states. The same provenance boundary also
recognizes these exact infrastructure kinds as `provider_notice`:
`compaction.summary`, `apply_patch.legacy_exec_command_warning`,
`model_switch.legacy_mismatch_warning`, `unified_exec.legacy_process_limit_warning`,
`guardian.node_repl_review_evidence`, `plugins.recommendations` and
`agents_md.instructions`. Compaction and legacy warnings have no mandatory text
wrapper; their explicit type is required. Delimited kinds require their complete
envelope. No wildcard type or text-prefix rule identifies these notices.

User-invoked shell commands, realtime user delegation and unsupported human media
remain content. Unknown extension context types are not silently discarded.
Positive human provenance preserves literal project/plugin instruction quotations
through the existing bounded normalizer and generated-authority sanitizer.

Older imported copies are projected only after checkpoint, source digest,
message identity, original timestamp and complete text prove the same record.
A forked transcript may contain its explicitly declared ancestor headers; an
unrelated or ambiguous header fails proof. Original transcripts and stored
events are not rewritten. The bounded repair cache is prepared on demand and
does not add polling. Runtime cursor identities are distinct from human text,
so a later identical human quotation cannot be consumed as a duplicate.

## Native assistant replay equivalence

Native Codex delivery removes leading emoji and shortcode decorations from
assistant lines. The history source retains those decorations. A replay may
therefore match the native-cleaned assistant body only when the exact public
provider item ID also matches, within the already-proven provider thread and
completed native turn. This applies both before importing new history and when
projecting an already-imported duplicate.

The original source digest, complete body, timestamps and checkpoint remain the
proof boundary. No user text is normalized, and missing or conflicting item IDs,
substantive body changes, private reasoning and truncated records do not gain
this fallback. The original native event retains its scheduled-job ownership;
only its proven imported replay is suppressed. Stored transcripts are unchanged,
and this adds no polling or per-event filesystem reads.
