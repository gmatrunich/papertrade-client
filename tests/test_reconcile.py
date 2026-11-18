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


# -- hedge pairs ------------------------------------------------------------------

PAIR_TX = "0x" + "ab" * 32
MINT_RATE = 10.29  # PAPER per $1 of mint basis on 2026-11-13


def pair_audit(pair="p1", long_pos=11, short_pos=12):
    ts = 1793900000.0
    audit = []
    for intent, side in (("0xl", "long"), ("0xs", "short")):
        audit.append(
            {
                "ts": ts,
                "event": "submit",
                "action": "openPosition",
                "intent_id": intent,
                "market": "BTC",
                "side": side,
                "margin_usd": "10",
                "leverage": 50,
                "mid": 77500.0,
                "pair": pair,
            }
        )
    exit_ = {"reason": "max_hold", "mark": 77550.0}
    audit.append(
        {
            "ts": ts + 300,
            "event": "submit",
            "action": "closePositions",
            "intent_id": "0xc",
            "position_ids": [long_pos, short_pos],
            "exit": {str(long_pos): exit_, str(short_pos): exit_ | {"reason": "pair:max_hold"}},
        }
    )
    return audit


def leg(position_id, is_long, entry=775000, exit_=775500, pnl=0.0, fee=0.0, basis=0.0, minted=0.0, tx=PAIR_TX):
    return {
        "timestampMs": 1793900305000, "positionId": str(position_id), "instrumentId": 0, "symbol": "BTC", "priceScaleRaw": "10",
        "isLong": is_long, "entryPriceRaw": str(entry), "marginRaw": str(10 * 10**18), "leverage": 50, "kind": "PositionClosed",
        "openedAtMs": 1793900003000, "exitPriceRaw": str(exit_), "adjustedPnlRaw": str(int(pnl * 1e18)),
        "userPaidFeeRaw": str(int(fee * 1e18)), "paperMintBasisRaw": str(int(basis * 1e18)), "paperMintedRaw": str(int(minted * 1e18)),
        "txHash": tx,
    }  # fmt: skip


def a_pair(trading, entry_short=775000, tx_short=PAIR_TX, basis=0.98 * 0.3226):
    # The price rose 0.0645%: the long wins and is held back, the short loses its raw $0.3226.
    winner = leg(11, True)
    winner["adjustedPnlRaw"] = str(int(reconcile._model_pnl(winner, trading) * 1e18))
    loser = leg(12, False, entry=entry_short, pnl=-0.3226, basis=basis, minted=basis * MINT_RATE, tx=tx_short)
    return [winner, loser]


def test_a_pair_is_reported_whole(trading):
    report = reconcile.reconcile(pair_audit(), a_pair(trading), trading)
    [pair] = report["pairs"]
    assert [r["pair"] for r in report["positions"]] == ["p1", "p1"]
    assert pair["complete"] and pair["same_entry"] and pair["same_close_tx"]
    assert pair["entry_gap_bps"] == 0 and pair["exit_gap_bps"] == 0
    win = pair["pnl_usd"] + 0.3226  # what the long was paid, held back by the payout curve
    assert 0.1 < win < 0.25 and pair["pnl_usd"] < 0
    assert report["discrepancies"] == []
    assert pair["loser_loss_usd"] == pytest.approx(0.3226)
    assert pair["paper_per_basis"] == pytest.approx(MINT_RATE)
    assert pair["basis_per_loss"] == pytest.approx(0.98)  # a win offsetting the loss would show here as less
    assert pair["cost_per_basis_usd"] == pytest.approx(-pair["pnl_usd"] / (0.98 * 0.3226), rel=1e-3)
    assert pair["cost_per_paper_usd"] == pytest.approx(pair["cost_per_basis_usd"] / MINT_RATE, rel=1e-3)


def test_legs_that_entered_apart_or_closed_apart_are_flagged(trading):
    # The short entered 5 units (0.65 bps) lower than the long and closed in another transaction.
    report = reconcile.reconcile(pair_audit(), a_pair(trading, entry_short=774995, tx_short="0x" + "cd" * 32), trading)
    [pair] = report["pairs"]
    assert not pair["same_entry"] and not pair["same_close_tx"]
    assert pair["entry_gap_bps"] == pytest.approx(5 / 774997.5 * 1e4)


def test_a_pair_with_one_closed_leg_is_incomplete(trading):
    report = reconcile.reconcile(pair_audit(), a_pair(trading)[:1], trading)
    [pair] = report["pairs"]
    assert not pair["complete"] and pair["positions"] == [11]
    assert "1 broken or open" in reconcile.digest(report, None)


def test_pairs_digest_and_plain_trades_stay_as_before(trading):
    text = reconcile.digest(reconcile.reconcile(pair_audit(), a_pair(trading), trading), 24)
    assert "pairs: 1 complete" in text and "same entry 1/1" in text and "same close tx 1/1" in text and "PAPER minted 3.3" in text
    plain = reconcile.reconcile(AUDIT, [TRADE], trading, open_intents=set())
    assert plain["pairs"] == [] and "pairs:" not in reconcile.digest(plain, 24)
