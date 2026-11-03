"""Local persistence: session keys, nonce counters, kill switch, audit log.

``state.json`` holds the session private key, so it is written atomically with
0600 permissions inside a 0700 directory. Back the data directory up.

Several processes may share one data directory (a strategy, the CLI, the drift
monitor's kill switch): every read comes from disk, and every update re-reads
the file under an exclusive ``flock`` before writing it back.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import protocol

_EMPTY: dict[str, Any] = {
    "session_keys": [],  # newest first: {private_key, address, expires_at, registered_at, intent_id}
    "nonces": {},
    "kill_switch": {"engaged": False, "reason": None, "changed_at": None},
    "daily": None,  # {"day": "YYYY-MM-DD", "realized_pnl_baseline_raw": "..."}
    # submitted opens not yet visible on the account stream:
    # {intent_id, action, deadline, submitted_at, acknowledged}
    "inflight": [],
}


class StateStore:
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir).expanduser().resolve()
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.dir / "state.json"
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def _exclusive(self):
        with self._lock:
            fd = os.open(self.dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def read(self) -> dict[str, Any]:
        # os.replace makes every version of the file complete, so reads need no lock.
        return {**json.loads(json.dumps(_EMPTY)), **(json.loads(self.path.read_text()) if self.path.exists() else {})}

    def update(self, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._exclusive():
            state = self.read()
            mutate(state)
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state.", suffix=".tmp")  # 0600
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(state, handle, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
                raise
            directory = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return state

    def take_nonce(self, lane: str, verifying_contract: str, user: str) -> int:
        """Client-chosen nonces: a random start, then +1 per intent, per (contract, lane, user)."""
        key = f"{verifying_contract.lower()}:{lane}:{user.lower()}"
        taken = 0

        def bump(state: dict[str, Any]) -> None:
            nonlocal taken
            taken = int(state["nonces"].get(key) or protocol.random_nonce())
            state["nonces"][key] = str(taken + 1)

        self.update(bump)
        return taken

    def set_kill_switch(self, engaged: bool, reason: str | None = None) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> None:
            state["kill_switch"] = {"engaged": engaged, "reason": reason, "changed_at": time.time()}

        return self.update(apply)["kill_switch"]

    def kill_switch(self) -> dict[str, Any]:
        return self.read()["kill_switch"]


class AuditLog:
    """Append-only JSON lines: every live intent before it leaves and the relayer's answer."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "audit.jsonl"
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        line = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}, default=str)
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as handle:
                handle.write(line + "\n")
