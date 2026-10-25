"""eval.py end to end on 2 posts, with a fake Claude client so nothing touches the network."""

from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from deal_board.cli import eval as ev
from deal_board.extractors.claude import ClaudeExtractor
from deal_board.schema import KEY_FIELDS, JSONObject
from deal_board.scoring import field_outcome, normalize, score, values_match

DEAL: JSONObject = {
    "id": "t1",
    "text": "Need a buyer agent in Austin, TX ...",
    "label": {
        "is_deal_request": True,
        "state": "TX",
        "city": "St. Louis",
        "property_type": "single_family",
        "transaction_type": "buy",
        "deal_value_usd": 650000,
        "timeline": "31_to_90_days",
    },
}
NON_DEAL: JSONObject = {
    "id": "t2",
    "text": "We're hiring in Denver!",
    "label": {
        "is_deal_request": False,
        **dict.fromkeys(["state", "city", "property_type", "transaction_type", "deal_value_usd", "timeline"]),
    },
}

# Every non-flag key of the strict tool's output, null: check_claude_raw rejects a missing key.
NULL_OUTPUT: JSONObject = dict.fromkeys(
    ["state", "city", "property_type", "transaction_type", "deal_value_usd", "timeline", "notes"]
)


class FakeClient:
    """Returns one canned tool call per request, in order, and counts the calls."""

    def __init__(self, outputs: Iterable[JSONObject]) -> None:
        self.outputs, self.calls, self.messages = list(outputs), 0, self

    def create(self, **kwargs: object) -> NS:
        self.calls += 1
        return NS(
            stop_reason="tool_use",
            content=[NS(type="tool_use", name="record_deal_requirement", input=self.outputs.pop(0))],
        )


def run(outputs: list[JSONObject]) -> tuple[FakeClient, dict[str, JSONObject], JSONObject]:
    client = FakeClient(outputs)
    records = ev.run_posts(ClaudeExtractor(client=client), [DEAL, NON_DEAL])
    return client, records, score([DEAL, NON_DEAL], records)


def test_eval_scores_fake_claude_outputs() -> None:
    deal_out = {
        "is_deal_request": True,
        "state": "tx",
        "city": "st louis",
        "property_type": "single_family",
        "transaction_type": "buy",
        "deal_value_usd": 653000,
        "timeline": "within_30_days",
        "notes": None,
    }
    non_deal_out = {"is_deal_request": False, **NULL_OUTPUT, "city": "Denver"}
    client, records, s = run([deal_out, non_deal_out])

    assert client.calls == 2
    assert records["t1"]["raw"] == deal_out  # raw output saved as returned
    assert s["is_deal_request"]["correct"] == 2
    assert s["fields"]["state"]["correct"] == 1  # "tx" normalized
    assert s["fields"]["city"]["correct"] == 1  # periods and case ignored
    assert s["fields"]["deal_value_usd"]["correct"] == 1  # within 1%
    assert s["fields"]["timeline"]["wrong"] == 1
    assert s["missing_fields"]["correct"] == 1
    assert s["non_deal_hallucinations"]["fields_filled"] == 1  # city on the hiring post
    assert s["fields"]["city"]["correct_missing"] == 0  # no free credit from the non-deal post


def test_invalid_output_is_an_error_and_rescore_needs_no_client() -> None:
    bad = {"is_deal_request": True, **NULL_OUTPUT, "state": "Texas"}  # not a 2-letter code
    _, records, s = run([bad, {"is_deal_request": False, **NULL_OUTPUT}])
    assert records["t1"]["error"] and records["t1"]["raw"] == bad
    assert s["fields"]["state"]["error"] == 1 and s["is_deal_request"]["accuracy"] == 0.5

    saved: JSONObject = {"datasets": {"x": {"path": "unused", "records": records}}}
    rescored = ev.rescore_records(saved["datasets"]["x"]["records"])  # eval-quality/CQ-5: the real code path
    assert score([DEAL, NON_DEAL], rescored) == s  # same scores from saved outputs, zero API calls


def test_rescore_records_keeps_refusal_error() -> None:
    """eval-quality/CQ-5: a refusal record (raw None, error set) keeps its error through rescore_records."""
    rescored = ev.rescore_records({"t1": {"raw": None, "prediction": None, "error": "refused"}})
    assert rescored["t1"]["error"] == "refused" and rescored["t1"]["prediction"] is None
    s = score([DEAL], rescored)
    assert s["is_deal_request"]["error"] == 1 and s["missing_fields"]["error"] == 1


