"""Unofficial Python client for papertrade.xyz synthetic perps on Hyperliquid's HyperEVM."""

from .client import PapertradeClient
from .config import Config, Limits
from .trader import GuardError, Trader, WriteResult

__all__ = ["Config", "GuardError", "Limits", "PapertradeClient", "Trader", "WriteResult"]
__version__ = "0.1.0"
