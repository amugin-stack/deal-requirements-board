"""Regression tests for fix set C1 (eval CLI, scoring, Claude runtime), written blind to the fixes.

One or more tests per finding, named after it; ids are `review/id` because ids collide across reviews.
Names the fixes add (request_sha, check_claude_raw, SCORING_VERSION) are looked up at call time,
so on the pre-fix code those tests fail instead of breaking collection. Every Claude extractor
uses a fake client: `_default_client` is patched and the only key ever set is a dummy placeholder,
so nothing can reach the network. Nothing is written into results/, data/ or .cache/.
"""

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from deal_board import scoring
from deal_board.cli import eval as ev
from deal_board.extractors import ExtractionError
from deal_board.extractors import claude as claude_mod
from deal_board.extractors.claude import ClaudeExtractor, Usage
from deal_board.schema import KEY_FIELDS, DealRequirement, JSONObject
from deal_board.scoring import score, wilson_interval

REPO = Path(__file__).parent.parent
TOOL_NAME = "record_deal_requirement"
N_DEV_POSTS = 24
IN_TOK, OUT_TOK = 100, 20
FULL: JSONObject = {
    "is_deal_request": True,
    "state": "TX",
    "city": "Austin",
    "property_type": "single_family",
    "transaction_type": "buy",
    "deal_value_usd": 450000,
    "timeline": "31_to_90_days",
    "notes": None,
}
ALL_NULL: JSONObject = {"is_deal_request": False, **dict.fromkeys(KEY_FIELDS), "notes": None}


def _attr(module: object, name: str) -> Any:
    """A name the fix adds; AttributeError (a plain test failure) on the pre-fix code."""
    return getattr(module, name)


class FakeClient:
    """Answers every request with one canned response and counts the calls; never touches the network."""

    def __init__(
        self, output: JSONObject | None = None, *, stop_reason: str = "tool_use", name: str = TOOL_NAME
    ) -> None:
        self.output = dict(FULL if output is None else output)
        self.stop_reason, self.name, self.calls, self.messages = stop_reason, name, 0, self

    def create(self, **kwargs: object) -> NS:
        self.calls += 1
        block = NS(type="tool_use", name=self.name, input=dict(self.output))
        return NS(stop_reason=self.stop_reason, content=[block], usage=NS(input_tokens=IN_TOK, output_tokens=OUT_TOK))


def results_listing() -> list[str]:
    return sorted(p.name for p in (REPO / "results").iterdir())


def load_results(directory: Path) -> list[JSONObject]:
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No real key, model override or default output dir reaches a test; results/ is left untouched."""
    for var in ("ANTHROPIC_API_KEY", "DEAL_MODEL", "DEAL_EXTRACTOR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path / "results-default")
    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path / "cache-default")
    before = results_listing()
    yield
    assert results_listing() == before


type ClaudeMain = Callable[..., FakeClient]


@pytest.fixture
def claude_main(monkeypatch: pytest.MonkeyPatch) -> ClaudeMain:
    """Run ev.main(--extractor claude --split dev ...) through the real make_extractor with a fake client.

    The key is a dummy placeholder (main only checks that one is set) and _default_client is
    patched, so the extractor main builds always gets the fake.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-a-real-key")

    def run(*extra: str, results_dir: Path, cache_dir: Path, model: str | None = None) -> FakeClient:
        client = FakeClient()
        monkeypatch.setattr(claude_mod, "_default_client", lambda api_key: client)
        if model is not None:
            monkeypatch.setenv("DEAL_MODEL", model)
        argv = ["--extractor", "claude", "--split", "dev", "--results-dir", str(results_dir)]
        ev.main([*argv, "--cache-dir", str(cache_dir), *extra])
        return client

    return run


# ---------- correctness-api/C-1, compliance-security/C-10, eval-quality/CQ-21: strict Claude output ----------

BAD_RESPONSES = {
    "max_tokens": FakeClient(stop_reason="max_tokens"),
    "pause_turn": FakeClient(stop_reason="pause_turn"),
    "max_tokens_partial": FakeClient({"is_deal_request": True}, stop_reason="max_tokens"),
    "wrong_tool_name": FakeClient(name="other"),
}


