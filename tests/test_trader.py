from __future__ import annotations

import dataclasses
import json
import time

import pytest
from eth_account import Account

from papertrade_client import guards, protocol, wire
from papertrade_client.api import AcknowledgementUnknown, PapertradeError
from papertrade_client.config import Config, Limits
from papertrade_client.exits import ExitPlan
from papertrade_client.hyperliquid import Bbo
from papertrade_client.store import AuditLog, StateStore
from papertrade_client.stream import OutcomeLog
from papertrade_client.trader import GuardError, PairBroken, Trader, pair_cost_curve

from .conftest import account_frame, position_tuple

NOW = 1_793_700_000.0
LIMITS = Limits(max_margin_usd=50, max_leverage=100, max_open_positions=2, max_total_margin_usd=100)


class FakeApi:
    def __init__(self, trading):
        self.trading = trading
        self.submitted: list[tuple[str, dict, str, dict | None]] = []
        self.fail_with: Exception | None = None

    def trading_state(self, max_age_s=None):
        return self.trading

    def submit(self, action, signed, intent_id, extra=None):
        self.submitted.append((action, signed, intent_id, extra))
        if self.fail_with:
            raise self.fail_with
        return {"ok": True, "action": action, "intentId": intent_id, "state": "queued"}


class FakeStream:
    def __init__(self, frame):
        self._state = wire.parse_account_snapshot(frame) if frame else None
        self.outcomes = OutcomeLog()
        self.listeners = []

    def add_listener(self, listener):
        self.listeners.append(listener)

    def state(self):
        return self._state

    def age_s(self):
        return 1.0

    def wait_for(self, predicate, timeout_s):
        if predicate(self._state):
            return self._state
        raise TimeoutError


class FakeHl:
    def __init__(self, mid=77_500.0):
        self.mid = mid

    def bbo(self, coin):
        return Bbo(coin, self.mid - 0.5, self.mid + 0.5, time.time())


@pytest.fixture
def setup(tmp_path, trading):
    wallet = Account.create()
    session_key = Account.create()

    def make(live=True, frame=None, limits=LIMITS, stream_down=False):
        frame = frame or account_frame(
            address=wallet.address.lower(), session_keys=[[session_key.address.lower(), str(int(NOW) + 30 * 86400)]]
        )
        store = StateStore(tmp_path / "data")
        store.update(
            lambda s: s.__setitem__(
                "session_keys",
                [
                    {
                        "private_key": session_key.key.hex(),
                        "address": session_key.address.lower(),
                        "expires_at": int(NOW) + 30 * 86400,
                        "registered_at": NOW - 100,
                        "intent_id": None,
                    }
                ],
            )
        )
        api = FakeApi(trading)
        config = Config(wallet_private_key="0x" + "11" * 32, data_dir=str(tmp_path / "data"), live_trading=live, limits=limits)
        stream = FakeStream(None if stream_down else frame)
        trader = Trader(config, wallet, api, stream, FakeHl(), store, AuditLog(tmp_path / "data"), clock=lambda: NOW)
        return trader, api, store

    return wallet, session_key, make


def test_dry_run_by_default_sends_nothing(setup):
    _, _, make = setup
    trader, api, _ = make(live=False)
    result = trader.open("BTC", "long", "10", 50, dry_run=False)  # live trading is off: still a dry run
    assert result.mode == "dry-run" and not api.submitted and not result.violations
    assert "v" not in result.message


def test_live_open_is_signed_by_the_session_key(setup):
    wallet, session_key, make = setup
    trader, api, store = make()
    result = trader.open("BTC", "long", "10", 50)
    assert result.mode == "live" and len(api.submitted) == 1
    action, signed, intent_id, _ = api.submitted[0]
    assert action == "openPosition" and signed["user"] == wallet.address.lower() and signed["size"] == str(10 * 10**18)
    message = {k: signed[k] for k in ("user", "instrumentId", "isLong", "size", "leverage", "useDebt", "nonce", "deadline")}
    assert (
        protocol.recover("OpenPosition", message, int(signed["v"]), int(signed["r"], 16), int(signed["s"], 16))
        == session_key.address.lower()
    )
    assert result.submissions[0].intent_id == intent_id and result.submissions[0].state == "queued"
    # Nonces count up.
    trader.open("BTC", "short", "10", 50)
    assert int(api.submitted[1][1]["nonce"]) == int(signed["nonce"]) + 1
    audit = [json.loads(line)["event"] for line in (store.dir / "audit.jsonl").read_text().splitlines()]
    assert audit == ["submit", "accepted", "submit", "accepted"]


