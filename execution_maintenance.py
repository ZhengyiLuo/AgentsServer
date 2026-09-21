"""Idle-only execution retirement leases; callers hold admission locks.

An expiring preparation lease cannot authorize a later process stop. Sealing
persists a non-expiring admission hold first. Only that exact hold may authorize
the service manager to retire the worker. A failed coordinator leaves a visible,
recoverable hold instead of opening a race with newly accepted work.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import time
from typing import Any, Callable
import uuid

from execution_control import ExecutionControlError


class ExecutionMaintenance:
    def __init__(self, path: Path, worker_instance_id: str, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.worker_instance_id = worker_instance_id
        self.clock = clock
        self.lease: dict[str, Any] | None = None
        # New worker epochs never inherit permission to stop from an old epoch.
        # An unfinished installation explicitly creates its own startup hold.
        if path.exists() or path.is_symlink():
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                        or info.st_size > 16_384):
                    raise ValueError("Invalid execution maintenance receipt")
                document = json.loads(os.read(descriptor, 16_385))
            finally:
                os.close(descriptor)
            if (not isinstance(document, dict) or set(document) != {"schema", "worker_instance_id", "lease"}
                    or document["schema"] != 1):
                raise ValueError("Invalid execution maintenance receipt")
            if document["worker_instance_id"] == worker_instance_id:
                self.lease = self._validate_lease(document["lease"])

    @staticmethod
    def _validate_lease(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"lease_id", "operation_id", "sealed", "expires_at"}:
            raise ValueError("Invalid execution maintenance lease")
        for name in ("lease_id", "operation_id"):
            if str(uuid.UUID(value[name])) != value[name]:
                raise ValueError("Invalid execution maintenance identity")
        if type(value["sealed"]) is not bool:
            raise ValueError("Invalid execution maintenance state")
        if value["sealed"]:
            if value["expires_at"] is not None:
                raise ValueError("A sealed lease cannot expire")
        elif type(value["expires_at"]) not in {int, float} or not 0 < value["expires_at"] < float("inf"):
            raise ValueError("Invalid execution maintenance expiry")
        return dict(value)

    def is_held(self) -> bool:
        return self.lease is not None and (
            self.lease["sealed"] is True or self.lease["expires_at"] > self.clock()
        )

    def status(self, blockers: dict[str, int]) -> dict[str, Any]:
        return {
            "worker_instance_id": self.worker_instance_id,
            "idle": not any(blockers.values()),
            "blockers": dict(blockers),
            "lease": dict(self.lease) if self.is_held() else None,
        }

    def _persist(self, lease: dict[str, Any] | None) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError("Execution maintenance directory is unsafe")
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump({"schema": 1, "worker_instance_id": self.worker_instance_id, "lease": lease}, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        self.lease = lease

    def hold_for_startup(self, operation_id: str) -> None:
        if str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError("Invalid startup operation")
        self._persist({"lease_id": str(uuid.uuid4()), "operation_id": operation_id,
                       "sealed": True, "expires_at": None})

    def apply(self, action: str, operation_id: str, lease_id: str | None,
              lifetime_seconds: int, blockers: dict[str, int]) -> dict[str, Any]:
        if str(uuid.UUID(operation_id)) != operation_id or not 30 <= lifetime_seconds <= 300:
            raise ExecutionControlError(400, "Invalid maintenance operation")
        if action == "acquire":
            if self.is_held():
                if self.lease["operation_id"] != operation_id:
                    raise ExecutionControlError(409, "Another execution operation owns admission")
                return self.status(blockers)
            if any(blockers.values()):
                raise ExecutionControlError(409, "Execution is busy; running agents were left untouched")
            self._persist({"lease_id": str(uuid.uuid4()), "operation_id": operation_id,
                           "sealed": False, "expires_at": self.clock() + lifetime_seconds})
        elif action in {"renew", "seal", "release"}:
            if (self.lease is None or self.lease["operation_id"] != operation_id
                    or self.lease["lease_id"] != lease_id):
                raise ExecutionControlError(409, "Execution maintenance ownership changed")
            if action == "release":
                self._persist(None)
            else:
                if not self.is_held():
                    raise ExecutionControlError(409, "Execution maintenance lease expired")
                if action == "seal":
                    if any(blockers.values()):
                        raise ExecutionControlError(409, "Execution work appeared before retirement")
                    self._persist({**self.lease, "sealed": True, "expires_at": None})
                elif self.lease["sealed"] is not True:
                    self._persist({**self.lease, "expires_at": self.clock() + lifetime_seconds})
        else:
            raise ExecutionControlError(400, "Unknown maintenance action")
        return self.status(blockers)
