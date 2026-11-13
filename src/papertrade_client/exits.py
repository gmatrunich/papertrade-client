"""Client-side exits: Papertrade has no stop-loss or take-profit orders, only market closes.

A strategy attaches an :class:`ExitPlan` when it opens (``trader.open(..., exit_plan=...)`` stores it
in ``state.json`` under the intent id). ``papertrade exits`` runs :class:`ExitEngine`, a long-lived
process that watches Hyperliquid's mid for every open position and closes it on its stop,
take-profit or holding limit. Every position, with a plan or without (opened on papertrade.xyz, say),
also gets the safety stop: ``PAPERTRADE_EXIT_STOP_FRACTION`` of the entry-to-bust distance.

A close executes in the relayer's next batch: 7-46 s after submission on 2026-11-04. A close at or
past the bust price settles as a liquidation, so stops have to sit well before bust.

Hedge pairs (``trader.open_pair``): the plan record also carries ``pair = {"id", "legs"}`` (the two
open intent ids). When either leg is due, both are closed in one ``Close`` intent, so they execute in
the same batch at the same mark. A pair that has lost a leg (rejected, liquidated, never showed up
within ``PAIR_GRACE_S``) is no longer a hedge: the surviving leg is closed at once.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import deployment
from .wire import Position

if TYPE_CHECKING:
    from .trader import Trader
    from .watchdog import Notifier

log = logging.getLogger(__name__)

HEARTBEAT_FILE = "exits.json"
RETRY_AFTER_S = 150  # a close that has not resolved by then is sent again (the first one waited 46 s)
PLAN_RETENTION_S = 2 * 3600  # plans whose position is not open are dropped after this
HEARTBEAT_EVERY_S = 10
RENEW_EVERY_S = 3600
# Both legs of a pair are submitted together, but the relayer may execute them seconds apart. A leg
# whose partner is still not open this long after its own open is on its own.
PAIR_GRACE_S = 90
# Open intents that can never produce the missing leg.
DEAD_INTENT_STATUSES = frozenset({"failed", "expired", "cancelled"})


@dataclass(frozen=True)
class ExitPlan:
    stop_pct: float | None = None  # adverse move from entry, in % of the entry price
    take_pct: float | None = None  # favourable move from entry, in % of the entry price
    max_hold_s: float | None = None


def trigger(position: Position, plan: ExitPlan | None, mark_raw: float, now_s: float, stop_fraction: float) -> str | None:
    """Why this position should be closed now, or None. Prices are raw (× the instrument's price scale)."""
    entry, bust = position.entry_price_raw, position.bust_price_raw
    favourable_pct = (mark_raw - entry) / entry * 100 * (1 if position.is_long else -1)
    covered = (entry - mark_raw) / (entry - bust) if entry != bust else 0.0  # share of the way to bust
    if covered >= 1:
        return None  # at or past bust a close settles as a liquidation; the keeper takes it from here
    if plan and plan.stop_pct is not None and favourable_pct <= -plan.stop_pct:
        return "stop"
    if covered >= stop_fraction:
        return "safety_stop"
    if plan and plan.take_pct is not None and favourable_pct >= plan.take_pct:
        return "take_profit"
    if plan and plan.max_hold_s is not None and now_s - position.opened_at_ms / 1000 >= plan.max_hold_s:
        return "max_hold"
    return None


class ExitEngine:
    def __init__(self, trader: Trader, notifier: Notifier | None = None, poll_s: float = 1.0):
        self.trader, self.notifier, self.poll_s = trader, notifier, poll_s
        self.closing: dict[int, dict[str, Any]] = {}  # position_id -> {"intent_id", "at", "reason"}
        self.seen: set[int] = set()

    @property
    def live(self) -> bool:
        return self.trader.config.live_trading

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self.trader.stream.start()
        # Logged, not sent: systemd restarts the engine within 5 s, and a crash loop must not flood the chat.
        log.info("exit engine started (%s)", "live" if self.live else "dry-run")
        last_beat = last_renew = 0.0
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("exit tick failed")
            now = self.trader.clock()
            if now - last_beat >= HEARTBEAT_EVERY_S:
                try:
                    self._heartbeat()
                except Exception:
                    log.exception("heartbeat failed")
                last_beat = now
            if now - last_renew >= RENEW_EVERY_S:
                try:
                    self.trader.session.maybe_renew()
                except Exception:
                    log.exception("session renewal failed")
                last_renew = now
            stop.wait(self.poll_s)

    def tick(self) -> list[dict[str, Any]]:
        """One pass over the open positions; returns the closes it sent (or would send in dry-run)."""
        account = self.trader.stream.state()
        if account is None:
            return []
        now = self.trader.clock()
        plans = self.trader.store.read()["exit_plans"]
        self._prune_plans(plans, {p.intent_id for p in account.open_positions}, now)
        open_ids = {p.position_id for p in account.open_positions}
        self.closing = {pid: c for pid, c in self.closing.items() if pid in open_ids}
        if not open_ids:
            return []
        trading = self.trader.api.trading_state(max_age_s=60)
        marks: dict[int, float] = {}

        def mark_of(p: Position) -> float:
            if p.instrument_id not in marks:
                market = next(m for m in deployment.MARKETS.values() if m.instrument_id == p.instrument_id)
                marks[p.instrument_id] = self.trader.hl.bbo(market.hl_coin).mid * trading.instrument(p.instrument_id).price_scale
            return marks[p.instrument_id]

        def due_entry(p: Position, reason: str) -> dict[str, Any]:
            scale = trading.instrument(p.instrument_id).price_scale
            return {"position_id": p.position_id, "reason": reason, "mark": mark_of(p) / scale, "position": p}

        due: list[dict[str, Any]] = []
        pairs: dict[int, dict[str, Any]] = {}  # position_id -> {"id", "legs"} of positions that belong to a hedge pair
        for p in account.open_positions:
            record = plans.get(p.intent_id or "")
            if record and record.get("pair"):
                pairs[p.position_id] = record["pair"]
            if p.position_id not in self.seen:
                self.seen.add(p.position_id)
                self.trader.audit.write("position", position_id=p.position_id, intent_id=p.intent_id, opened_at_ms=p.opened_at_ms)
            if self._still_closing(p.position_id, now):
                continue
            plan = ExitPlan(**record["plan"]) if record else None
            reason = trigger(p, plan, mark_of(p), now, self.trader.config.exit_stop_fraction)
            if reason:
                due.append(due_entry(p, reason))
        due += self._pair_legs(account, due, pairs, due_entry, now)
        if due:
            self._close(due, now)
        return due

    def _pair_legs(
        self, account, due: list[dict[str, Any]], pairs: dict[int, dict[str, Any]], due_entry, now: float
    ) -> list[dict[str, Any]]:
        """Legs to close along with a due leg (same intent, same batch), or because their pair is broken."""
        if not pairs:
            return []
        by_intent = {p.intent_id: p for p in account.open_positions if p.intent_id}
        due_reason = {d["position_id"]: d["reason"] for d in due}
        added: list[dict[str, Any]] = []
        for p in account.open_positions:
            pair = pairs.get(p.position_id)
            if not pair or p.position_id in due_reason or p.position_id in self.closing:
                continue
            reason = None
            for partner_intent in (i for i in pair["legs"] if i != p.intent_id):
                partner = by_intent.get(partner_intent)
                if partner is not None:
                    if partner.position_id in due_reason:
                        reason = f"pair:{due_reason[partner.position_id]}"
                elif now - p.opened_at_ms / 1000 >= PAIR_GRACE_S or self.trader.intent_status(partner_intent) in DEAD_INTENT_STATUSES:
                    reason = "pair_broken"
            if reason:
                entry = due_entry(p, reason)
                due_reason[p.position_id] = reason
                added.append(entry)
        return added

    def _still_closing(self, position_id: int, now: float) -> bool:
        pending = self.closing.get(position_id)
        if not pending:
            return False
        status = self.trader.intent_status(pending["intent_id"]) if pending["intent_id"] else "dry-run"
        if status in ("pending", "unknown", "dry-run") and now - pending["at"] < RETRY_AFTER_S:
            return True
        if status not in ("pending", "unknown", "dry-run"):
            # Resolved but the position is still open: the close failed or expired. Try again.
            self._notify(f"close of #{position_id} ended {status}; retrying")
        del self.closing[position_id]
        return False

    def _close(self, due: list[dict[str, Any]], now: float) -> None:
        ids = [d["position_id"] for d in due]
        context = {"exit": {str(d["position_id"]): {"reason": d["reason"], "mark": d["mark"]} for d in due}}
        try:
            result = self.trader.close(ids, context=context)
        except Exception as error:
            log.error("close of %s failed: %r", ids, error)
            self._notify(f"⚠️ exit close of {ids} failed: {error!r}"[:300])
            return
        intent = result.submissions[0].intent_id if result.submissions else None
        for d in due:
            self.closing[d["position_id"]] = {"intent_id": intent, "at": now, "reason": d["reason"]}
            p: Position = d["position"]
            market = next(m.symbol for m in deployment.MARKETS.values() if m.instrument_id == p.instrument_id)
            side = "long" if p.is_long else "short"
            verb = "closing" if result.mode == "live" else "dry-run, would close"
            self._notify(f"{verb} {market} {side} #{p.position_id}: {d['reason']} at mark {d['mark']:.1f}")

    def _prune_plans(self, plans: dict[str, Any], open_intents: set[str | None], now: float) -> None:
        stale = [i for i, r in plans.items() if i not in open_intents and now - r["created_at"] > PLAN_RETENTION_S]
        if not stale:
            return

        def apply(state: dict[str, Any]) -> None:
            for intent_id in stale:
                state["exit_plans"].pop(intent_id, None)

        self.trader.store.update(apply)

    def _heartbeat(self) -> None:
        account = self.trader.stream.state()
        beat = {
            "at": self.trader.clock(),
            "mode": "live" if self.live else "dry-run",
            "stream_age_s": self.trader.stream.age_s(),
            "open_positions": len(account.open_positions) if account else None,
            "closing": {str(k): v for k, v in self.closing.items()},
        }
        path = Path(self.trader.store.dir) / HEARTBEAT_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(beat, default=str))
        os.replace(tmp, path)

    def _notify(self, text: str) -> None:
        log.info(text)
        if self.notifier is None:
            return
        try:
            self.notifier.send(f"exits: {text}")
        except Exception:
            log.exception("notification failed")


def read_heartbeat(data_dir: str | Path) -> dict[str, Any] | None:
    try:
        return json.loads((Path(data_dir) / HEARTBEAT_FILE).read_text())
    except (OSError, ValueError):
        return None
