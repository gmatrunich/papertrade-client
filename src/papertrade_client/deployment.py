"""Pinned facts about the Papertrade production deployment.

Everything here was observed on 2026-11-03 (contract set 2, frontend build
38397c220121) and is re-checked daily by ``papertrade_client.drift``.
"""

from __future__ import annotations

from dataclasses import dataclass

CHAIN_ID = 999  # HyperEVM mainnet

EXCHANGE_ORIGIN = "https://exchange.papertrade.xyz"
DOCS_ORIGIN = "https://docs.papertrade.xyz"
HYPEREVM_RPC_URL = "https://rpc.hyperliquid.xyz/evm"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"

# Proxies are the addresses to call; implementations are what the proxies run.
EXCHANGE = "0x6cd5661646289fb6e65ea5c032310fded797d0a2"
EXCHANGE_IMPLEMENTATION = "0x0a44049dbbb6b4762a6ba37aa0f7548c0b4a3723"
BATCH_EXECUTOR = "0x233ff29b239abc1d7748605da9b205c3e008626e"
SESSION_KEY_MANAGER = "0x8326eb8896e358717f10502ec73ce8d28e37b108"
BUILDER_REGISTRY = "0xfdc6550436dd75ce8a0b145f935b7d3a64f53248"
DEPOSIT_PROXY_FACTORY = "0x1629b7d46bef6e13ff754174fc9f32dea309b38f"
PAPER_TOKEN = "0xe40f17915daa230324030003a197cdaef2261c0e"
PAPER_STAKING = "0xaad6c7b0cc3014fc80ffedae0ed7ce5967b0f016"
TIMELOCK = "0xb915fa41e824d6aa31bb833ee81220c0f2c8ea0f"
MULTISIG = "0x696292762fd5ac3677a45edbe54eaece4194d1f6"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# EIP-1967 implementation slot, used to notice contract upgrades.
EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"


@dataclass(frozen=True)
class Market:
    symbol: str
    instrument_id: int
    hl_coin: str


MARKETS: dict[str, Market] = {
    "BTC": Market("BTC", 0, "BTC"),
    "ETH": Market("ETH", 1, "ETH"),
}

# Every signed intent is valid for one hour, like the canonical frontend's.
INTENT_LIFETIME_S = 3_600
# Session keys are registered for 30 days.
SESSION_LIFETIME_S = 30 * 24 * 3_600
# The frontend closes at most 12 positions per signed Close intent.
MAX_CLOSE_BATCH = 12
# Collateral amounts inside the Exchange are 18-decimal; USDC itself has 6.
COLLATERAL_DECIMALS = 18
USDC_DECIMALS = 6
