"""``papertrade`` command line: operator tasks and the drift monitor.

Writes are dry-runs unless both ``PAPERTRADE_LIVE_TRADING=true`` and ``--live``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from eth_account import Account

from . import drift
from .trader import GuardError, WriteResult

# A close waited 46 s for its batch on 2026-11-04; give live writes a minute and a half to settle.
OUTCOME_WAIT_S = 90
# The probe opens only while the exit engine has reported within this window.
EXITS_FRESH_S = 60


def _duration(text: str) -> float:
    """Seconds from "300", "300s", "5m", "2h" or "1d"."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (e.g. 300, 5m, 2h, 1d)")
    return float(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]


def _print(value: Any) -> None:
    def default(o: Any) -> Any:
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        if isinstance(o, (set, frozenset, tuple)):
            return list(o)
        return str(o)

    print(json.dumps(value, indent=2, default=default))


def _client(args: argparse.Namespace, stream: bool = True, require_snapshot: bool = True):
    from .client import PapertradeClient

    client = PapertradeClient.from_env()
    if not stream:
        return client
    try:
        return client.start()
    except TimeoutError:
        if require_snapshot:
            raise TimeoutError(f"no account snapshot from Papertrade ({client.stream.last_error or 'stream not ready'})") from None
        print(f"warning: no account snapshot ({client.stream.last_error or 'stream not ready'})", file=sys.stderr)
        return client


def _write(result: WriteResult, client=None) -> int:
    output = dataclasses.asdict(result)
    if client is not None and result.mode == "live" and client.stream.state() is not None:
        # Wait while the stream is up, so the outcome is recorded for the processes that come after.
        output["outcomes"] = {
            s.intent_id: client.trader.wait_intent(s.intent_id, OUTCOME_WAIT_S) for s in result.submissions if s.acknowledged
        }
    _print(output)
    if result.mode == "dry-run" and result.violations:
        print("dry-run: a live request would be refused by the guards above", file=sys.stderr)
        return 1
    return 0


