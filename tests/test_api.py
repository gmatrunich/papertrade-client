from __future__ import annotations

from urllib.parse import parse_qsl

import httpx
import pytest

from papertrade_client import reconcile
from papertrade_client.api import PapertradeApi, PapertradeError

USER = "0x" + "ab" * 20


def _api(handler) -> PapertradeApi:
    return PapertradeApi(client=httpx.Client(transport=httpx.MockTransport(handler)), build_id="0123456789abcdef")


def _strict_history(pages: dict[str | None, dict], seen: list[str]):
    """Papertrade's history route as of 2026-11-10: parameters must come in canonical (sorted) order."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query.decode()
        seen.append(query)
        params = parse_qsl(query)
        if params != sorted(params):
            canonical = f"{request.url.path}?" + "&".join(f"{k}={v}" for k, v in sorted(params))
            return httpx.Response(400, json={"error": "use this route's canonical URL", "canonical": canonical})
        return httpx.Response(200, json=pages[dict(params).get("cursor")])

    return handler


def test_trade_history_pages_past_the_first():
    pages = {
        None: {"items": [{"id": 1, "timestampMs": 3_000}], "nextCursor": "eyJhZnRlciI6MX0"},
        "eyJhZnRlciI6MX0": {"items": [{"id": 2, "timestampMs": 2_000}], "nextCursor": None},
    }
    seen: list[str] = []
    trades = reconcile.fetch_trades(_api(_strict_history(pages, seen)), USER)
    assert [t["id"] for t in trades] == [1, 2]
    assert seen == ["limit=75", "cursor=eyJhZnRlciI6MX0&limit=75"]  # canonical at the first try


def test_a_get_follows_the_canonical_url_once():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(400, json={"error": "use this route's canonical URL", "canonical": "/query/notices?b=1"})

    with pytest.raises(PapertradeError, match="canonical"):
        _api(handler).get("/query/notices?a=1")
    assert seen == ["/query/notices?a=1", "/query/notices?b=1"]


def test_a_post_never_follows_a_canonical_url():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(400, json={"error": "nope", "canonical": "/elsewhere"})

    with pytest.raises(PapertradeError):
        _api(handler).post("/funding/deposits/check", {"user": USER})
    assert seen == ["/funding/deposits/check"]
