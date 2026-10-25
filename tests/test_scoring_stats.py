"""Statistics in scoring.py (P6 contract): Wilson intervals, exact McNemar, per-post correctness,
paired comparison, and the additive ci95/rate keys in score(). Written blind to the implementation."""

import json
import math
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from deal_board.cli import eval as ev
from deal_board.extractors.claude import ClaudeExtractor
from deal_board.schema import KEY_FIELDS, JSONObject
from deal_board.scoring import (
    SCORED_FIELDS,
    exact_mcnemar_p,
    load_posts,
    paired_comparison,
    post_correctness,
    score,
    wilson_interval,
)

ROOT = Path(__file__).parent.parent
NULL_FIELDS = dict.fromkeys(KEY_FIELDS)
type Synthetic = tuple[list[JSONObject], dict[str, JSONObject]]  # (posts, records), as score() takes them


class FakeClient:
    """Returns one canned tool call per request, in order, with no usage attribute."""

    def __init__(self, outputs: list[JSONObject]) -> None:
        self.outputs, self.calls, self.messages = list(outputs), 0, self

    def create(self, **kwargs: object) -> NS:
        self.calls += 1
        return NS(stop_reason="tool_use", content=[NS(type="tool_use", input=self.outputs.pop(0))])


def deal(pid: str, **label: object) -> JSONObject:
    """A labeled deal post with every key field set unless overridden."""
    base = {
        "is_deal_request": True,
        "state": "TX",
        "city": "Austin",
        "property_type": "single_family",
        "transaction_type": "buy",
        "deal_value_usd": 650000,
        "timeline": "31_to_90_days",
    }
    return {"id": pid, "text": f"post {pid}", "label": {**base, **label}}


def non_deal(pid: str) -> JSONObject:
    """A labeled non-deal post."""
    return {"id": pid, "text": f"post {pid}", "label": {"is_deal_request": False, **NULL_FIELDS}}


def records_for(posts: list[JSONObject], outputs: list[JSONObject]) -> dict[str, JSONObject]:
    """Run posts through ClaudeExtractor with canned outputs (no network)."""
    return ev.run_posts(ClaudeExtractor(client=FakeClient(outputs)), posts)


# Three deal posts (one correct, one partly wrong, one errored) and two non-deal posts.
POSTS = [deal("d1"), deal("d2", timeline=None), deal("d3"), non_deal("n1"), non_deal("n2")]
OUTPUTS = [
    {**deal("d1")["label"]},  # all right
    {**deal("d2")["label"], "state": "CA", "city": None},  # state wrong, city missed, timeline hallucinated
    {"is_deal_request": True, "state": "Texas"},  # invalid -> errored record
    {"is_deal_request": False, **NULL_FIELDS},  # right
    {"is_deal_request": True, **NULL_FIELDS, "city": "Denver"},  # wrong is_deal_request + a hallucination
]


@pytest.fixture(scope="module")
def synthetic() -> Synthetic:
    return POSTS, records_for(POSTS, OUTPUTS)


# ---------- wilson_interval ----------


@pytest.mark.parametrize(
    "successes,n,expected",
    [
        (0, 10, (0.0, 0.2775)),
        (10, 10, (0.7225, 1.0)),
        (5, 10, (0.2366, 0.7634)),
        (14, 15, (0.7018, 0.9881)),
        (1, 1, (0.2065, 1.0)),
    ],
)
def test_wilson_reference_values(successes: int, n: int, expected: tuple[float, float]) -> None:
    """Wilson bounds match the contract's reference values to 4 decimals."""
    lo, hi = wilson_interval(successes, n)
    assert (round(lo, 4), round(hi, 4)) == expected


def test_wilson_default_z_is_1_96() -> None:
    """The default z is 1.96, so passing it explicitly changes nothing."""
    assert wilson_interval(5, 10) == wilson_interval(5, 10, z=1.96)


def test_wilson_no_data_is_whole_unit_interval() -> None:
    """n == 0 returns (0.0, 1.0): with no data every rate is plausible."""
    assert wilson_interval(0, 0) == (0.0, 1.0)


