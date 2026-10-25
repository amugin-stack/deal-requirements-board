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
- Every rate carries a 95% Wilson interval (ci95), because at 15-24 posts one post moves accuracy
  by 4-7 points. post_correctness + paired_comparison compare two runs post by post (exact McNemar).
"""

import json
import math
from pathlib import Path
from typing import Any

from deal_board.schema import KEY_FIELDS, JSONObject

DEAL_VALUE_TOLERANCE = 0.01  # deal_value_usd matches if within 1% of the label
FIELD_OUTCOMES = ("correct", "wrong", "missed", "correct_missing", "hallucinated", "error")
BINARY_OUTCOMES = ("correct", "wrong", "error")
GOOD_OUTCOMES = ("correct", "correct_missing")
SCORED_FIELDS: tuple[str, ...] = ("is_deal_request", *KEY_FIELDS, "missing_fields")
Z_95: float = 1.96
# Bumped whenever a rule above changes what a score means, and written into every results file, so a
# number can always be traced to the rules that produced it. Version 1 is deal-only field scoring with
# Wilson intervals; results/baseline-v1_comparison.json predates it (key fields scored on all posts).
SCORING_VERSION = 1


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for successes/n.

    Why Wilson and not the normal (Wald) interval: at n = 15 and accuracy near 100% Wald collapses
    to zero width (p = 1 gives [1, 1]) or spills outside [0, 1]. Wilson stays inside [0, 1] and
    keeps an honest width. With n == 0 every rate is plausible, so the interval is the whole range.
    The bounds at 0 and n are pinned exactly, since the formula yields -0.0 / 0.9999999 drift there.
    """
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"need 0 <= successes <= n, got successes={successes}, n={n}")
    if z <= 0:  # a non-positive z would silently return an inverted or zero-width interval
        raise ValueError(f"z must be > 0, got {z}")
    if n == 0:
        return (0.0, 1.0)
    p, z2 = successes / n, z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    lo = 0.0 if successes == 0 else max(0.0, centre - half)
    hi = 1.0 if successes == n else min(1.0, centre + half)
    return (lo, hi)


def exact_mcnemar_p(b_only: int, a_only: int) -> float:
    """Two-sided exact McNemar p-value: the exact binomial sign test on the discordant pairs.

    Under H0 each post that only one run got right is equally likely to favour either run, so the
    smaller discordant count is Binomial(b_only + a_only, 0.5). Why exact and not chi-square
    McNemar: our discordant counts are 0-10, far below the ~25 where the chi-square approximation
    holds. Why paired at all: both runs score the same posts, and an unpaired two-proportion test
    would ignore that most posts are right (or wrong) in both runs.
    """
    if b_only < 0 or a_only < 0:
        raise ValueError(f"discordant counts must be >= 0, got b_only={b_only}, a_only={a_only}")
    n = b_only + a_only
    tail: float = sum(math.comb(n, k) for k in range(min(b_only, a_only) + 1)) / 2**n
    return min(1.0, 2 * tail)


def load_posts(path: str | Path) -> list[JSONObject]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def normalize(field: str, value: Any) -> Any:
    if field == "state":
        return value.strip().upper()
    if field == "city":  # "St. Louis," == "st  louis"; no spelling tolerance
        return " ".join(value.replace(".", "").replace(",", " ").lower().split())
    return value


def values_match(field: str, gold: Any, pred: Any) -> bool:
    if field == "deal_value_usd":
        return bool(abs(pred - gold) <= DEAL_VALUE_TOLERANCE * gold)
    return bool(normalize(field, gold) == normalize(field, pred))


def field_outcome(field: str, gold: Any, pred: Any) -> str:
    if gold is None:
        return "correct_missing" if pred is None else "hallucinated"
    if pred is None:
        return "missed"
    return "correct" if values_match(field, gold, pred) else "wrong"


def expected_missing(label: JSONObject) -> set[str]:
    return {f for f in KEY_FIELDS if label[f] is None}


def _ci95(successes: int, n: int) -> list[float] | None:
    """Wilson interval as a JSON-friendly list, None when there is nothing to estimate."""
    return list(wilson_interval(successes, n)) if n else None


def _rate(successes: int, n: int) -> float | None:
    return successes / n if n else None


def _with_accuracy(counts: dict[str, int], n: int) -> JSONObject:
    good = sum(counts.get(k, 0) for k in GOOD_OUTCOMES)
    return {**counts, "accuracy": _rate(good, n), "ci95": _ci95(good, n)}


