"""Every reason an open would be refused, by Papertrade or by our own limits."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Limits
from .units import format_usd, parse_usd
from .wire import AccountState, Instrument, TradingState

WAD = 10**18
MIN_SESSION_VALIDITY_S = 3600
MAX_INDEXER_LAG_S = 600
# HyperEVM makes a small block every second and a large one every minute: 61 blocks a minute
# (measured 2026-11-05: 346 blocks in 342 s). Erring high reads the lag slightly long rather than short.
HYPEREVM_BLOCKS_PER_S = 61 / 60


@dataclass(frozen=True)
class Violation:
    code: str
    message: str


def indexer_lag_s(account: AccountState, trading: TradingState, now_s: float) -> float:
    """How far Papertrade's indexer is behind the chain, in seconds.

    Not ``now - account.indexed_through_timestamp``: that timestamp moves only when the account
    changes or the serving instance rebuilds its copy, and each instance keeps its own. A fresh
    connection to an idle account got copies 971-6624 s old on 2026-11-05 while /state/trading
    was 2-17 blocks behind the chain head. /state/trading's ``indexedThroughBlock`` follows the
    indexer itself; the account frame ties one block to a time, which dates it.
    """
    blocks_since = trading.indexed_through_block - account.indexed_through_block
    return now_s - account.indexed_through_timestamp - blocks_since / HYPEREVM_BLOCKS_PER_S


class GuardError(Exception):
    def __init__(self, violations: list[Violation]):
        super().__init__("; ".join(v.message for v in violations))
        self.violations = violations


@dataclass(frozen=True)
class OpenCheck:
    margin_raw: int
    leverage: int
    is_long: bool
    instrument: Instrument
    trading: TradingState
    account: AccountState | None
    account_age_s: float | None  # since the stream's last event, heartbeats included
    session_valid_for_s: float | None
    kill_switch: bool
    realized_today_raw: int
    mid_price: float | None
    limits: Limits
    snapshot_max_age_s: float
    now_s: float = 0
    # Opens we submitted that the account stream does not show yet, and their margin.
    inflight_opens: int = 0
    inflight_margin_raw: int = 0


def check_open(c: OpenCheck) -> list[Violation]:
    out: list[Violation] = []
    add = lambda code, message: out.append(Violation(code, message))  # noqa: E731
    t, i, limits = c.trading, c.instrument, c.limits
    notional_raw = c.margin_raw * c.leverage

    if c.kill_switch:
        add("kill_switch", "kill switch is engaged; only closes are allowed")
    if t.trading_paused:
        add("trading_paused", "Papertrade has paused all opens")
    if "openPosition" in t.intake.paused_actions:
        add("intake_paused", "Papertrade intake for openPosition is paused")
    if not i.openable:
        add("market_closed", f"{i.symbol} is not open for new positions")

    floor = max(1, t.minimum_leverage or 1)
    ceiling = min(i.max_leverage, t.maximum_leverage or i.max_leverage)
    if not floor <= c.leverage <= ceiling:
        add("leverage_range", f"leverage must be between {floor} and {ceiling}")
    if c.margin_raw < t.minimum_trade_size_raw:
        add("margin_below_min", f"margin is below Papertrade's minimum ${format_usd(t.minimum_trade_size_raw, 2)}")
    if notional_raw < t.minimum_open_notional_raw:
        add("notional_below_min", f"margin × leverage is below Papertrade's minimum ${format_usd(t.minimum_open_notional_raw, 2)}")
    if notional_raw > i.max_position_notional_raw:
        add("notional_above_max", "position value exceeds the market maximum")
    if c.mid_price:
        # Side caps are asset quantities (1e18), so convert the notional at the current mid.
        quantity_raw = int(notional_raw / c.mid_price)
        oi, cap = (i.long_oi_raw, i.long_cap_raw) if c.is_long else (i.short_oi_raw, i.short_cap_raw)
        if oi + quantity_raw > cap:
            add("side_oi_cap", f"{'long' if c.is_long else 'short'} open interest cap for {i.symbol} would be exceeded")

    if c.margin_raw > parse_usd(limits.max_margin_usd):
        add("limit_margin", f"margin exceeds MAX_MARGIN_USD {limits.max_margin_usd}")
    if c.leverage > limits.max_leverage:
        add("limit_leverage", f"leverage exceeds MAX_LEVERAGE {limits.max_leverage}")
    if c.realized_today_raw <= -parse_usd(limits.daily_loss_limit_usd):
        add("limit_daily_loss", f"realised loss today reached DAILY_LOSS_LIMIT_USD {limits.daily_loss_limit_usd}")

    a = c.account
    if a is None:
        add("account_unknown", "no account snapshot from Papertrade yet")
    else:
        if c.account_age_s is None or c.account_age_s > c.snapshot_max_age_s:
            add("account_stale", "account stream is silent")
        lag_s = indexer_lag_s(a, t, c.now_s)
        if lag_s > MAX_INDEXER_LAG_S:
            add("account_stale", f"Papertrade's indexer is {lag_s:.0f} s behind the chain (indexer lag)")
        pending_opens = sum(1 for op in a.operations if op.action == "openPosition") + c.inflight_opens
        if len(a.open_positions) + pending_opens >= limits.max_open_positions:
            add("limit_positions", f"open + pending positions reached MAX_OPEN_POSITIONS {limits.max_open_positions}")
        committed = c.inflight_margin_raw + c.margin_raw
        if a.locked + a.funds.reserved_balance + committed > parse_usd(limits.max_total_margin_usd):
            add("limit_total_margin", f"total margin would exceed MAX_TOTAL_MARGIN_USD {limits.max_total_margin_usd}")
        if committed > a.funds.available_balance:
            add("insufficient_funds", "margin exceeds the available balance")
        account_cap = a.oi_caps.get(i.id, t.account_oi_cap_raw)
        if account_cap:
            open_notional = sum(p.notional_raw for p in a.open_positions if p.instrument_id == i.id)
            if open_notional + notional_raw > account_cap:
                add("account_oi_cap", f"account open interest cap ${format_usd(account_cap, 0)} on {i.symbol} would be exceeded")

    if c.session_valid_for_s is None or c.session_valid_for_s < MIN_SESSION_VALIDITY_S:
        add("session_invalid", "no registered session key valid for at least another hour")
    return out


def check_close(position_ids: list[int], account: AccountState | None) -> list[Violation]:
    if account is None:
        return [Violation("account_unknown", "no account snapshot from Papertrade yet")]
    open_ids = {p.position_id for p in account.open_positions}
    unknown = [pid for pid in position_ids if pid not in open_ids]
    return [Violation("unknown_position", f"not open on this account: {unknown}")] if unknown else []
