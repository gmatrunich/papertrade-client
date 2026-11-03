"""Hyperliquid book top: Papertrade fills at the BBO mid of HL's native book."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from . import deployment


@dataclass(frozen=True)
class Bbo:
    coin: str
    bid: float
    ask: float
    at: float  # local unix time of the read

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


class HyperliquidInfo:
    def __init__(self, url: str = deployment.HYPERLIQUID_INFO_URL, client: httpx.Client | None = None):
        self.url = url
        self.client = client or httpx.Client(timeout=5)

    def bbo(self, coin: str) -> Bbo:
        response = self.client.post(self.url, json={"type": "l2Book", "coin": coin})
        response.raise_for_status()
        bids, asks = response.json()["levels"]
        if not bids or not asks:
            raise RuntimeError(f"Hyperliquid {coin} book is empty")
        bid, ask = float(bids[0]["px"]), float(asks[0]["px"])
        if not (bid > 0 and ask >= bid):
            raise RuntimeError(f"Hyperliquid {coin} book is crossed: {bid} / {ask}")
        return Bbo(coin, bid, ask, time.time())