def test_guards_refuse_before_signing(setup):
    _, _, make = setup
    trader, api, _ = make()
    with pytest.raises(GuardError) as refused:
        trader.open("BTC", "long", "5", 50)  # below the $10 minimum margin, and below $500 notional
    assert {"margin_below_min", "notional_below_min"} <= {v.code for v in refused.value.violations}
    with pytest.raises(GuardError, match="MAX_MARGIN_USD"):
        trader.open("BTC", "long", "60", 50)
    trader.store.set_kill_switch(True, "test")
    with pytest.raises(GuardError, match="kill switch"):
        trader.open("BTC", "long", "10", 50)
    assert not api.submitted


def test_unacknowledged_open_is_never_resubmitted(setup):
    _, _, make = setup
    trader, api, store = make(limits=Limits(max_margin_usd=50, max_leverage=100, max_open_positions=1, max_total_margin_usd=100))
    api.fail_with = AcknowledgementUnknown("0x00", TimeoutError())
    result = trader.open("BTC", "long", "10", 50)
    assert result.submissions[0].acknowledged is False
    assert store.read()["inflight"][0] | {"submitted_at": None} == {
        "intent_id": result.submissions[0].intent_id,
        "action": "openPosition",
        "deadline": int(NOW) + 3600,
        "submitted_at": None,
        "acknowledged": False,
        "margin_raw": str(10 * 10**18),
    }
    # It still counts as a pending position, so the next open is refused instead of doubling up.
    api.fail_with = None
    with pytest.raises(GuardError, match="MAX_OPEN_POSITIONS"):
        trader.open("BTC", "long", "10", 50)
    assert len(api.submitted) == 1


def test_relayer_refusal_is_raised_and_audited(setup):
    _, _, make = setup
    trader, api, store = make()
    api.fail_with = PapertradeError("nope", 422, "invalid_signature", {"error": "invalid_signature"})
    with pytest.raises(PapertradeError):
        trader.open("BTC", "long", "10", 50)
    assert json.loads((store.dir / "audit.jsonl").read_text().splitlines()[-1])["event"] == "rejected"


def test_close_batches_and_bust_warning(setup):
    wallet, session_key, make = setup
    positions = [position_tuple(i, bust_raw=780_000 if i == 1 else 700_000) for i in range(1, 15)]
    frame = account_frame(
        address=wallet.address.lower(), positions=positions, session_keys=[[session_key.address.lower(), str(int(NOW) + 30 * 86400)]]
    )
    trader, api, _ = make(frame=frame)
    result = trader.close_all()
    assert [len(json.loads(json.dumps(b))["positionIds"]) for b in result.message["batches"]] == [12, 2]
    assert [a for a, *_ in api.submitted] == ["closePositions", "closePositions"]
    assert any("position 1" in w for w in result.warnings)  # mark 77,500 is below that long's bust at 78,000
    with pytest.raises(GuardError, match="not open"):
        trader.close([999])


def test_withdraw_uses_usdc_units_and_the_wallet_key(setup):
    wallet, _, make = setup
    trader, api, _ = make()
    trader.withdraw("12.5")
    action, signed, _, extra = api.submitted[0]
    assert action == "withdrawToCore" and signed["amount"] == "12500000" and extra == {"withdrawal": {"route": "hypercore"}}
    message = {k: signed[k] for k in ("user", "amount", "nonce", "deadline")}
    assert (
        protocol.recover("WithdrawToCore", message, int(signed["v"]), int(signed["r"], 16), int(signed["s"], 16)) == wallet.address.lower()
    )
    with pytest.raises(GuardError, match="minimum"):
        trader.withdraw("5")


def test_session_must_be_registered(setup):
    wallet, _, make = setup
    trader, api, _ = make(frame=account_frame(address=wallet.address.lower()))  # snapshot lists no session key
    with pytest.raises(GuardError, match="session"):
        trader.open("BTC", "long", "10", 50)
    assert not api.submitted


def test_acknowledged_open_counts_until_the_stream_shows_it(setup):
    _, _, make = setup
    trader, api, store = make(limits=Limits(max_margin_usd=50, max_leverage=100, max_open_positions=1, max_total_margin_usd=100))
    first = trader.open("BTC", "long", "10", 50)
    assert first.submissions[0].acknowledged and trader.inflight()[0]["intent_id"] == first.submissions[0].intent_id
    with pytest.raises(GuardError, match="MAX_OPEN_POSITIONS"):
        trader.open("BTC", "long", "10", 50)
    # Once the stream reports an outcome, the in-flight entry stops counting.
    trader.stream.outcomes.intents[first.submissions[0].intent_id] = "failed"
    assert trader.inflight() == []
    trader.open("BTC", "long", "10", 50)
    assert len(api.submitted) == 2