@pytest.mark.parametrize("case", list(BAD_RESPONSES))
def test_claude_extractor_rejects_max_tokens_truncated_tool_call(case: str) -> None:
    """correctness-api/C-1, compliance-security/C-10: a non-tool_use stop or another tool's call raises."""
    client = BAD_RESPONSES[case]
    ext = ClaudeExtractor(client=client)
    with pytest.raises(ExtractionError):
        ext.extract_raw("Need an agent in Austin, TX")
    assert ext.last_usage == Usage(IN_TOK, OUT_TOK)  # the call was billed even though it failed
    with pytest.raises(ExtractionError):
        ext.extract("Need an agent in Austin, TX")
    assert ext.last_usage == Usage(IN_TOK, OUT_TOK)


@pytest.mark.parametrize("case", list(BAD_RESPONSES))
def test_truncated_call_is_recorded_as_an_error(case: str) -> None:
    """eval-quality/CQ-21: run_posts records a truncated or mislabelled call as an error, not a prediction."""
    posts = [{"id": "t1", "text": "Need an agent in Austin, TX", "label": {"is_deal_request": True}}]
    rec = ev.run_posts(ClaudeExtractor(client=BAD_RESPONSES[case]), posts)["t1"]
    assert rec["error"] is not None and rec["prediction"] is None


LOOSE_RAWS = {
    "missing_city": {k: v for k, v in FULL.items() if k != "city"},
    "missing_notes": {k: v for k, v in FULL.items() if k != "notes"},
    "extra_key": {**FULL, "inferred_state": "TX"},
    "is_deal_request_yes": {**FULL, "is_deal_request": "yes"},
    "deal_value_string": {**FULL, "deal_value_usd": "450000"},
    "deal_value_float": {**FULL, "deal_value_usd": 4.9e5},
    "deal_value_bool": {**FULL, "deal_value_usd": True},
}


@pytest.mark.parametrize("case", list(LOOSE_RAWS))
def test_claude_rejects_truncated_or_loose_output(case: str) -> None:
    """compliance-security/C-10: keys must be exactly ExtractedFields' with strict JSON types, else ExtractionError."""
    raw = LOOSE_RAWS[case]
    with pytest.raises(ExtractionError):
        _attr(claude_mod, "check_claude_raw")(raw)
    with pytest.raises(ExtractionError):
        ClaudeExtractor(client=FakeClient(raw)).extract("Need an agent in Austin, TX")


@pytest.mark.parametrize("case", list(LOOSE_RAWS))
def test_loose_output_is_an_error_in_eval(case: str) -> None:
    """compliance-security/C-10: eval scores loose Claude output as an error, never as a prediction."""
    posts = [{"id": "t1", "text": "Need an agent in Austin, TX", "label": {"is_deal_request": True}}]
    rec = ev.run_posts(ClaudeExtractor(client=FakeClient(LOOSE_RAWS[case])), posts)["t1"]
    assert rec["error"] is not None and rec["prediction"] is None


@pytest.mark.parametrize("raw", [FULL, ALL_NULL, {**FULL, "notes": "pre-approved", "deal_value_usd": None}])
def test_fully_populated_strict_output_parses(raw: JSONObject) -> None:
    """compliance-security/C-10: an output with every key and strict types passes the check and parses."""
    assert _attr(claude_mod, "check_claude_raw")(dict(raw)) is None
    req = ClaudeExtractor(client=FakeClient(raw)).extract("post")
    assert isinstance(req, DealRequirement) and req.is_deal_request is raw["is_deal_request"]


def test_rescore_applies_the_strict_check_to_claude_raws() -> None:
    """compliance-security/C-10: --rescore of a Claude run turns a saved loose raw into an error."""
    loose = {k: v for k, v in FULL.items() if k != "city"}
    saved = {
        "extractor": "claude",
        "datasets": {
            "posts": {
                "path": "data/posts.jsonl",
                "records": {
                    "s01": {"raw": loose, "prediction": None, "error": None},
                    "s05": {"raw": dict(ALL_NULL), "prediction": None, "error": None},
                },
            }
        },
    }
    records = ev.rescore(saved)["posts"]["records"]
    assert records["s01"]["error"] is not None and records["s01"]["prediction"] is None
    assert records["s05"]["error"] is None  # a strict-shaped raw still scores


# ---------- compliance-security/S-4: rescored_from is never an absolute path ----------


def _frozen_run() -> JSONObject:
    saved: JSONObject = json.loads((REPO / "results" / "regex_20261024T224817Z.json").read_text())
    return saved