def _with_hallucination_rate(cell: JSONObject, n_deal: int) -> JSONObject:
    """A key field's hallucinations on deal posts, as a rate so runs of different size compare."""
    return {
        **cell,
        "hallucination_rate": _rate(cell["hallucinated"], n_deal),
        "hallucination_ci95": _ci95(cell["hallucinated"], n_deal),
    }


def _post_correct(field: str, gold: JSONObject, pred: JSONObject) -> bool:
    """Whether one non-errored prediction is right on one field, by the same rules as score()."""
    if field == "is_deal_request":
        return bool(pred["is_deal_request"] == gold["is_deal_request"])
    if field == "missing_fields":
        return expected_missing(gold) == set(pred["missing_fields"])
    return field_outcome(field, gold[field], pred[field]) in GOOD_OUTCOMES


def post_correctness(posts: list[JSONObject], records: dict[str, JSONObject]) -> dict[str, dict[str, bool]]:
    """Per field, {post_id: correct} on exactly the posts score() scores that field on.

    This is what a paired comparison needs: score() only reports totals, and two runs with equal
    totals can disagree on many posts. is_deal_request is scored on every post, the other fields
    only on posts labeled as deal requests; an errored record is wrong on every field it is scored
    on. The True counts equal score()'s accuracy numerators.
    """
    out: dict[str, dict[str, bool]] = {f: {} for f in SCORED_FIELDS}
    for p in posts:
        gold, rec = p["label"], records[p["id"]]
        scored = SCORED_FIELDS if gold["is_deal_request"] else ("is_deal_request",)
        for f in scored:
            out[f][p["id"]] = not rec["error"] and _post_correct(f, gold, rec["prediction"])
    return out


def paired_comparison(a: dict[str, bool], b: dict[str, bool]) -> JSONObject:
    """Compare two {post_id: correct} maps on their common posts with the exact McNemar test.

    Only discordant posts carry information about which run is better; the ids are listed so a
    reader can open the posts that changed instead of trusting a single p-value.
    """
    common = a.keys() & b.keys()
    b_only = sorted(i for i in common if b[i] and not a[i])
    a_only = sorted(i for i in common if a[i] and not b[i])
    return {
        "n": len(common),
        "a_correct": sum(a[i] for i in common),
        "b_correct": sum(b[i] for i in common),
        "b_only": b_only,
        "a_only": a_only,
        "p_value": exact_mcnemar_p(len(b_only), len(a_only)),
    }


def score(posts: list[JSONObject], records: dict[str, JSONObject]) -> JSONObject:
    """records maps post id -> {"prediction": dict | None, "error": str | None}."""
    is_deal = dict.fromkeys(BINARY_OUTCOMES, 0)
    fields = {f: dict.fromkeys(FIELD_OUTCOMES, 0) for f in KEY_FIELDS}
    missing = dict.fromkeys(BINARY_OUTCOMES, 0)
    non_deal_filled = dict.fromkeys(KEY_FIELDS, 0)
    non_deal_posts_with_any = 0
    failures: list[JSONObject] = []

    def fail(post: JSONObject, field: str, outcome: str, expected: Any = None, got: Any = None) -> None:
        failures.append(
            {
                "id": post["id"],
                "field": field,
                "outcome": outcome,
                "expected": expected,
                "got": got,
                "text": post["text"],
            }
        )

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
                if o not in GOOD_OUTCOMES:
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
    n_non_deal = len(posts) - n_deal
    return {
        "n_posts": len(posts),
        "n_deal_posts": n_deal,
        "n_non_deal_posts": n_non_deal,
        "is_deal_request": _with_accuracy(is_deal, len(posts)),
        "fields": {f: _with_hallucination_rate(_with_accuracy(c, n_deal), n_deal) for f, c in fields.items()},
        "missing_fields": _with_accuracy(missing, n_deal),
        "non_deal_hallucinations": {
            "fields_filled": sum(non_deal_filled.values()),
            "posts_with_any": non_deal_posts_with_any,
            "by_field": non_deal_filled,
            "posts_with_any_rate": _rate(non_deal_posts_with_any, n_non_deal),
            "posts_with_any_ci95": _ci95(non_deal_posts_with_any, n_non_deal),
        },
        "failures": failures,
    }
