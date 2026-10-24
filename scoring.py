"""How predictions are judged against labels. Shared by scripts/eval.py and the tests.

Rules:
- is_deal_request is scored on every post: correct, wrong, or error.
- The six key fields and missing_fields are scored ONLY on posts whose label is a deal request.
  Non-deal posts get no free credit for empty fields. Instead we count every field the extractor
  filled in on them, as hallucinations.
- Each key-field prediction gets one outcome:
    correct          both filled, and they match
    wrong            both filled, but they differ
    missed           label filled, prediction empty
    correct_missing  both empty: the extractor correctly said "the post doesn't say"
    hallucinated     label empty, prediction filled (a guess)
    error            the extractor failed on this post (refusal, no tool call, invalid output)
  Accuracy = (correct + correct_missing) / posts scored. Errored posts stay in the denominator.
- missing_fields is scored as exact set equality (correct / wrong / error).
- notes is free text and is not scored.
"""
import json
from pathlib import Path

from schema import KEY_FIELDS

DEAL_VALUE_TOLERANCE = 0.01  # deal_value_usd matches if within 1% of the label
FIELD_OUTCOMES = ("correct", "wrong", "missed", "correct_missing", "hallucinated", "error")
BINARY_OUTCOMES = ("correct", "wrong", "error")


def load_posts(path: str | Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def normalize(field: str, value):
    if field == "state":
        return value.strip().upper()
    if field == "city":  # "St. Louis," == "st  louis"; no spelling tolerance
        return " ".join(value.replace(".", "").replace(",", " ").lower().split())
    return value


def values_match(field: str, gold, pred) -> bool:
    if field == "deal_value_usd":
        return abs(pred - gold) <= DEAL_VALUE_TOLERANCE * gold
    return normalize(field, gold) == normalize(field, pred)


def field_outcome(field: str, gold, pred) -> str:
    if gold is None:
        return "correct_missing" if pred is None else "hallucinated"
    if pred is None:
        return "missed"
    return "correct" if values_match(field, gold, pred) else "wrong"


def expected_missing(label: dict) -> set[str]:
    return {f for f in KEY_FIELDS if label[f] is None}


def _with_accuracy(counts: dict, n: int, good=("correct", "correct_missing")) -> dict:
    return {**counts, "accuracy": sum(counts.get(k, 0) for k in good) / n if n else None}


def score(posts: list[dict], records: dict[str, dict]) -> dict:
    """records maps post id -> {"prediction": dict | None, "error": str | None}."""
    is_deal = dict.fromkeys(BINARY_OUTCOMES, 0)
    fields = {f: dict.fromkeys(FIELD_OUTCOMES, 0) for f in KEY_FIELDS}
    missing = dict.fromkeys(BINARY_OUTCOMES, 0)
    non_deal_filled = dict.fromkeys(KEY_FIELDS, 0)
    non_deal_posts_with_any = 0
    failures = []

    def fail(post, field, outcome, expected=None, got=None):
        failures.append({"id": post["id"], "field": field, "outcome": outcome,
                         "expected": expected, "got": got, "text": post["text"]})

    for p in posts:
        gold, rec = p["label"], records[p["id"]]
        pred, err = rec["prediction"], rec["error"]
        if err:
            is_deal["error"] += 1
            fail(p, "(all)", "error", got=err)
            if gold["is_deal_request"]:
                for f in KEY_FIELDS:
                    fields[f]["error"] += 1
                missing["error"] += 1
            continue

        o = "correct" if pred["is_deal_request"] == gold["is_deal_request"] else "wrong"
        is_deal[o] += 1
        if o == "wrong":
            fail(p, "is_deal_request", o, gold["is_deal_request"], pred["is_deal_request"])

        if gold["is_deal_request"]:
            for f in KEY_FIELDS:
                o = field_outcome(f, gold[f], pred[f])
                fields[f][o] += 1
                if o not in ("correct", "correct_missing"):
                    fail(p, f, o, gold[f], pred[f])
            exp, got = expected_missing(gold), set(pred["missing_fields"])
            o = "correct" if exp == got else "wrong"
            missing[o] += 1
            if o == "wrong":
                fail(p, "missing_fields", o, sorted(exp), sorted(got))
        else:
            filled = [f for f in KEY_FIELDS if pred[f] is not None]
            for f in filled:
                non_deal_filled[f] += 1
                fail(p, f, "hallucinated_on_non_deal", None, pred[f])
            non_deal_posts_with_any += bool(filled)

    n_deal = sum(1 for p in posts if p["label"]["is_deal_request"])
    return {
        "n_posts": len(posts),
        "n_deal_posts": n_deal,
        "n_non_deal_posts": len(posts) - n_deal,
        "is_deal_request": _with_accuracy(is_deal, len(posts)),
        "fields": {f: _with_accuracy(c, n_deal) for f, c in fields.items()},
        "missing_fields": _with_accuracy(missing, n_deal),
        "non_deal_hallucinations": {
            "fields_filled": sum(non_deal_filled.values()),
            "posts_with_any": non_deal_posts_with_any,
            "by_field": non_deal_filled,
        },
        "failures": failures,
    }