def test_outcomes_outlive_the_process_that_saw_them(setup):
    # 2026-11-04: `open --live` and `close-all --live` exited before their outcomes arrived; once the
    # position had closed, later processes counted the open as in flight until its one-hour deadline.
    wallet, session_key, make = setup
    keys = [[session_key.address.lower(), str(int(NOW) + 30 * 86400)]]
    limits = Limits(max_margin_usd=50, max_leverage=100, max_open_positions=1, max_total_margin_usd=100)
    trader, api, store = make(limits=limits)
    opened = trader.open("BTC", "long", "10", 50).submissions[0].intent_id

    with_position = account_frame(address=wallet.address.lower(), session_keys=keys, positions=[position_tuple(intent_id=opened)])
    trader, api, store = make(limits=limits, frame=with_position)
    trader._record_outcomes(trader.stream.state())
    closed = trader.close([1]).submissions[0].intent_id
    assert [i["intent_id"] for i in trader.inflight()] == [closed]

    later, api, store = make(limits=limits)  # a new process: the position is gone, no outcome event seen
    assert later.intent_status(opened) == "succeeded" and later.intent_status(closed) == "positions_closed"
    assert later.inflight() == []
    later.open("BTC", "long", "10", 50)
    assert len(api.submitted) == 1


def test_refused_open_is_not_left_in_flight(setup):
    _, _, make = setup
    trader, api, store = make()
    api.fail_with = PapertradeError("nope", 422, "invalid_signature", {"error": "invalid_signature"})
    with pytest.raises(PapertradeError):
        trader.open("BTC", "long", "10", 50)
    assert store.read()["inflight"] == []


def _dated_frame(wallet, session_key, trading, age_s, blocks_behind_indexer):
    frame = account_frame(address=wallet.address.lower(), session_keys=[[session_key.address.lower(), str(int(NOW) + 30 * 86400)]])
    frame["indexedThroughTimestamp"] = int(NOW - age_s)
    frame["indexedThroughBlock"] = trading.indexed_through_block - blocks_behind_indexer
    return frame


def test_stale_indexer_blocks_opens(setup, trading):
    wallet, session_key, make = setup
    # The account copy is an hour old and the indexer has not moved since.
    trader, api, _ = make(frame=_dated_frame(wallet, session_key, trading, age_s=3600, blocks_behind_indexer=0))
    with pytest.raises(GuardError, match="indexer lag"):
        trader.open("BTC", "long", "10", 50)


def test_an_idle_account_with_a_current_indexer_can_open(setup, trading):
    wallet, session_key, make = setup
    # An idle account served from an instance whose copy is two hours old, while the indexer
    # has kept up with the chain (61 blocks a minute): what refused a third of the probes on 2026-11-05.
    trader, api, _ = make(frame=_dated_frame(wallet, session_key, trading, age_s=7200, blocks_behind_indexer=7320))
    assert trader.open("BTC", "long", "10", 50).violations == []


def test_force_close_works_without_the_account_stream(setup):
    _, session_key, make = setup
    trader, api, _ = make(stream_down=True)
    with pytest.raises(GuardError, match="snapshot"):
        trader.close([7])
    result = trader.close([7], force=True)
    action, signed, *_ = api.submitted[0]
    message = {k: signed[k] for k in ("user", "positionIds", "nonce", "deadline")}
    message["positionIds"] = [int(i) for i in message["positionIds"]]
    assert action == "closePositions" and result.mode == "live"
    assert protocol.recover("Close", message, int(signed["v"]), int(signed["r"], 16), int(signed["s"], 16)) == session_key.address.lower()


def test_bust_warnings_never_block_a_close(setup):
    wallet, session_key, make = setup
    positions = [position_tuple(1, instrument_id=9), position_tuple(2)]  # 9: an instrument this client does not know
    frame = account_frame(
        address=wallet.address.lower(), positions=positions, session_keys=[[session_key.address.lower(), str(int(NOW) + 30 * 86400)]]
    )
    trader, api, _ = make(frame=frame)
    trader.hl.bbo = lambda coin: (_ for _ in ()).throw(TimeoutError("hyperliquid down"))
    assert trader.close_all().submissions and len(api.submitted) == 1


