from __future__ import annotations

import copy

import pytest

from papertrade_client import reconcile

OPEN = "0x9311619e08180e21b243773526c6c4b94f79534c6c397c4018d93815730883d7"
CLOSE = "0x6d66a141c400e76005e637c1d94afb79310d7ee46a20b618b9469c548a6ea171"

# The first live trade, 2026-11-04, as the audit log and the indexer recorded it.
AUDIT = [
    {"at": "2026-11-04T14:40:24Z", "event": "submit", "action": "openPosition", "intent_id": OPEN,
     "market": "BTC", "side": "long", "margin_usd": "10", "leverage": 50, "mid": 78773.5},
    {"at": "2026-11-04T14:40:25Z", "event": "accepted", "action": "openPosition", "intent_id": OPEN},
    {"at": "2026-11-04T14:41:14Z", "event": "submit", "action": "closePositions", "intent_id": CLOSE, "position_ids": [2106422]},
    {"at": "2026-11-04T14:41:14Z", "event": "accepted", "action": "closePositions", "intent_id": CLOSE},
]  # fmt: skip
TRADE = {
    "timestampMs": 1793803320000,
    "positionId": "2106422",
    "instrumentId": 0,
    "symbol": "BTC",
    "priceScaleRaw": "10",
    "isLong": True,
    "entryPriceRaw": "787995",
    "marginRaw": "10000000000000000000",
    "leverage": 50,
    "kind": "PositionClosed",
    "openedAtMs": 1793803232000,
    "exitPriceRaw": "791415",
    "adjustedPnlRaw": "1862829936359721314",
    "userPaidFeeRaw": "37256598727194426",
}


def test_first_live_trade(trading):
    report = reconcile.reconcile(AUDIT, [TRADE], trading, open_intents=set(), now_s=1793810000)
    [row] = report["positions"]
    assert row["open_intent"] == OPEN and row["close_reason"] == "manual"
    assert row["open_latency_s"] == 8.0 and row["close_latency_s"] == 46.0
    assert row["entry_slippage_bps"] == pytest.approx(3.3006, abs=1e-4)
    assert row["pnl_usd"] == pytest.approx(1.825573, abs=1e-6)
    assert row["model_pnl_usd"] == pytest.approx(row["pnl_usd"], abs=reconcile.MODEL_TOLERANCE_USD)
    assert report["discrepancies"] == []
    assert report["summary"]["positions"] == 1 and report["summary"]["wins"] == 1


def test_engine_exit_reason_and_exit_slippage(trading):
    audit = copy.deepcopy(AUDIT)
    audit[2]["exit"] = {"2106422": {"reason": "max_hold", "mark": 79150.0}}
    audit.append({"at": "2026-11-04T14:40:33Z", "event": "position", "position_id": 2106422, "intent_id": OPEN})
    [row] = reconcile.reconcile(audit, [TRADE], trading)["positions"]
    assert row["close_reason"] == "max_hold"
    assert row["exit_slippage_bps"] == pytest.approx((79150.0 - 79141.5) / 79150.0 * 1e4)


def test_discrepancies(trading):
    foreign = TRADE | {"positionId": "99", "openedAtMs": 1793700000000, "timestampMs": 1793700060000}
    wrong_pnl = TRADE | {"adjustedPnlRaw": str(5 * 10**18)}
    report = reconcile.reconcile(AUDIT, [foreign, wrong_pnl], trading)
    assert any("#99: no audited open" in d for d in report["discrepancies"])
    assert any("#99: closed without an audited close" in d for d in report["discrepancies"])
    assert any("#2106422: booked $4.9" in d for d in report["discrepancies"])


def test_accepted_open_without_a_position(trading):
    report = reconcile.reconcile(AUDIT, [], trading, open_intents=set(), now_s=1793810000)
    assert report["discrepancies"] == [f"open {OPEN[:10]}… was accepted but no closed position matches it"]
    assert reconcile.reconcile(AUDIT, [], trading, open_intents={OPEN}, now_s=1793810000)["discrepancies"] == []
    assert reconcile.reconcile(AUDIT, [], trading, open_intents=None, now_s=1793810000)["discrepancies"] == []


def test_since_window(trading):
    assert reconcile.reconcile(AUDIT, [TRADE], trading, since_s=1793803321)["positions"] == []


def test_digest(trading):
    text = reconcile.digest(reconcile.reconcile(AUDIT, [TRADE], trading, open_intents=set()), 24)
    assert "1 positions, 1 wins, pnl $+1.83" in text and "close latency median 46.0 s" in text
