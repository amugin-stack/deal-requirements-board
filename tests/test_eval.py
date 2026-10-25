"""eval.py end to end on 2 posts, with a fake Claude client so nothing touches the network."""
from types import SimpleNamespace as NS

from deal_board.extractors.claude import ClaudeExtractor
from deal_board.scoring import score
from deal_board.cli import eval as ev

DEAL = {"id": "t1", "text": "Need a buyer agent in Austin, TX ...", "label": {
    "is_deal_request": True, "state": "TX", "city": "St. Louis", "property_type": "single_family",
    "transaction_type": "buy", "deal_value_usd": 650000, "timeline": "31_to_90_days"}}
NON_DEAL = {"id": "t2", "text": "We're hiring in Denver!", "label": {
    "is_deal_request": False, **dict.fromkeys(
        ["state", "city", "property_type", "transaction_type", "deal_value_usd", "timeline"])}}


class FakeClient:
    """Returns one canned tool call per request, in order, and counts the calls."""

    def __init__(self, outputs):
        self.outputs, self.calls, self.messages = list(outputs), 0, self

    def create(self, **kwargs):
        self.calls += 1
        return NS(stop_reason="tool_use", content=[NS(type="tool_use", input=self.outputs.pop(0))])


def run(outputs):
    client = FakeClient(outputs)
    records = ev.run_posts(ClaudeExtractor(client=client), [DEAL, NON_DEAL])
    return client, records, score([DEAL, NON_DEAL], records)


def test_eval_scores_fake_claude_outputs():
    deal_out = {"is_deal_request": True, "state": "tx", "city": "st louis", "property_type": "single_family",
                "transaction_type": "buy", "deal_value_usd": 653000, "timeline": "within_30_days"}
    non_deal_out = {"is_deal_request": False, "city": "Denver"}
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


def test_invalid_output_is_an_error_and_rescore_needs_no_client():
    bad = {"is_deal_request": True, "state": "Texas"}  # not a 2-letter code
    _, records, s = run([bad, {"is_deal_request": False}])
    assert records["t1"]["error"] and records["t1"]["raw"] == bad
    assert s["fields"]["state"]["error"] == 1 and s["is_deal_request"]["accuracy"] == 0.5

    saved = {"datasets": {"x": {"path": "unused", "records": records}}}
    rescored = {pid: ev.to_record(r["raw"], r["error"]) for pid, r in saved["datasets"]["x"]["records"].items()}
    assert score([DEAL, NON_DEAL], rescored) == s  # same scores from saved outputs, zero API calls


def test_dev_split_never_scores_holdout():
    from deal_board.extractors.regex_baseline import RegexExtractor
    result = ev.evaluate(RegexExtractor(), "dev")
    assert list(result) == ["posts_dev"]
    assert all(pid.startswith("s") for pid in result["posts_dev"]["records"])
    assert result["posts_dev"]["scores"]["n_posts"] == 24


def test_no_api_key_runs_baseline_only(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path)
    ev.main(["--extractor", "all"])
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().out
    assert [p.name.split("_")[0] for p in tmp_path.glob("*.json")] == ["regex"]