def test_daily_baseline_is_set_by_the_first_update_of_the_day(setup):
    wallet, session_key, make = setup
    trader, _, store = make()
    keys = [[session_key.address.lower(), str(int(NOW) + 30 * 86400)]]
    for listener in trader.stream.listeners:
        listener(wire.parse_account_snapshot(account_frame(address=wallet.address.lower(), session_keys=keys, realized_pnl_raw=0)))
    # A loss realised later the same day counts against the limit.
    trader.stream._state = wire.parse_account_snapshot(
        account_frame(address=wallet.address.lower(), session_keys=keys, realized_pnl_raw=-11 * 10**18)
    )
    assert trader.realized_today_raw() == -11 * 10**18
    with pytest.raises(GuardError, match="DAILY_LOSS_LIMIT_USD"):
        trader.open("BTC", "long", "10", 50)


def test_session_registration_is_dry_run_gated(setup):
    _, _, make = setup
    trader, api, _ = make(live=False)
    with pytest.raises(GuardError, match="LIVE_TRADING"):
        trader.session.register()
    assert trader.session.maybe_renew() is False and not api.submitted


def test_failed_registration_without_a_snapshot_keeps_the_valid_key(setup):
    _, session_key, make = setup
    trader, api, store = make(stream_down=True)
    api.fail_with = PapertradeError("down", 503, None, None)
    with pytest.raises(PapertradeError):
        trader.session.register()
    assert session_key.address.lower() in [k["address"] for k in store.read()["session_keys"]]
    # Nothing renews while the account state is unknown.
    assert trader.session.maybe_renew() is False


def test_store_is_shared_between_processes(tmp_path):
    strategy, operator = StateStore(tmp_path / "data"), StateStore(tmp_path / "data")
    nonce = strategy.take_nonce("exchange", "0xc", "0xu")
    operator.set_kill_switch(True, "operator")
    assert strategy.kill_switch()["engaged"]
    assert operator.take_nonce("exchange", "0xc", "0xu") == nonce + 1
    strategy.take_nonce("exchange", "0xc", "0xu")  # must not revert the operator's kill switch
    assert operator.kill_switch()["engaged"]
    assert (tmp_path / "data" / "state.json").stat().st_mode & 0o777 == 0o600


def test_a_write_cut_short_leaves_no_copy_of_the_state(tmp_path):
    store = StateStore(tmp_path / "data")
    orphan = store.dir / ".state.dead1234.tmp"  # what a writer killed between mkstemp and os.replace leaves
    orphan.write_text('{"session_keys": [{"private_key": "0x..."}]}')
    store.update(lambda s: s["nonces"].__setitem__("k", "1"))
    assert not orphan.exists() and store.read()["nonces"] == {"k": "1"}


def test_indexer_lag_is_dated_by_the_indexer_block(trading):
    frame = account_frame()
    # Sampled on 2026-11-05: an account copy 342 s old, the indexer 346 blocks further on.
    frame["indexedThroughTimestamp"], frame["indexedThroughBlock"] = int(NOW) - 342, 50_440_087
    account = wire.parse_account_snapshot(frame)
    current = dataclasses.replace(trading, indexed_through_block=50_440_433)
    assert guards.indexer_lag_s(account, current, NOW) == pytest.approx(1.7, abs=0.1)
    stuck = dataclasses.replace(trading, indexed_through_block=50_440_087)
    assert guards.indexer_lag_s(account, stuck, NOW) == 342


# -- hedge pairs ------------------------------------------------------------------


def test_pair_dry_run_sends_nothing_and_prices_the_pair(setup):
    _, _, make = setup
    trader, api, store = make(live=False)
    result = trader.open_pair("BTC", "10", 50, dry_run=False)  # live trading is off: still a dry run
    assert result.mode == "dry-run" and result.action == "openPair" and not result.violations
    assert result.message["long"]["isLong"] is True and result.message["short"]["isLong"] is False
    assert not api.submitted and store.read()["exit_plans"] == {}
    curve = result.quote["pair_cost_curve"]
    assert all(point["net_usd"] < 0 for point in curve)  # the winner's haircut makes every ending a loss
    assert [p["move_pct"] for p in curve] == sorted(p["move_pct"] for p in curve)


