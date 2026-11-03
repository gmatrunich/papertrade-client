"""Close settlement under contract set 2 (docs: "Asymmetric impact PnL").

A winning close goes through: deadband (entry / 50000) -> holdback band of the
payout curve in force -> impact scale -> win fee. Losses pay the raw loss, and a
mark at or past the bust price forfeits the whole margin. Prices are in the
instrument's raw integer units (price × priceScale), as the contract sees them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .wire import Instrument, TradingState

WAD = 10**18
DEADBAND_DIVISOR = 50_000


@dataclass(frozen=True)
class Curve:
    start_ppm: int
    end_ppm: int
    holdback: float
    cubic_fall: bool


# Fixed in the contract (deployment config of the "next" exchange implementation).
STANDARD_CURVE = Curve(75, 600, 3.597, False)
OFF_HOURS_CURVE = Curve(30, 1000, 6.085, True)


def curve_in_force(state: TradingState) -> Curve | None:
    """None means the contract-set-1 payout (no holdback band)."""
    if state.exchange != "next":
        return None
    return OFF_HOURS_CURVE if state.off_hours else STANDARD_CURVE


@dataclass(frozen=True)
class Impact:
    base_rate: float
    rate_multiplier: float
    position_multiplier: float
    reference_notional: float

    @classmethod
    def of(cls, instrument: Instrument) -> Impact:
        return cls(
            instrument.base_rate_raw / WAD,
            instrument.rate_multiplier_raw / WAD,
            instrument.position_multiplier_raw / WAD,
            instrument.reference_notional_raw / WAD,
        )

    @property
    def terms(self) -> tuple[float, float]:
        return 1 / self.rate_multiplier, self.reference_notional / (1e6 * self.position_multiplier)

    @property
    def per_move(self) -> float:
        return sum(self.terms)


def holdback(move_raw: float, entry_raw: int, curve: Curve) -> float:
    """How far (in raw price units) a winning move is held back inside the band."""
    band_start = math.floor(entry_raw * curve.start_ppm / 1e6)
    band_end = math.floor(entry_raw * curve.end_ppm / 1e6)
    if not band_start < move_raw < band_end:
        return 0.0
    width = band_end - band_start
    u = (move_raw - band_start) / width
    shape = (u * (1 - u)) ** 2 * ((1 - u) if curve.cubic_fall else 1)
    return curve.holdback * width * shape


@dataclass(frozen=True)
class Settlement:
    raw_pnl_usd: float  # price move × notional, before anything is taken
    adjusted_pnl_usd: float  # after deadband, holdback and impact (losses: raw, floored at -margin)
    payout_pnl_usd: float  # after the win fee: what the account is credited
    liquidated: bool

    @property
    def keep_fraction(self) -> float:
        return 1.0 if self.raw_pnl_usd <= 0 else self.payout_pnl_usd / self.raw_pnl_usd


def settle(
    *,
    is_long: bool,
    entry_raw: int,
    mark_raw: float,
    margin_usd: float,
    leverage: int,
    impact: Impact,
    curve: Curve | None,
    win_fee_rate: float = 0.02,
    bust_raw: float | None = None,
) -> Settlement:
    notional = margin_usd * leverage
    favourable = (mark_raw - entry_raw) if is_long else (entry_raw - mark_raw)
    raw_pnl = notional * favourable / entry_raw

    crossed_bust = bust_raw is not None and (mark_raw <= bust_raw if is_long else mark_raw >= bust_raw)
    zero_equity = leverage * -favourable >= entry_raw
    if crossed_bust or zero_equity:
        return Settlement(raw_pnl, -margin_usd, -margin_usd, True)
    if favourable <= 0:
        loss = max(-margin_usd, raw_pnl)
        return Settlement(raw_pnl, loss, loss, False)

    move = favourable - entry_raw / DEADBAND_DIVISOR
    if curve is not None:
        move -= holdback(favourable, entry_raw, curve)
    move_fraction = max(0.0, move) / entry_raw
    if move_fraction == 0 or move_fraction * WAD < max(impact.terms):
        return Settlement(raw_pnl, 0.0, 0.0, False)
    adjusted = notional * move_fraction * (1 - impact.base_rate) / (1 + impact.per_move / move_fraction)
    return Settlement(raw_pnl, adjusted, adjusted * (1 - win_fee_rate), False)


def estimate_bust_price(entry: float, leverage: int, is_long: bool, bust_buffer: float) -> float:
    """Hard-liquidation trigger: the zero-equity price moved toward entry by the bust buffer."""
    return entry * (1 - 1 / leverage + bust_buffer) if is_long else entry * (1 + 1 / leverage - bust_buffer)
