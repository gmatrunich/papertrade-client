"""Configuration from environment variables (``PAPERTRADE_*``)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import deployment


def default_data_dir(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return str(Path(base) / "papertrade-client")


def data_dir(env: dict[str, str] | None = None) -> str:
    """The resolved data directory; needs no wallet key (the kill switch uses it alone)."""
    env = os.environ if env is None else env
    return str(Path(env.get("PAPERTRADE_DATA_DIR") or default_data_dir(env)).expanduser().resolve())


@dataclass(frozen=True)
class Limits:
    """Our own caps, enforced before anything is signed (Papertrade's own limits are checked too)."""

    max_margin_usd: float = 10
    max_leverage: int = 50
    max_open_positions: int = 1
    max_total_margin_usd: float = 10
    daily_loss_limit_usd: float = 10


@dataclass(frozen=True)
class Config:
    wallet_private_key: str = field(repr=False)
    data_dir: str = field(default_factory=default_data_dir)
    live_trading: bool = False
    limits: Limits = field(default_factory=Limits)
    snapshot_max_age_s: float = 30
    # SessionManager.maybe_renew() registers a new key once the active one has less than this left.
    session_renew_before_s: float = 72 * 3600
    # The exit engine closes any position once the mark has covered this share of the entry-to-bust distance.
    exit_stop_fraction: float = 0.5
    exchange_origin: str = deployment.EXCHANGE_ORIGIN
    hyperevm_rpc_url: str = deployment.HYPEREVM_RPC_URL
    hyperliquid_info_url: str = deployment.HYPERLIQUID_INFO_URL


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    if value.lower() not in ("true", "false"):
        raise ValueError(f"expected true/false, got {value!r}")
    return value.lower() == "true"


def load(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    key = env.get("PAPERTRADE_WALLET_PRIVATE_KEY", "")
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", key):
        raise ValueError("PAPERTRADE_WALLET_PRIVATE_KEY must be a 0x-prefixed 32-byte hex key (run `papertrade keygen`)")
    get = lambda name, default: env.get(f"PAPERTRADE_{name}") or default  # noqa: E731
    return Config(
        wallet_private_key=key,
        data_dir=data_dir(env),
        live_trading=_flag(env.get("PAPERTRADE_LIVE_TRADING"), False),
        limits=Limits(
            max_margin_usd=float(get("MAX_MARGIN_USD", Limits.max_margin_usd)),
            max_leverage=int(get("MAX_LEVERAGE", Limits.max_leverage)),
            max_open_positions=int(get("MAX_OPEN_POSITIONS", Limits.max_open_positions)),
            max_total_margin_usd=float(get("MAX_TOTAL_MARGIN_USD", Limits.max_total_margin_usd)),
            daily_loss_limit_usd=float(get("DAILY_LOSS_LIMIT_USD", Limits.daily_loss_limit_usd)),
        ),
        snapshot_max_age_s=float(get("SNAPSHOT_MAX_AGE_S", 30)),
        session_renew_before_s=float(get("SESSION_RENEW_BEFORE_H", 72)) * 3600,
        exit_stop_fraction=float(get("EXIT_STOP_FRACTION", Config.exit_stop_fraction)),
        exchange_origin=get("EXCHANGE_ORIGIN", deployment.EXCHANGE_ORIGIN),
        hyperevm_rpc_url=get("HYPEREVM_RPC_URL", deployment.HYPEREVM_RPC_URL),
        hyperliquid_info_url=get("HYPERLIQUID_INFO_URL", deployment.HYPERLIQUID_INFO_URL),
    )
