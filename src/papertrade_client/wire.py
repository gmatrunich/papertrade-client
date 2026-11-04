"""Parsers for exchange.papertrade.xyz frames.

Tuple layouts are positional and undocumented, so every parser checks lengths
and raises ``WireError`` on anything unexpected: a format change should stop
trading loudly, not be read as wrong numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

INSTRUMENT_TUPLE_LEN = 23
POSITION_TUPLE_LEN = 10
OPERATION_TUPLE_LEN = 10
CLOSED_POSITION_TUPLE_LEN = 8

INTENT_OUTCOMES = {"succeeded", "failed", "expired", "cancelled", "unknown"}


class WireError(ValueError):
    pass


def _uint(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise WireError(f"{what}: expected an unsigned integer, got {value!r}")
    try:
        number = int(value)
    except ValueError as error:
        raise WireError(f"{what}: expected an unsigned integer, got {value!r}") from error
    if number < 0:
        raise WireError(f"{what}: negative value {value!r}")
    return number


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise WireError(f"{what}: expected an integer, got {value!r}")
    return int(value)


def _opt_uint(value: Any, what: str) -> int | None:
    return None if value is None else _uint(value, what)


def _bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise WireError(f"{what}: expected a boolean, got {value!r}")
    return value


def _tuple(value: Any, length: int, what: str) -> list[Any]:
    if not isinstance(value, list) or len(value) != length:
        got = len(value) if isinstance(value, list) else type(value).__name__
        raise WireError(f"{what}: expected a {length}-element tuple, got {got}")
    return value


def _hex(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise WireError(f"{what}: expected hex, got {value!r}")
    return value.lower()


@dataclass(frozen=True)
class Instrument:
    id: int
    symbol: str
    asset_index: int
    price_scale: int
    active: bool
    paused: bool
    terminal: bool
    retired: bool
    close_only: bool
    frozen_bid_raw: int | None
    frozen_ask_raw: int | None
    paused_at_ms: int | None
    rate_multiplier_raw: int
    position_multiplier_raw: int
    max_leverage: int
    max_position_notional_raw: int
    bust_buffer_raw: int
    base_rate_raw: int
    reference_notional_raw: int
    # Open interest and caps are asset quantities scaled by 1e18 (inferred: BTC
    # caps sit near current OI in BTC, not in USD).
    long_oi_raw: int
    short_oi_raw: int
    long_cap_raw: int
    short_cap_raw: int

    @property
    def openable(self) -> bool:
        return self.active and not (self.paused or self.terminal or self.retired or self.close_only)


def parse_instrument(value: Any) -> Instrument:
    t = _tuple(value, INSTRUMENT_TUPLE_LEN, "instrument")
    u = lambda i, what: _uint(t[i], f"instrument.{what}")  # noqa: E731
    return Instrument(
        id=u(0, "id"),
        symbol=str(t[1]),
        asset_index=u(2, "assetIndex"),
        price_scale=u(3, "priceScale"),
        active=_bool(t[4], "instrument.active"),
        paused=_bool(t[5], "instrument.paused"),
        terminal=_bool(t[6], "instrument.terminal"),
        retired=_bool(t[7], "instrument.retired"),
        close_only=_bool(t[8], "instrument.closeOnly"),
        frozen_bid_raw=_opt_uint(t[9], "instrument.frozenBid"),
        frozen_ask_raw=_opt_uint(t[10], "instrument.frozenAsk"),
        paused_at_ms=_opt_uint(t[11], "instrument.pausedAt"),
        rate_multiplier_raw=u(12, "rateMultiplier"),
        position_multiplier_raw=u(13, "positionMultiplier"),
        max_leverage=u(14, "maxLeverage"),
        max_position_notional_raw=u(15, "maxPositionNotional"),
        bust_buffer_raw=u(16, "bustBuffer"),
        base_rate_raw=u(17, "baseRate"),
        reference_notional_raw=u(18, "referenceNotional"),
        long_oi_raw=u(19, "longOi"),
        short_oi_raw=u(20, "shortOi"),
        long_cap_raw=u(21, "longCap"),
        short_cap_raw=u(22, "shortCap"),
    )


@dataclass(frozen=True)
class Intake:
    revision: int = 0
    minimum_paper_stake_raw: int = 0
    paused_actions: tuple[str, ...] = ()


# Present only while the off-hours payout curve is in force (absent rather than false, seen 2026-11-03/04).
OPTIONAL_TRADING_STATE_KEYS = frozenset({"offHours"})

# Keys /state/trading carried on 2026-11-03; anything else is reported by the drift monitor.
TRADING_STATE_KEYS = frozenset(
    {
        "schemaVersion",
        "indexedThroughBlock",
        "tradingPaused",
        "minimumTradeSizeRaw",
        "minimumOpenNotionalRaw",
        "winFeeRateRaw",
        "accRewardPerShareRaw",
        "instruments",
        "exchange",
        "offHours",
        "intakeControls",
        "maximumLeverage",
        "minimumLeverage",
        "accountOiCapRaw",
    }
)


@dataclass(frozen=True)
class TradingState:
    schema_version: int
    indexed_through_block: int
    trading_paused: bool
    minimum_trade_size_raw: int
    minimum_open_notional_raw: int
    win_fee_rate_raw: int
    acc_reward_per_share_raw: int | None
    instruments: tuple[Instrument, ...]
    exchange: str  # "origin" (contract set 1 curve) or "next" (holdback curves)
    off_hours: bool
    intake: Intake
    maximum_leverage: int | None
    minimum_leverage: int | None
    account_oi_cap_raw: int | None
    unknown_keys: frozenset[str] = frozenset()

    def instrument(self, instrument_id: int) -> Instrument:
        for item in self.instruments:
            if item.id == instrument_id:
                return item
        raise WireError(f"instrument {instrument_id} is not in the trading state")


def parse_trading_state(frame: dict[str, Any]) -> TradingState:
    if frame.get("schemaVersion") != 1:
        raise WireError(f"unsupported trading schemaVersion {frame.get('schemaVersion')!r}")
    intake = Intake()
    if "intakeControls" in frame:
        controls = _tuple(frame["intakeControls"], 3, "intakeControls")
        if not isinstance(controls[2], list):
            raise WireError("intakeControls.pausedActions: expected a list")
        intake = Intake(_uint(controls[0], "intake.revision"), _uint(controls[1], "intake.minStake"), tuple(map(str, controls[2])))
    exchange = frame.get("exchange")
    return TradingState(
        schema_version=1,
        indexed_through_block=_uint(frame["indexedThroughBlock"], "indexedThroughBlock"),
        trading_paused=_bool(frame["tradingPaused"], "tradingPaused"),
        minimum_trade_size_raw=_uint(frame["minimumTradeSizeRaw"], "minimumTradeSizeRaw"),
        minimum_open_notional_raw=_uint(frame["minimumOpenNotionalRaw"], "minimumOpenNotionalRaw"),
        win_fee_rate_raw=_uint(frame["winFeeRateRaw"], "winFeeRateRaw"),
        acc_reward_per_share_raw=_opt_uint(frame.get("accRewardPerShareRaw"), "accRewardPerShareRaw"),
        instruments=tuple(parse_instrument(item) for item in frame["instruments"]),
        exchange="origin" if exchange is None else ("next" if exchange == "next" else f"unknown:{exchange}"),
        off_hours=frame.get("offHours") is True,
        intake=intake,
        maximum_leverage=_opt_uint(frame.get("maximumLeverage"), "maximumLeverage"),
        minimum_leverage=_opt_uint(frame.get("minimumLeverage"), "minimumLeverage"),
        account_oi_cap_raw=_opt_uint(frame.get("accountOiCapRaw"), "accountOiCapRaw"),
        unknown_keys=frozenset(frame) - TRADING_STATE_KEYS,
    )


@dataclass(frozen=True)
class Position:
    position_id: int
    instrument_id: int
    is_long: bool
    use_debt: bool
    entry_price_raw: int
    margin_raw: int
    leverage: int
    bust_price_raw: int
    opened_at_ms: int
    intent_id: str | None

    @property
    def notional_raw(self) -> int:
        return self.margin_raw * self.leverage


def parse_position(value: Any) -> Position:
    t = _tuple(value, POSITION_TUPLE_LEN, "position")
    return Position(
        position_id=_uint(t[0], "position.id"),
        instrument_id=_uint(t[1], "position.instrumentId"),
        is_long=_bool(t[2], "position.isLong"),
        use_debt=_bool(t[3], "position.useDebt"),
        entry_price_raw=_uint(t[4], "position.entryPrice"),
        margin_raw=_uint(t[5], "position.margin"),
        leverage=_uint(t[6], "position.leverage"),
        bust_price_raw=_uint(t[7], "position.bustPrice"),
        opened_at_ms=_uint(t[8], "position.openedAt"),
        intent_id=None if t[9] is None else _hex(t[9], "position.intentId"),
    )


@dataclass(frozen=True)
class Operation:
    """An intent the relayer accepted that has not settled yet."""

    intent_id: str
    action: str
    instrument_id: int | None
    is_long: bool | None
    use_debt: bool | None
    amount_raw: int | None
    leverage: int | None
    accepted_at_ms: int
    deadline: int
    position_ids: tuple[int, ...] | None


def parse_operation(value: Any) -> Operation:
    t = _tuple(value, OPERATION_TUPLE_LEN, "operation")
    return Operation(
        intent_id=_hex(t[0], "operation.intentId"),
        action=str(t[1]),
        instrument_id=_opt_uint(t[2], "operation.instrumentId"),
        is_long=None if t[3] is None else _bool(t[3], "operation.isLong"),
        use_debt=None if t[4] is None else _bool(t[4], "operation.useDebt"),
        amount_raw=_opt_uint(t[5], "operation.amount"),
        leverage=_opt_uint(t[6], "operation.leverage"),
        accepted_at_ms=_uint(t[7], "operation.acceptedAt"),
        deadline=_uint(t[8], "operation.deadline"),
        position_ids=None if t[9] is None else tuple(_uint(i, "operation.positionId") for i in t[9]),
    )


@dataclass(frozen=True)
class ClosedPosition:
    position_id: int
    instrument_id: int
    is_long: bool
    margin_raw: int
    leverage: int
    liquidated: bool
    net_pnl_raw: int
    paper_minted_raw: int


def parse_closed_position(value: Any) -> ClosedPosition:
    t = _tuple(value, CLOSED_POSITION_TUPLE_LEN, "closedPosition")
    return ClosedPosition(
        position_id=_uint(t[0], "closed.id"),
        instrument_id=_uint(t[1], "closed.instrumentId"),
        is_long=_bool(t[2], "closed.isLong"),
        margin_raw=_uint(t[3], "closed.margin"),
        leverage=_uint(t[4], "closed.leverage"),
        liquidated=_bool(t[5], "closed.liquidated"),
        net_pnl_raw=_int(t[6], "closed.netPnl"),
        paper_minted_raw=_uint(t[7], "closed.paperMinted"),
    )


@dataclass(frozen=True)
class Funds:
    reserved_balance: int
    reserved_queued: int
    available_balance: int
    available_queued: int


@dataclass(frozen=True)
class Lifetime:
    deposited_raw: int
    withdrawn_raw: int
    realized_pnl_raw: int
    fees_paid_raw: int
    traded_notional_raw: int
    paper_rewards_claimed_raw: int


ACCOUNT_KEYS = frozenset(
    {
        "address",
        "accountRevision",
        "indexedThroughBlock",
        "indexedThroughTimestamp",
        "walletRevision",
        "sessionKeys",
        "queueRevision",
        "balance",
        "queued",
        "locked",
        "funds",
        "queueNodeCount",
        "consumableDebt",
        "paper",
        "lifetime",
        "operations",
        "openPositions",
        "oiCaps",
    }
)


@dataclass(frozen=True)
class AccountState:
    address: str
    account_revision: int
    indexed_through_block: int
    indexed_through_timestamp: int
    session_keys: dict[str, int]  # key address -> expiry (unix seconds)
    balance: int
    queued: int
    locked: int
    funds: Funds
    consumable_debt: int
    lifetime: Lifetime
    open_positions: tuple[Position, ...]
    operations: tuple[Operation, ...]
    oi_caps: dict[int, int] = field(default_factory=dict)  # per-account override, instrument -> raw
    raw_state: dict[str, Any] = field(default_factory=dict, repr=False)

    def position(self, position_id: int) -> Position | None:
        return next((p for p in self.open_positions if p.position_id == position_id), None)


def _funds(value: Any) -> Funds:
    return Funds(*(_uint(value[k], f"funds.{k}") for k in ("reservedBalance", "reservedQueued", "availableBalance", "availableQueued")))


def _lifetime(value: Any) -> Lifetime:
    return Lifetime(
        deposited_raw=_uint(value["depositedRaw"], "lifetime.deposited"),
        withdrawn_raw=_uint(value["withdrawnRaw"], "lifetime.withdrawn"),
        realized_pnl_raw=_int(value["realizedPnlRaw"], "lifetime.realizedPnl"),
        fees_paid_raw=_uint(value["feesPaidRaw"], "lifetime.feesPaid"),
        traded_notional_raw=_uint(value["tradedNotionalRaw"], "lifetime.tradedNotional"),
        paper_rewards_claimed_raw=_uint(value["paperRewardsClaimedRaw"], "lifetime.paperRewardsClaimed"),
    )


def _session_keys(value: Any) -> dict[str, int]:
    keys = {}
    for item in value:
        key, expiry = _tuple(item, 2, "sessionKey")
        keys[_hex(key, "sessionKey.key")] = _uint(expiry, "sessionKey.expiry")
    return keys


def _oi_caps(value: Any) -> dict[int, int]:
    return {_uint(i, "oiCap.instrument"): _uint(cap, "oiCap.cap") for i, cap in (_tuple(x, 2, "oiCap") for x in value or [])}


def _state_fields(frame: dict[str, Any]) -> dict[str, Any]:
    """Scalar account fields present in a snapshot, or in a patch's partial ``state``."""
    out: dict[str, Any] = {}
    if "indexedThroughBlock" in frame:
        out["indexed_through_block"] = _uint(frame["indexedThroughBlock"], "indexedThroughBlock")
    if "indexedThroughTimestamp" in frame:
        out["indexed_through_timestamp"] = _uint(frame["indexedThroughTimestamp"], "indexedThroughTimestamp")
    if "sessionKeys" in frame:
        out["session_keys"] = _session_keys(frame["sessionKeys"])
    for key, name in (("balance", "balance"), ("queued", "queued"), ("locked", "locked"), ("consumableDebt", "consumable_debt")):
        if key in frame:
            out[name] = _uint(frame[key], key)
    if "funds" in frame:
        out["funds"] = _funds(frame["funds"])
    if "oiCaps" in frame:
        out["oi_caps"] = _oi_caps(frame["oiCaps"])
    return out


