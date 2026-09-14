# Historical Claude scheduled reports

Older imported provider history can replay scheduled input and output without
the native job ownership. Those copies must not become ordinary user/assistant
messages, but text resemblance alone is not sufficient to hide them.

Semantic page reads now reuse the already-built import-run byte bounds to
prepare one historical proof window. This handles proven duplicates that have
moved outside the newest history tail. The proof requires the original import
checkpoint, exact provider origin and matching native scheduled occurrence.
Ambiguous records, genuine questions and unmatched output remain visible.

The page proof is separate from the global timeline-index signature. It cannot
trigger a full index rebuild, rewrite either transcript, execute a job or
introduce polling. Each proof reads at most 32 MiB from the event log and 32 MiB
from the original provider checkpoint. Unchanged positive and negative windows
are cached; changed file stamps require revalidation.

Page responses include same-ID repaired records so clients can replace stale
cached bubbles rather than retaining records omitted from a newer response.
Original reports remain available in native scheduled-job history. Unproven
legacy imports without sufficient original provenance are deliberately retained.

Validation uses source-extracted page/index functions, isolated synthetic
provider logs, exact-ID stale-cache replacements, and controls for genuine
messages, current-page reads, bounded I/O and unchanged global signatures.
This source change is not a deployment or release.

## Decorated assistant replays

The `release/1.0` line did not include main's earlier provider-message identity
fix (`e5f72fc`, PR #103). Its newer replay proof also compared raw source text
with already-cleaned live output. Thus a source reply beginning with `✅`
could appear again even with the same Claude message UUID.

The fix restores identity-aware parsing and cursor reconciliation on the 1.0
line. Distinct UUIDs remain distinct occurrences; missing identity retains only
the existing exact-text fallback, not decoration-only matching. SDK text blocks
and their terminal echo continue to consume one provider-message credit.

Read repair accepts the live `clean_assistant_text` function as an explicit
normalizer. Decoration equivalence is used only after proving one native
message UUID, one matching raw source record, provider-session ownership,
timestamp/phase compatibility, a successful completed native run, and the
original import checkpoint. Imported targets retain their original raw digest,
so editing an imported string cannot inherit a previously cached repair.
Ambiguous ownership, missing IDs, changed content, and genuinely distinct
replies remain visible. This applies to bounded recent and historical windows.

The existing mailbox-wake pre-import filter uses the same proof for assistant
output, not just its generated input. This matters because a native wake's
empty public prompt cannot match the provider's internal prompt as an ordinary
timeline credit. Proven replay imports become blank, metadata-only records
before publication; a wholly metadata-only batch has neutral boundary events.
No native answer or existing append-only event is rewritten or deleted.

Validation on `87eb7ca` (`1.0.0-beta.5` base):

- The reported native/import pair was checked read-only against the real
  transcripts: native seq 2137 remains unchanged; imported seq 2145 becomes
  metadata-only. The pre-import path also suppresses its duplicate bubble.
  The event bytes and source file stamps remained unchanged.
- Synthetic regressions cover `✅`, `🎉`, `👉`, shortcode decoration, small and
  bounded large-history windows, stale target text, conflicting/missing IDs,
  repeated messages, phase/timestamp mismatches, and pre-publication metadata.
  The focused identity/replay/pre-publication group passes all 38 tests.
- Broader validation: 222 tests, 213 passed and nine existing Codex failures.
  All nine also reproduce from a clean archive of unchanged `87eb7ca`: eight
  durable-cursor fixtures in `test_provider_history_sync` and the source-scan
  deadline fixture in `test_codex_native_history_repair`. No Codex-specific
  parser or repair module was changed. Hosted CI was not run.

No Electron fallback is required for this case once the server fix is active;
existing metadata-only and same-ID page replacements are the client contract.
The local running server has not been replaced by this source change. A later
authorized local test must preserve any other installed hotfixes, including
the separate Cursor native-tool integration.
