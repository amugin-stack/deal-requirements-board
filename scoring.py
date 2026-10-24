"""How one predicted field is judged against its label. Shared by every eval script.

Each (field, post) pair gets exactly one outcome:
  correct         both filled, and they match
  correct_missing both empty: the extractor correctly said "the post doesn't say"
  hallucinated    label is empty but the extractor filled it (a guess)
  missed          label is filled but the extractor left it empty
  wrong           both filled, but they differ
Accuracy counts correct + correct_missing.
"""
import json
from pathlib import Path

from schema import KEY_FIELDS

SCORED_FIELDS = ("is_deal_request", *KEY_FIELDS)
OUTCOMES = ("correct", "correct_missing", "hallucinated", "missed", "wrong")


def load_posts(path: str | Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def values_match(field: str, gold, pred) -> bool:
    if field == "city":
        return " ".join(gold.lower().split()) == " ".join(pred.lower().split())
    if field == "deal_value_usd":
        return abs(pred - gold) <= 0.01 * gold
    return gold == pred


def outcome(field: str, gold, pred) -> str:
    if gold is None:
        return "correct_missing" if pred is None else "hallucinated"
    if pred is None:
        return "missed"
    return "correct" if values_match(field, gold, pred) else "wrong"


def score(posts: list[dict], predictions: dict[str, dict | None]) -> dict:
    """predictions maps post id -> predicted label dict, or None if the extractor errored.

    An errored post counts as every field empty, so errors show up as misses, not as skipped posts.
    """
    counts = {f: dict.fromkeys(OUTCOMES, 0) for f in SCORED_FIELDS}
    failures = []
    for p in posts:
        pred = predictions[p["id"]] or {}
        for f in SCORED_FIELDS:
            o = outcome(f, p["label"][f], pred.get(f))
            counts[f][o] += 1
            if o not in ("correct", "correct_missing"):
                failures.append({"id": p["id"], "field": f, "outcome": o,
                                 "expected": p["label"][f], "got": pred.get(f), "text": p["text"]})
    n = len(posts)
    accuracy = {f: (c["correct"] + c["correct_missing"]) / n for f, c in counts.items()}
    return {"n_posts": n, "accuracy": accuracy, "counts": counts, "failures": failures}