def test_pair_cost_curve_is_the_winner_payout_less_the_loser_loss():
    keep = [{"move_pct": 0.1, "keep": 0.74, "payout_usd": 0.37}]
    [point] = pair_cost_curve(keep, 500)
    assert point["loser_loss_usd"] == pytest.approx(0.5)
    assert point["net_usd"] == pytest.approx(-0.13) and point["net_bps_of_leg"] == pytest.approx(-2.6)


def test_live_pair_sends_both_legs_with_a_shared_plan(setup):
    wallet, session_key, make = setup
    trader, api, store = make()
    result = trader.open_pair("BTC", "10", 50, exit_plan=ExitPlan(max_hold_s=300))
    assert result.mode == "live" and result.action == "openPair" and len(result.submissions) == 2
    assert sorted(a for a, *_ in api.submitted) == ["openPosition", "openPosition"]
    by_side = {s["isLong"]: s for _, s, _, _ in api.submitted}
    assert set(by_side) == {True, False}
    assert abs(int(by_side[True]["nonce"]) - int(by_side[False]["nonce"])) == 1
    for signed in by_side.values():
        message = {k: signed[k] for k in ("user", "instrumentId", "isLong", "size", "leverage", "useDebt", "nonce", "deadline")}
        recovered = protocol.recover("OpenPosition", message, int(signed["v"]), int(signed["r"], 16), int(signed["s"], 16))
        assert recovered == session_key.address.lower()
    plans = store.read()["exit_plans"]
    legs = sorted(i for _, _, i, _ in api.submitted)
    assert sorted(plans) == legs
    assert {p["pair"]["id"] for p in plans.values()} == {result.message["pair"]}
    assert all(sorted(p["pair"]["legs"]) == legs and p["plan"]["max_hold_s"] == 300 for p in plans.values())
    events = [json.loads(line) for line in (store.dir / "audit.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events].count("pair") == 1
    submits = [e for e in events if e["event"] == "submit"]
    assert len(submits) == 2 and {e["pair"] for e in submits} == {result.message["pair"]}


def test_a_pair_that_does_not_fit_the_limits_sends_nothing(setup):
    _, _, make = setup
    one_position = Limits(max_margin_usd=50, max_leverage=100, max_open_positions=1, max_total_margin_usd=100)
    trader, api, store = make(limits=one_position)
    with pytest.raises(GuardError, match="MAX_OPEN_POSITIONS"):
        trader.open_pair("BTC", "10", 50)  # the first leg fits, the second does not
    tight = Limits(max_margin_usd=50, max_leverage=100, max_open_positions=2, max_total_margin_usd=10)
    trader, api, store = make(limits=tight)
    with pytest.raises(GuardError, match="MAX_TOTAL_MARGIN_USD"):
        trader.open_pair("BTC", "10", 50)
    assert not api.submitted and store.read()["exit_plans"] == {} and store.read()["inflight"] == []
    # The dry run says the same without sending anything.
    result = trader.open_pair("BTC", "10", 50, dry_run=True)
    assert {v.code for v in result.violations} == {"limit_total_margin"}


def test_a_refused_leg_leaves_the_other_closing_at_once(setup):
    _, _, make = setup
    trader, api, store = make()
    send = api.submit

    def refuse_the_short(action, signed, intent_id, extra=None):
        if signed["isLong"] is False:
            raise PapertradeError("nope", 422, "invalid_signature", {"error": "invalid_signature"})
        return send(action, signed, intent_id, extra)

    api.submit = refuse_the_short
    with pytest.raises(PairBroken, match="short leg refused") as broken:
        trader.open_pair("BTC", "10", 50, exit_plan=ExitPlan(max_hold_s=300))
    [sent] = broken.value.result.submissions
    plans = store.read()["exit_plans"]
    assert list(plans) == [sent.intent_id] and plans[sent.intent_id]["plan"]["max_hold_s"] == 0
    assert [i["intent_id"] for i in store.read()["inflight"]] == [sent.intent_id]
    events = [json.loads(line)["event"] for line in (store.dir / "audit.jsonl").read_text().splitlines()]
    assert "pair_broken" in events and "rejected" in events


def test_both_legs_refused_leaves_nothing_behind(setup):
    _, _, make = setup
    trader, api, store = make()
    api.fail_with = PapertradeError("nope", 422, "invalid_signature", {"error": "invalid_signature"})
    with pytest.raises(PapertradeError):
        trader.open_pair("BTC", "10", 50, exit_plan=ExitPlan(max_hold_s=300))
    assert store.read()["exit_plans"] == {} and store.read()["inflight"] == []
