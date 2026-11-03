"""HTTP access to exchange.papertrade.xyz (relayer, state and query endpoints)."""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

from . import deployment, wire

log = logging.getLogger(__name__)

USER_AGENT = "papertrade-client/0.1 (+https://github.com/gmatrunich/papertrade-client)"

_ASSET = re.compile(r"(?:assets/|\./)([A-Za-z0-9_\-.]+\.js)")
_BUILD_CHUNK = re.compile(r"^build-[A-Za-z0-9_-]+\.js$")
_BUILD_VALUE = re.compile(r"`([0-9a-f]{8,64})`")
_STALE_BUILD = re.compile(r"build|outdated|client.?version", re.I)


class PapertradeError(Exception):
    def __init__(self, message: str, status: int, code: str, payload: Any, retry_after_s: float | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload
        self.retry_after_s = retry_after_s


class AcknowledgementUnknown(Exception):
    """The intent may or may not have reached the relayer. Never resubmit it blindly:
    track its intent id until the deadline instead (the canonical frontend does the same)."""

    def __init__(self, intent_id: str, cause: BaseException):
        super().__init__(f"no acknowledgement for {intent_id}: {cause!r}")
        self.intent_id = intent_id


def discover_build_id(client: httpx.Client, origin: str, max_chunks: int = 60) -> str:
    """The frontend tags requests with X-Papertrade-Build; read the current id from its bundle."""
    html = client.get(f"{origin}/", headers={"Accept": "text/html"}).raise_for_status().text
    queue = _ASSET.findall(html)
    seen: set[str] = set()
    while queue and len(seen) < max_chunks:
        chunk = next((name for name in queue if _BUILD_CHUNK.match(name)), None)
        if chunk:
            match = _BUILD_VALUE.search(client.get(f"{origin}/assets/{chunk}").raise_for_status().text)
            if not match:
                raise RuntimeError(f"build chunk {chunk} has no build id")
            return match.group(1)
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        source = client.get(f"{origin}/assets/{name}").raise_for_status().text
        queue.extend(n for n in _ASSET.findall(source) if n not in seen)
    raise RuntimeError("build id chunk not found in the frontend bundle")


class PapertradeApi:
    def __init__(
        self,
        origin: str = deployment.EXCHANGE_ORIGIN,
        client: httpx.Client | None = None,
        build_id: str | None = None,
        build_ttl_s: float = 1800,
        state_cache_s: float = 3.0,
    ):
        self.origin = origin.rstrip("/")
        self.client = client or httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT}, follow_redirects=False)
        self._build_id = build_id
        self._build_at = time.monotonic() if build_id else 0.0
        self._build_ttl_s = build_ttl_s
        self._state_cache_s = state_cache_s
        self._state: tuple[float, wire.TradingState] | None = None
        self._lock = threading.Lock()

    # -- build id -----------------------------------------------------------------

    def build_id(self, refresh: bool = False) -> str | None:
        with self._lock:
            fresh = self._build_id and time.monotonic() - self._build_at < self._build_ttl_s
            if fresh and not refresh:
                return self._build_id
            try:
                value = discover_build_id(self.client, self.origin)
                if value != self._build_id:
                    log.info("papertrade build id %s", value)
                self._build_id = value
                self._build_at = time.monotonic()
            except Exception as error:  # keep the last known id, retry in 5 minutes
                log.warning("build id discovery failed: %s", error)
                self._build_at = time.monotonic() - self._build_ttl_s + 300
            return self._build_id

    # -- transport ----------------------------------------------------------------

    def _request(self, method: str, path: str, body: Any = None, timeout_s: float = 15, retried: bool = False) -> Any:
        headers = {"Accept": "application/json"}
        build = self.build_id()
        if build:
            headers["X-Papertrade-Build"] = build
        response = self.client.request(method, f"{self.origin}{path}", json=body, headers=headers, timeout=timeout_s)
        try:
            payload = {} if response.status_code == 204 else response.json()
        except ValueError:
            payload = {}
        record = payload if isinstance(payload, dict) else {}
        if response.is_success and not record.get("error") and record.get("ok") is not False:
            return payload
        code = str(record.get("code") or record.get("error") or f"http_{response.status_code}")
        message = str(record.get("message") or record.get("error") or f"{method} {path} returned {response.status_code}")
        if not retried and response.status_code < 500 and _STALE_BUILD.search(f"{code} {message}"):
            self.build_id(refresh=True)
            return self._request(method, path, body, timeout_s, retried=True)
        retry_after = response.headers.get("retry-after")
        raise PapertradeError(
            message, response.status_code, code, payload, float(retry_after) if retry_after and retry_after.isdigit() else None
        )

    def get(self, path: str, timeout_s: float = 15) -> Any:
        return self._request("GET", path, timeout_s=timeout_s)

    def post(self, path: str, body: Any, timeout_s: float = 15) -> Any:
        return self._request("POST", path, body, timeout_s=timeout_s)

    # -- reads --------------------------------------------------------------------

    def trading_state_frame(self) -> dict[str, Any]:
        return self.get("/state/trading")

    def trading_state(self, max_age_s: float | None = None) -> wire.TradingState:
        max_age = self._state_cache_s if max_age_s is None else max_age_s
        cached = self._state
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        state = wire.parse_trading_state(self.trading_state_frame())
        self._state = (time.monotonic(), state)
        return state

    def protocol_summary(self) -> Any:
        return self.get("/query/protocol/summary")

    def queue_summary(self) -> Any:
        return self.get("/query/protocol/queue-summary")

    def market_stats(self) -> Any:
        return self.get("/query/protocol/market-stats")

    def notices(self) -> Any:
        return self.get("/query/notices")

    def history(self, user: str, kind: str, cursor: str | None = None) -> Any:
        """kind: trades | cashflows | staking | queue. The indexer only accepts limit=75."""
        if kind not in ("trades", "cashflows", "staking", "queue"):
            raise ValueError(f"unknown history kind {kind!r}")
        query = "limit=75" + (f"&cursor={quote(cursor, safe='')}" if cursor else "")
        return self.get(f"/query/accounts/{user.lower()}/{kind}?{query}")

    def deposit_address(self, user: str) -> str:
        """Registers (and returns) the user's CREATE2 deposit proxy with the relayer."""
        return str(self.post("/funding/deposits/check", {"user": user.lower()}, timeout_s=12)["proxy"]).lower()

    # -- writes -------------------------------------------------------------------

    def submit(self, action: str, signed: dict[str, Any], intent_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST /intents. Raises PapertradeError on a refusal, AcknowledgementUnknown when the
        outcome of the request itself is unknown (timeout, dropped connection, 5xx)."""
        try:
            response = self.post("/intents", {"action": action, "signed": signed, **(extra or {})}, timeout_s=35)
        except PapertradeError as error:
            if error.status >= 500:
                raise AcknowledgementUnknown(intent_id, error) from error
            raise
        except httpx.TransportError as error:
            raise AcknowledgementUnknown(intent_id, error) from error
        if not isinstance(response, dict):
            # A 2xx is an acceptance even when the body is not the expected shape.
            log.warning("unexpected /intents response body for %s: %r", intent_id, response)
            return {"raw": response}
        if response.get("intentId") and str(response["intentId"]).lower() != intent_id:
            log.warning("relayer intent id %s differs from local %s", response["intentId"], intent_id)
        return response

    def cancel(self, intent_id: str, body: dict[str, Any]) -> Any:
        return self.post(f"/intents/{intent_id}/cancel", body, timeout_s=35)
