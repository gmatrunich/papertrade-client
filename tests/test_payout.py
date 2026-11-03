from __future__ import annotations

import pytest

from papertrade_client import payout

ENTRY = 775_000  # $77,500 at BTC's price scale of 10


@pytest.fixture
def btc(trading):
    return payout.Impact.of(trading.instrument(0))


def settle(btc, move, curve=None, fee=0.0, **kw):
    return payout.settle(
        is_long=True,
        entry_raw=ENTRY,
        mark_raw=ENTRY * (1 + move),
        margin_usd=100,
        leverage=100,
        impact=btc,
        curve=curve,
        win_fee_rate=fee,
        **kw,
    )


def test_docs_keep_fractions(btc):
    # Docs: "On BTC, counting the deadband, a 1% move keeps about 88% of the raw gain and a 0.1% move about 74%."
    assert settle(btc, 0.01).keep_fraction == pytest.approx(0.88, abs=0.005)
    assert settle(btc, 0.001).keep_fraction == pytest.approx(0.74, abs=0.005)


@pytest.mark.parametrize(("curve", "peak_bps", "at_bps"), [(payout.STANDARD_CURVE, 1.2, 3.4), (payout.OFF_HOURS_CURVE, 2.0, 4.2)])
def test_docs_holdback_peaks(curve, peak_bps, at_bps):
    peak, where = max((payout.holdback(ENTRY * b / 1e4, ENTRY, curve) / ENTRY * 1e4, b) for b in [x / 100 for x in range(1, 1200)])
    assert peak == pytest.approx(peak_bps, abs=0.05)
    assert where == pytest.approx(at_bps, abs=0.05)


def test_holdback_only_inside_the_band(btc):
    assert settle(btc, 0.0050, payout.STANDARD_CURVE).payout_pnl_usd == settle(btc, 0.0050).payout_pnl_usd
    assert settle(btc, 0.0004, payout.OFF_HOURS_CURVE).payout_pnl_usd < settle(btc, 0.0004, payout.STANDARD_CURVE).payout_pnl_usd


def test_payout_rises_with_the_move(btc):
    for curve in (None, payout.STANDARD_CURVE, payout.OFF_HOURS_CURVE):
        payouts = [settle(btc, b / 1e4, curve).payout_pnl_usd for b in range(1, 200)]
        assert payouts == sorted(payouts)


def test_deadband_zeroes_tiny_wins(btc):
    assert settle(btc, 0.00001).payout_pnl_usd == 0


def test_win_fee(btc):
    assert settle(btc, 0.01, fee=0.02).payout_pnl_usd == pytest.approx(settle(btc, 0.01).payout_pnl_usd * 0.98)


def test_losses_are_raw_and_floored(btc):
    loss = settle(btc, -0.005)
    assert loss.payout_pnl_usd == pytest.approx(-50) and not loss.liquidated
    assert settle(btc, -0.02).payout_pnl_usd == -100


def test_bust_forfeits_the_margin(btc):
    bust = payout.estimate_bust_price(ENTRY, 100, True, 0.000476)
    s = settle(btc, (bust - ENTRY) / ENTRY - 1e-6, bust_raw=bust)
    assert s.liquidated and s.payout_pnl_usd == -100
    # Just before the trigger, a voluntary close still returns most of the margin.
    assert settle(btc, (bust - ENTRY) / ENTRY + 1e-4, bust_raw=bust).payout_pnl_usd > -100


def test_curve_in_force(trading):
    assert payout.curve_in_force(trading) is (payout.OFF_HOURS_CURVE if trading.off_hours else payout.STANDARD_CURVE)
