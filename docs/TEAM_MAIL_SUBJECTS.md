# Team mail subjects (optional server capability)

The Hub advertises `capabilities.team_mail_subjects_v1` as
`{"available":true,"version":1,"max_subject_chars":160}`. Check the actual
connected Hub, not the desktop or guest server version. Missing or malformed
capability means unsupported.

Modern `POST /v1/teams/{team}/network/messages` accepts optional `title` for
`kind:"message"`. This explicit titled creation returns its subject. Subjects
are trimmed, single-line, 1–160 Unicode codepoints; control characters, Unicode
line/paragraph separators, and invalid Unicode are rejected. Internal spaces
and Unicode spelling are preserved. The normalized subject participates in
idempotency. Existing skill title/version semantics are unchanged.

List and detail GETs, and body-revision POST responses, expose an ordinary
message's subject only with `include_mail_subject=true`. All other readers
still receive `title:null`; revision-history GET shape is unchanged. Subjects
are immutable on body edits. The separate nullable `mail_subject` column is
not backfilled from legacy ordinary-message titles. Recipient authorization,
all-server recipient snapshots, receipts, and legacy mailbox APIs are unchanged.

The agent helper accepts message `--title` and rejects an unsupported actual
Hub before uploading attachments or sending. It never silently drops a subject
or resends without one. List/read commands optionally accept
`--include-mail-subject`; default calls and their request counts are unchanged.
This feature adds no inbox polling, notification streaming, or body refresh.

Migration 18 is included in the package and installer manifests. Before any
future deployment, take and verify a supported Hub backup/recovery snapshot and
plan restore with the matching server version. The database rejects older
binaries against a newer schema: replacing the binary alone is not a safe
rollback. This local implementation does not migrate any live Hub or deploy a
server; clients remain compatible when the optional capability is absent.
