"""Capture signed intents from recent relayer batches as offline test vectors.

uv run python scripts/capture_vectors.py --blocks 3000 --out tests/fixtures/onchain_vectors.json
"""

from __future__ import annotations

import argparse
import json

from papertrade_client import onchain


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blocks", type=int, default=3000)
    parser.add_argument("--per-type", type=int, default=2)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    vectors: dict[str, list[dict]] = {}
    for tx in onchain.Rpc().iter_batches(args.blocks):
        for call in onchain.decode_batch(tx["input"]):
            verified = onchain.verify_call(call)
            if verified is None:
                print("unmatched call", tx["hash"], call.target, call.selector)
                continue
            bucket = vectors.setdefault(verified.primary_type, [])
            if len(bucket) < args.per_type:
                bucket.append(
                    {
                        "tx": tx["hash"],
                        "block": int(tx["blockNumber"], 16),
                        "target": call.target,
                        "data": "0x" + call.data.hex(),
                        "intentId": call.intent_id,
                        "signer": verified.signer,
                    }
                )
                print("captured", verified.primary_type, tx["hash"])
        if "RegisterSessionKey" in vectors and all(len(v) >= args.per_type for v in vectors.values()):
            break
    with open(args.out, "w") as handle:
        json.dump(vectors, handle, indent=2)
    print({name: len(items) for name, items in vectors.items()})


if __name__ == "__main__":
    main()