@pytest.mark.parametrize("n", [1, 2, 10, 12, 15, 24, 35, 1000])
def test_wilson_zero_successes_lower_is_exact_positive_zero(n: int) -> None:
    """successes == 0 gives lower == 0.0 exactly, not -0.0 or a tiny float."""
    lo, _ = wilson_interval(0, n)
    assert lo == 0.0
    assert math.copysign(1.0, lo) == 1.0


@pytest.mark.parametrize("n", [1, 2, 10, 12, 15, 24, 35, 1000])
def test_wilson_all_successes_upper_is_exact_one(n: int) -> None:
    """successes == n gives upper == 1.0 exactly (no 0.9999999 drift)."""
    _, hi = wilson_interval(n, n)
    assert hi == 1.0


@pytest.mark.parametrize("successes,n", [(-1, 10), (0, -1), (11, 10), (1, 0), (-1, -1)])
def test_wilson_invalid_counts_raise(successes: int, n: int) -> None:
    """Negative counts or successes > n raise ValueError."""
    with pytest.raises(ValueError):
        wilson_interval(successes, n)


def test_wilson_contains_p_and_stays_in_unit_interval() -> None:
    """For every (successes, n) the interval lies in [0, 1] and contains successes/n."""
    for n in range(1, 41):
        for s in range(n + 1):
            lo, hi = wilson_interval(s, n)
            assert 0.0 <= lo <= s / n <= hi <= 1.0, (s, n, lo, hi)


@pytest.mark.parametrize("p", [0.0, 0.5, 0.8, 1.0])
def test_wilson_wider_at_smaller_n(p: float) -> None:
    """Same proportion, fewer posts: the interval is wider."""
    widths = []
    for n in (10, 20, 40, 80):
        lo, hi = wilson_interval(round(p * n), n)
        widths.append(hi - lo)
    assert widths == sorted(widths, reverse=True) and len(set(widths)) == len(widths)


# ---------- exact_mcnemar_p ----------


@pytest.mark.parametrize(
    "b_only,a_only,expected",
    [
        (5, 0, 0.0625),
        (0, 5, 0.0625),
        (3, 1, 0.625),
        (0, 0, 1.0),
        (1, 1, 1.0),
        (10, 0, 0.001953125),
        (2, 1, 1.0),
        (7, 2, 0.1796875),
    ],
)
def test_mcnemar_reference_values(b_only: int, a_only: int, expected: float) -> None:
    """Exact two-sided McNemar matches hand-computed binomial sign-test values."""
    assert exact_mcnemar_p(b_only, a_only) == pytest.approx(expected, abs=1e-12)


def test_mcnemar_is_symmetric() -> None:
    """Swapping which run is A and which is B gives the same p-value."""
    for b in range(12):
        for a in range(12):
            assert exact_mcnemar_p(b, a) == exact_mcnemar_p(a, b)


def test_mcnemar_no_discordant_pairs_is_one() -> None:
    """(0, 0) returns exactly 1.0."""
    assert exact_mcnemar_p(0, 0) == 1.0


@pytest.mark.parametrize("b_only,a_only", [(-1, 0), (0, -1), (-3, -2)])
def test_mcnemar_negative_raises(b_only: int, a_only: int) -> None:
    """Negative discordant counts raise ValueError."""
    with pytest.raises(ValueError):
        exact_mcnemar_p(b_only, a_only)


def test_mcnemar_p_in_unit_interval() -> None:
    """p is capped at 1 and never negative."""
    for b in range(15):
        for a in range(15):
            assert 0.0 < exact_mcnemar_p(b, a) <= 1.0


# ---------- post_correctness ----------


def test_scored_fields_order() -> None:
    """SCORED_FIELDS is is_deal_request, the key fields, then missing_fields."""
    assert ("is_deal_request", *KEY_FIELDS, "missing_fields") == SCORED_FIELDS


def test_post_correctness_covers_the_right_posts(synthetic: Synthetic) -> None:
    """is_deal_request covers every post; other fields only labeled deal posts."""
    posts, records = synthetic
    pc = post_correctness(posts, records)
    assert list(pc) == list(SCORED_FIELDS)
    assert set(pc["is_deal_request"]) == {"d1", "d2", "d3", "n1", "n2"}
    for field in (*KEY_FIELDS, "missing_fields"):
        assert set(pc[field]) == {"d1", "d2", "d3"}, field


