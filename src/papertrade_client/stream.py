"""Live account state from ``GET /state/user/live?wallet=…`` (server-sent events).

The stream opens with a full ``wallet`` snapshot, then sends patches keyed by
``accountRevision`` and a ``liveness`` heartbeat every ~10 s. ``AccountStream``
keeps the reduced state in a background thread and reconnects on its own.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator

import httpx

from . import deployment, wire
from .api import USER_AGENT

log = logging.getLogger(__name__)


def iter_sse(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    event, data = "message", []
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        else:
            name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if name == "event":
                event = value
            elif name == "data":
                data.append(value)


class OutcomeLog:
    """Bounded memory of how intents ended and which positions closed."""

    def __init__(self, capacity: int = 1000):
        self.capacity = capacity
        self.intents: OrderedDict[str, str] = OrderedDict()
        self.closed: OrderedDict[int, wire.ClosedPosition] = OrderedDict()

    def record(self, patch: wire.AccountPatch) -> None:
        for intent, outcome in patch.intent_outcomes:
            self.intents[intent] = outcome
        for position in patch.closed_positions:
            self.closed[position.position_id] = position
        for book in (self.intents, self.closed):
            while len(book) > self.capacity:
                book.popitem(last=False)


class AccountStream:
    def __init__(
        self,
        wallet: str,
        origin: str = deployment.EXCHANGE_ORIGIN,
        client: httpx.Client | None = None,
        resync_s: float = 300,
        on_change: Callable[[wire.AccountState], None] | None = None,
    ):
        self.wallet = wallet.lower()
        self.url = f"{origin.rstrip('/')}/state/user/live?wallet={self.wallet}"
        self.client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(10, read=30))
        self.resync_s = resync_s
        self._listeners: list[Callable[[wire.AccountState], None]] = [on_change] if on_change else []
        self.outcomes = OutcomeLog()
        self._state: wire.AccountState | None = None
        self._last_event_at: float | None = None
        self._cond = threading.Condition()
        self._running = False
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    # -- public -------------------------------------------------------------------

    def start(self) -> AccountStream:
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._loop, name="papertrade-account-stream", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._running = False

    def add_listener(self, listener: Callable[[wire.AccountState], None]) -> None:
        """Called from the stream thread after every applied wallet frame."""
        self._listeners.append(listener)

    def state(self) -> wire.AccountState | None:
        with self._cond:
            return self._state

    def age_s(self) -> float | None:
        """Seconds since the last event (heartbeats included); None before the first one.
        Transport liveness only: how current the data is shows in ``indexed_through_timestamp``."""
        with self._cond:
            return None if self._last_event_at is None else time.monotonic() - self._last_event_at

    def wait_for(self, predicate: Callable[[wire.AccountState], bool], timeout_s: float) -> wire.AccountState:
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while self._state is None or not predicate(self._state):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for account state")
                self._cond.wait(remaining)
            return self._state

    def wait_ready(self, timeout_s: float = 15) -> wire.AccountState:
        return self.wait_for(lambda _: True, timeout_s)

    # -- internals ----------------------------------------------------------------

    def handle(self, event: str, data: str) -> None:
        with self._cond:
            self._last_event_at = time.monotonic()
            if event == "wallet":
                frame = json.loads(data)
                if wire.is_patch(frame):
                    if self._state is None:
                        raise wire.WireError("wallet patch received before the snapshot")
                    patch = wire.parse_account_patch(frame)
                    self.outcomes.record(patch)
                    self._state = wire.apply_patch(self._state, patch)
                else:
                    self._state = wire.parse_account_snapshot(frame)
            self._cond.notify_all()
            state = self._state
        if event == "wallet" and state is not None:
            for listener in self._listeners:
                try:
                    listener(state)
                except Exception:
                    log.exception("account listener failed")

    def _connect_once(self) -> None:
        started = time.monotonic()
        with self.client.stream("GET", self.url, headers={"Accept": "text/event-stream"}) as response:
            response.raise_for_status()
            for event, data in iter_sse(response.iter_lines()):
                self.handle(event, data)
                # Reconnect now and then to re-baseline from a full snapshot.
                if not self._running or time.monotonic() - started > self.resync_s:
                    return

    def _loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                self._connect_once()
                backoff = 1.0
                self.last_error = None
            except wire.WireError as error:
                # A format change: keep reconnecting, but loudly; guards treat the state as stale.
                self.last_error = f"wire: {error}"
                log.error("account stream format error: %s", error)
                with self._cond:
                    self._state = None
                    self._cond.notify_all()
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as error:
                self.last_error = repr(error)
                log.warning("account stream disconnected: %s (retry in %.0fs)", error, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
