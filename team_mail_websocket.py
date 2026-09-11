"""Authenticated local passive Mail socket, outside managed Hub ASGI drains.

No global subscription copies, mailbox fetches, shared executor waits or idle
queries. At most sixteen dedicated Mail workers each own one coalescing broker
lease and one serial bounded ASGI writer. Production remains feature-gated.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import threading
import uuid
from typing import Any, Callable

from agentsdock_team_hub.mail_hints import MAIL_IDENTIFIER_RE, MailArrival

MAIL_WEBSOCKET_PROTOCOL = "agentsdock.team-mail-hints.v1"
_SLOTS = threading.BoundedSemaphore(16)


class _MailSocketWriter:
    """Bridge one dedicated worker to an actual event-loop Task completion.

    A cancelled concurrent Future is not proof its asyncio Task settled. Keep
    the worker's lease/slot until a Task done callback proves completion, even
    if an ASGI implementation resists cancellation. That consumes only this
    bounded Mail capacity, never a control request slot or event-loop wait.
    """
    def __init__(self, loop, *, timeout=5):
        self.loop, self.timeout = loop, timeout
        self.guard = threading.Lock()
        self.stopped = False
        self.task = None

    def cancel(self):
        with self.guard:
            self.stopped = True

        def cancel_task():
            with self.guard:
                task = self.task
            if task is not None:
                task.cancel()
        with suppress(RuntimeError):
            self.loop.call_soon_threadsafe(cancel_task)

    def send(self, coroutine_factory):
        settled = threading.Event()
        outcome = {}

        def finished(task):
            try:
                outcome["result"] = task.result()
            except BaseException as exc:
                outcome["error"] = exc
            with self.guard:
                if self.task is task:
                    self.task = None
            settled.set()

        def start():
            try:
                task = self.loop.create_task(coroutine_factory())
                # This callback also runs when cancelled before its first step.
                task.add_done_callback(finished)
                with self.guard:
                    self.task = task
                    stopped = self.stopped
                if stopped:
                    task.cancel()
            except BaseException as exc:
                outcome["error"] = exc
                settled.set()

        self.loop.call_soon_threadsafe(start)
        if not settled.wait(self.timeout):
            self.cancel()
        settled.wait()
        if "error" in outcome:
            raise outcome["error"]


async def serve_team_mail_hints(ws: Any, runtime: Any, *, server_identity: str,
                                authorized: Callable[[], bool], protocols: list[str]) -> None:
    await ws.accept(subprotocol=MAIL_WEBSOCKET_PROTOCOL if MAIL_WEBSOCKET_PROTOCOL in protocols else None)
    if not authorized() or ws.query_params:
        await ws.close(code=4401)
        return
    if MAIL_WEBSOCKET_PROTOCOL not in protocols or not runtime.team_mail_hint_capability().get("enabled"):
        await ws.close(code=4406)
        return
    if not _SLOTS.acquire(blocking=False):
        await ws.close(code=1013)
        return
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    stopped = threading.Event()
    guard = threading.Lock()
    holder: dict[str, Any] = {}
    writer = _MailSocketWriter(loop)
    stream_id = uuid.uuid4().hex
    worker = None
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=5)
        if len(raw.encode("utf-8")) > 4096:
            raise ValueError("Mail subscription frame is too large")
        request = json.loads(raw)
        if (not isinstance(request, dict) or set(request) != {"version", "team_id", "previous_cursor"}
                or type(request["version"]) is not int or request["version"] != 1
                or not isinstance(request["team_id"], str) or MAIL_IDENTIFIER_RE.fullmatch(request["team_id"]) is None):
            raise ValueError("Mail subscription frame is invalid")
        if request["previous_cursor"] is not None:
            previous = MailArrival.from_dict(request["previous_cursor"])
            if previous.team_id != request["team_id"]:
                await ws.close(code=4403)
                return

        def abort():
            stopped.set()
            writer.cancel()

        def run():
            lease = None
            try:
                lease = runtime.subscribe_team_mail_hints(request["team_id"], request["previous_cursor"])
                with guard:
                    holder["lease"] = lease
                lease.set_aborter(abort)
                if stopped.is_set():
                    return

                def write(kind, cursor):
                    async def send():
                        if stopped.is_set() or lease.closed or not authorized():
                            raise RuntimeError("Mail socket authority changed")
                        frame = {"type": kind, "server_identity": server_identity,
                                 "hub_id": lease.hub_id, "stream_id": stream_id, "cursor": cursor}
                        data = json.dumps(frame, separators=(",", ":"))
                        if len(data.encode("utf-8")) > 4096:
                            raise ValueError("Mail socket metadata is too large")
                        await ws.send_text(data)
                    writer.send(send)

                lease.write(lambda cursor: write("snapshot", cursor), lease.snapshot)
                while not stopped.is_set():
                    arrival = lease.take()
                    if arrival is None:
                        break
                    lease.write(lambda cursor: write("hint", cursor), arrival.as_dict(reset=False))
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                holder["close_code"] = (4403 if getattr(exc, "status_code", None) in (401, 403)
                    else 4406 if getattr(exc, "status_code", None) in (404, 501)
                    else 1008 if isinstance(exc, ValueError) or getattr(exc, "code", None) == "remote_invalid"
                    else 1012)
            finally:
                if lease is not None:
                    holder.setdefault("close_code", lease.close_code)
                    lease.close()
                _SLOTS.release()
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(done.set)

        pending_worker = threading.Thread(target=run, name="agentsdock-mail-local", daemon=True)
        pending_worker.start()
        worker = pending_worker
        receive = asyncio.create_task(ws.receive_text())
        finished = asyncio.create_task(done.wait())
        try:
            completed, _pending = await asyncio.wait((receive, finished), return_when=asyncio.FIRST_COMPLETED)
            if receive in completed:
                # Subscriptions have no command channel or acknowledgement.
                receive.result()
                await ws.close(code=1008)
            else:
                await ws.close(code=holder.get("close_code", 1012))
        finally:
            for task in (receive, finished):
                task.cancel()
                with suppress(BaseException):
                    await task
    except (ValueError, asyncio.TimeoutError):
        with suppress(Exception):
            await ws.close(code=1008)
    except Exception:
        pass
    finally:
        stopped.set()
        with guard:
            lease = holder.get("lease")
        if lease is not None:
            lease.cancel()
        if worker is not None:
            # No join/default executor on the event loop. The socket's bounded
            # worker signals completion after closing its subscription.
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(done.wait(), timeout=15)
        else:
            _SLOTS.release()
