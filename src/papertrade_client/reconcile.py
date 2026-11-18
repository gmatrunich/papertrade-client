"""``papertrade reconcile``: the audit log against Papertrade's trade history.

For every closed position: how long the relayer took to execute the open and the close, how far the
fills were from Hyperliquid's mid when we signed the open or decided to exit, and whether the booked
payout matches :mod:`payout`. Whatever does not line up is listed under ``discrepancies``.

Hedge pairs (``papertrade probe-pair``) are also reported as pairs: whether both legs entered at the
same price and closed in the same transaction, what the pair lost, and how much $PAPER the losing leg
minted against the model (``paper_minted`` per ``paper_mint_basis``; basis is 98% of the loss).
"""

from __future__ import annotations

import calendar
import json
import statistics
import time
from pathlib import Path
from typing import Any

from . import deployment, payout
from .wire import TradingState

MODEL_TOLERANCE_USD = 0.01
MAX_PAGES = 40


def read_audit(path: str | Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def fetch_trades(api, user: str, since_s: float | None = None) -> list[dict[str, Any]]:
    """Trade history, newest first, paging back until ``since_s`` (unix seconds)."""
    items: list[dict[str, Any]] = []
    cursor = None
    for _ in range(MAX_PAGES):
        page = api.history(user, "trades", cursor)
        items += page.get("items", [])
        cursor = page.get("nextCursor")
        oldest = min((i["timestampMs"] for i in page.get("items", [])), default=None)
        if not cursor or (since_s is not None and oldest is not None and oldest / 1000 < since_s):
            break
    return items


def _ts(event: dict[str, Any]) -> float:
    # Lines written before 2026-11-04 carry only the second-resolution "at".
    return event.get("ts") or calendar.timegm(time.strptime(event["at"], "%Y-%m-%dT%H:%M:%SZ"))


def reconcile(
    audit: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    trading: TradingState,
    since_s: float | None = None,
    open_intents: set[str] | None = None,
    now_s: float | None = None,
) -> dict[str, Any]:
    now_s = time.time() if now_s is None else now_s
    opens: dict[str, dict[str, Any]] = {}
    accepted: set[str] = set()
    seen: dict[int, str] = {}  # position_id -> open intent, recorded by the exit engine
    closes: dict[int, list[dict[str, Any]]] = {}
    for e in audit:
        if e.get("event") == "submit" and e.get("action") == "openPosition":
            opens[e["intent_id"]] = {
                "ts": _ts(e),
                "mid": e.get("mid"),
                "market": e.get("market"),
                "side": e.get("side"),
                "margin_usd": float(e.get("margin_usd") or 0),
                "leverage": e.get("leverage"),
                "pair": e.get("pair"),
            }
        elif e.get("event") == "accepted" and e.get("action") == "openPosition":
            accepted.add(e["intent_id"])
        elif e.get("event") == "position" and e.get("intent_id"):
            seen[int(e["position_id"])] = e["intent_id"]
        elif e.get("event") == "submit" and e.get("action") == "closePositions":
            for pid in e.get("position_ids") or []:
                exit_ = (e.get("exit") or {}).get(str(pid), {})
                closes.setdefault(int(pid), []).append({"ts": _ts(e), "reason": exit_.get("reason"), "mark": exit_.get("mark")})

    rows, discrepancies, used = [], [], set()
    for t in sorted(trades, key=lambda t: t["timestampMs"]):
        if t.get("exitPriceRaw") is None:
            continue
        closed_s = t["timestampMs"] / 1000
        if since_s is not None and closed_s < since_s:
            continue
        pid, scale, sign = int(t["positionId"]), int(t["priceScaleRaw"]), 1 if t["isLong"] else -1
        entry, exit_ = int(t["entryPriceRaw"]) / scale, int(t["exitPriceRaw"]) / scale
        margin, leverage = int(t["marginRaw"]) / 1e18, int(t["leverage"])
        row: dict[str, Any] = {
            "position_id": pid,
            "market": t["symbol"],
            "side": "long" if t["isLong"] else "short",
            "margin_usd": margin,
            "leverage": leverage,
            "kind": t["kind"],
            "opened_at": t["openedAtMs"] / 1000,
            "closed_at": closed_s,
            "entry": entry,
            "exit": exit_,
            "pnl_usd": (int(t["adjustedPnlRaw"]) - int(t.get("userPaidFeeRaw") or 0)) / 1e18,
        }
        if t.get("txHash"):
            row["tx_hash"] = t["txHash"]
        if t.get("paperMintedRaw") is not None:
            row["paper_minted"] = int(t["paperMintedRaw"]) / 1e18
            row["paper_mint_basis"] = int(t.get("paperMintBasisRaw") or 0) / 1e18
        intent = seen.get(pid) or _match_open(opens, t, used)
        if intent and intent in opens:
            used.add(intent)
            o = opens[intent]
            row["open_intent"] = intent
            if o.get("pair"):
                row["pair"] = o["pair"]
            row["open_latency_s"] = t["openedAtMs"] / 1000 - o["ts"]
            if o["mid"]:
                row["entry_slippage_bps"] = sign * (entry - o["mid"]) / o["mid"] * 1e4  # positive: worse for us
        else:
            discrepancies.append(f"#{pid}: no audited open (opened outside this client?)")
        close = max((c for c in closes.get(pid, []) if c["ts"] <= closed_s), key=lambda c: c["ts"], default=None)
        if close:
            row["close_reason"] = close["reason"] or "manual"
            row["close_latency_s"] = closed_s - close["ts"]
            if close["mark"]:
                row["exit_slippage_bps"] = sign * (close["mark"] - exit_) / close["mark"] * 1e4
        elif t["kind"] == "PositionClosed":
            discrepancies.append(f"#{pid}: closed without an audited close")
        if t["kind"] == "PositionClosed":
            model = _model_pnl(t, trading)
            if model is not None:
                row["model_pnl_usd"] = model
                if abs(model - row["pnl_usd"]) > MODEL_TOLERANCE_USD:
                    discrepancies.append(f"#{pid}: booked ${row['pnl_usd']:.4f}, model ${model:.4f}")
        rows.append(row)

    # Needs the open positions: without an account snapshot, a position still open would look lost.
    for intent in accepted - used - open_intents if open_intents is not None else ():
        o = opens.get(intent)
        if o and now_s - o["ts"] > deployment.INTENT_LIFETIME_S and (since_s is None or o["ts"] >= since_s):
            discrepancies.append(f"open {intent[:10]}… was accepted but no closed position matches it")
    return {"positions": rows, "summary": _summary(rows), "pairs": _pairs(rows), "discrepancies": discrepancies}


def _pairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per hedge pair that has a closed position, oldest first."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("pair"):
            groups.setdefault(r["pair"], []).append(r)
    out = []
    for pair_id, legs in groups.items():
        legs = sorted(legs, key=lambda r: r["closed_at"])
        long = next((r for r in legs if r["side"] == "long"), None)
        short = next((r for r in legs if r["side"] == "short"), None)
        complete = bool(long and short and len(legs) == 2)
        entry: dict[str, Any] = {"pair": pair_id, "positions": [r["position_id"] for r in legs], "complete": complete}
        if not complete:
            out.append(entry)  # a leg is still open, was refused, or was closed on its own
            continue
        mid = (long["entry"] + short["entry"]) / 2
        pnl = long["pnl_usd"] + short["pnl_usd"]
        loser = min((long, short), key=lambda r: r["pnl_usd"])
        basis = sum(r.get("paper_mint_basis", 0.0) for r in legs)
        minted = sum(r.get("paper_minted", 0.0) for r in legs)
        entry |= {
            "market": long["market"],
            "margin_usd": long["margin_usd"],
            "leverage": long["leverage"],
            "opened_at": min(r["opened_at"] for r in legs),
            "closed_at": max(r["closed_at"] for r in legs),
            # Positive: the long entered above the short, which costs the pair that much (it is locked in).
            "entry_gap_bps": (long["entry"] - short["entry"]) / mid * 1e4,
            "exit_gap_bps": (long["exit"] - short["exit"]) / mid * 1e4,
            "opened_gap_s": abs(long["opened_at"] - short["opened_at"]),
            "same_entry": long["entry"] == short["entry"],
            "same_close_tx": bool(long.get("tx_hash")) and long.get("tx_hash") == short.get("tx_hash"),
            "kinds": [r["kind"] for r in legs],
            "pnl_usd": pnl,
            "loser_loss_usd": -loser["pnl_usd"],
        }
        models = [r.get("model_pnl_usd") for r in legs]
        if all(m is not None for m in models):
            entry["model_pnl_usd"] = sum(models)
        if minted or basis:
            entry["paper_mint_basis"] = basis
            entry["paper_minted"] = minted
            if basis > 0:
                entry["paper_per_basis"] = minted / basis
                entry["cost_per_basis_usd"] = -pnl / basis
                # 0.98 = the loss less the 2% LP-side fee (1.0 for a liquidation). Lower would mean a win offset it.
                entry["basis_per_loss"] = basis / -loser["pnl_usd"] if loser["pnl_usd"] < 0 else None
            if minted > 0:
                entry["cost_per_paper_usd"] = -pnl / minted
        out.append(entry)
    return sorted(out, key=lambda e: e.get("closed_at", 0))


def _match_open(opens: dict[str, dict[str, Any]], trade: dict[str, Any], used: set[str]) -> str | None:
    opened_s = trade["openedAtMs"] / 1000
    candidates = [
        (o["ts"], intent)
        for intent, o in opens.items()
        if intent not in used
        and o["market"] == trade["symbol"]
        and (o["side"] == "long") == trade["isLong"]
        and abs(o["margin_usd"] - int(trade["marginRaw"]) / 1e18) < 1e-9
        and o["leverage"] == int(trade["leverage"])
        and o["ts"] - 5 <= opened_s <= o["ts"] + deployment.INTENT_LIFETIME_S
    ]
    return max(candidates)[1] if candidates else None


def _model_pnl(trade: dict[str, Any], trading: TradingState) -> float | None:
    """The model payout closest to the booked one over both curves (the history does not say which applied)."""
    try:
        instrument = trading.instrument(int(trade["instrumentId"]))
    except Exception:
        return None
    booked = (int(trade["adjustedPnlRaw"]) - int(trade.get("userPaidFeeRaw") or 0)) / 1e18
    models = [
        payout.settle(
            is_long=trade["isLong"],
            entry_raw=int(trade["entryPriceRaw"]),
            mark_raw=int(trade["exitPriceRaw"]),
            margin_usd=int(trade["marginRaw"]) / 1e18,
            leverage=int(trade["leverage"]),
            impact=payout.Impact.of(instrument),
            curve=curve,
            win_fee_rate=trading.win_fee_rate_raw / 1e18,
        ).payout_pnl_usd
        for curve in (payout.STANDARD_CURVE, payout.OFF_HOURS_CURVE)
    ]
    return min(models, key=lambda m: abs(m - booked))


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def stats(key: str) -> dict[str, float] | None:
        values = [r[key] for r in rows if r.get(key) is not None]
        if not values:
            return None
        return {"median": statistics.median(values), "mean": statistics.fmean(values), "max": max(values), "n": len(values)}

    reasons: dict[str, int] = {}
    for r in rows:
        reasons[r.get("close_reason") or r["kind"]] = reasons.get(r.get("close_reason") or r["kind"], 0) + 1
    return {
        "positions": len(rows),
        "pnl_usd": sum(r["pnl_usd"] for r in rows),
        "wins": sum(1 for r in rows if r["pnl_usd"] > 0),
        "open_latency_s": stats("open_latency_s"),
        "close_latency_s": stats("close_latency_s"),
        "entry_slippage_bps": stats("entry_slippage_bps"),
        "exit_slippage_bps": stats("exit_slippage_bps"),
        "close_reasons": reasons,
    }


def digest(report: dict[str, Any], hours: float | None) -> str:
    s = report["summary"]
    window = f"last {hours:g} h" if hours else "all time"

    def fmt(key: str, unit: str) -> str:
        v = s[key]
        return f"median {v['median']:.1f}{unit}, max {v['max']:.1f}{unit}" if v else "n/a"

    lines = [
        f"papertrade reconcile ({window}): {s['positions']} positions, {s['wins']} wins, pnl ${s['pnl_usd']:+.2f}",
        f"open latency {fmt('open_latency_s', ' s')}; close latency {fmt('close_latency_s', ' s')}",
        f"entry slippage {fmt('entry_slippage_bps', ' bps')}; exit slippage {fmt('exit_slippage_bps', ' bps')}",
    ]
    if report.get("pairs"):
        lines.append(_pairs_line(report["pairs"]))
    lines.append(f"discrepancies: {len(report['discrepancies'])}" + "".join(f"\n• {d}" for d in report["discrepancies"][:10]))
    return "\n".join(lines)


def _pairs_line(pairs: list[dict[str, Any]]) -> str:
    done = [p for p in pairs if p["complete"]]
    text = f"pairs: {len(done)} complete" + (f", {len(pairs) - len(done)} broken or open" if len(done) != len(pairs) else "")
    if not done:
        return text
    minted = sum(p.get("paper_minted", 0.0) for p in done)
    cost_basis = [p["cost_per_basis_usd"] for p in done if "cost_per_basis_usd" in p]
    text += (
        f", pnl ${sum(p['pnl_usd'] for p in done):+.2f}, same entry {sum(p['same_entry'] for p in done)}/{len(done)}, "
        f"same close tx {sum(p['same_close_tx'] for p in done)}/{len(done)}, entry gap max "
        f"{max(abs(p['entry_gap_bps']) for p in done):.1f} bps, PAPER minted {minted:,.1f}"
    )
    if cost_basis:
        text += f", cost ${statistics.median(cost_basis):.2f} per $1 of mint basis (median)"
    return text
