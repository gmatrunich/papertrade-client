from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from papertrade_client import watchdog
from papertrade_client.watchdog import Observation

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC).timestamp()


class Recorder:
    configured = True

    def __init__(self, fail=False):
        self.sent: list[str] = []
        self.fail = fail

    def send(self, text):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(text)


def healthy(**overrides) -> Observation:
    obs = Observation(
        now=NOW,
        wallet="0xabc",
        mode="live",
        stream_age_s=2,
        kill_switch={"engaged": False, "reason": None},
        balance_usd=30,
        session_valid_for_s=20 * 86400,
        drift={"level": "ok", "findings": [], "mtime": NOW - 600},
    )
    for key, value in overrides.items():
        setattr(obs, key, value)
    return obs


def position(mark, entry=100.0, bust=98.0, side="long"):
    return {"position_id": 7, "market": "BTC", "side": side, "margin_usd": 10, "leverage": 50, "entry": entry, "bust": bust, "mark": mark}


def test_healthy_has_no_problems():
    assert watchdog.problems(healthy()) == {}


@pytest.mark.parametrize(
    "overrides, key",
    [
        ({"kill_switch": {"engaged": True, "reason": "protocol drift: x"}}, "kill_switch"),
        ({"trading_paused": True}, "trading_paused"),
        ({"paused_actions": ["openPosition"]}, "paused_actions"),
        ({"stream_age_s": 120}, "stream_stale"),
        ({"session_valid_for_s": 3600}, "session_expiring"),
        ({"session_valid_for_s": None}, "session_missing"),
        ({"errors": {"stream": "403"}}, "unobservable:stream"),
        ({"drift": None}, "drift_missing"),
        ({"drift": {"level": "ok", "findings": [], "mtime": NOW - 5 * 3600}}, "drift_stale"),
        ({"drift": {"level": "breaking", "findings": [{"level": "breaking", "message": "typed data"}], "mtime": NOW}}, "drift_breaking"),
        ({"inflight": [{"intent_id": "0x" + "ab" * 32, "action": "openPosition", "submitted_at": NOW - 600}]}, "inflight:0x" + "ab" * 32),
        ({"positions": [position(mark=98.4)]}, "bust:7"),
    ],
)
def test_each_problem(overrides, key):
    assert key in watchdog.problems(healthy(**overrides))


def test_session_missing_only_matters_when_live_and_funded():
    assert watchdog.problems(healthy(session_valid_for_s=None, mode="dry-run")) == {}
    assert watchdog.problems(healthy(session_valid_for_s=None, balance_usd=0)) == {}


def test_recent_inflight_is_fine():
    obs = healthy(inflight=[{"intent_id": "0x01", "action": "openPosition", "submitted_at": NOW - 30}])
    assert watchdog.problems(obs) == {}


@pytest.mark.parametrize(
    "p, left",
    [
        (position(mark=100.0), 1.0),
        (position(mark=99.0), 0.5),
        (position(mark=97.0), 0.0),
        (position(mark=101.0, bust=102.0, side="short"), 0.5),
        (position(mark=103.0, bust=102.0, side="short"), 0.0),
        (position(mark=None), None),
    ],
)
def test_bust_fraction_left(p, left):
    assert watchdog.bust_fraction_left(p) == left


def test_sends_only_changes_and_persists(tmp_path):
    notifier = Recorder()
    watchdog.run(tmp_path, notifier, healthy(kill_switch={"engaged": True, "reason": "manual"}))
    assert [m for m in notifier.sent if m.startswith("🔴")] == ["🔴 kill switch engaged: manual"]

    notifier.sent.clear()
    watchdog.run(tmp_path, notifier, healthy(kill_switch={"engaged": True, "reason": "manual"}))
    assert notifier.sent == []

    watchdog.run(tmp_path, notifier, healthy())
    assert notifier.sent == ["✅ cleared: kill switch engaged: manual"]


def test_upstream_errors_alert_after_five_minutes(tmp_path):
    notifier = Recorder()
    for minute in (0, 2, 4):
        result = watchdog.run(tmp_path, notifier, healthy(now=NOW + minute * 60, errors={"stream": "timeout"}))
        assert result["problems"] == {} and result["unconfirmed"] == ["unobservable:stream"]
    assert not any(m.startswith("🔴") for m in notifier.sent)

    watchdog.run(tmp_path, notifier, healthy(now=NOW + 360, errors={"stream": "timeout"}))
    assert "🔴 stream unavailable: timeout" in notifier.sent

    notifier.sent.clear()
    watchdog.run(tmp_path, notifier, healthy(now=NOW + 480))
    assert notifier.sent == ["✅ cleared: stream unavailable: timeout"]


def test_blip_that_clears_is_never_sent(tmp_path):
    notifier = Recorder()
    for minute in (0, 2, 4):
        watchdog.run(tmp_path, notifier, healthy(now=NOW + minute * 60, errors={"papertrade_api": "502"}))
    watchdog.run(tmp_path, notifier, healthy(now=NOW + 360))
    assert not any("papertrade_api" in m for m in notifier.sent)


def test_drift_error_alerts_only_after_three_blind_hours(tmp_path):
    def drift(level, at):
        return {"level": level, "findings": [{"level": level, "message": "could not observe: ReadTimeout"}], "mtime": at}

    notifier = Recorder()
    watchdog.run(tmp_path, notifier, healthy(drift=drift("ok", NOW)))
    for hour in (1, 2):
        at = NOW + hour * 3600
        result = watchdog.run(tmp_path, notifier, healthy(now=at + 60, drift=drift("error", at)))
        assert result["problems"] == {}
    at = NOW + 3 * 3600
    watchdog.run(tmp_path, notifier, healthy(now=at + 60, drift=drift("error", at)))
    assert any(m.startswith("🔴 drift monitor has not observed the protocol for 3 h") for m in notifier.sent)


def test_daily_heartbeat_once(tmp_path):
    notifier = Recorder()
    watchdog.run(tmp_path, notifier, healthy())
    watchdog.run(tmp_path, notifier, healthy())
    assert len(notifier.sent) == 1 and notifier.sent[0].startswith("papertrade daily")

    early = datetime(2026, 11, 4, 3, 0, tzinfo=UTC).timestamp()
    watchdog.run(tmp_path, notifier, healthy(now=early, drift={"level": "ok", "findings": [], "mtime": early}))
    assert len(notifier.sent) == 1


def test_failed_send_is_retried(tmp_path):
    with pytest.raises(RuntimeError):
        watchdog.run(tmp_path, Recorder(fail=True), healthy(trading_paused=True))
    notifier = Recorder()
    watchdog.run(tmp_path, notifier, healthy(trading_paused=True))
    assert "🔴 Papertrade has paused trading" in notifier.sent


def test_reads_drift_report(tmp_path):
    path = tmp_path / watchdog.DRIFT_REPORT
    path.write_text(json.dumps({"level": "warn", "findings": [{"level": "warn", "message": "new intent type"}], "observed": {}}))
    report = watchdog._read_drift(path)
    assert report["level"] == "warn" and report["findings"][0]["message"] == "new intent type"
    assert watchdog._read_drift(tmp_path / "missing.json") is None