def cmd_keygen(args: argparse.Namespace) -> int:
    account = Account.create()
    text = (
        f"# Papertrade wallet {account.address.lower()} (fund it through `papertrade deposit-address`)\n"
        f"PAPERTRADE_WALLET_PRIVATE_KEY=0x{account.key.hex().removeprefix('0x')}\n"
    )
    if not args.out:
        sys.stdout.write(text)
        return 0
    # O_EXCL: never overwrite an existing key file; 0600: the key is real money.
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    print(f"wrote wallet {account.address.lower()} to {args.out}", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    client = _client(args)
    account = client.stream.state()
    trading = client.api.trading_state()
    _print(
        {
            "wallet": client.address,
            "mode": "live" if client.config.live_trading else "dry-run",
            "data_dir": str(client.store.dir),
            "kill_switch": client.store.kill_switch(),
            "session": client.trader.session.status(),
            "stream_age_s": client.stream.age_s(),
            "indexer_lag_s": account and client.trader.clock() - account.indexed_through_timestamp,
            "inflight": client.trader.inflight(),
            "balances_usd": account
            and {
                "balance": account.balance / 1e18,
                "available": account.funds.available_balance / 1e18,
                "locked_margin": account.locked / 1e18,
                "queued": account.queued / 1e18,
            },
            "lifetime_realized_pnl_usd": account and account.lifetime.realized_pnl_raw / 1e18,
            "open_positions": account and len(account.open_positions),
            "pending_operations": account and [dataclasses.asdict(op) for op in account.operations],
            "papertrade": {
                "trading_paused": trading.trading_paused,
                "paused_actions": trading.intake.paused_actions,
                "exchange": trading.exchange,
                "off_hours": trading.off_hours,
            },
            "limits": client.config.limits,
        }
    )
    return 0


def cmd_markets(args: argparse.Namespace) -> int:
    from .api import PapertradeApi

    state = PapertradeApi().trading_state()
    _print(
        {
            "trading_paused": state.trading_paused,
            "exchange": state.exchange,
            "off_hours": state.off_hours,
            "minimum_margin_usd": state.minimum_trade_size_raw / 1e18,
            "minimum_notional_usd": state.minimum_open_notional_raw / 1e18,
            "win_fee_rate": state.win_fee_rate_raw / 1e18,
            "account_oi_cap_usd": (state.account_oi_cap_raw or 0) / 1e18,
            "instruments": [
                {
                    "symbol": i.symbol,
                    "id": i.id,
                    "openable": i.openable,
                    "max_leverage": i.max_leverage,
                    "max_position_notional_usd": i.max_position_notional_raw / 1e18,
                    "bust_buffer_bps": i.bust_buffer_raw / 1e14,
                    "long_headroom": (i.long_cap_raw - i.long_oi_raw) / 1e18,
                    "short_headroom": (i.short_cap_raw - i.short_oi_raw) / 1e18,
                }
                for i in state.instruments
            ],
        }
    )
    return 0


def cmd_positions(args: argparse.Namespace) -> int:
    _print(_client(args).trader.position_marks())
    return 0


def cmd_quote(args: argparse.Namespace) -> int:
    _print(_client(args, stream=False).trader.quote(args.market, args.side, args.margin, args.leverage, args.exit))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    client = _client(args, stream=False)
    _print(client.api.history(client.address, args.kind, args.cursor))
    return 0


def cmd_deposit_address(args: argparse.Namespace) -> int:
    info = _client(args, stream=False).deposit_address()
    info["notes"] = [
        "Send USDC on Hyperliquid (HyperCore) to deposit_address, only if matches_relayer is true.",
        "Not on Arbitrum, Ethereum or HyperEVM, and not to the wallet address: Papertrade never credits those transfers.",
        "Funds on another chain: use Deposit in papertrade.xyz, connected as this wallet; it bridges to deposit_address.",
        "At least 10 USDC must arrive; the first deposit pays a one-time 1 USDC activation fee.",
        "Send one deposit at a time and wait for it to be credited.",
    ]
    _print(info)
    return 0 if info["matches_relayer"] else 1


def cmd_session(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.action == "register":
        if not client.config.live_trading:
            print("session registration is a signed write; set PAPERTRADE_LIVE_TRADING=true to send it", file=sys.stderr)
            return 1
        _print(client.trader.session.register())
        client.stream.wait_for(lambda s: client.trader.session.status().active_key is not None, 60)
    _print(client.trader.session.status())
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    from .exits import ExitPlan

    plan = ExitPlan(args.stop, args.take, args.max_hold) if (args.stop, args.take, args.max_hold) != (None, None, None) else None
    client = _client(args)
    result = client.trader.open(args.market, args.side, args.margin, args.leverage, dry_run=not args.live, exit_plan=plan)
    return _write(result, client)


def cmd_close(args: argparse.Namespace) -> int:
    client = _client(args, require_snapshot=not args.force)
    return _write(client.trader.close(args.position_ids, dry_run=not args.live, force=args.force), client)


def cmd_close_all(args: argparse.Namespace) -> int:
    client = _client(args)
    return _write(client.trader.close_all(dry_run=not args.live), client)


def cmd_cancel(args: argparse.Namespace) -> int:
    client = _client(args)
    return _write(client.trader.cancel(args.intent_id, dry_run=not args.live), client)


def cmd_withdraw(args: argparse.Namespace) -> int:
    return _write(_client(args).trader.withdraw(args.amount, dry_run=not args.live))


def _kill_switch_store():
    # Only the data directory: the kill switch must work even when the rest of the config is broken.
    from .config import data_dir
    from .store import StateStore

    return StateStore(data_dir())


def cmd_kill_switch(args: argparse.Namespace) -> int:
    store = _kill_switch_store()
    if args.action in ("on", "off"):
        store.set_kill_switch(args.action == "on", args.reason)
    _print({"data_dir": str(store.dir), **store.kill_switch()})
    return 0


def cmd_drift(args: argparse.Namespace) -> int:
    try:
        return _drift(args)
    except Exception as error:  # a crash must not read as "warn" (exit 1)
        report = drift.Report()
        report.add("monitor", "error", f"drift monitor crashed: {error!r}")
        if args.markdown:
            Path(args.markdown).write_text(report.markdown())
        print(report.markdown())
        logging.getLogger(__name__).exception("drift monitor crashed")
        return 3


def _drift(args: argparse.Namespace) -> int:
    observed, errors = drift.observe(batch_blocks=args.blocks)
    if args.pin:
        if errors:
            _print([dataclasses.asdict(e) for e in errors])
            print("not pinning: some sources could not be observed", file=sys.stderr)
            return 3
        drift.pin(observed)
        print(f"pinned the live protocol to {drift.PINNED_PATH}", file=sys.stderr)
        return 0
    report = drift.compare(drift.load_pinned(), observed, errors)
    if args.json:
        Path(args.json).write_text(json.dumps(report.as_dict(), indent=2, default=str))
    if args.markdown:
        Path(args.markdown).write_text(report.markdown())
    print(report.markdown())
    breaking = [f.message for f in report.findings if f.level == "breaking"]
    if args.kill_on_breaking and breaking:
        store = _kill_switch_store()
        store.set_kill_switch(True, ("protocol drift: " + "; ".join(breaking))[:200])
        print(f"kill switch engaged in {store.dir}", file=sys.stderr)
    return report.exit_code()


def cmd_exits(args: argparse.Namespace) -> int:
    from . import watchdog
    from .client import PapertradeClient
    from .exits import ExitEngine

    logging.getLogger("papertrade_client").setLevel(logging.INFO)  # only ours: httpx would log the Telegram token
    ExitEngine(PapertradeClient.from_env().trader, watchdog.Notifier.from_env(), poll_s=args.poll).run_forever()
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """One minimum-size trade with a random side, closed by the exit engine after --hold."""
    from .exits import ExitPlan, read_heartbeat

    client = _client(args)
    beat = read_heartbeat(client.store.dir)
    if args.live and client.config.live_trading and (not beat or beat.get("mode") != "live" or time.time() - beat["at"] > EXITS_FRESH_S):
        print("refused: the exit engine is not running live, nothing would close the probe", file=sys.stderr)
        return 1
    trading = client.api.trading_state()
    margin = trading.minimum_trade_size_raw / 1e18
    leverage = math.ceil(trading.minimum_open_notional_raw / trading.minimum_trade_size_raw)
    side = secrets.choice(("long", "short"))
    plan = ExitPlan(max_hold_s=args.hold)
    return _write(client.trader.open(args.market, side, f"{margin:g}", leverage, dry_run=not args.live, exit_plan=plan), client)


def cmd_reconcile(args: argparse.Namespace) -> int:
    from . import reconcile, watchdog

    client = _client(args, require_snapshot=False)
    since = time.time() - args.since if args.since else None
    account = client.stream.state()
    open_intents = {p.intent_id for p in account.open_positions if p.intent_id} if account else None
    trades = reconcile.fetch_trades(client.api, client.address, since)
    report = reconcile.reconcile(reconcile.read_audit(client.audit.path), trades, client.api.trading_state(), since, open_intents)
    text = reconcile.digest(report, args.since / 3600 if args.since else None)
    if args.notify:
        watchdog.Notifier.from_env().send(text)
    if args.digest:
        print(text)
    else:
        _print(report)
    return 1 if report["discrepancies"] else 0


def cmd_watchdog(args: argparse.Namespace) -> int:
    from . import watchdog
    from .config import data_dir

    result = watchdog.run(Path(data_dir()), watchdog.Notifier.from_env(), watchdog.observe(), test_message=args.test)
    _print(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="papertrade", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("keygen", help="generate a dedicated wallet key")
    p.add_argument("--out", help="write to this file (mode 0600, never overwritten) instead of stdout")
    p.set_defaults(fn=cmd_keygen)
    sub.add_parser("status", help="account, session, limits and mode").set_defaults(fn=cmd_status)
    sub.add_parser("markets", help="market parameters (no wallet needed)").set_defaults(fn=cmd_markets)
    sub.add_parser("positions", help="open positions with mark and close estimate").set_defaults(fn=cmd_positions)
    sub.add_parser("deposit-address", help="where to send USDC on Hyperliquid").set_defaults(fn=cmd_deposit_address)

    p = sub.add_parser("quote", help="bust price and payout curve for a hypothetical open")
    p.add_argument("market", choices=["BTC", "ETH"])
    p.add_argument("side", choices=["long", "short"])
    p.add_argument("margin")
    p.add_argument("leverage", type=int)
    p.add_argument("--exit", type=float)
    p.set_defaults(fn=cmd_quote)

    p = sub.add_parser("history", help="indexer history")
    p.add_argument("kind", choices=["trades", "cashflows", "staking", "queue"])
    p.add_argument("--cursor")
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("session", help="session key status / registration")
    p.add_argument("action", choices=["status", "register"], nargs="?", default="status")
    p.set_defaults(fn=cmd_session)

    for name, fn, helptext in (("open", cmd_open, "open a position"),):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("market", choices=["BTC", "ETH"])
        p.add_argument("side", choices=["long", "short"])
        p.add_argument("margin", help="margin in USD, e.g. 10")
        p.add_argument("leverage", type=int)
        p.add_argument("--live", action="store_true")
        p.add_argument("--stop", type=float, metavar="PCT", help="exit plan: close after this adverse move, %% of entry")
        p.add_argument("--take", type=float, metavar="PCT", help="exit plan: close after this favourable move, %% of entry")
        p.add_argument("--max-hold", type=_duration, metavar="DURATION", help="exit plan: close after this long (5m, 2h)")
        p.set_defaults(fn=fn)

    p = sub.add_parser("close", help="close positions by id")
    p.add_argument("position_ids", nargs="+", type=int)
    p.add_argument("--live", action="store_true")
    p.add_argument("--force", action="store_true", help="no account snapshot needed: sign with the newest stored session key")
    p.set_defaults(fn=cmd_close)

    p = sub.add_parser("close-all", help="close every open position")
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn=cmd_close_all)

    p = sub.add_parser("cancel", help="cancel a pending intent")
    p.add_argument("intent_id")
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn=cmd_cancel)

    p = sub.add_parser("withdraw", help="withdraw USDC to the wallet's Hyperliquid account")
    p.add_argument("amount", help="USDC, at least 10")
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn=cmd_withdraw)

    p = sub.add_parser("kill-switch", help="block opens (closes still work)")
    p.add_argument("action", choices=["status", "on", "off"], nargs="?", default="status")
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_kill_switch)

    p = sub.add_parser("drift", help="compare the live protocol with the pinned one")
    p.add_argument("--blocks", type=int, default=300, help="recent HyperEVM blocks to re-verify")
    p.add_argument("--json")
    p.add_argument("--markdown")
    p.add_argument("--pin", action="store_true", help="write the live protocol as the new baseline")
    p.add_argument("--kill-on-breaking", action="store_true", help="engage the local kill switch on breaking drift")
    p.set_defaults(fn=cmd_drift)

    p = sub.add_parser("exits", help="run the exit engine: stops, take-profits, holding limits (long-running)")
    p.add_argument("--poll", type=float, default=1.0, help="seconds between passes")
    p.set_defaults(fn=cmd_exits)

    p = sub.add_parser("probe", help="execution probe: one minimum-size trade, random side, timed exit")
    p.add_argument("--market", choices=["BTC", "ETH"], default="BTC")
    p.add_argument("--hold", type=_duration, default=300, help="close after this long (default 5m)")
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("reconcile", help="audit log against the trade history: latency, slippage, payout")
    p.add_argument("--since", type=_duration, help="only positions closed within this window (24h)")
    p.add_argument("--digest", action="store_true", help="print the short text digest instead of JSON")
    p.add_argument("--notify", action="store_true", help="also send the digest to Telegram")
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("watchdog", help="health check; alerts to Telegram when a problem appears or clears")
    p.add_argument("--test", action="store_true", help="also send the daily summary now")
    p.set_defaults(fn=cmd_watchdog)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(args)
    except GuardError as error:
        _print({"refused": [dataclasses.asdict(v) for v in error.violations]})
        return 1
    except (ValueError, TimeoutError, FileExistsError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
