"""Read-only checks against the live endpoints: ``pytest -m network``."""

from __future__ import annotations

import pytest

from papertrade_client import api, drift, onchain

pytestmark = pytest.mark.network


def test_live_trading_state_parses():
    state = api.PapertradeApi().trading_state()
    assert {i.symbol for i in state.instruments} >= {"BTC", "ETH"}


def test_live_drift_has_no_breaking_change():
    report = drift.run(batch_blocks=120)
    assert report.level not in ("breaking", "error"), report.markdown()


def test_live_deposit_proxy_derivation():
    # Read-only: the factory's CREATE2 derivation (the relayer's /funding/deposits/check registers addresses).
    proxy = onchain.Rpc().compute_deposit_proxy("0x" + "12" * 20)
    assert proxy.startswith("0x") and len(proxy) == 42 and proxy != "0x" + "00" * 20
