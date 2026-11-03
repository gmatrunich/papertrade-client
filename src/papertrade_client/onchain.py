"""Read-only HyperEVM access: proxy implementations and the relayer's batches.

The relayer submits every signed intent through the BatchExecutor, so recent
batches are a free, live test vector for our EIP-712 encoding: for each call we
re-derive the digest from the decoded arguments, recover the signer and check
that our intent id equals the one the relayer put next to the call.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
from eth_abi import decode as abi_decode
from eth_utils import keccak

from . import deployment, protocol

_SIG_TAIL = ["uint8", "bytes32", "bytes32"]
_BATCH_TUPLE = "(address,bytes,uint256,uint256,bytes32)[]"

# Which contract each intent type is executed on.
# Where the relayer sends each intent. Registration goes to the SessionKeyManager but
# is signed under the exchange domain.
_TARGETS: dict[str, str] = {
    name: (deployment.PAPER_STAKING if domain is protocol.PAPER_STAKING_DOMAIN else deployment.EXCHANGE)
    for name, domain in protocol.DOMAIN_FOR.items()
    if name != "CancelIntent"
} | {"RegisterSessionKey": deployment.SESSION_KEY_MANAGER}


class Rpc:
    def __init__(self, url: str = deployment.HYPEREVM_RPC_URL, client: httpx.Client | None = None):
        self.url = url
        self.client = client or httpx.Client(timeout=15)

    def call(self, method: str, params: list[Any], retries: int = 5) -> Any:
        # The public endpoint rate-limits bursts (HTTP 429 or JSON-RPC -32005): back off and retry.
        for attempt in range(retries + 1):
            response = self.client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            limited = response.status_code == 429
            if not limited:
                response.raise_for_status()
                body = response.json()
                limited = "error" in body and body["error"].get("code") == -32005
                if not limited:
                    if "error" in body:
                        raise RuntimeError(f"{method}: {body['error']}")
                    return body["result"]
            if attempt < retries:
                time.sleep(min(2**attempt, 20))
        raise RuntimeError(f"{method}: rate limited after {retries} retries")

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def implementation_of(self, proxy: str) -> str:
        word = self.call("eth_getStorageAt", [proxy, deployment.EIP1967_IMPLEMENTATION_SLOT, "latest"])
        return "0x" + word[-40:].lower()

    def compute_deposit_proxy(self, user: str) -> str:
        """The factory's CREATE2 derivation of a user's deposit address, to cross-check the relayer's."""
        data = "0x" + keccak(text="computeProxyAddress(address)")[:4].hex() + user.lower().removeprefix("0x").rjust(64, "0")
        word = self.call("eth_call", [{"to": deployment.DEPOSIT_PROXY_FACTORY, "data": data}, "latest"])
        return "0x" + word[-40:].lower()

    def iter_batches(self, max_blocks: int) -> Iterator[dict[str, Any]]:
        head = self.block_number()
        for number in range(head, max(head - max_blocks, 0), -1):
            block = self.call("eth_getBlockByNumber", [hex(number), True])
            for tx in block["transactions"]:
                if (tx.get("to") or "").lower() == deployment.BATCH_EXECUTOR:
                    yield tx


@dataclass(frozen=True)
class BatchCall:
    target: str
    data: bytes
    intent_id: str

    @property
    def selector(self) -> str:
        return self.data[:4].hex()


@dataclass(frozen=True)
class Verified:
    call: BatchCall
    primary_type: str
    message: dict[str, Any]
    signer: str
    computed_id: str


def decode_batch(tx_input: str) -> list[BatchCall]:
    raw = bytes.fromhex(tx_input.removeprefix("0x"))
    (calls,) = abi_decode([_BATCH_TUPLE], raw[4:])
    return [BatchCall(target.lower(), bytes(data), "0x" + bytes(intent).hex()) for target, data, _, _, intent in calls]


def _decode_as(primary_type: str, data: bytes) -> tuple[dict[str, Any], tuple[int, int, int]] | None:
    fields = protocol.INTENT_TYPES[primary_type]
    try:
        values = abi_decode([kind for _, kind in fields] + _SIG_TAIL, data[4:], strict=True)
    except Exception:
        return None
    *args, v, r, s = values
    message = {name: (list(value) if isinstance(value, tuple) else value) for (name, _), value in zip(fields, args, strict=True)}
    return message, (v, int.from_bytes(r, "big"), int.from_bytes(s, "big"))


def verify_call(call: BatchCall) -> Verified | None:
    """Find the intent type whose re-derived intent id matches the relayer's, if any."""
    for primary_type, target in _TARGETS.items():
        if call.target != target:
            continue
        decoded = _decode_as(primary_type, call.data)
        if decoded is None:
            continue
        message, (v, r, s) = decoded
        try:
            signer = protocol.recover(primary_type, message, v, r, s)
        except Exception:
            continue
        computed = protocol.intent_id(protocol.digest(primary_type, message), signer)
        if computed == call.intent_id:
            return Verified(call, primary_type, message, signer, computed)
    return None
