from __future__ import annotations

import pytest

from papertrade_client import wire

from .conftest import account_frame, position_tuple


def test_trading_state_parses(trading):
    btc = trading.instrument(0)
    assert btc.symbol == "BTC" and btc.price_scale == 10 and btc.max_leverage == 1000
    assert trading.instrument(1).symbol == "ETH"
    assert trading.exchange == "next"
    assert trading.minimum_trade_size_raw == 10 * 10**18
    assert not trading.unknown_keys


def test_trading_state_shape_change_is_loud(trading_frame):
    trading_frame["instruments"][0] = trading_frame["instruments"][0][:-1]
    with pytest.raises(wire.WireError, match="23-element"):
        wire.parse_trading_state(trading_frame)


def test_unknown_trading_keys_are_kept(trading_frame):
    trading_frame["somethingNew"] = 1
    assert wire.parse_trading_state(trading_frame).unknown_keys == {"somethingNew"}


def test_snapshot_and_patch():
    snapshot = wire.parse_account_snapshot(
        account_frame(positions=[position_tuple(1), position_tuple(2)], session_keys=[["0x" + "ab" * 20, "1800000000"]])
    )
    assert [p.position_id for p in snapshot.open_positions] == [1, 2]
    assert snapshot.session_keys == {"0x" + "ab" * 20: 1_800_000_000}

    intent = "0x" + "cd" * 32
    patch = wire.parse_account_patch(
        {
            "address": snapshot.address,
            "accountRevision": snapshot.account_revision + 1,
            "state": {
                "balance": "5",
                "funds": {"reservedBalance": "0", "reservedQueued": "0", "availableBalance": "5", "availableQueued": "0"},
            },
            "addedPositions": [position_tuple(3, intent_id=intent)],
            "removedPositionIds": ["1"],
            "addedOperations": [],
            "removedOperationIds": [],
            "closedPositions": [["1", 0, True, str(10**19), 50, False, "-123", "0"]],
            "intentOutcomes": [[intent, "succeeded"]],
        }
    )
    after = wire.apply_patch(snapshot, patch)
    assert [p.position_id for p in after.open_positions] == [2, 3]
    assert after.balance == 5 and after.funds.available_balance == 5
    assert after.position(3).intent_id == intent
    assert patch.closed_positions[0].net_pnl_raw == -123
    # Stale revisions are ignored.
    assert wire.apply_patch(after, patch) is after


def test_unknown_outcome_is_rejected():
    with pytest.raises(wire.WireError):
        wire.parse_account_patch({"accountRevision": 2, "state": {}, "intentOutcomes": [["0x" + "00" * 32, "weird"]]})


def test_snapshot_missing_field():
    frame = account_frame()
    del frame["funds"]
    with pytest.raises(wire.WireError, match="funds"):
        wire.parse_account_snapshot(frame)
