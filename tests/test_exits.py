from __future__ import annotations

import pytest
from eth_account import Account

from papertrade_client import exits
from papertrade_client.config import Config, Limits
from papertrade_client.exits import ExitEngine, ExitPlan, trigger
from papertrade_client.store import AuditLog, StateStore
from papertrade_client.trader import Trader
from papertrade_client.wire import Position

from .conftest import account_frame, position_tuple
from .test_trader import FakeApi, FakeHl, FakeStream

NOW = 1_793_700_000.0


def pos(is_long=True, entry=775_000, bust=760_000, age_s=60, intent="0xaa"):
    return Position(1, 0, is_long, False, entry, 10 * 10**18, 50, bust, int((NOW - age_s) * 1000), intent)


@pytest.mark.parametrize(
    "position, plan, mark, expected",
    [
        (pos(), None, 775_000, None),
        (pos(), None, 767_600, None),
        (pos(), None, 767_500, "safety_stop"),  # half way to bust
        (pos(), None, 759_000, None),  # past bust: a close would settle as a liquidation anyway
        (pos(), ExitPlan(stop_pct=0.5), 771_125, "stop"),
        (pos(), ExitPlan(stop_pct=0.5), 771_200, None),
        (pos(), ExitPlan(take_pct=0.2), 776_550, "take_profit"),
        (pos(age_s=60), ExitPlan(max_hold_s=30), 775_000, "max_hold"),
        (pos(age_s=10), ExitPlan(max_hold_s=30), 775_000, None),
        (pos(is_long=False, bust=790_000), None, 782_500, "safety_stop"),
        (pos(is_long=False, bust=790_000), ExitPlan(stop_pct=0.5), 778_875, "stop"),
        (pos(is_long=False, bust=790_000), ExitPlan(take_pct=0.2), 773_450, "take_profit"),
        (pos(is_long=False, bust=790_000), ExitPlan(take_pct=0.2), 776_000, None),
    ],
)
def test_trigger(position, plan, mark, expected):
    assert trigger(position, plan, mark, NOW, stop_fraction=0.5) == expected


@pytest.fixture
def engine(tmp_path, trading):
    wallet, session_key = Account.create(), Account.create()

    def make(live=True, positions=None, mid=77_500.0, plans=None):
        frame = account_frame(
            address=wallet.address.lower(),
            session_keys=[[session_key.address.lower(), str(int(NOW) + 30 * 86400)]],
            positions=positions,
        )
        store = StateStore(tmp_path / "data")
        key = {
            "private_key": session_key.key.hex(),
            "address": session_key.address.lower(),
            "expires_at": int(NOW) + 30 * 86400,
            "registered_at": NOW - 100,
            "intent_id": None,
        }
        store.update(lambda s: s.update(session_keys=[key], exit_plans=plans or {}))
        clock = {"now": NOW}
        config = Config(wallet_private_key="0x" + "11" * 32, data_dir=str(tmp_path / "data"), live_trading=live, limits=Limits())
        api = FakeApi(trading)
        trader = Trader(config, wallet, api, FakeStream(frame), FakeHl(mid), store, AuditLog(tmp_path / "data"), clock=lambda: clock["now"])
        return ExitEngine(trader), api, store, clock

    return make


def held(intent="0xaa", age_s=600):
    return [position_tuple(position_id=1, intent_id=intent)[:8] + [int((NOW - age_s) * 1000), intent]]


def test_engine_closes_once_then_retries_an_unresolved_close(engine):
    plans = {"0xaa": {"plan": {"stop_pct": None, "take_pct": None, "max_hold_s": 300}, "created_at": NOW - 600}}
    eng, api, store, clock = engine(positions=held(), plans=plans)

    due = eng.tick()
    assert [(d["position_id"], d["reason"]) for d in due] == [(1, "max_hold")]
    assert [s[0] for s in api.submitted] == ["closePositions"]
    audit = [line for line in (store.dir / "audit.jsonl").read_text().splitlines()]
    assert '"event": "position"' in audit[0] and '"reason": "max_hold"' in audit[1]

    assert eng.tick() == [] and len(api.submitted) == 1  # the close is still in flight
    clock["now"] = NOW + exits.RETRY_AFTER_S + 1
    eng.tick()
    assert len(api.submitted) == 2


def test_positions_without_a_plan_get_the_safety_stop(engine):
    eng, api, _, _ = engine(positions=held(intent=None), mid=76_700.0)
    assert [d["reason"] for d in eng.tick()] == ["safety_stop"]
    assert len(api.submitted) == 1


def test_dry_run_engine_sends_nothing(engine):
    eng, api, _, _ = engine(live=False, positions=held(intent=None), mid=76_700.0)
    assert len(eng.tick()) == 1 and api.submitted == []
    assert eng.tick() == []  # not repeated every second


def test_quiet_market_sends_nothing(engine):
    eng, api, _, _ = engine(positions=held(intent=None))
    assert eng.tick() == [] and api.submitted == []


