"""EIP-712 intents: definitions, signing, wire format and intent ids.

Papertrade's relayer accepts the same signed intents its frontend produces:
``POST /intents`` with ``{"action": ..., "signed": {...fields, v, r, s}}``.
Field order matters for the struct hash, so the lists below are verbatim.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.messages import SignableMessage, encode_typed_data
from eth_account.signers.local import LocalAccount
from eth_utils import keccak, to_checksum_address

from . import deployment

Field = tuple[str, str]

INTENT_TYPES: dict[str, tuple[Field, ...]] = {
    "RegisterSessionKey": (
        ("user", "address"),
        ("key", "address"),
        ("expiry", "uint64"),
        ("feeAddress", "address"),
        ("nonce", "uint256"),
        ("deadline", "uint64"),
    ),
    "OpenPosition": (
        ("user", "address"),
        ("instrumentId", "uint32"),
        ("isLong", "bool"),
        ("size", "uint128"),
        ("leverage", "uint32"),
        ("useDebt", "bool"),
        ("nonce", "uint256"),
        ("deadline", "uint64"),
    ),
    "Close": (
        ("user", "address"),
        ("positionIds", "uint256[]"),
        ("nonce", "uint256"),
        ("deadline", "uint64"),
    ),
    "WithdrawToCore": (
        ("user", "address"),
        ("amount", "uint256"),
        ("nonce", "uint256"),
        ("deadline", "uint64"),
    ),
    "Stake": (("user", "address"), ("amount", "uint256"), ("nonce", "uint256"), ("deadline", "uint64")),
    "Unstake": (("user", "address"), ("amount", "uint256"), ("nonce", "uint256"), ("deadline", "uint64")),
    "Claim": (("user", "address"), ("nonce", "uint256"), ("deadline", "uint64")),
    "Distribute": (("user", "address"), ("nonce", "uint256"), ("deadline", "uint64")),
    "CancelIntent": (("user", "address"), ("intentId", "bytes32"), ("deadline", "uint256")),
}

# The relayer's name for each intent type.
ACTIONS: dict[str, str] = {
    "RegisterSessionKey": "registerSessionKey",
    "OpenPosition": "openPosition",
    "Close": "closePositions",
    "WithdrawToCore": "withdrawToCore",
    "Stake": "paperStake",
    "Unstake": "paperUnstake",
    "Claim": "paperClaim",
    "Distribute": "paperDistribute",
}

INTENT_ID_TAG = "papertrade:intent-id:v2"
_INTENT_ID_TAG_HASH = keccak(text=INTENT_ID_TAG)


@dataclass(frozen=True)
class Domain:
    name: str
    version: str
    chain_id: int
    verifying_contract: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "chainId": self.chain_id,
            "verifyingContract": to_checksum_address(self.verifying_contract),
        }


# The exchange domain name really is "String" in the canonical frontend.
EXCHANGE_DOMAIN = Domain("String", "1", deployment.CHAIN_ID, deployment.EXCHANGE)
PAPER_STAKING_DOMAIN = Domain("PaperStaking", "1", deployment.CHAIN_ID, deployment.PAPER_STAKING)
CANCELLATION_DOMAIN = Domain("PaperTrade Intent Cancel", "2", deployment.CHAIN_ID, deployment.EXCHANGE)

# Which domain (and therefore which key) each intent type is signed under.
DOMAIN_FOR: dict[str, Domain] = {
    "RegisterSessionKey": EXCHANGE_DOMAIN,
    "OpenPosition": EXCHANGE_DOMAIN,
    "Close": EXCHANGE_DOMAIN,
    "WithdrawToCore": EXCHANGE_DOMAIN,
    "Stake": PAPER_STAKING_DOMAIN,
    "Unstake": PAPER_STAKING_DOMAIN,
    "Claim": PAPER_STAKING_DOMAIN,
    "Distribute": PAPER_STAKING_DOMAIN,
    "CancelIntent": CANCELLATION_DOMAIN,
}

_DOMAIN_FIELDS = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]


def _typed_value(kind: str, value: Any) -> Any:
    if kind == "address":
        return to_checksum_address(value)
    if kind == "uint256[]":
        return [int(item) for item in value]
    if kind.startswith("uint"):
        return int(value)
    if kind == "bytes32":
        return bytes.fromhex(str(value).removeprefix("0x"))
    return value


def _wire_value(kind: str, value: Any) -> Any:
    # The relayer wants every integer as a decimal string and hex lower-cased.
    if kind == "address":
        return str(value).lower()
    if kind == "uint256[]":
        return [str(int(item)) for item in value]
    if kind.startswith("uint"):
        return str(int(value))
    if kind == "bytes32":
        return str(value).lower()
    return value


def signable(primary_type: str, message: Mapping[str, Any], domain: Domain | None = None) -> SignableMessage:
    fields = INTENT_TYPES[primary_type]
    missing = [name for name, _ in fields if name not in message]
    if missing:
        raise ValueError(f"{primary_type} is missing {', '.join(missing)}")
    return encode_typed_data(
        full_message={
            "types": {
                "EIP712Domain": _DOMAIN_FIELDS,
                primary_type: [{"name": name, "type": kind} for name, kind in fields],
            },
            "primaryType": primary_type,
            "domain": (domain or DOMAIN_FOR[primary_type]).as_dict(),
            "message": {name: _typed_value(kind, message[name]) for name, kind in fields},
        }
    )


def digest(primary_type: str, message: Mapping[str, Any], domain: Domain | None = None) -> bytes:
    msg = signable(primary_type, message, domain)
    return keccak(b"\x19" + msg.version + msg.header + msg.body)


def intent_id(digest_bytes: bytes, signer: str) -> str:
    """keccak256(keccak256(tag) ‖ digest ‖ signer), as the relayer derives it."""
    return "0x" + keccak(_INTENT_ID_TAG_HASH + digest_bytes + bytes.fromhex(signer.lower().removeprefix("0x"))).hex()


@dataclass(frozen=True)
class SignedIntent:
    primary_type: str
    action: str | None
    wire: dict[str, Any]  # what goes into "signed", v/r/s included
    digest: bytes
    signer: str
    intent_id: str

    def unsigned(self) -> dict[str, Any]:
        """The wire message without v/r/s, safe to log or show."""
        return {k: v for k, v in self.wire.items() if k not in ("v", "r", "s")}


def sign(signer: LocalAccount, primary_type: str, message: Mapping[str, Any], domain: Domain | None = None) -> SignedIntent:
    msg = signable(primary_type, message, domain)
    sig = signer.sign_message(msg)
    recovered = Account.recover_message(msg, vrs=(sig.v, sig.r, sig.s)).lower()
    if recovered != signer.address.lower():
        raise RuntimeError("recovered signer does not match the signing key")
    wire = {name: _wire_value(kind, message[name]) for name, kind in INTENT_TYPES[primary_type]}
    wire |= {"v": str(sig.v), "r": "0x" + sig.r.to_bytes(32, "big").hex(), "s": "0x" + sig.s.to_bytes(32, "big").hex()}
    d = keccak(b"\x19" + msg.version + msg.header + msg.body)
    return SignedIntent(primary_type, ACTIONS.get(primary_type), wire, d, recovered, intent_id(d, recovered))


def recover(primary_type: str, message: Mapping[str, Any], v: int, r: int, s: int, domain: Domain | None = None) -> str:
    return Account.recover_message(signable(primary_type, message, domain), vrs=(v, r, s)).lower()


def preview(primary_type: str, message: Mapping[str, Any], domain: Domain | None = None) -> SignedIntent:
    """Sign with a throwaway key and a random nonce: nothing replayable on a real account."""
    throwaway = Account.create()
    fields = dict(message)
    if "nonce" in fields:
        fields["nonce"] = random_nonce()
    return sign(throwaway, primary_type, fields, domain)


def random_nonce() -> int:
    # Nonces are client-chosen; the frontend starts at a random 24-bit value × 256.
    return (secrets.randbits(24) or 1) * 256


def deadline(now_s: float, lifetime_s: int = deployment.INTENT_LIFETIME_S) -> int:
    return int(now_s) + lifetime_s
