"""One object wiring the pieces together for a strategy process or the CLI."""

from __future__ import annotations

from eth_account import Account

from . import config as config_module
from .api import PapertradeApi
from .config import Config
from .hyperliquid import HyperliquidInfo
from .onchain import Rpc
from .store import AuditLog, StateStore
from .stream import AccountStream
from .trader import Trader


class PapertradeClient:
    def __init__(self, config: Config):
        self.config = config
        self.wallet = Account.from_key(config.wallet_private_key)
        self.api = PapertradeApi(config.exchange_origin)
        self.stream = AccountStream(self.wallet.address, config.exchange_origin)
        self.hyperliquid = HyperliquidInfo(config.hyperliquid_info_url)
        self.rpc = Rpc(config.hyperevm_rpc_url)
        self.store = StateStore(config.data_dir)
        self.audit = AuditLog(config.data_dir)
        self.trader = Trader(config, self.wallet, self.api, self.stream, self.hyperliquid, self.store, self.audit)

    @classmethod
    def from_env(cls) -> PapertradeClient:
        return cls(config_module.load())

    @property
    def address(self) -> str:
        return self.wallet.address.lower()

    def start(self, wait_s: float = 15) -> PapertradeClient:
        """Start the account stream and wait for the first snapshot."""
        self.stream.start()
        self.stream.wait_ready(wait_s)
        return self

    def deposit_address(self) -> dict:
        relayer = self.api.deposit_address(self.address)
        chain = self.rpc.compute_deposit_proxy(self.address)
        return {"wallet": self.address, "deposit_address": chain, "relayer_address": relayer, "matches_relayer": relayer == chain}
