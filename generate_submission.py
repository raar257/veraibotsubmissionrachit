"""generate_submission.py — builds submission.jsonl for the 30 canonical test pairs
(challenge-brief.md §6/§7) by calling composer.compose_proactive() directly —
no server/network needed, fully deterministic.

Usage: python3 generate_submission.py --dataset expanded_reference --out submission.jsonl
"""
import argparse
import json
from pathlib import Path
import composer


def load_index(root: Path, sub: str, key: str) -> dict:
    idx = {}
    for f in (root / sub).glob("*.json"):
        d = json.load(open(f))
        idx[d[key]] = d
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="expanded_reference")
    ap.add_argument("--out", default="submission.jsonl")
    args = ap.parse_args()

    root = Path(args.dataset)
    categories = load_index(root, "categories", "slug")
    merchants = load_index(root, "merchants", "merchant_id")
    customers = load_index(root, "customers", "customer_id")
    triggers = load_index(root, "triggers", "id")
    pairs = json.load(open(root / "test_pairs.json"))["pairs"]

    lines = []
    for pair in pairs:
        trg = triggers.get(pair["trigger_id"])
        mx = merchants.get(pair["merchant_id"])
        if not trg or not mx:
            continue
        cat = categories.get(mx["category_slug"])
        cx = customers.get(pair["customer_id"]) if pair.get("customer_id") else None
        composed = composer.compose_proactive(cat, mx, trg, cx)
        lines.append({
            "test_id": pair["test_id"],
            "body": composed["body"],
            "cta": composed["cta"],
            "send_as": composed["send_as"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

    with open(args.out, "w") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    print(f"Wrote {len(lines)} lines to {args.out}")


if __name__ == "__main__":
    main()
