from __future__ import annotations

import json
from types import SimpleNamespace

from papertrade_client import cli
from papertrade_client.exits import ExitPlan
from papertrade_client.trader import PairBroken, WriteResult


def fake_client(trading, tmp_path, live=False, beat=None):
    calls = []
    if beat is not None:
        (tmp_path / "exits.json").write_text(json.dumps(beat))

    def open_pair(market, margin, leverage, dry_run=None, exit_plan=None):
        calls.append((market, margin, leverage, dry_run, exit_plan))
        return WriteResult("dry-run" if dry_run else "live", "openPair", {})

    client = SimpleNamespace(
        store=SimpleNamespace(dir=tmp_path),
        config=SimpleNamespace(live_trading=live),
        api=SimpleNamespace(trading_state=lambda: trading),
        trader=SimpleNamespace(open_pair=open_pair),
        stream=SimpleNamespace(state=lambda: None),
    )
    return client, calls


def args(*argv):
    return cli.build_parser().parse_args(["probe-pair", *argv])


def test_probe_pair_parses_like_probe():
    a = args("--hold", "10m", "--market", "ETH")
    assert (a.hold, a.market, a.live, a.fn) == (600, "ETH", False, cli.cmd_probe_pair)
    assert args().hold == 300


def test_probe_pair_dry_run_uses_the_minimum_trade_and_a_holding_limit(trading, tmp_path, monkeypatch):
    client, calls = fake_client(trading, tmp_path)
    monkeypatch.setattr(cli, "_client", lambda *a, **k: client)
    assert cli.cmd_probe_pair(args("--hold", "2m")) == 0
    assert calls == [("BTC", "10", 50, True, ExitPlan(max_hold_s=120))]


def test_live_probe_pair_needs_the_exit_engine(trading, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.time, "time", lambda: 1000.0)
    for beat in (None, {"mode": "dry-run", "at": 990.0}, {"mode": "live", "at": 900.0}):
        client, calls = fake_client(trading, tmp_path, live=True, beat=beat)
        monkeypatch.setattr(cli, "_client", lambda *a, _client=client, **k: _client)
        assert cli.cmd_probe_pair(args("--live")) == 1 and calls == []
        assert "exit engine is not running live" in capsys.readouterr().err
        (tmp_path / "exits.json").unlink(missing_ok=True)
    client, calls = fake_client(trading, tmp_path, live=True, beat={"mode": "live", "at": 990.0})
    monkeypatch.setattr(cli, "_client", lambda *a, **k: client)
    assert cli.cmd_probe_pair(args("--live")) == 0 and calls[0][3] is False  # dry_run=False: it goes live


def test_a_broken_pair_exits_with_the_result_printed(monkeypatch, capsys):
    result = WriteResult("live", "openPair", {"pair": "p1"})

    def broken(_):
        raise PairBroken("short leg refused (nope); the other leg closes as soon as it shows", result)

    monkeypatch.setattr(cli, "cmd_probe_pair", broken)
    assert cli.main(["probe-pair"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert "short leg refused" in printed["pair_broken"] and printed["sent"]["action"] == "openPair"