def test_dev_split_never_scores_holdout() -> None:
    from deal_board.extractors.regex_baseline import RegexExtractor

    result = ev.evaluate(RegexExtractor(), "dev")
    assert list(result) == ["posts_dev"]
    assert all(pid.startswith("s") for pid in result["posts_dev"]["records"])
    assert result["posts_dev"]["scores"]["n_posts"] == 24


def test_no_api_key_runs_baseline_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path)
    ev.main(["--extractor", "all"])
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().out
    assert [p.name.split("_")[0] for p in tmp_path.glob("*.json")] == ["regex"]


# ---------- eval-quality/CQ-41: the deal-value tolerance and city normalisation rules ----------


@pytest.mark.parametrize(
    "pred,outcome",
    [
        (101_000, "correct"),
        (99_000, "correct"),
        (100_000, "correct"),
        (101_001, "wrong"),
        (98_999, "wrong"),
        (1, "wrong"),
    ],
)
def test_deal_value_tolerance_is_one_percent_either_side(pred: int, outcome: str) -> None:
    """eval-quality/CQ-41: deal_value_usd matches within +/-1% inclusive of the label, both directions."""
    assert field_outcome("deal_value_usd", 100_000, pred) == outcome
    assert values_match("deal_value_usd", 100_000, pred) is (outcome == "correct")


@pytest.mark.parametrize(
    "pred,outcome",
    [
        ("st  louis,", "correct"),
        ("ST. LOUIS", "correct"),
        ("St Louis", "correct"),
        ("Stlouis", "wrong"),
        ("St Lewis", "wrong"),
    ],
)
def test_city_ignores_case_spacing_periods_commas_only(pred: str, outcome: str) -> None:
    """eval-quality/CQ-41: city ignores case, periods, commas and extra spaces, with no spelling tolerance."""
    assert field_outcome("city", "St. Louis", pred) == outcome
    assert values_match("city", "St. Louis", pred) is (outcome == "correct")


def test_city_normalize_form() -> None:
    """eval-quality/CQ-41: normalize maps 'St. Louis,' and 'st  louis' to the same token string."""
    assert normalize("city", "St. Louis,") == normalize("city", "st  louis") == "st louis"


def test_errored_deal_post_counts_a_missing_fields_error() -> None:
    """eval-quality/CQ-41: an errored deal post is one missing_fields error, never correct."""
    s = score([DEAL], {"t1": {"raw": None, "prediction": None, "error": "refused"}})
    assert s["missing_fields"]["error"] == 1 and s["missing_fields"]["accuracy"] == 0.0


# ---------- eval-quality/CQ-43: summarize_repeats and format_repeat_summary ----------


def _scores(acc: float | None) -> JSONObject:
    """A score dict whose every scored field has the given accuracy (all summarize_repeats reads)."""
    return {
        "is_deal_request": {"accuracy": acc},
        "fields": {f: {"accuracy": acc} for f in KEY_FIELDS},
        "missing_fields": {"accuracy": acc},
    }


def _run(**datasets: float | None) -> JSONObject:
    return {"datasets": {name: {"scores": _scores(acc)} for name, acc in datasets.items()}}


def test_summarize_repeats_mean_min_max_and_runs() -> None:
    """eval-quality/CQ-43: None accuracies are skipped (not 0%), runs counts real values, range is min..max."""
    summary = ev.summarize_repeats([_run(posts_dev=0.5, holdout=0.8), _run(posts_dev=0.9), _run(posts_dev=None)])
    assert list(summary) == ["posts_dev", "holdout"]
    for field in ("is_deal_request", *KEY_FIELDS, "missing_fields"):
        s = summary["posts_dev"][field]
        assert s["mean"] == pytest.approx(0.7) and s["min"] == 0.5 and s["max"] == 0.9 and s["runs"] == 2, field
        h = summary["holdout"][field]
        assert (h["mean"], h["min"], h["max"], h["runs"]) == (pytest.approx(0.8), 0.8, 0.8, 1), field


def test_format_repeat_summary_renders_range_and_runs() -> None:
    """eval-quality/CQ-43: the rendered summary shows '50–90%' and '(2 runs)', and 'n/a' with no accuracy."""
    text = ev.format_repeat_summary(ev.summarize_repeats([_run(posts_dev=0.5), _run(posts_dev=0.9)]))
    assert "50–90%" in text and "(2 runs)" in text and "mean 70%" in text
    empty = ev.format_repeat_summary(ev.summarize_repeats([_run(posts_dev=None), _run(posts_dev=None)]))
    assert "n/a" in empty and "(0 runs)" in empty and "%" not in empty.replace("min–max", "")
