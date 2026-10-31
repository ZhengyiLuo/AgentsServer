"""Incremental unread provenance; journal recovery runs off the event loop."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable
from transcript_reader import jsonl_records


COUNTER = "latest_non_job_agent_event_seq"
CHECKPOINT = "_non_job_agent_event_checkpoint_seq"
PENDING_COUNTER = "_non_job_agent_event_pending_seq"
LAST_RUN = "_non_job_agent_last_run"
JOURNAL = "_non_job_agent_event_journal"


def imported_event(event: dict[str, Any]) -> bool:
    return event.get("imported") is True or str(event.get("run_id") or "").startswith("import_")


def scheduled_event(event: dict[str, Any]) -> bool:
    job = event.get("job")
    return bool(
        event.get("purpose") == "scheduled_job"
        or str(event.get("job_id") or "").strip()
        or isinstance(job, dict) and str(job.get("id") or "").strip()
        or str(event.get("type") or "").startswith("job_")
    )


def project_event(
    session: dict[str, Any], event: dict[str, Any], *,
    visible: bool, run_metadata: dict[str, Any],
) -> None:
    """Apply an appended event without I/O or changing the ordinary read cursor."""
    seq = int(event.get("seq") or 0)
    if seq <= 0:
        return
    # Only a new journal can establish an empty baseline without recovery.
    if seq == 1 and not int(session.get("latest_event_seq") or 0):
        session.setdefault(COUNTER, 0)
        session.setdefault(CHECKPOINT, 0)
    known = isinstance(session.get(COUNTER), int) and CHECKPOINT in session
    run_id = str(event.get("run_id") or "")
    contexts: list[dict[str, Any]] = []
    if event.get("type") == "turn_started" and run_id:
        context = {"run_id": run_id, "scheduled_job": scheduled_event(event) or scheduled_event(run_metadata)}
        session[LAST_RUN] = context
        contexts.append(context)
    else:
        for candidate in (session.get("active_run"), session.get(LAST_RUN)):
            if isinstance(candidate, dict) and run_id and candidate.get("run_id") == run_id:
                contexts.append(candidate)
    scheduled = scheduled_event(event) or scheduled_event(run_metadata) or any(
        scheduled_event(context) or context.get("scheduled_job") is True for context in contexts
    )
    if scheduled and run_id and (session.get(LAST_RUN) or {}).get("run_id") == run_id:
        session[LAST_RUN] = {"run_id": run_id, "scheduled_job": True}
    if visible and not scheduled and not imported_event(event):
        field = COUNTER if known else PENDING_COUNTER
        session[field] = max(int(session.get(field) or 0), seq)
    if known:
        session[CHECKPOINT] = max(int(session.get(CHECKPOINT) or 0), seq)


def recover_projection(
    path: Path, snapshot: dict[str, Any], *,
    visible: Callable[[str, dict[str, Any]], bool],
    resume_offset: Callable[[Path, int], int],
    prior_events: Callable[[], Iterable[dict[str, Any]]],
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    """Project one fixed journal extent, then resolve missing run provenance once.

    Legacy chats need one forward pass. A durable checkpoint resumes through the
    existing sparse index; only an untagged tail run whose start precedes that
    checkpoint requires a backwards provenance pass. No history is read by the
    session-list endpoint or the live event projection.
    """
    known = isinstance(snapshot.get(COUNTER), int) and CHECKPOINT in snapshot
    after = int(snapshot.get(CHECKPOINT) or 0) if known else 0
    latest = int(snapshot.get(COUNTER) or 0) if known else 0
    through = after
    last_run = dict(snapshot.get(LAST_RUN) or {})
    runs: dict[str, dict[str, Any]] = {}
    unresolved: dict[str, list[dict[str, Any]]] = {}

    def finish(run_id: str, run: dict[str, Any]) -> None:
        nonlocal latest
        if run["scheduled"] or not run["visible_seq"]:
            return
        if run["origin"]:
            latest = max(latest, run["visible_seq"])
        else:
            unresolved.setdefault(run_id, []).append(run)

    try:
        source = path.open("rb")
    except FileNotFoundError:
        if after or int(snapshot.get("latest_event_seq") or 0):
            raise
        return {COUNTER: 0, CHECKPOINT: 0, LAST_RUN: {}}
    with source:
        extent = source.seek(0, 2)
        inode = path.stat().st_ino
        journal = snapshot.get(JOURNAL) or {}
        if known and journal and (journal.get("inode") != inode or int(journal.get("offset") or 0) > extent):
            after = latest = through = 0
            last_run = {}
        offset = int(journal.get("offset") or 0) if after and journal.get("inode") == inode else 0
        source.seek(offset or (resume_offset(path, after) if after else 0))
        complete_offset = source.tell()
        for event, ending, raw in jsonl_records(
            source, end=extent, project=False, require_newline=False, deadline=float("inf"), cancelled=cancelled,
            max_records=float("inf"), max_nodes=None, max_depth=512,
            retained_fields=frozenset({"seq", "type", "run_id", "purpose", "job_id", "job", "text", "result_text", "goal", "imported"}),
        ):
            if not raw.complete:
                break  # Leave a concurrently written/crash fragment unclaimed.
            complete_offset = ending
            if not isinstance(event, dict):
                continue
            seq = int(event.get("seq") or 0)
            if seq <= after:
                continue
            through = max(through, seq)
            event_type = str(event.get("type") or "")
            is_visible = visible(event_type, event) and not imported_event(event)
            run_id = str(event.get("run_id") or "")
            scheduled = scheduled_event(event)
            if not run_id:
                if is_visible and not scheduled:
                    latest = max(latest, seq)
                continue
            if event_type == "turn_started" and run_id in runs:
                finish(run_id, runs.pop(run_id))
            run = runs.setdefault(run_id, {"first_seq": seq, "visible_seq": 0, "scheduled": False, "origin": False})
            run["scheduled"] = run["scheduled"] or scheduled
            if event_type == "turn_started":
                run["origin"] = True
                last_run = {"run_id": run_id, "scheduled_job": scheduled}
            elif scheduled and last_run.get("run_id") == run_id:
                last_run["scheduled_job"] = True
            if is_visible:
                run["visible_seq"] = max(run["visible_seq"], seq)
            if event_type in {"turn_finished", "turn_stopped"} and (run["scheduled"] or not run["visible_seq"]):
                # Completed cron runs do not accumulate with the schedule's
                # history. A later untagged event is resolved by the single
                # backwards provenance pass, just like a restart tail.
                runs.pop(run_id, None)
        for run_id, run in runs.items():
            finish(run_id, run)
    # One reverse pass resolves all tail runs together, including stripped raw
    # provider events and a job_ran marker emitted after the provider's start.
    if unresolved:
        for event in prior_events():
            if cancelled():
                raise InterruptedError("unread projection cancelled")
            run_id = str(event.get("run_id") or "")
            candidates = unresolved.get(run_id)
            if not candidates:
                continue
            seq = int(event.get("seq") or 0)
            scheduled = scheduled_event(event)
            if not scheduled and event.get("type") != "turn_started":
                continue
            remaining = []
            for run in candidates:
                if seq >= run["first_seq"]:
                    remaining.append(run)
                elif not scheduled:
                    latest = max(latest, run["visible_seq"])
            if remaining:
                unresolved[run_id] = remaining
            else:
                unresolved.pop(run_id)
            if not unresolved:
                break
        # Legacy output with no recorded job provenance remains ordinary;
        # merely owning a schedule never suppresses a human conversation.
        for candidates in unresolved.values():
            for run in candidates:
                latest = max(latest, run["visible_seq"])
    return {COUNTER: latest, CHECKPOINT: through, LAST_RUN: last_run,
            JOURNAL: {"inode": inode, "offset": complete_offset}}