def test_old_plans_without_a_position_are_dropped(engine):
    plans = {
        "0xold": {"plan": {"stop_pct": 1, "take_pct": None, "max_hold_s": None}, "created_at": NOW - 3 * 3600},
        "0xnew": {"plan": {"stop_pct": 1, "take_pct": None, "max_hold_s": None}, "created_at": NOW - 60},
        "0xaa": {"plan": {"stop_pct": 1, "take_pct": None, "max_hold_s": None}, "created_at": NOW - 3 * 3600},
    }
    eng, _, store, _ = engine(positions=held(), plans=plans)
    eng.tick()
    assert set(store.read()["exit_plans"]) == {"0xnew", "0xaa"}


def test_heartbeat_reports_mode_and_positions(engine):
    eng, _, store, _ = engine(positions=held(intent=None))
    eng._heartbeat()
    beat = exits.read_heartbeat(store.dir)
    assert beat["mode"] == "live" and beat["open_positions"] == 1 and beat["at"] == NOW


def test_open_stores_the_exit_plan_only_when_live(engine):
    eng, _, store, _ = engine()
    result = eng.trader.open("BTC", "long", "10", 50, exit_plan=ExitPlan(max_hold_s=300))
    intent = result.submissions[0].intent_id
    assert store.read()["exit_plans"][intent]["plan"] == {"stop_pct": None, "take_pct": None, "max_hold_s": 300}

    eng, _, store, _ = engine(live=False)
    eng.trader.open("BTC", "long", "10", 50, exit_plan=ExitPlan(max_hold_s=300))
    assert store.read()["exit_plans"] == {}


# -- hedge pairs ------------------------------------------------------------------

LEGS = ["0xaa", "0xbb"]


def pair_plans(max_hold_s=None, created=NOW - 600):
    plan = {"stop_pct": None, "take_pct": None, "max_hold_s": max_hold_s}
    record = {"plan": plan, "created_at": created, "pair": {"id": "p1", "legs": LEGS}}
    return {intent: dict(record) for intent in LEGS}


def pair_positions(age_s=600, both=True):
    ts = int((NOW - age_s) * 1000)
    long = position_tuple(position_id=1, is_long=True, intent_id="0xaa")[:8] + [ts, "0xaa"]
    short = position_tuple(position_id=2, is_long=False, bust_raw=790_000, intent_id="0xbb")[:8] + [ts, "0xbb"]
    return [long, short] if both else [long]


def closed_ids(api):
    return [sorted(int(i) for i in signed["positionIds"]) for action, signed, *_ in api.submitted if action == "closePositions"]


def test_one_due_leg_closes_both_in_one_intent(engine):
    # The market is 1% down: the long is half way to bust (safety stop); the short is winning and not due.
    eng, api, _, _ = engine(positions=pair_positions(), plans=pair_plans(), mid=76_700.0)
    due = eng.tick()
    assert {d["position_id"]: d["reason"] for d in due} == {1: "safety_stop", 2: "pair:safety_stop"}
    assert closed_ids(api) == [[1, 2]]  # one Close intent: the same batch, the same mark
    assert eng.tick() == [] and len(api.submitted) == 1  # both legs count as closing


def test_both_legs_due_by_the_holding_limit_close_together(engine):
    eng, api, _, _ = engine(positions=pair_positions(), plans=pair_plans(max_hold_s=300))
    assert {d["reason"] for d in eng.tick()} == {"max_hold"}
    assert closed_ids(api) == [[1, 2]]


def test_a_quiet_pair_is_left_alone(engine):
    eng, api, _, _ = engine(positions=pair_positions(age_s=100), plans=pair_plans(max_hold_s=300))
    assert eng.tick() == [] and api.submitted == []


def test_a_leg_whose_partner_is_gone_closes_after_the_grace(engine):
    # The partner was liquidated or closed on its own: what is left is no longer a hedge.
    eng, api, _, _ = engine(positions=pair_positions(age_s=exits.PAIR_GRACE_S + 1, both=False), plans=pair_plans())
    assert [d["reason"] for d in eng.tick()] == ["pair_broken"]
    assert closed_ids(api) == [[1]]


def test_a_young_leg_waits_for_its_partner_to_show(engine):
    eng, api, _, _ = engine(positions=pair_positions(age_s=10, both=False), plans=pair_plans())
    assert eng.tick() == [] and api.submitted == []


def test_a_young_leg_whose_partner_was_refused_closes_at_once(engine):
    eng, api, _, _ = engine(positions=pair_positions(age_s=10, both=False), plans=pair_plans())
    eng.trader.stream.outcomes.intents["0xbb"] = "failed"
    assert [d["reason"] for d in eng.tick()] == ["pair_broken"]
    assert closed_ids(api) == [[1]]


def test_a_refused_open_leaves_a_survivor_that_closes_as_soon_as_it_shows(engine):
    # open_pair sets max_hold_s=0 on the surviving leg when the other was refused.
    plans = pair_plans(max_hold_s=0)
    plans.pop("0xbb")
    eng, api, _, _ = engine(positions=pair_positions(age_s=2, both=False), plans=plans)
    assert [d["reason"] for d in eng.tick()] == ["max_hold"]


def test_positions_outside_a_pair_ignore_the_pair_logic(engine):
    plans = {"0xaa": {"plan": {"stop_pct": None, "take_pct": None, "max_hold_s": None}, "created_at": NOW - 600}}
    eng, api, _, _ = engine(positions=held(), plans=plans)
    assert eng.tick() == [] and api.submitted == []