def test_post_correctness_values(synthetic: Synthetic) -> None:
    """Correct and correct_missing count as True; wrong, missed, hallucinated and errors as False."""
    posts, records = synthetic
    pc = post_correctness(posts, records)
    assert pc["is_deal_request"] == {"d1": True, "d2": True, "d3": False, "n1": True, "n2": False}
    assert pc["state"] == {"d1": True, "d2": False, "d3": False}
    assert pc["city"] == {"d1": True, "d2": False, "d3": False}
    assert pc["timeline"] == {"d1": True, "d2": False, "d3": False}  # hallucinated on d2
    assert pc["property_type"] == {"d1": True, "d2": True, "d3": False}
    assert pc["missing_fields"]["d1"] is True and pc["missing_fields"]["d3"] is False


def test_post_correctness_correct_missing_is_true() -> None:
    """A field labeled null and predicted null is correct (correct_missing)."""
    posts = [deal("d1", timeline=None)]
    records = records_for(posts, [{**deal("d1")["label"], "timeline": None}])
    assert post_correctness(posts, records)["timeline"] == {"d1": True}


def test_post_correctness_errored_record_wrong_everywhere(synthetic: Synthetic) -> None:
    """An errored record is incorrect on every field it is scored on."""
    posts, records = synthetic
    assert records["d3"]["error"]
    pc = post_correctness(posts, records)
    assert all(pc[field]["d3"] is False for field in SCORED_FIELDS)


def _accuracy_cell(s: JSONObject, field: str) -> JSONObject:
    cell: JSONObject = s["fields"][field] if field in KEY_FIELDS else s[field]
    return cell


def _denominator(s: JSONObject, field: str) -> int:
    n: int = s["n_posts"] if field == "is_deal_request" else s["n_deal_posts"]
    return n


def _assert_numerators_match(posts: list[JSONObject], records: dict[str, JSONObject]) -> None:
    s, pc = score(posts, records), post_correctness(posts, records)
    for field in SCORED_FIELDS:
        acc, den = _accuracy_cell(s, field)["accuracy"], _denominator(s, field)
        assert sum(pc[field].values()) == round(acc * den), field
        assert len(pc[field]) == den, field


def test_post_correctness_matches_score_numerators(synthetic: Synthetic) -> None:
    """True counts per field equal score()'s accuracy numerators (synthetic posts)."""
    _assert_numerators_match(*synthetic)


@pytest.mark.parametrize("stem", ["posts", "holdout"])
def test_post_correctness_matches_score_on_saved_regex_run(stem: str) -> None:
    """True counts equal score()'s numerators on real posts with saved regex outputs."""
    saved = json.loads((ROOT / "results" / "regex_20261024T224817Z.json").read_text())
    ds = saved["datasets"][stem]
    records = {pid: ev.to_record(r["raw"], r["error"]) for pid, r in ds["records"].items()}
    _assert_numerators_match(load_posts(ROOT / ds["path"]), records)


# ---------- paired_comparison ----------


def test_paired_comparison_on_common_ids_only() -> None:
    """Only ids in both maps count; b_only/a_only are sorted; p is exact McNemar."""
    a = {"p5": False, "p1": True, "p3": False, "p2": True, "p4": True, "only_a": False}
    b = {"p3": True, "p1": True, "p5": True, "p2": False, "p4": True, "only_b": True}
    out = paired_comparison(a, b)
    assert out == {
        "n": 5,
        "a_correct": 3,
        "b_correct": 4,
        "b_only": ["p3", "p5"],
        "a_only": ["p2"],
        "p_value": exact_mcnemar_p(2, 1),
    }


def test_paired_comparison_identical_runs() -> None:
    """Identical runs have no discordant posts and p == 1.0."""
    a = {"x": True, "y": False}
    out = paired_comparison(a, dict(a))
    assert out["b_only"] == [] and out["a_only"] == [] and out["p_value"] == 1.0 and out["n"] == 2


def test_paired_comparison_no_common_ids() -> None:
    """Disjoint maps compare zero posts."""
    out = paired_comparison({"a": True}, {"b": True})
    assert out["n"] == 0 and out["a_correct"] == 0 and out["b_correct"] == 0 and out["p_value"] == 1.0


