"""Amount conversions.

Inside the Exchange every collateral amount (margin, balances, PnL) is an
18-decimal integer; USDC on-chain, and WithdrawToCore amounts, use 6 decimals.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

WAD = 10**18
USDC_UNIT = 10**6


def parse_usd(value: str | int | float | Decimal) -> int:
    """'12.5' -> 12.5 * 1e18. Rejects negatives and more than 18 decimals."""
    try:
        amount = Decimal(str(value)).normalize() if not isinstance(value, Decimal) else value
    except InvalidOperation as error:
        raise ValueError(f"invalid USD amount {value!r}") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"invalid USD amount {value!r}")
    raw = amount * WAD
    if raw != raw.to_integral_value():
        raise ValueError(f"USD amount {value!r} has more than 18 decimals")
    return int(raw)


def to_decimal(raw: int, scale: int = WAD) -> Decimal:
    return Decimal(raw) / scale


def to_float(raw: int, scale: int = WAD) -> float:
    return float(to_decimal(raw, scale))


def format_usd(raw: int, digits: int = 6) -> str:
    text = f"{to_decimal(raw):.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def wad_to_usdc(raw: int) -> int:
    """18-decimal collateral -> 6-decimal USDC, rounded down."""
    return raw // (WAD // USDC_UNIT)


def usdc_to_wad(amount: int) -> int:
    return amount * (WAD // USDC_UNIT)
