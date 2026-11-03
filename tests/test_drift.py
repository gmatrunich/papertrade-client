from __future__ import annotations

import copy

from papertrade_client import drift, protocol
from papertrade_client.stream import AccountStream, iter_sse

from .conftest import account_frame

TYPED_DATA_CHUNK = (
    "var e={"
    + ",".join(
        f"{name}:[" + ",".join(f"{{name:`{f}`,type:`{t}`}}" for f, t in fields) + "]" for name, fields in protocol.INTENT_TYPES.items()
    )
    + "},t={exchange:{name:`String`,version:`1`},paperStaking:{name:`PaperStaking`,version:`1`},"
    "cancellation:{name:`PaperTrade Intent Cancel`,version:`2`}},n=`papertrade:intent-id:v2`;"
)


def observed_from_pinned(pinned):
    observed = copy.deepcopy(pinned)
    observed["bundle"]["typed_data"] = drift.parse_typed_data(TYPED_DATA_CHUNK)
    observed["bundle"]["intent_id_tag"] = protocol.INTENT_ID_TAG
    observed["trading_state"]["off_hours"] = True
    observed["conformance"] = {
        "blocks": 10,
        "batches": 3,
        "verified": {"OpenPosition": 2, "Close": 1},
        "unmatched": [],
        "unmatched_count": 0,
    }
    return observed


def test_bundle_parsers():
    assert drift.parse_typed_data(TYPED_DATA_CHUNK) == {k: [list(f) for f in v] for k, v in protocol.INTENT_TYPES.items()}
    assert drift.parse_domains(TYPED_DATA_CHUNK)["cancellation"] == ["PaperTrade Intent Cancel", "2"]


def test_unchanged_protocol_is_ok():
    pinned = drift.load_pinned()
    report = drift.compare(pinned, observed_from_pinned(pinned), [])
    assert report.level == "info" and report.exit_code() == 0


def test_build_id_change_is_info_only():
    pinned = drift.load_pinned()
    observed = observed_from_pinned(pinned)
    observed["bundle"]["build_id"] = "deadbeef0000"
    assert drift.compare(pinned, observed, []).exit_code() == 0


def test_typed_data_change_is_breaking():
    pinned = drift.load_pinned()
    observed = observed_from_pinned(pinned)
    observed["bundle"]["typed_data"]["OpenPosition"].insert(3, ["builder", "address"])
    report = drift.compare(pinned, observed, [])
    assert report.level == "breaking" and report.exit_code() == 2
    assert "OpenPosition" in report.markdown()


def test_upgrade_and_unverified_calls_are_breaking():
    pinned = drift.load_pinned()
    observed = observed_from_pinned(pinned)
    observed["implementations"]["exchange"] = "0x" + "99" * 20
    assert drift.compare(pinned, observed, []).level == "breaking"
    observed = observed_from_pinned(pinned)
    observed["conformance"] |= {"unmatched": [{"tx": "0x1", "target": "0x2", "selector": "abcd"}], "unmatched_count": 1}
    assert drift.compare(pinned, observed, []).level == "breaking"


def test_parameter_change_is_a_warning():
    pinned = drift.load_pinned()
    observed = observed_from_pinned(pinned)
    observed["trading_state"]["params"]["minimumOpenNotionalRaw"] = "1000000000000000000000"
    report = drift.compare(pinned, observed, [])
    assert report.level == "warn" and report.exit_code() == 1


def test_unobservable_source_is_an_error():
    pinned = drift.load_pinned()
    observed = observed_from_pinned(pinned)
    del observed["docs"]
    report = drift.compare(pinned, observed, [drift.Finding("docs", "error", "timeout")])
    assert report.exit_code() == 3


def test_breaking_outranks_an_unobservable_source():
    pinned = drift.load_pinned()
    observed = observed_from_pinned(pinned)
    observed["implementations"]["exchange"] = "0x" + "99" * 20
    report = drift.compare(pinned, observed, [drift.Finding("docs", "error", "timeout")])
    assert report.level == "breaking" and report.exit_code() == 2


def test_markdown_neutralises_server_text():
    report = drift.Report()
    report.add("trading_state", "warn", "new key `x` | @everyone <img>", {"k": "```\n@everyone"})
    text = report.markdown()
    table, details = text.split("<details>")
    assert "@everyone" not in table and "<img>" not in table  # mentions inside a code fence do not notify
    assert "````json" in details  # the fence is longer than any backtick run inside


def test_sse_parser_and_stream_reducer():
    frames = list(
        iter_sse(
            [": connected", "", "event: liveness", 'data: {"heartbeatIntervalMs":10000}', "", "event: wallet", "data: {", "data: }", ""]
        )
    )
    assert frames == [("liveness", '{"heartbeatIntervalMs":10000}'), ("wallet", "{\n}")]

    import json

    stream = AccountStream("0x" + "11" * 20)
    stream.handle("liveness", "{}")
    assert stream.state() is None and stream.age_s() is not None
    stream.handle("wallet", json.dumps(account_frame()))
    assert stream.state().balance == 100 * 10**18