_SNAPSHOT_REQUIRED = ACCOUNT_KEYS - {"walletRevision", "queueRevision", "queueNodeCount", "paper", "oiCaps"}


def parse_account_snapshot(frame: dict[str, Any]) -> AccountState:
    missing = _SNAPSHOT_REQUIRED - frame.keys()
    if missing:
        raise WireError(f"account snapshot is missing {sorted(missing)}")
    return AccountState(
        address=_hex(frame["address"], "address"),
        account_revision=_uint(frame["accountRevision"], "accountRevision"),
        lifetime=_lifetime(frame["lifetime"]),
        open_positions=tuple(parse_position(p) for p in frame["openPositions"]),
        operations=tuple(parse_operation(o) for o in frame["operations"]),
        raw_state={k: v for k, v in frame.items() if k not in ("openPositions", "operations")},
        **_state_fields(frame),  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class AccountPatch:
    account_revision: int
    fields: dict[str, Any]
    lifetime: Lifetime | None
    added_positions: tuple[Position, ...]
    removed_position_ids: tuple[int, ...]
    added_operations: tuple[Operation, ...]
    removed_operation_ids: tuple[str, ...]
    closed_positions: tuple[ClosedPosition, ...]
    intent_outcomes: tuple[tuple[str, str], ...]


def is_patch(frame: dict[str, Any]) -> bool:
    return "state" in frame


def parse_account_patch(frame: dict[str, Any]) -> AccountPatch:
    outcomes = []
    for item in frame.get("intentOutcomes") or []:
        intent, outcome = _tuple(item, 2, "intentOutcome")
        if outcome not in INTENT_OUTCOMES:
            raise WireError(f"unknown intent outcome {outcome!r}")
        outcomes.append((_hex(intent, "intentOutcome.intentId"), outcome))
    return AccountPatch(
        account_revision=_uint(frame["accountRevision"], "accountRevision"),
        fields=_state_fields(frame["state"]),
        lifetime=_lifetime(frame["lifetime"]) if frame.get("lifetime") else None,
        added_positions=tuple(parse_position(p) for p in frame.get("addedPositions") or []),
        removed_position_ids=tuple(_uint(i, "removedPositionId") for i in frame.get("removedPositionIds") or []),
        added_operations=tuple(parse_operation(o) for o in frame.get("addedOperations") or []),
        removed_operation_ids=tuple(_hex(i, "removedOperationId") for i in frame.get("removedOperationIds") or []),
        closed_positions=tuple(parse_closed_position(c) for c in frame.get("closedPositions") or []),
        intent_outcomes=tuple(outcomes),
    )


def apply_patch(state: AccountState, patch: AccountPatch) -> AccountState:
    if patch.account_revision <= state.account_revision:
        return state
    positions = {p.position_id: p for p in state.open_positions}
    for position_id in patch.removed_position_ids:
        positions.pop(position_id, None)
    positions.update({p.position_id: p for p in patch.added_positions})
    operations = {o.intent_id: o for o in state.operations}
    for intent in patch.removed_operation_ids:
        operations.pop(intent, None)
    operations.update({o.intent_id: o for o in patch.added_operations})
    return replace(
        state,
        **patch.fields,
        account_revision=patch.account_revision,
        lifetime=patch.lifetime or state.lifetime,
        open_positions=tuple(positions.values()),
        operations=tuple(operations.values()),
    )
