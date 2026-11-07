"""``papertrade watchdog``: a periodic health check of a deployed client, alerting to Telegram.

Each run observes the account stream, Papertrade's trading state, the kill switch, the session key,
in-flight intents, open positions and the drift monitor's last report, and turns them into a set of
problems. Only changes are sent: a problem that appeared or one that cleared. A daily summary doubles
as a heartbeat, so a silent chat means the watchdog itself has stopped.

Without ``PAPERTRADE_TELEGRAM_BOT_TOKEN`` and ``PAPERTRADE_TELEGRAM_CHAT_ID`` messages go to stdout.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DRIFT_REPORT = "drift.json"
STATE_FILE = "watchdog.json"

INFLIGHT_ALERT_S = 300  # an open the relayer accepted but the account stream still does not show
BUST_ALERT_FRACTION = 0.3  # alert when less than this share of the entry-to-bust distance is left
CONFIRM_AFTER_S = 300  # Papertrade's API drops out for 2-4 minutes several times a day; alert when it lasts longer
DRIFT_MAX_AGE_S = 3 * 3600  # the drift timer runs hourly; allow two failed observations
EXITS_SILENT_S = 120  # the exit engine writes a heartbeat every 10 s and systemd restarts it within 5 s
HEARTBEAT_HOUR_UTC = 7


@dataclass
class Observation:
    now: float
    wallet: str | None = None
    mode: str | None = None
    errors: dict[str, str] = field(default_factory=dict)  # source -> why it could not be observed
    stream_age_s: float | None = None
    snapshot_max_age_s: float = 30
    kill_switch: dict[str, Any] | None = None
    trading_paused: bool = False
    paused_actions: list[str] = field(default_factory=list)
    balance_usd: float | None = None
    session_valid_for_s: float | None = None
    session_renew_before_s: float = 72 * 3600
    inflight: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    drift: dict[str, Any] | None = None  # the drift monitor's --json report plus its file mtime
    drift_observed_at: float | None = None  # mtime of the last report that was not an error (kept by run())
    exits: dict[str, Any] | None = None  # the exit engine's heartbeat


def observe(env: dict[str, str] | None = None) -> Observation:
    """Gather everything the checks need; a source that fails is recorded, never raised."""
    from .client import PapertradeClient
    from .config import data_dir, load
    from .exits import read_heartbeat

    obs = Observation(now=time.time())
    obs.drift = _read_drift(Path(data_dir(env)) / DRIFT_REPORT)
    obs.exits = read_heartbeat(data_dir(env))
    try:
        config = load(env)
    except ValueError as error:
        obs.errors["config"] = str(error)
        return obs
    client = PapertradeClient(config)
    obs.wallet = client.address
    obs.mode = "live" if config.live_trading else "dry-run"
    obs.snapshot_max_age_s = config.snapshot_max_age_s
    obs.session_renew_before_s = config.session_renew_before_s
    obs.kill_switch = client.store.kill_switch()
    try:
        trading = client.api.trading_state()
        obs.trading_paused = trading.trading_paused
        obs.paused_actions = list(trading.intake.paused_actions)
    except Exception as error:
        obs.errors["papertrade_api"] = _short(error)
    try:
        client.start()
    except Exception as error:
        obs.errors["stream"] = client.stream.last_error or _short(error)
        client.stream.stop()
        return obs
    try:
        account = client.stream.state()
        obs.stream_age_s = client.stream.age_s()
        obs.balance_usd = account.balance / 1e18
        obs.session_valid_for_s = client.trader.session.status().valid_for_s
        obs.inflight = client.trader.inflight()
        obs.positions = client.trader.position_marks()
    except Exception as error:
        obs.errors["account"] = _short(error)
    finally:
        client.stream.stop()
    return obs


def _needs_confirmation(key: str) -> bool:
    # Upstream hiccups clear by themselves; alert only when they persist for CONFIRM_AFTER_S.
    return key.startswith("unobservable:") or key == "stream_stale"


def problems(obs: Observation) -> dict[str, str]:
    """Current problems keyed by a stable id, so a value that drifts (an age, a price) does not re-alert."""
    out: dict[str, str] = {}
    for source, error in obs.errors.items():
        out[f"unobservable:{source}"] = f"{source} unavailable: {error}"
    if obs.stream_age_s is not None and obs.stream_age_s > obs.snapshot_max_age_s:
        out["stream_stale"] = f"account stream silent for {obs.stream_age_s:.0f} s; opens are refused"
    if obs.kill_switch and obs.kill_switch.get("engaged"):
        out["kill_switch"] = f"kill switch engaged: {obs.kill_switch.get('reason') or 'no reason given'}"
    if obs.trading_paused:
        out["trading_paused"] = "Papertrade has paused trading"
    # One key per action: a pause that spreads from opens to closes is a new alert, not the same one reworded.
    for action in obs.paused_actions:
        if action == "closePositions" and obs.positions:
            out[f"paused:{action}"] = f"Papertrade paused closePositions: {len(obs.positions)} open position(s) cannot be closed"
        else:
            out[f"paused:{action}"] = f"Papertrade paused {action}"
    if obs.balance_usd and obs.session_valid_for_s is None and obs.mode == "live":
        out["session_missing"] = "no active session key: trades cannot be signed (`papertrade session register`)"
    elif obs.session_valid_for_s is not None and obs.session_valid_for_s < obs.session_renew_before_s:
        out["session_expiring"] = f"session key expires in {obs.session_valid_for_s / 3600:.0f} h"
    for item in obs.inflight:
        age = obs.now - item.get("submitted_at", obs.now)
        if age > INFLIGHT_ALERT_S:
            intent = f"{item['action']} {item['intent_id'][:10]}…"
            out[f"inflight:{item['intent_id']}"] = f"{intent} submitted {age / 60:.0f} min ago, not on the stream"
    for p in obs.positions:
        left = bust_fraction_left(p)
        if left is not None and left < BUST_ALERT_FRACTION:
            where = f"mark {p['mark']:.1f}, bust {p['bust']:.1f} ({left:.0%} of the distance left)"
            out[f"bust:{p['position_id']}"] = f"{p['market']} {p['side']} #{p['position_id']} near bust: {where}"
    out.update(_drift_problems(obs))
    out.update(_exit_problems(obs))
    return out


def _exit_problems(obs: Observation) -> dict[str, str]:
    beat = obs.exits
    if beat is None:
        return {"exits_missing": "positions are open but the exit engine has never run"} if obs.positions else {}
    out = {}
    silent = obs.now - beat["at"]
    if silent > EXITS_SILENT_S:
        out["exits_silent"] = f"exit engine silent for {silent / 60:.0f} min: open positions have no stops"
    if obs.mode == "live" and beat.get("mode") != "live":
        out["exits_dry_run"] = "trading is live but the exit engine runs dry-run: restart papertrade-exits"
    return out


def bust_fraction_left(position: dict[str, Any]) -> float | None:
    """Share of the entry-to-bust distance the mark has not covered yet (0 at or past bust)."""
    mark, entry, bust = position.get("mark"), position["entry"], position["bust"]
    if mark is None or entry == bust:
        return None
    return max(0.0, (mark - bust) / (entry - bust))


def _drift_problems(obs: Observation) -> dict[str, str]:
    report = obs.drift
    if report is None:
        return {"drift_missing": "no drift report yet: is papertrade-drift.timer running?"}
    out = {}
    age = obs.now - report["mtime"]
    if age > DRIFT_MAX_AGE_S:
        out["drift_stale"] = f"drift report is {age / 3600:.0f} h old: is papertrade-drift.timer running?"
    level = report.get("level")
    findings = "; ".join(f["message"] for f in report.get("findings", []) if f.get("level") == level)[:500]
    if level == "error":
        # One failed hourly observation is routine; alert when none has succeeded for DRIFT_MAX_AGE_S.
        blind_s = obs.now - (obs.drift_observed_at if obs.drift_observed_at is not None else obs.now)
        if blind_s > DRIFT_MAX_AGE_S:
            out["drift_unobservable"] = f"drift monitor has not observed the protocol for {blind_s / 3600:.0f} h: {findings}"
    elif level in ("warn", "breaking"):
        out[f"drift_{level}"] = f"protocol drift {level}: {findings}"
    return out


def _read_drift(path: Path) -> dict[str, Any] | None:
    try:
        report = json.loads(path.read_text())
        return {"level": report.get("level"), "findings": report.get("findings", []), "mtime": path.stat().st_mtime}
    except (OSError, ValueError):
        return None


def summary(obs: Observation, current: dict[str, str]) -> str:
    positions = ", ".join(f"{p['market']} {p['side']} ${p['margin_usd']:.0f}×{p['leverage']}" for p in obs.positions) or "none"
    lines = [
        f"papertrade daily: {obs.mode or '?'} {obs.wallet or ''}",
        f"balance ${obs.balance_usd:.2f}" if obs.balance_usd is not None else "balance unknown",
        f"positions: {positions}",
        f"session key: {obs.session_valid_for_s / 86400:.1f} days left" if obs.session_valid_for_s is not None else "session key: none",
        f"drift: {obs.drift['level'] if obs.drift else 'no report'}",
        f"problems: {len(current)}" + "".join(f"\n• {m}" for m in current.values()),
    ]
    return "\n".join(lines)


def messages(previous: dict[str, str], current: dict[str, str]) -> list[str]:
    out = [f"🔴 {msg}" for key, msg in current.items() if key not in previous]
    out += [f"✅ cleared: {msg}" for key, msg in previous.items() if key not in current]
    return out


class Notifier:
    def __init__(self, token: str | None, chat_id: str | None, timeout_s: float = 10):
        self.token, self.chat_id, self.timeout_s = token, chat_id, timeout_s

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Notifier:
        env = os.environ if env is None else env
        return cls(env.get("PAPERTRADE_TELEGRAM_BOT_TOKEN") or None, env.get("PAPERTRADE_TELEGRAM_CHAT_ID") or None)

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> None:
        if not self.configured:
            print(text, flush=True)
            return
        response = httpx.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
            timeout=self.timeout_s,
        )
        if response.status_code != 200:
            # The URL holds the token, so report only Telegram's own description.
            raise RuntimeError(f"telegram sendMessage failed: {response.status_code} {response.text[:200]}")


def run(data_dir: Path, notifier: Notifier, obs: Observation, test_message: bool = False) -> dict[str, Any]:
    """Compare with the previous run, send what changed, and persist the new state.

    State is saved only after every message went out, so a failed send is retried next run.
    """
    path = data_dir / STATE_FILE
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    if obs.drift and obs.drift.get("level") != "error":
        obs.drift_observed_at = obs.drift["mtime"]
    else:
        obs.drift_observed_at = state.get("drift_observed_at", obs.now)
    seen = problems(obs)
    since = {k: state.get("since", {}).get(k, obs.now) for k in seen}
    confirmed = {k: v for k, v in seen.items() if not _needs_confirmation(k) or obs.now - since[k] >= CONFIRM_AFTER_S}
    outgoing = messages(state.get("alerted", {}), confirmed)
    today = datetime.fromtimestamp(obs.now, UTC)
    heartbeat_due = state.get("heartbeat_day") != today.date().isoformat() and today.hour >= HEARTBEAT_HOUR_UTC
    if heartbeat_due or test_message:
        outgoing.append(summary(obs, confirmed))
    for text in outgoing:
        notifier.send(text)
    state = {
        "since": since,
        "alerted": confirmed,
        "drift_observed_at": obs.drift_observed_at,
        "heartbeat_day": today.date().isoformat() if heartbeat_due else state.get("heartbeat_day"),
        "checked_at": obs.now,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)
    unconfirmed = sorted(seen.keys() - confirmed.keys())
    return {"problems": confirmed, "unconfirmed": unconfirmed, "sent": len(outgoing), "telegram": notifier.configured}


def _short(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:300]
