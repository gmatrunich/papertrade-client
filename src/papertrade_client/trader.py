"""Order execution: guards, signing, submission, audit, and outcome tracking.

Dry-run is the default: unless ``live_trading`` is on *and* the call does not
ask for a dry run, intents are signed with a throwaway key and a random nonce
and returned unsigned.

Opens and withdrawals (the writes that spend margin) run their guard checks and
submission under one lock, and every submitted open stays "in flight" in the
store until the account stream shows it, so a quick second open cannot slip
past the limits. Closes take no lock and never wait on market data.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from eth_account.signers.local import LocalAccount

from . import deployment, guards, payout, protocol
from .api import AcknowledgementUnknown, PapertradeApi
from .config import Config
from .guards import GuardError
from .hyperliquid import HyperliquidInfo
from .session import SessionManager
from .store import AuditLog, StateStore
from .stream import AccountStream
from .units import USDC_UNIT, format_usd, parse_usd, to_float, usdc_to_wad
from .wire import AccountState, Position

log = logging.getLogger(__name__)

MIN_WITHDRAW_USDC = 10

__all__ = ["GuardError", "Submission", "Trader", "WriteResult"]


@dataclass
class Submission:
    intent_id: str
    acknowledged: bool  # False: the request outcome is unknown, track intent_id until the deadline
    state: str | None = None
    response: Any = None


@dataclass
class WriteResult:
    mode: str  # "live" | "dry-run"
    action: str
    message: dict[str, Any]
    submissions: list[Submission] = field(default_factory=list)
    violations: list[guards.Violation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    quote: dict[str, Any] | None = None


class Trader:
    def __init__(
        self,
        config: Config,
        wallet: LocalAccount,
        api: PapertradeApi,
        stream: AccountStream,
        hyperliquid: HyperliquidInfo,
        store: StateStore,
        audit: AuditLog,
        clock: Callable[[], float] = time.time,
    ):
        self.config, self.wallet, self.api, self.stream = config, wallet, api, stream
        self.hl, self.store, self.audit, self.clock = hyperliquid, store, audit, clock
        self.lock = threading.RLock()
        self.session = SessionManager(
            wallet, api, store, audit, stream.state, self.lock, clock, config.live_trading, config.session_renew_before_s
        )
        stream.add_listener(self._record_daily_baseline)

    # -- helpers ------------------------------------------------------------------

    def _live(self, dry_run: bool | None) -> bool:
        return self.config.live_trading and dry_run is not True

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self.clock()))

    def _record_daily_baseline(self, account: AccountState) -> None:
        """Stream listener: the first account update of each UTC day sets that day's baseline."""
        day = self._today()
        if (self.store.read()["daily"] or {}).get("day") == day:
            return

        def apply(state: dict[str, Any]) -> None:
            if (state["daily"] or {}).get("day") != day:  # another process may have set it meanwhile
                state["daily"] = {"day": day, "realized_pnl_baseline_raw": str(account.lifetime.realized_pnl_raw)}

        self.store.update(apply)

    def realized_today_raw(self) -> int:
        """Realised PnL since the first account update seen this UTC day (by any process sharing
        the data directory). Losses realised while nothing was running are not counted."""
        account = self.stream.state()
        if account is None:
            return 0
        self._record_daily_baseline(account)
        return account.lifetime.realized_pnl_raw - int(self.store.read()["daily"]["realized_pnl_baseline_raw"])

    def intent_status(self, intent_id: str) -> str:
        """succeeded | failed | expired | cancelled | pending | unknown."""
        intent_id = intent_id.lower()
        outcome = self.stream.outcomes.intents.get(intent_id)
        if outcome:
            return outcome
        account = self.stream.state()
        if account and any(p.intent_id == intent_id for p in account.open_positions):
            return "succeeded"
        if account and any(op.intent_id == intent_id for op in account.operations):
            return "pending"
        return "unknown"

    def wait_intent(self, intent_id: str, timeout_s: float = 30) -> str:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self.intent_status(intent_id)
            if status not in ("pending", "unknown"):
                return status
            try:
                self.stream.wait_for(
                    lambda _: self.intent_status(intent_id) not in ("pending", "unknown"), min(2.0, deadline - time.monotonic())
                )
            except TimeoutError:
                pass
        return self.intent_status(intent_id)

    def inflight(self) -> list[dict[str, Any]]:
        """Submitted intents (before their deadline) that the account stream does not show yet."""
        now = self.clock()
        return [
            item for item in self.store.read()["inflight"] if item["deadline"] > now and self.intent_status(item["intent_id"]) == "unknown"
        ]

    def _set_inflight(self, intent_id: str, entry: dict[str, Any] | None) -> None:
        now = self.clock()

        def apply(state: dict[str, Any]) -> None:
            rest = [i for i in state["inflight"] if i["intent_id"] != intent_id and i["deadline"] > now]
            state["inflight"] = rest + ([entry] if entry else [])

        self.store.update(apply)

    def _submit(self, signed: protocol.SignedIntent, extra: dict[str, Any] | None = None, **context: Any) -> Submission:
        action = signed.action or "cancelIntent"
        entry = {
            "intent_id": signed.intent_id,
            "action": action,
            "deadline": int(signed.wire["deadline"]),
            "submitted_at": self.clock(),
            "acknowledged": None,
            "margin_raw": signed.wire.get("size") if action == "openPosition" else None,
        }
        # Recorded before sending: if we crash mid-request, the intent still counts until its deadline.
        self._set_inflight(signed.intent_id, entry)
        self.audit.write("submit", action=action, intent_id=signed.intent_id, signed=signed.unsigned(), **context)
        try:
            response = self.api.submit(action, signed.wire, signed.intent_id, extra)
        except AcknowledgementUnknown as error:
            self._set_inflight(signed.intent_id, entry | {"acknowledged": False})
            self.audit.write("unacknowledged", action=action, intent_id=signed.intent_id, error=repr(error))
            log.error("no acknowledgement for %s %s; not resubmitting, track it until the deadline", action, signed.intent_id)
            return Submission(signed.intent_id, acknowledged=False)
        except Exception as error:
            self._set_inflight(signed.intent_id, None)
            self.audit.write(
                "rejected", action=action, intent_id=signed.intent_id, error=repr(error), payload=getattr(error, "payload", None)
            )
            raise
        # The relayer has the intent now: nothing below may turn that into an exception.
        try:
            self._set_inflight(signed.intent_id, entry | {"acknowledged": True})
            self.audit.write("accepted", action=action, intent_id=signed.intent_id, response=response)
        except Exception:
            log.exception("bookkeeping failed for accepted intent %s", signed.intent_id)
        return Submission(signed.intent_id, True, response.get("state"), response)

    # -- quotes -------------------------------------------------------------------

    def quote(self, market: str, side: str, margin_usd: str | float, leverage: int, exit_price: float | None = None) -> dict[str, Any]:
        m = deployment.MARKETS[market]
        trading = self.api.trading_state()
        instrument = trading.instrument(m.instrument_id)
        bbo = self.hl.bbo(m.hl_coin)
        is_long = side == "long"
        margin = to_float(parse_usd(margin_usd))
        bust = payout.estimate_bust_price(bbo.mid, leverage, is_long, instrument.bust_buffer_raw / 1e18)
        curve = payout.curve_in_force(trading)
        impact = payout.Impact.of(instrument)
        entry_raw = round(bbo.mid * instrument.price_scale)

        def at(price: float) -> payout.Settlement:
            return payout.settle(
                is_long=is_long,
                entry_raw=entry_raw,
                mark_raw=price * instrument.price_scale,
                margin_usd=margin,
                leverage=leverage,
                impact=impact,
                curve=curve,
                win_fee_rate=trading.win_fee_rate_raw / 1e18,
                bust_raw=bust * instrument.price_scale,
            )

        direction = 1 if is_long else -1
        curve_moves = [0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02]
        return {
            "market": market,
            "side": side,
            "margin_usd": margin,
            "leverage": leverage,
            "notional_usd": margin * leverage,
            "entry": bbo.mid,
            "bid": bbo.bid,
            "ask": bbo.ask,
            "bust": bust,
            "adverse_move_to_bust_pct": abs(bust - bbo.mid) / bbo.mid * 100,
            "curve": "off-hours" if trading.off_hours else "standard",
            "exchange": trading.exchange,
            "keep_curve": [
                {
                    "move_pct": mv * 100,
                    "keep": at(bbo.mid * (1 + direction * mv)).keep_fraction,
                    "payout_usd": at(bbo.mid * (1 + direction * mv)).payout_pnl_usd,
                }
                for mv in curve_moves
            ],
            "scenario": None if exit_price is None else at(exit_price).__dict__,
        }

    def position_marks(self) -> list[dict[str, Any]]:
        """Open positions with mark, bust distance and an estimated close-now payout."""
        account = self.stream.state()
        if account is None:
            return []
        trading = self.api.trading_state()
        bbos = {m.instrument_id: self.hl.bbo(m.hl_coin) for m in deployment.MARKETS.values()}
        out = []
        for p in account.open_positions:
            out.append(self._position_view(p, trading, bbos.get(p.instrument_id)))
        return out

    def _position_view(self, p: Position, trading, bbo) -> dict[str, Any]:
        instrument = trading.instrument(p.instrument_id)
        scale = instrument.price_scale
        view: dict[str, Any] = {
            "position_id": p.position_id,
            "market": instrument.symbol,
            "side": "long" if p.is_long else "short",
            "margin_usd": to_float(p.margin_raw),
            "leverage": p.leverage,
            "notional_usd": to_float(p.notional_raw),
            "entry": p.entry_price_raw / scale,
            "bust": p.bust_price_raw / scale,
            "opened_at_ms": p.opened_at_ms,
            "intent_id": p.intent_id,
            "mark": None,
            "estimate": None,
        }
        if bbo is not None:
            settled = payout.settle(
                is_long=p.is_long,
                entry_raw=p.entry_price_raw,
                mark_raw=bbo.mid * scale,
                margin_usd=to_float(p.margin_raw),
                leverage=p.leverage,
                impact=payout.Impact.of(instrument),
                curve=payout.curve_in_force(trading),
                win_fee_rate=trading.win_fee_rate_raw / 1e18,
                bust_raw=p.bust_price_raw,
            )
            view["mark"] = bbo.mid
            view["bust_distance_pct"] = abs(bbo.mid - view["bust"]) / bbo.mid * 100
            view["estimate"] = settled.__dict__ | {"keep_fraction": settled.keep_fraction}
        return view

    # -- writes -------------------------------------------------------------------

    def open(self, market: str, side: str, margin_usd: str | float, leverage: int, dry_run: bool | None = None) -> WriteResult:
        if side not in ("long", "short"):
            raise ValueError("side must be 'long' or 'short'")
        m = deployment.MARKETS[market]
        live = self._live(dry_run)
        margin_raw = parse_usd(margin_usd)
        # Market data first, outside the lock, so a slow upstream never blocks other writes.
        trading = self.api.trading_state()
        instrument = trading.instrument(m.instrument_id)
        bbo = self.hl.bbo(m.hl_coin)
        quote = {"bid": bbo.bid, "ask": bbo.ask, "mid": bbo.mid}

        def evaluate() -> tuple[list[guards.Violation], dict[str, Any]]:
            inflight = [i for i in self.inflight() if i["action"] == "openPosition"]
            violations = guards.check_open(
                guards.OpenCheck(
                    margin_raw=margin_raw,
                    leverage=leverage,
                    is_long=side == "long",
                    instrument=instrument,
                    trading=trading,
                    account=self.stream.state(),
                    account_age_s=self.stream.age_s(),
                    session_valid_for_s=self.session.status().valid_for_s,
                    kill_switch=self.store.kill_switch()["engaged"],
                    realized_today_raw=self.realized_today_raw(),
                    mid_price=bbo.mid,
                    limits=self.config.limits,
                    snapshot_max_age_s=self.config.snapshot_max_age_s,
                    now_s=self.clock(),
                    inflight_opens=len(inflight),
                    inflight_margin_raw=sum(int(i["margin_raw"] or 0) for i in inflight),
                )
            )
            message = {
                "user": self.wallet.address,
                "instrumentId": m.instrument_id,
                "isLong": side == "long",
                "size": margin_raw,
                "leverage": leverage,
                "useDebt": False,
                "nonce": 0,
                "deadline": protocol.deadline(self.clock()),
            }
            return violations, message

        if not live:
            violations, message = evaluate()
            preview = protocol.preview("OpenPosition", message)
            return WriteResult("dry-run", "openPosition", preview.unsigned(), violations=violations, quote=quote)

        with self.lock:
            violations, message = evaluate()
            if violations:
                raise GuardError(violations)
            signer = self.session.signer()
            if signer is None:
                raise GuardError([guards.Violation("session_invalid", "no registered session key")])
            message["nonce"] = self.store.take_nonce("exchange", deployment.EXCHANGE, self.wallet.address)
            signed = protocol.sign(signer, "OpenPosition", message)
            submission = self._submit(signed, market=market, side=side, margin_usd=str(margin_usd), leverage=leverage, mid=bbo.mid)
            return WriteResult("live", "openPosition", signed.unsigned(), [submission], quote=quote)

    def close(self, position_ids: list[int], dry_run: bool | None = None, force: bool = False) -> WriteResult:
        """Close positions in batches of 12. ``force`` skips the account checks and signs with the
        newest stored session key, for when the account stream is down but the relayer is not."""
        ids = list(dict.fromkeys(int(i) for i in position_ids))
        live = self._live(dry_run)
        account = self.stream.state()
        violations = [] if force else guards.check_close(ids, account)
        if not live:
            batches = [ids[i : i + deployment.MAX_CLOSE_BATCH] for i in range(0, len(ids), deployment.MAX_CLOSE_BATCH)]
            previews = [
                protocol.preview(
                    "Close", {"user": self.wallet.address, "positionIds": chunk, "nonce": 0, "deadline": protocol.deadline(self.clock())}
                ).unsigned()
                for chunk in batches
            ]
            return WriteResult("dry-run", "closePositions", {"batches": previews}, violations=violations, warnings=self._bust_warnings(ids))
        if violations:
            raise GuardError(violations)
        signer = self.session.signer() or (self.session.stored_signer() if force else None)
        if signer is None:
            raise GuardError([guards.Violation("session_invalid", "no registered session key")])
        result = WriteResult("live", "closePositions", {"batches": []})
        for i in range(0, len(ids), deployment.MAX_CLOSE_BATCH):
            chunk = ids[i : i + deployment.MAX_CLOSE_BATCH]
            signed = protocol.sign(
                signer,
                "Close",
                {
                    "user": self.wallet.address,
                    "positionIds": chunk,
                    "nonce": self.store.take_nonce("exchange", deployment.EXCHANGE, self.wallet.address),
                    "deadline": protocol.deadline(self.clock()),
                },
            )
            result.message["batches"].append(signed.unsigned())
            result.submissions.append(self._submit(signed, position_ids=chunk))
        result.warnings = self._bust_warnings(ids)  # after sending: informational only
        return result

    def close_all(self, dry_run: bool | None = None) -> WriteResult:
        account = self.stream.state()
        if account is None:
            raise GuardError([guards.Violation("account_unknown", "no account snapshot; close by id with force=True")])
        closing = {pid for op in account.operations if op.action == "closePositions" for pid in (op.position_ids or ())}
        result = self.close([p.position_id for p in account.open_positions if p.position_id not in closing], dry_run)
        age = self.stream.age_s()
        lag = self.clock() - account.indexed_through_timestamp
        if age is None or age > self.config.snapshot_max_age_s or lag > guards.MAX_INDEXER_LAG_S:
            stale = f"account data may be stale (stream {age} s, indexer {lag:.0f} s): positions opened since may be missed"
            result.warnings.insert(0, stale)
        return result

    def _bust_warnings(self, ids: list[int]) -> list[str]:
        # Contract set 2 settles a close at or past the bust price as a liquidation. Best effort.
        try:
            account = self.stream.state()
            positions = [p for p in (account.position(pid) for pid in ids) if p is not None] if account else []
            if not positions:
                return []
            trading = self.api.trading_state()
            mids: dict[int, float] = {}
            warnings = []
            for p in positions:
                market = next((m for m in deployment.MARKETS.values() if m.instrument_id == p.instrument_id), None)
                if market is None:
                    continue
                if p.instrument_id not in mids:
                    mids[p.instrument_id] = self.hl.bbo(market.hl_coin).mid * trading.instrument(p.instrument_id).price_scale
                mid = mids[p.instrument_id]
                if (mid <= p.bust_price_raw) if p.is_long else (mid >= p.bust_price_raw):
                    warnings.append(f"position {p.position_id}: mark is at or past the bust price, the close settles as a liquidation")
            return warnings
        except Exception as error:
            log.warning("bust check skipped: %r", error)
            return []

    def cancel(self, intent_id: str, dry_run: bool | None = None) -> WriteResult:
        intent_id = intent_id.lower()
        message = {"user": self.wallet.address, "intentId": intent_id, "deadline": protocol.deadline(self.clock())}
        if not self._live(dry_run):
            return WriteResult("dry-run", "cancelIntent", protocol.preview("CancelIntent", message).unsigned())
        account = self.stream.state()
        if account is None or not any(op.intent_id == intent_id for op in account.operations):
            raise GuardError([guards.Violation("not_cancellable", "intent is not pending on this account")])
        signer = self.session.signer()
        if signer is None:
            raise GuardError([guards.Violation("session_invalid", "no registered session key")])
        signed = protocol.sign(signer, "CancelIntent", message)
        body = {k: v for k, v in signed.wire.items() if k != "intentId"}
        self.audit.write("submit", action="cancelIntent", intent_id=intent_id, signed=signed.unsigned())
        response = self.api.cancel(intent_id, body)
        self.audit.write("accepted", action="cancelIntent", intent_id=intent_id, response=response)
        return WriteResult("live", "cancelIntent", signed.unsigned(), [Submission(intent_id, True, None, response)])

    def withdraw(self, amount_usdc: str | float, dry_run: bool | None = None) -> WriteResult:
        """Withdraw to the wallet's HyperCore (Hyperliquid) spot account. Signed by the wallet."""
        amount = int(parse_usd(amount_usdc) // (10**18 // USDC_UNIT))  # 6-decimal USDC units, as on-chain
        live = self._live(dry_run)
        account = self.stream.state()
        violations = []
        if amount < MIN_WITHDRAW_USDC * USDC_UNIT:
            violations.append(guards.Violation("below_minimum", f"minimum withdrawal is {MIN_WITHDRAW_USDC} USDC"))
        if account is None:
            violations.append(guards.Violation("account_unknown", "no account snapshot from Papertrade yet"))
        elif usdc_to_wad(amount) > account.funds.available_balance:
            violations.append(
                guards.Violation("insufficient_funds", f"only ${format_usd(account.funds.available_balance, 2)} is available")
            )
        message = {"user": self.wallet.address, "amount": amount, "nonce": 0, "deadline": protocol.deadline(self.clock())}
        if not live:
            return WriteResult("dry-run", "withdrawToCore", protocol.preview("WithdrawToCore", message).unsigned(), violations=violations)
        with self.lock:
            if violations:
                raise GuardError(violations)
            message["nonce"] = self.store.take_nonce("exchange", deployment.EXCHANGE, self.wallet.address)
            signed = protocol.sign(self.wallet, "WithdrawToCore", message)
            submission = self._submit(signed, extra={"withdrawal": {"route": "hypercore"}}, amount_usdc=str(amount_usdc))
            return WriteResult("live", "withdrawToCore", signed.unsigned(), [submission])
