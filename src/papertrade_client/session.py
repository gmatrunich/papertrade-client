"""Session keys: generated locally, registered by the wallet, used to sign trades.

A session key can open and close positions but cannot withdraw. Registration is
a wallet-signed ``RegisterSessionKey`` intent sent through the relayer; a key
counts as usable only once the account snapshot lists it with a future expiry.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from eth_account import Account
from eth_account.signers.local import LocalAccount

from . import deployment, protocol
from .api import PapertradeApi
from .guards import GuardError, Violation
from .store import AuditLog, StateStore
from .wire import AccountState

log = logging.getLogger(__name__)

PENDING_GRACE_S = 600


@dataclass(frozen=True)
class SessionStatus:
    active_key: str | None
    expires_at: int | None
    valid_for_s: float | None
    pending_keys: tuple[str, ...]


class SessionManager:
    def __init__(
        self,
        wallet: LocalAccount,
        api: PapertradeApi,
        store: StateStore,
        audit: AuditLog,
        account: Callable[[], AccountState | None],
        write_lock: threading.RLock,
        clock: Callable[[], float] = time.time,
        live_trading: bool = False,
        renew_before_s: float = 72 * 3600,
    ):
        self.wallet, self.api, self.store, self.audit = wallet, api, store, audit
        self.account, self.write_lock, self.clock = account, write_lock, clock
        self.live_trading, self.renew_before_s = live_trading, renew_before_s

    def _registered_expiry(self, address: str) -> int | None:
        state = self.account()
        return None if state is None else state.session_keys.get(address.lower())

    def _active(self) -> tuple[dict, int] | None:
        for key in self.store.read()["session_keys"]:
            expiry = self._registered_expiry(key["address"])
            if expiry is not None and expiry > self.clock():
                return key, expiry
        return None

    def status(self) -> SessionStatus:
        active = self._active()
        pending = tuple(k["address"] for k in self.store.read()["session_keys"] if self._registered_expiry(k["address"]) is None)
        return SessionStatus(
            active_key=active[0]["address"] if active else None,
            expires_at=active[1] if active else None,
            valid_for_s=active[1] - self.clock() if active else None,
            pending_keys=pending,
        )

    def signer(self) -> LocalAccount | None:
        active = self._active()
        return Account.from_key(active[0]["private_key"]) if active else None

    def stored_signer(self) -> LocalAccount | None:
        """The newest stored key that has not expired by its own clock, whether or not the
        account stream confirms it. For emergency closes while the stream is unavailable."""
        for key in self.store.read()["session_keys"]:
            if key["expires_at"] > self.clock():
                return Account.from_key(key["private_key"])
        return None

    def register(self) -> dict:
        """Generate a new key, persist it, and submit the wallet-signed registration."""
        if not self.live_trading:
            raise GuardError([Violation("dry_run", "session registration is a signed write; set PAPERTRADE_LIVE_TRADING=true")])
        with self.write_lock:
            key = Account.create()
            now = self.clock()
            expiry = int(now) + deployment.SESSION_LIFETIME_S
            nonce = self.store.take_nonce("exchange", deployment.EXCHANGE, self.wallet.address)
            signed = protocol.sign(
                self.wallet,
                "RegisterSessionKey",
                {
                    "user": self.wallet.address,
                    "key": key.address,
                    "expiry": expiry,
                    "feeAddress": deployment.ZERO_ADDRESS,  # no builder
                    "nonce": nonce,
                    "deadline": protocol.deadline(now),
                },
            )

            account = self.account()

            def still_useful(k: dict) -> bool:
                # Drop a key only when a snapshot shows it is not (or no longer) registered.
                if k["expires_at"] <= now:
                    return False
                if account is None:
                    return True
                return account.session_keys.get(k["address"], 0) > now or now - k["registered_at"] < PENDING_GRACE_S

            # Persist before submitting so a crash cannot orphan a registered key.
            def remember(state: dict) -> None:
                stored = {
                    "private_key": key.key.hex(),
                    "address": key.address.lower(),
                    "expires_at": expiry,
                    "registered_at": now,
                    "intent_id": signed.intent_id,
                }
                state["session_keys"] = [stored, *filter(still_useful, state["session_keys"])]

            self.store.update(remember)
            self.audit.write("submit", action="registerSessionKey", intent_id=signed.intent_id, key=key.address, signed=signed.unsigned())
            try:
                response = self.api.submit("registerSessionKey", signed.wire, signed.intent_id)
            except Exception as error:
                self.audit.write("rejected", action="registerSessionKey", intent_id=signed.intent_id, error=repr(error))
                raise
            self.audit.write("accepted", action="registerSessionKey", intent_id=signed.intent_id, response=response)
            log.info("session key %s registration accepted", key.address)
            return {"key": key.address.lower(), "intent_id": signed.intent_id, "response": response}

    def maybe_renew(self, renew_before_s: float | None = None) -> bool:
        """Register a new key when the active one is about to expire. Call it periodically
        from a long-running process; it does nothing in dry-run or without a snapshot."""
        if not self.live_trading or self.account() is None:
            return False
        renew_before_s = self.renew_before_s if renew_before_s is None else renew_before_s
        status = self.status()
        recent = any(
            self.clock() - k["registered_at"] < PENDING_GRACE_S and k["address"] in status.pending_keys
            for k in self.store.read()["session_keys"]
        )
        if recent:
            return False
        if status.valid_for_s is None or status.valid_for_s < renew_before_s:
            log.info("renewing session key (valid for %s s)", status.valid_for_s)
            self.register()
            return True
        return False
