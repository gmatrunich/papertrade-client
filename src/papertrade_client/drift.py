"""Daily protocol drift monitor.

Papertrade's relayer protocol is undocumented and changes with frontend and
contract deploys, so this compares what is live against ``pinned.json``:

- frontend bundle: EIP-712 types, domains, intent-id tag, deployment addresses, payout curves
- HyperEVM: implementation behind every proxy (contract upgrades)
- live conformance: recent relayer batches re-verified with our own encoding
- ``/state/trading`` and the account stream: frame shape and protocol parameters
- docs: the latest contract set on the "Protocol changes" page

Levels: ``breaking`` (stop trading until the client is updated), ``warn`` (review
needed), ``info`` (expected churn such as the build id), ``error`` (check could not run).
A breaking finding outranks an error elsewhere, so it is never filed as "could not observe".
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import deployment, onchain, protocol, wire
from .api import USER_AGENT
from .stream import iter_sse

PINNED_PATH = Path(__file__).with_name("pinned.json")
LEVELS = ("ok", "info", "warn", "error", "breaking")
_MAX_CELL = 300
_MAX_DETAIL = 4000

_ASSET = re.compile(r"(?:assets/|\./)([A-Za-z0-9_\-.]+\.js)")
_TYPE_BLOCK = re.compile(r"(\w+):\[((?:\{name:`\w+`,type:`[\w\[\]]+`\},?)+)\]")
_FIELD = re.compile(r"\{name:`(\w+)`,type:`([\w\[\]]+)`\}")
_DOMAIN = re.compile(r"(\w+):\{name:`([^`]*)`,version:`([^`]*)`\}")
_INTENT_TAG = re.compile(r"`(papertrade:intent-id:v\d+)`")
_ADDRESS_KEY = re.compile(r"(\w+):`(0x[0-9a-fA-F]{40})`")
_CURVE = re.compile(
    r"(profitCarveOut|profitOffHours):\{startPpm:`(\d+)`,endPpm:`(\d+)`,holdbackWad:`(\d+)`(?:,cubicFall:(!0|!1|true|false))?\}"
)
_BUILD_VALUE = re.compile(r"`([0-9a-f]{8,64})`")


@dataclass
class Finding:
    check: str
    level: str
    message: str
    detail: Any = None


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    observed: dict[str, Any] = field(default_factory=dict)

    def add(self, check: str, level: str, message: str, detail: Any = None) -> None:
        self.findings.append(Finding(check, level, message, detail))

    @property
    def level(self) -> str:
        return max((f.level for f in self.findings), key=LEVELS.index, default="ok")

    def exit_code(self) -> int:
        return {"ok": 0, "info": 0, "warn": 1, "breaking": 2, "error": 3}[self.level]

    def markdown(self) -> str:
        """Rendered into a public GitHub issue, so text that came from the server is neutralised."""
        icon = {"ok": "✅", "info": "ℹ️", "warn": "⚠️", "breaking": "🛑", "error": "❌"}
        lines = [f"## Papertrade protocol drift: **{self.level}**", "", "| | Check | Finding |", "| --- | --- | --- |"]
        for f in sorted(self.findings, key=lambda f: -LEVELS.index(f.level)):
            lines.append(f"| {icon[f.level]} | `{_md_text(f.check)}` | {_md_text(f.message)} |")
        details = [f for f in self.findings if f.detail is not None and f.level in ("warn", "breaking", "error")]
        for f in details:
            body = json.dumps(f.detail, indent=2, default=str)
            if len(body) > _MAX_DETAIL:
                body = body[:_MAX_DETAIL] + "\n… (truncated, see the JSON artifact)"
            fence = "`" * max(3, _longest_run(body, "`") + 1)
            lines += ["", f"<details><summary>{_md_text(f.check)}</summary>", "", fence + "json", body, fence, "</details>"]
        return "\n".join(lines) + "\n"

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "findings": [asdict(f) for f in self.findings], "observed": self.observed}


def _longest_run(text: str, char: str) -> int:
    return max((len(m) for m in re.findall(f"{re.escape(char)}+", text)), default=0)


def _md_text(text: str) -> str:
    """One table cell: no newlines, pipes, HTML, backticks or @mentions."""
    text = " ".join(str(text).split())[:_MAX_CELL]
    for char, safe in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ("|", "\\|"), ("`", "'"), ("@", "@\u200b")):
        text = text.replace(char, safe)
    return text


# -- bundle parsing ---------------------------------------------------------------------


def parse_typed_data(source: str) -> dict[str, list[list[str]]]:
    return {name: [list(f) for f in _FIELD.findall(body)] for name, body in _TYPE_BLOCK.findall(source)}


def parse_domains(source: str) -> dict[str, list[str]]:
    return {key: [name, version] for key, name, version in _DOMAIN.findall(source)}


def parse_deployment(source: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    chain = re.search(r"chainId:(\d+)", source)
    out["chainId"] = int(chain.group(1)) if chain else None
    addresses: dict[str, str] = {}
    for key, value in _ADDRESS_KEY.findall(source):
        addresses.setdefault(key, value.lower())
    out["addresses"] = {
        k: addresses.get(k)
        for k in ("exchange", "exchangeImplementation", "paper", "paperStaking", "depositProxyFactory", "multisig", "timelock")
    }
    nxt = re.search(r"exchangeNext:\{implementation:`(0x[0-9a-fA-F]{40})`", source)
    out["addresses"]["exchangeNextImplementation"] = nxt.group(1).lower() if nxt else None
    out["curves"] = {
        name: {"startPpm": int(a), "endPpm": int(b), "holdbackWad": b2, "cubicFall": c in ("!0", "true")}
        for name, a, b, b2, c in _CURVE.findall(source)
    }
    divisor = re.search(r"profitThresholdDivisor:`(\d+)`", source)
    out["profitThresholdDivisor"] = int(divisor.group(1)) if divisor else None
    return out


def crawl_bundle(client: httpx.Client, origin: str, max_chunks: int = 400) -> dict[str, str]:
    """Fetch frontend chunks breadth-first until the typed-data and deployment chunks are found."""
    html = client.get(f"{origin}/", headers={"Accept": "text/html"}).raise_for_status().text
    queue = list(dict.fromkeys(_ASSET.findall(html)))
    # Likely chunks first; names are content-hashed but keep their module prefix.
    queue.sort(key=lambda n: 0 if re.match(r"(typed-data|deployment|build)-", n) else 1)
    found: dict[str, str] = {}
    seen: set[str] = set()
    while queue and len(seen) < max_chunks:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        source = client.get(f"{origin}/assets/{name}").raise_for_status().text
        if name.startswith("build-"):
            found["build"] = source
        if "RegisterSessionKey:[" in source and "intent-id" in source:
            found["typed_data"] = source
        if "depositProxyFactory:`0x" in source and "chainId:" in source:
            found["deployment"] = source
        if {"build", "typed_data", "deployment"} <= found.keys():
            break
        queue.extend(n for n in _ASSET.findall(source) if n not in seen)
    found["_chunks_scanned"] = str(len(seen))
    return found


# -- observation ----------------------------------------------------------------------------


def observe(
    client: httpx.Client | None = None, rpc: onchain.Rpc | None = None, batch_blocks: int = 300
) -> tuple[dict[str, Any], list[Finding]]:
    """Collect the live facts. Each section that fails is reported as an ``error`` finding."""
    client = client or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})
    rpc = rpc or onchain.Rpc(client=client)
    origin = deployment.EXCHANGE_ORIGIN
    observed: dict[str, Any] = {}
    errors: list[Finding] = []

    def section(name: str, fn: Callable[[], Any]) -> None:
        try:
            observed[name] = fn()
        except Exception as error:  # one broken source must not hide the others
            errors.append(Finding(name, "error", f"could not observe: {error!r}"))

    def bundle() -> dict[str, Any]:
        chunks = crawl_bundle(client, origin)
        missing = {"build", "typed_data", "deployment"} - chunks.keys()
        if missing:
            raise RuntimeError(f"bundle chunks not found: {sorted(missing)} after {chunks['_chunks_scanned']} chunks")
        build = _BUILD_VALUE.search(chunks["build"])
        tag = _INTENT_TAG.search(chunks["typed_data"])
        return {
            "build_id": build.group(1) if build else None,
            "typed_data": parse_typed_data(chunks["typed_data"]),
            "domains": parse_domains(chunks["typed_data"]),
            "intent_id_tag": tag.group(1) if tag else None,
            "deployment": parse_deployment(chunks["deployment"]),
        }

    def implementations() -> dict[str, str]:
        proxies = {
            "exchange": deployment.EXCHANGE,
            "batchExecutor": deployment.BATCH_EXECUTOR,
            "sessionKeyManager": deployment.SESSION_KEY_MANAGER,
            "depositProxyFactory": deployment.DEPOSIT_PROXY_FACTORY,
            "paperToken": deployment.PAPER_TOKEN,
            "paperStaking": deployment.PAPER_STAKING,
        }
        return {name: rpc.implementation_of(address) for name, address in proxies.items()}

    def trading() -> dict[str, Any]:
        frame = client.get(f"{origin}/state/trading").raise_for_status().json()
        state = wire.parse_trading_state(frame)  # raises WireError on a shape change
        return {
            "keys": sorted(frame),
            "exchange": state.exchange,
            "off_hours": state.off_hours,
            "params": {
                "minimumTradeSizeRaw": str(state.minimum_trade_size_raw),
                "minimumOpenNotionalRaw": str(state.minimum_open_notional_raw),
                "winFeeRateRaw": str(state.win_fee_rate_raw),
                "maximumLeverage": state.maximum_leverage,
                "minimumLeverage": state.minimum_leverage,
                "accountOiCapRaw": str(state.account_oi_cap_raw),
                "instruments": {
                    i.symbol: {
                        "id": i.id,
                        "assetIndex": i.asset_index,
                        "priceScale": i.price_scale,
                        "openable": i.openable,
                        "maxLeverage": i.max_leverage,
                        "maxPositionNotionalRaw": str(i.max_position_notional_raw),
                        "bustBufferRaw": str(i.bust_buffer_raw),
                        "baseRateRaw": str(i.base_rate_raw),
                        "rateMultiplierRaw": str(i.rate_multiplier_raw),
                        "positionMultiplierRaw": str(i.position_multiplier_raw),
                        "referenceNotionalRaw": str(i.reference_notional_raw),
                    }
                    for i in state.instruments
                },
            },
        }

    def account_stream() -> dict[str, Any]:
        wallet = "0x" + secrets.token_hex(20)  # an empty account: shape only
        with client.stream("GET", f"{origin}/state/user/live?wallet={wallet}", headers={"Accept": "text/event-stream"}, timeout=15) as r:
            r.raise_for_status()
            events = []
            for event, data in iter_sse(r.iter_lines()):
                events.append(event)
                if event == "wallet":
                    frame = json.loads(data)
                    wire.parse_account_snapshot(frame)
                    return {"keys": sorted(frame), "events_before_snapshot": events}
        raise RuntimeError("stream closed before a wallet snapshot")

    def conformance() -> dict[str, Any]:
        verified: dict[str, int] = {}
        unmatched = []
        batches = 0
        for tx in rpc.iter_batches(batch_blocks):
            batches += 1
            for call in onchain.decode_batch(tx["input"]):
                result = onchain.verify_call(call)
                if result:
                    verified[result.primary_type] = verified.get(result.primary_type, 0) + 1
                else:
                    unmatched.append({"tx": tx["hash"], "target": call.target, "selector": call.selector})
        return {
            "blocks": batch_blocks,
            "batches": batches,
            "verified": verified,
            "unmatched": unmatched[:20],
            "unmatched_count": len(unmatched),
        }

    def docs() -> dict[str, Any]:
        html = client.get(f"{deployment.DOCS_ORIGIN}/dev/protocol-changes/").raise_for_status().text
        sets = [int(n) for n in re.findall(r"Contract set (\d+)", html)]
        updated = re.findall(r"Last updated:(?:\s|<[^>]+>)*(\d{4}-\d{2}-\d{2})", html)
        return {"latest_contract_set": max(sets) if sets else None, "last_updated": max(updated) if updated else None}

    for name, fn in (
        ("bundle", bundle),
        ("implementations", implementations),
        ("trading_state", trading),
        ("account_stream", account_stream),
        ("conformance", conformance),
        ("docs", docs),
    ):
        section(name, fn)
    return observed, errors


# -- comparison ---------------------------------------------------------------------------------


def _diff(pinned: Any, live: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(pinned, dict) and isinstance(live, dict):
        out = []
        for key in sorted(set(pinned) | set(live)):
            out += _diff(pinned.get(key, "<absent>"), live.get(key, "<absent>"), f"{path}.{key}" if path else key)
        return out
    return [] if pinned == live else [{"path": path, "pinned": pinned, "live": live}]


def compare(pinned: dict[str, Any], observed: dict[str, Any], errors: list[Finding]) -> Report:
    report = Report(list(errors), observed)
    b, pb = observed.get("bundle"), pinned["bundle"]
    if b:
        if b["build_id"] != pb["build_id"]:
            report.add("bundle.build_id", "info", f"frontend redeployed: build {pb['build_id']} → {b['build_id']}")
        ours = {name: [list(f) for f in fields] for name, fields in protocol.INTENT_TYPES.items()}
        for name in sorted(set(ours) | set(b["typed_data"])):
            if name not in b["typed_data"]:
                report.add("bundle.typed_data", "breaking", f"`{name}` is gone from the frontend's typed data")
            elif name not in ours:
                report.add("bundle.typed_data", "warn", f"new intent type `{name}` in the frontend", b["typed_data"][name])
            elif ours[name] != b["typed_data"][name]:
                report.add("bundle.typed_data", "breaking", f"`{name}` fields changed", {"ours": ours[name], "live": b["typed_data"][name]})
        for diff in _diff(pb["domains"], b["domains"]):
            report.add("bundle.domains", "breaking", f"EIP-712 domain `{diff['path']}` changed", diff)
        if b["intent_id_tag"] != protocol.INTENT_ID_TAG:
            report.add("bundle.intent_id_tag", "breaking", f"intent-id tag is now {b['intent_id_tag']!r}")
        for diff in _diff(pb["deployment"], b["deployment"]):
            critical = (
                diff["path"].split(".")[-1] in ("exchange", "paper", "paperStaking", "depositProxyFactory") or diff["path"] == "chainId"
            )
            report.add("bundle.deployment", "breaking" if critical else "warn", f"deployment `{diff['path']}` changed", diff)
    impl = observed.get("implementations")
    if impl:
        for diff in _diff(pinned["implementations"], impl):
            report.add(
                "onchain.implementations", "breaking", f"proxy `{diff['path']}` was upgraded: {diff['pinned']} → {diff['live']}", diff
            )
    t = observed.get("trading_state")
    if t:
        required = lambda keys: [k for k in keys if k not in wire.OPTIONAL_TRADING_STATE_KEYS]  # noqa: E731
        for diff in _diff(
            {"keys": required(pinned["trading_state"]["keys"]), "exchange": pinned["trading_state"]["exchange"]},
            {"keys": required(t["keys"]), "exchange": t["exchange"]},
        ):
            report.add("trading_state.shape", "warn", f"`/state/trading` {diff['path']} changed", diff)
        for diff in _diff(pinned["trading_state"]["params"], t["params"]):
            report.add("trading_state.params", "warn", f"parameter `{diff['path']}`: {diff['pinned']} → {diff['live']}", diff)
        report.add("trading_state.off_hours", "info", f"payout curve in force: {'off-hours' if t['off_hours'] else 'standard'}")
    s = observed.get("account_stream")
    if s and s["keys"] != pinned["account_stream"]["keys"]:
        report.add(
            "account_stream.shape",
            "warn",
            "account snapshot keys changed",
            _diff({"keys": pinned["account_stream"]["keys"]}, {"keys": s["keys"]}),
        )
    c = observed.get("conformance")
    if c:
        if c["unmatched_count"]:
            report.add(
                "conformance",
                "breaking",
                f"{c['unmatched_count']} relayer calls in the last {c['blocks']} blocks do not verify with our encoding",
                c["unmatched"],
            )
        elif not c["verified"].get("OpenPosition") and not c["verified"].get("Close"):
            report.add("conformance", "warn", f"no opens or closes seen in the last {c['blocks']} blocks ({c['batches']} batches)")
        else:
            report.add(
                "conformance",
                "ok",
                f"{sum(c['verified'].values())} live calls verified: " + ", ".join(f"{k} {v}" for k, v in sorted(c["verified"].items())),
            )
    d = observed.get("docs")
    if d:
        for diff in _diff(pinned["docs"], d):
            level = "breaking" if diff["path"] == "latest_contract_set" else "warn"
            report.add("docs", level, f"docs `{diff['path']}`: {diff['pinned']} → {diff['live']}", diff)
    if not any(f.level != "ok" for f in report.findings if f.check != "trading_state.off_hours"):
        report.add("summary", "ok", "live protocol matches the pinned one")
    return report


def load_pinned(path: Path = PINNED_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def pin(observed: dict[str, Any], path: Path = PINNED_PATH) -> dict[str, Any]:
    """Write the observed protocol as the new baseline (after the client was updated for it)."""
    pinned = {
        "bundle": {k: observed["bundle"][k] for k in ("build_id", "domains", "deployment")},
        "implementations": observed["implementations"],
        "trading_state": {k: observed["trading_state"][k] for k in ("keys", "exchange", "params")},
        "account_stream": {"keys": observed["account_stream"]["keys"]},
        "docs": observed["docs"],
    }
    path.write_text(json.dumps(pinned, indent=2, sort_keys=True) + "\n")
    return pinned


def run(pinned_path: Path = PINNED_PATH, batch_blocks: int = 300) -> Report:
    observed, errors = observe(batch_blocks=batch_blocks)
    return compare(load_pinned(pinned_path), observed, errors)