GIT = {"commit": "0123456789abcdef", "dirty": False}


@pytest.mark.parametrize("where", ["in_repo", "outside_repo"])
def test_rescored_from_is_repo_relative(where: str, tmp_path: Path) -> None:
    """compliance-security/S-4: rescored_from is repo-relative POSIX, or the bare file name outside the repo."""
    source, expected = (
        (ev.ROOT / "results" / "x.json", "results/x.json") if where == "in_repo" else (tmp_path / "y.json", "y.json")
    )
    result = ev.rescored_result(_frozen_run(), source, "20261024T000000Z", GIT)
    assert result["rescored_from"] == expected
    text = ev.markdown(result) + json.dumps(result)
    assert str(ev.ROOT) not in text and str(tmp_path) not in text
    assert f"`{expected}`" in ev.markdown(result)


# ---------- eval-quality/CQ-2: git dirty ignores eval's own output ----------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_git_dirty_ignores_results_and_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """eval-quality/CQ-2: --repeats 2 on a clean tree is clean twice; .cache/ is ignored; a .py edit is dirty."""
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    shutil.copy(REPO / "data" / "posts.jsonl", repo / "data" / "posts.jsonl")
    (repo / "mod.py").write_text("X = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    monkeypatch.setattr(ev, "ROOT", repo)
    monkeypatch.setattr(ev, "RESULTS_DIR", repo / "results")
    monkeypatch.setattr(ev, "CACHE_DIR", repo / ".cache" / "extractor")

    ev.main(["--extractor", "regex", "--split", "dev", "--repeats", "2"])
    results = load_results(repo / "results")
    assert len(results) == 2
    assert [r["git"]["dirty"] for r in results] == [False, False]

    (repo / ".cache" / "extractor").mkdir(parents=True)
    (repo / ".cache" / "extractor" / "entry.json").write_text("{}")
    assert ev.git_info()["dirty"] is False
    (repo / "mod.py").write_text("X = 2\n")
    assert ev.git_info()["dirty"] is True


# ---------- eval-quality/CQ-3: request_sha ----------


def test_request_sha_is_short_hex_and_changes_with_model() -> None:
    """eval-quality/CQ-3: request_sha(model) is 12 hex chars, deterministic, and differs per model."""
    request_sha = _attr(claude_mod, "request_sha")
    a, b = request_sha("claude-haiku-5-5"), request_sha("claude-other-model")
    assert re.fullmatch(r"[0-9a-f]{12}", a) and re.fullmatch(r"[0-9a-f]{12}", b)
    assert a == request_sha("claude-haiku-5-5") and a != b


def test_claude_results_carry_request_sha(claude_main: ClaudeMain, tmp_path: Path) -> None:
    """eval-quality/CQ-3: every Claude result records request_sha for its model, so a model change shows."""
    claude_main(results_dir=tmp_path / "a", cache_dir=tmp_path / "c")
    claude_main(results_dir=tmp_path / "b", cache_dir=tmp_path / "c", model="claude-other-model")
    (a,) = load_results(tmp_path / "a")
    (b,) = load_results(tmp_path / "b")
    request_sha = _attr(claude_mod, "request_sha")
    assert a["request_sha"] == request_sha(a["model"])
    assert b["model"] == "claude-other-model" and b["request_sha"] == request_sha("claude-other-model")
    assert a["request_sha"] != b["request_sha"]


# ---------- eval-quality/EM-1: no cross-invocation replay without --resume ----------


def test_second_invocation_makes_fresh_calls_unless_resume(claude_main: ClaudeMain, tmp_path: Path) -> None:
    """eval-quality/EM-1: a second invocation on the same cache calls fresh; only --resume replays."""
    cache_dir = tmp_path / "cache"
    first = claude_main(results_dir=tmp_path / "a", cache_dir=cache_dir)
    second = claude_main(results_dir=tmp_path / "b", cache_dir=cache_dir)
    assert first.calls == second.calls == N_DEV_POSTS
    (b,) = load_results(tmp_path / "b")
    records = [r for ds in b["datasets"].values() for r in ds["records"].values()]
    assert len(records) == N_DEV_POSTS and not any(r["call"]["cached"] for r in records)
    assert b["usage"]["cache_hits"] == 0 and b["usage"]["api_calls"] == N_DEV_POSTS

    resumed = claude_main("--resume", results_dir=tmp_path / "c", cache_dir=cache_dir)
    assert resumed.calls == 0
    (c,) = load_results(tmp_path / "c")
    assert all(r["call"]["cached"] for ds in c["datasets"].values() for r in ds["records"].values())


# ---------- eval-quality/CQ-24: load_run exits 2 on any non-run file ----------

REGEX_RUN = REPO / "results" / "regex_20261024T224817Z.json"


@pytest.mark.parametrize("kind", ["markdown", "empty", "missing"])
@pytest.mark.parametrize("mode", ["rescore", "compare_a", "compare_b"])
def test_non_json_input_exits_2(kind: str, mode: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """eval-quality/CQ-24: a .md twin, an empty file or a missing path exits 2 with a message, no traceback."""
    bad = tmp_path / f"bad_{kind}.json"
    if kind == "markdown":
        bad.write_text("# Eval: regex\n\n| field | accuracy |\n")
    elif kind == "empty":
        bad.write_text("")
    argv = {
        "rescore": ["--rescore", str(bad)],
        "compare_a": ["--compare", str(bad), str(REGEX_RUN)],
        "compare_b": ["--compare", str(REGEX_RUN), str(bad)],
    }[mode]
    with pytest.raises(SystemExit) as exc:
        ev.main([*argv, "--results-dir", str(tmp_path / "out")])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.err.strip() and "Traceback" not in captured.out + captured.err
    assert not (tmp_path / "out").exists()


# ---------- eval-quality/CQ-52: explicit --extractor claude needs a key ----------


def test_explicit_claude_without_key_exits_nonzero(tmp_path: Path) -> None:
    """eval-quality/CQ-52: --extractor claude with no API key exits non-zero and writes nothing."""
    out, cache_dir = tmp_path / "out", tmp_path / "c"
    with pytest.raises(SystemExit) as exc:
        ev.main(["--extractor", "claude", "--split", "dev", "--results-dir", str(out), "--cache-dir", str(cache_dir)])
    assert exc.value.code not in (0, None)
    assert not out.exists() or not any(out.iterdir())
    assert not (tmp_path / "results-default").exists()
    assert not cache_dir.exists()


# ---------- eval-quality/CQ-61, EM-23: rescored results describe the rules they were scored under ----------


def test_rescore_writes_current_scoring_note() -> None:
    """eval-quality/CQ-61: a rescore's 'scoring' is the current SCORING_NOTE; the source's is kept as source_scoring."""
    saved = {**_frozen_run(), "scoring": "OLD NOTE"}
    result = ev.rescored_result(saved, ev.ROOT / "results" / "x.json", "20261024T000000Z", GIT)
    assert result["scoring"] == ev.SCORING_NOTE
    assert result["source_scoring"] == "OLD NOTE"
    assert ev.SCORING_NOTE in ev.markdown(result) and "OLD NOTE" not in ev.markdown(result)


def test_new_and_rescored_results_carry_scoring_version(tmp_path: Path) -> None:
    """eval-quality/EM-23: every new or rescored result records scoring.SCORING_VERSION."""
    version = _attr(scoring, "SCORING_VERSION")
    ev.main(["--extractor", "regex", "--split", "dev", "--results-dir", str(tmp_path / "out")])
    (fresh,) = load_results(tmp_path / "out")
    assert fresh["scoring_version"] == version
    rescored = ev.rescored_result(_frozen_run(), ev.ROOT / "results" / "x.json", "20261024T000000Z", GIT)
    assert rescored["scoring_version"] == version


def test_legacy_summary_named_in_exit_message(capsys: pytest.CaptureFixture[str]) -> None:
    """eval-quality/EM-23: the exit-2 message calls a datasets-less JSON a legacy summary."""
    with pytest.raises(SystemExit) as exc:
        ev.main(["--rescore", str(REPO / "results" / "baseline-v1_comparison.json")])
    assert exc.value.code == 2
    assert "legacy" in capsys.readouterr().err.lower()


# ---------- eval-quality/CQ-8: failures list follows GOOD_OUTCOMES ----------


def _post(pid: str, deal: bool = True, **label: object) -> JSONObject:
    base = (
        {"is_deal_request": True, "state": "TX", "city": "Austin", "property_type": "single_family"}
        | {"transaction_type": "buy", "deal_value_usd": 650000, "timeline": "31_to_90_days"}
        if deal
        else {"is_deal_request": False, **dict.fromkeys(KEY_FIELDS)}
    )
    return {"id": pid, "text": f"post {pid}", "label": {**base, **label}}


def _ok(pred: JSONObject) -> JSONObject:
    """A non-errored record; missing_fields is derived the way DealRequirement derives it."""
    return {"prediction": {**pred, "missing_fields": [f for f in KEY_FIELDS if pred[f] is None]}, "error": None}


ERRORED: JSONObject = {"prediction": None, "error": "refused"}


def _cq8_set() -> tuple[list[JSONObject], dict[str, JSONObject]]:
    """Deal and non-deal posts covering every outcome, correct_missing and errors included."""
    posts = [
        _post("d1"),
        _post("d2", city=None, timeline=None),
        _post("d3"),
        _post("d4", deal_value_usd=None),
        _post("n1", deal=False),
        _post("n2", deal=False),
        _post("n3", deal=False),
    ]
    lab = {p["id"]: dict(p["label"]) for p in posts}
    records = {
        "d1": _ok(lab["d1"]),
        "d2": _ok({**lab["d2"], "state": "CA", "city": "Dallas"}),  # wrong, hallucinated, correct_missing
        "d3": ERRORED,
        "d4": _ok({**lab["d4"], "property_type": None}),  # missed, correct_missing
        "n1": _ok({**lab["n1"], "city": "Denver"}),  # hallucinated on non-deal
        "n2": ERRORED,
        "n3": _ok({**lab["n3"], "is_deal_request": True}),  # is_deal_request wrong
    }
    return posts, records


def _assert_failures_match_accuracy(posts: list[JSONObject], records: dict[str, JSONObject]) -> None:
    s = score(posts, records)
    deal_ids = {p["id"] for p in posts if p["label"]["is_deal_request"]}
    errored_deal = sum(1 for x in s["failures"] if x["field"] == "(all)" and x["id"] in deal_ids)
    errored_all = sum(1 for x in s["failures"] if x["field"] == "(all)")
    n_deal = s["n_deal_posts"]
    for field in (*KEY_FIELDS, "missing_fields"):
        cell = s["fields"][field] if field in KEY_FIELDS else s["missing_fields"]
        rows = sum(1 for x in s["failures"] if x["field"] == field and x["id"] in deal_ids)
        assert n_deal - round(cell["accuracy"] * n_deal) == rows + errored_deal, field
    rows = sum(1 for x in s["failures"] if x["field"] == "is_deal_request")
    assert s["n_posts"] - round(s["is_deal_request"]["accuracy"] * s["n_posts"]) == rows + errored_all


def test_failures_count_matches_accuracy_numerators() -> None:
    """eval-quality/CQ-8: per field, posts scored minus the accuracy numerator equals its failure rows."""
    _assert_failures_match_accuracy(*_cq8_set())


def test_failures_follow_patched_good_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    """eval-quality/CQ-8: with GOOD_OUTCOMES = ('correct',) the failures list still matches accuracy."""
    monkeypatch.setattr(scoring, "GOOD_OUTCOMES", ("correct",))
    _assert_failures_match_accuracy(*_cq8_set())


# ---------- eval-quality/EM-24, EM-61: hallucination-rate denominators ----------


def test_all_errored_run_has_no_hallucination_rate() -> None:
    """eval-quality/EM-24: an all-errored run reports None hallucination rates, not 0%."""
    posts = [_post("d1"), _post("d2", city=None), _post("n1", deal=False), _post("n2", deal=False)]
    s = score(posts, {p["id"]: ERRORED for p in posts})
    for field in KEY_FIELDS:
        cell = s["fields"][field]
        assert cell["hallucination_rate"] is None and cell["hallucination_ci95"] is None, field
    nd = s["non_deal_hallucinations"]
    assert nd["posts_with_any_rate"] is None and nd["posts_with_any_ci95"] is None


def test_errored_posts_leave_hallucination_denominators() -> None:
    """eval-quality/EM-24: 1 errored of 3 deal posts, 1 guess among the other 2 -> rate 1/2 (and 1/1 non-deal)."""
    posts = [
        _post("d1", property_type=None),
        _post("d2", property_type=None),
        _post("d3", property_type=None),
        _post("n1", deal=False),
        _post("n2", deal=False),
    ]
    records = {
        "d1": _ok({**posts[0]["label"], "property_type": "office"}),
        "d2": _ok(posts[1]["label"]),
        "d3": ERRORED,
        "n1": _ok({**posts[3]["label"], "city": "Denver"}),
        "n2": ERRORED,
    }
    s = score(posts, records)
    cell = s["fields"]["property_type"]
    assert cell["hallucinated"] == 1 and cell["hallucination_rate"] == pytest.approx(0.5)
    assert list(cell["hallucination_ci95"]) == pytest.approx(list(wilson_interval(1, 2)))
    nd = s["non_deal_hallucinations"]
    assert nd["posts_with_any_rate"] == 1.0
    assert list(nd["posts_with_any_ci95"]) == pytest.approx(list(wilson_interval(1, 1)))


def test_hallucination_rate_divides_by_null_labels() -> None:
    """eval-quality/EM-61: hallucination_rate = hallucinated / n_null; None for a field with no null labels."""
    posts = [_post("d1", property_type=None), _post("d2"), _post("d3"), _post("d4")]
    records = {p["id"]: _ok(p["label"]) for p in posts}
    records["d1"] = _ok({**posts[0]["label"], "property_type": "retail"})
    s = score(posts, records)
    pt = s["fields"]["property_type"]
    assert pt["n_null"] == 1 and pt["hallucinated"] == 1
    assert pt["hallucination_rate"] == 1.0
    assert list(pt["hallucination_ci95"]) == pytest.approx(list(wilson_interval(1, 1)))
    state = s["fields"]["state"]
    assert state["n_null"] == 0 and state["hallucination_rate"] is None and state["hallucination_ci95"] is None


# ---------- eval-quality/EM-6: rescore/compare detect text and label drift ----------


def _write_posts(path: Path, posts: list[JSONObject]) -> None:
    path.write_text("".join(json.dumps(p) + "\n" for p in posts))


def test_rescore_and_compare_report_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """eval-quality/EM-6: records store text/label hashes; changed ids are excluded and reported as drift."""
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    monkeypatch.setattr(ev, "ROOT", root)
    monkeypatch.setattr(ev, "git_info", lambda: dict(GIT))
    posts = [
        {**_post("p1"), "text": "Need a buyer agent in Austin, TX for a $650k SFR within 60 days."},
        {**_post("p2", deal=False), "text": "We're hiring in Denver!"},
        {**_post("p3"), "text": "Seller rep wanted in Houston, TX."},
        {**_post("p4", deal=False), "text": "Happy Friday, everyone."},
    ]
    _write_posts(root / "data" / "posts.jsonl", posts)
    _write_posts(root / "data" / "holdout.jsonl", [{**_post("h1"), "text": "Buyer agent in Boise, ID."}])
    ev.main(["--extractor", "regex", "--results-dir", str(tmp_path / "run")])
    ((run_path),) = sorted((tmp_path / "run").glob("*.json"))
    run = json.loads(run_path.read_text())
    for p in posts:
        rec = run["datasets"]["posts"]["records"][p["id"]]
        assert rec["text_sha256"] == ev.text_sha256(p["text"])
        assert isinstance(rec["label_sha256"], str) and rec["label_sha256"]

    p1 = {**posts[0], "text": posts[0]["text"] + " Edited."}
    p3 = {**posts[2], "label": {**posts[2]["label"], "timeline": None}}
    p5 = {**_post("p5", deal=False), "text": "New post."}
    _write_posts(root / "data" / "posts.jsonl", [p1, p3, posts[3], p5])  # p2 deleted, p5 added

    capsys.readouterr()
    ev.main(["--rescore", str(run_path), "--results-dir", str(tmp_path / "rescored")])
    (rescored,) = load_results(tmp_path / "rescored")
    ds = rescored["datasets"]["posts"]
    assert ds["drift"]["changed"] == ["p1", "p3"]
    assert ds["drift"]["missing"] == ["p2"]
    assert ds["drift"]["new"] == ["p5"]
    assert ds["scores"]["n_posts"] == 1  # only p4 is unchanged and still present
    holdout_drift = rescored["datasets"]["holdout"]["drift"]
    assert (holdout_drift["changed"], holdout_drift["missing"], holdout_drift["new"]) == ([], [], [])

    cmp = ev.compare_results(run, run)
    assert cmp["data/posts.jsonl"]["n_common_posts"] == 1
    assert cmp["data/holdout.jsonl"]["n_common_posts"] == 1
