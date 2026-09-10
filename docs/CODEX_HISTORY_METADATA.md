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