# ---------- score() additions ----------


def _wilson_list(num: int, den: int) -> list[float]:
    return list(wilson_interval(num, den))


def test_score_ci95_on_every_accuracy_cell(synthetic: Synthetic) -> None:
    """Every accuracy cell gains ci95 = Wilson(numerator, denominator)."""
    posts, records = synthetic
    s = score(posts, records)
    for field in SCORED_FIELDS:
        cell, den = _accuracy_cell(s, field), _denominator(s, field)
        num = round(cell["accuracy"] * den)
        assert list(cell["ci95"]) == pytest.approx(_wilson_list(num, den)), field


def test_score_hallucination_rate_and_ci(synthetic: Synthetic) -> None:
    """fields[f] gains hallucination_rate = hallucinated / n_null (non-errored deal posts whose label
    for f is empty) and its Wilson interval; None when n_null is 0 (eval-quality/EM-61)."""
    posts, records = synthetic
    s = score(posts, records)
    assert s["fields"]["timeline"]["hallucinated"] == 1
    for field in KEY_FIELDS:
        cell = s["fields"][field]
        n_null = sum(
            1
            for p in posts
            if p["label"]["is_deal_request"] and not records[p["id"]]["error"] and p["label"][field] is None
        )
        assert cell["n_null"] == n_null
        if n_null == 0:
            assert cell["hallucination_rate"] is None and cell["hallucination_ci95"] is None
            continue
        assert cell["hallucination_rate"] == pytest.approx(cell["hallucinated"] / n_null)
        assert list(cell["hallucination_ci95"]) == pytest.approx(_wilson_list(cell["hallucinated"], n_null))


def test_score_non_deal_rate_and_ci(synthetic: Synthetic) -> None:
    """non_deal_hallucinations gains posts_with_any_rate and its Wilson interval."""
    posts, records = synthetic
    nd = score(posts, records)["non_deal_hallucinations"]
    assert nd["posts_with_any"] == 1
    assert nd["posts_with_any_rate"] == pytest.approx(0.5)
    assert list(nd["posts_with_any_ci95"]) == pytest.approx(_wilson_list(1, 2))


def test_score_no_deal_posts_gives_none() -> None:
    """With no deal posts, field and missing_fields accuracy, ci95 and hallucination keys are None."""
    posts = [non_deal("n1"), non_deal("n2")]
    s = score(posts, records_for(posts, [{"is_deal_request": False, **NULL_FIELDS}] * 2))
    assert s["is_deal_request"]["ci95"] is not None
    for field in KEY_FIELDS:
        cell = s["fields"][field]
        assert cell["accuracy"] is None and cell["ci95"] is None
        assert cell["hallucination_rate"] is None and cell["hallucination_ci95"] is None
    assert s["missing_fields"]["accuracy"] is None and s["missing_fields"]["ci95"] is None


def test_score_no_non_deal_posts_gives_none() -> None:
    """With no non-deal posts, posts_with_any_rate and its ci95 are None."""
    posts = [deal("d1")]
    nd = score(posts, records_for(posts, [deal("d1")["label"]]))["non_deal_hallucinations"]
    assert nd["posts_with_any_rate"] is None and nd["posts_with_any_ci95"] is None


def _assert_subset(old: object, new: object, path: str = "") -> None:
    """Every key in old exists in new with the same value (dicts compared recursively)."""
    if isinstance(old, dict):
        assert isinstance(new, dict), path
        for k, v in old.items():
            assert k in new, f"{path}.{k} removed"
            _assert_subset(v, new[k], f"{path}.{k}")
    else:
        assert new == old, path


@pytest.mark.parametrize("stem", ["posts", "holdout"])
def test_score_preexisting_keys_unchanged(stem: str) -> None:
    """Re-scoring a pre-P6 run keeps every old key and value; keys are only added."""
    saved = json.loads((ROOT / "results" / "regex_20261024T224817Z.json").read_text())
    ds = saved["datasets"][stem]
    records = {pid: ev.to_record(r["raw"], r["error"]) for pid, r in ds["records"].items()}
    _assert_subset(ds["scores"], score(load_posts(ROOT / ds["path"]), records))
