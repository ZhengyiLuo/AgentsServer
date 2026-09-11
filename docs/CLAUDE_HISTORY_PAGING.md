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
