from __future__ import annotations

import json
from pathlib import Path

import pytest
from eth_account import Account

from papertrade_client import deployment, drift, onchain, payout, protocol

VECTORS = Path(__file__).parent / "fixtures" / "onchain_vectors.json"


def test_sign_round_trip_and_wire_format():
    key = Account.create()
    message = {
        "user": key.address,
        "instrumentId": 0,
        "isLong": True,
        "size": 10 * 10**18,
        "leverage": 50,
        "useDebt": False,
        "nonce": 1234,
        "deadline": 1_800_000_000,
    }
    signed = protocol.sign(key, "OpenPosition", message)

    assert signed.signer == key.address.lower()
    assert signed.wire["user"] == key.address.lower()
    assert signed.wire["size"] == str(10 * 10**18) and signed.wire["leverage"] == "50" and signed.wire["isLong"] is True
    assert signed.wire["v"] in ("27", "28") and len(signed.wire["r"]) == 66
    v, r, s = int(signed.wire["v"]), int(signed.wire["r"], 16), int(signed.wire["s"], 16)
    assert protocol.recover("OpenPosition", message, v, r, s) == key.address.lower()
    assert signed.intent_id == protocol.intent_id(protocol.digest("OpenPosition", message), key.address)
    assert "v" not in signed.unsigned()


def test_domain_changes_the_digest():
    message = {"user": deployment.ZERO_ADDRESS, "nonce": 1, "deadline": 2}
    assert protocol.digest("Claim", message) != protocol.digest("Claim", message, protocol.EXCHANGE_DOMAIN)


def test_missing_field_is_rejected():
    with pytest.raises(ValueError, match="missing"):
        protocol.digest("Close", {"user": deployment.ZERO_ADDRESS, "nonce": 1, "deadline": 2})


def test_preview_never_uses_the_given_nonce():
    signed = protocol.preview("Claim", {"user": deployment.ZERO_ADDRESS, "nonce": 7, "deadline": 2})
    assert signed.wire["nonce"] != "7"


@pytest.mark.skipif(not VECTORS.exists(), reason="run scripts/capture_vectors.py first")
def test_live_relayer_vectors_verify_with_our_encoding():
    """Signed intents taken from real BatchExecutor transactions on HyperEVM."""
    vectors = json.loads(VECTORS.read_text())
    assert {"OpenPosition", "Close", "RegisterSessionKey", "WithdrawToCore"} <= vectors.keys()
    for primary_type, items in vectors.items():
        for item in items:
            call = onchain.BatchCall(item["target"], bytes.fromhex(item["data"][2:]), item["intentId"])
            verified = onchain.verify_call(call)
            assert verified is not None, f"{primary_type} {item['tx']}"
            assert verified.primary_type == primary_type
            assert verified.signer == item["signer"]


def test_pinned_protocol_matches_the_code():
    pinned = drift.load_pinned()
    domains = pinned["bundle"]["domains"]
    assert domains["exchange"] == [protocol.EXCHANGE_DOMAIN.name, protocol.EXCHANGE_DOMAIN.version]
    assert domains["paperStaking"] == [protocol.PAPER_STAKING_DOMAIN.name, protocol.PAPER_STAKING_DOMAIN.version]
    assert domains["cancellation"] == [protocol.CANCELLATION_DOMAIN.name, protocol.CANCELLATION_DOMAIN.version]

    d = pinned["bundle"]["deployment"]
    assert d["chainId"] == deployment.CHAIN_ID
    assert d["addresses"]["exchange"] == deployment.EXCHANGE
    assert d["addresses"]["paperStaking"] == deployment.PAPER_STAKING
    assert d["addresses"]["depositProxyFactory"] == deployment.DEPOSIT_PROXY_FACTORY
    assert d["addresses"]["exchangeNextImplementation"] == deployment.EXCHANGE_IMPLEMENTATION
    assert pinned["implementations"]["exchange"] == deployment.EXCHANGE_IMPLEMENTATION
    assert d["profitThresholdDivisor"] == payout.DEADBAND_DIVISOR

    for name, curve in (("profitCarveOut", payout.STANDARD_CURVE), ("profitOffHours", payout.OFF_HOURS_CURVE)):
        live = d["curves"][name]
        assert (live["startPpm"], live["endPpm"], live["cubicFall"]) == (curve.start_ppm, curve.end_ppm, curve.cubic_fall)
        assert int(live["holdbackWad"]) / 10**18 == pytest.approx(curve.holdback)


def test_rpc_backs_off_when_rate_limited(monkeypatch):
    import httpx

    replies = iter(
        [
            httpx.Response(429),
            httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limited"}}),
            httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x10"}),
        ]
    )
    monkeypatch.setattr(onchain.time, "sleep", lambda s: None)
    rpc = onchain.Rpc(client=httpx.Client(transport=httpx.MockTransport(lambda request: next(replies))))
    assert rpc.block_number() == 16
