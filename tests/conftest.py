from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from papertrade_client import wire

FIXTURES = Path(__file__).parent / "fixtures"
WALLET = "0x1111111111111111111111111111111111111111"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def trading_frame():
    return load("trading_state.json")


@pytest.fixture
def trading(trading_frame):
    return wire.parse_trading_state(trading_frame)


def account_frame(
    *,
    address: str = WALLET,
    available_usd: float = 100,
    positions: list | None = None,
    operations: list | None = None,
    session_keys: list | None = None,
    realized_pnl_raw: int = 0,
):
    frame = copy.deepcopy(load("account_empty.json"))
    frame["address"] = address
    frame["balance"] = str(int(available_usd * 10**18))
    frame["funds"]["availableBalance"] = str(int(available_usd * 10**18))
    frame["openPositions"] = positions or []
    frame["operations"] = operations or []
    frame["sessionKeys"] = session_keys or []
    frame["lifetime"]["realizedPnlRaw"] = str(realized_pnl_raw)
    return frame


def position_tuple(
    position_id=1, instrument_id=0, is_long=True, entry_raw=775_000, margin_usd=10, leverage=50, bust_raw=760_000, intent_id=None
):
    return [
        str(position_id),
        instrument_id,
        is_long,
        False,
        str(entry_raw),
        str(margin_usd * 10**18),
        leverage,
        str(bust_raw),
        1_793_700_000_000,
        intent_id,
    ]
