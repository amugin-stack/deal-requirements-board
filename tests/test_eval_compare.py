"""--compare, --rescore and compare_results in scripts/eval.py (P6 contract). Written blind to the
implementation. Reads real results files but never writes into results/."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from deal_board.cli import eval as ev
from deal_board.schema import KEY_FIELDS, JSONObject
from deal_board.scoring import SCORED_FIELDS, exact_mcnemar_p, load_posts

ROOT = Path(__file__).parent.parent
RESULTS = ROOT / "results"
POSTS_PATH = "data/posts.jsonl"
HOLDOUT_PATH = "data/holdout.jsonl"
LABELS: dict[str, JSONObject] = {p["id"]: p["label"] for p in load_posts(ROOT / POSTS_PATH)}
REGEX_RUN = RESULTS / "regex_20261024T224817Z.json"
CLAUDE_RUN = RESULTS / "claude_20261024T234359Z.json"
SUMMARY = RESULTS / "baseline-v1_comparison.json"
EVAL_RUNS = sorted(p for p in RESULTS.glob("*.json") if "datasets" in json.loads(p.read_text()))


def listing() -> list[str]:
    return sorted(p.name for p in RESULTS.iterdir())


def raw(pid: str, *, wrong_state: bool = False) -> JSONObject:
    """The label as a raw output (correct on every field), optionally with the state wrong."""
    out = {**LABELS[pid], "notes": None}
    if wrong_state:
        out["state"] = "WY" if out["state"] != "WY" else "VT"
    return out


def record(pid: str, *, wrong_state: bool = False) -> JSONObject:
    r = raw(pid, wrong_state=wrong_state)
    return {"raw": r, "prediction": r, "error": None}


def saved(datasets: dict[str, tuple[str, dict[str, JSONObject]]]) -> JSONObject:
    """A minimal saved-results dict: {name: (path, records)}."""
    return {
        "extractor": "fake",
        "model": None,
        "datasets": {name: {"path": path, "records": recs} for name, (path, recs) in datasets.items()},
    }


# Deal posts s01, s03, s04 have a labeled state; s02's is null; s05 is a non-deal post.
# A (named posts_dev) covers s01..s05 with state wrong on s01 and s04; B (named posts) covers
# s01..s04 plus s07 with state wrong on s03. Common ids: s01..s04.
A = saved(
    {
        "posts_dev": (
            POSTS_PATH,
            {
                "s04": record("s04", wrong_state=True),
                "s01": record("s01", wrong_state=True),
                "s02": record("s02"),
                "s03": record("s03"),
                "s05": record("s05"),
            },
        ),
        "holdout": (HOLDOUT_PATH, {}),
    }
)
B = saved(
    {
        "posts": (
            POSTS_PATH,
            {
                "s01": record("s01"),
                "s02": record("s02"),
                "s03": record("s03", wrong_state=True),
                "s04": record("s04"),
                "s07": record("s07", wrong_state=True),
            },
        ),
    }
)


@pytest.fixture(autouse=True)
def results_untouched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Every test here leaves results/ exactly as it found it."""
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path / "results")
    before = listing()
    yield
    assert listing() == before


def test_label_assumptions() -> None:
    """The real labels these synthetic runs rely on."""
    assert all(LABELS[p]["is_deal_request"] for p in ("s01", "s02", "s03", "s04", "s07"))
    assert LABELS["s05"]["is_deal_request"] is False
    assert LABELS["s02"]["state"] is None and all(LABELS[p]["state"] for p in ("s01", "s03", "s04"))


def test_compare_matches_by_path_and_common_ids() -> None:
    """posts_dev and posts match by path; only ids in both runs count; holdout has no partner."""
    cmp = ev.compare_results(A, B)
    assert list(cmp) == [POSTS_PATH]
    assert cmp[POSTS_PATH]["n_common_posts"] == 4
    assert list(cmp[POSTS_PATH]["fields"]) == list(SCORED_FIELDS)


def test_compare_state_discordant_ids_and_p() -> None:
    """b_only/a_only list the sorted discordant ids and p is exact McNemar on their counts."""
    state = ev.compare_results(A, B)[POSTS_PATH]["fields"]["state"]
    assert state["n"] == 4
    assert state["b_only"] == ["s01", "s04"] and state["a_only"] == ["s03"]
    assert (state["a_correct"], state["b_correct"]) == (2, 3)
    assert state["p_value"] == pytest.approx(exact_mcnemar_p(2, 1))


def test_compare_other_fields_concordant() -> None:
    """Fields both runs got right on every common post have no discordant ids and p == 1."""
    fields = ev.compare_results(A, B)[POSTS_PATH]["fields"]
    assert fields["is_deal_request"]["n"] == 4
    for f in ("is_deal_request", *(k for k in KEY_FIELDS if k != "state"), "missing_fields"):
        assert fields[f]["b_only"] == [] and fields[f]["a_only"] == [], f
        assert fields[f]["p_value"] == 1.0, f


def test_compare_is_antisymmetric() -> None:
    """Swapping A and B swaps b_only and a_only and keeps p."""
    ab = ev.compare_results(A, B)[POSTS_PATH]["fields"]["state"]
    ba = ev.compare_results(B, A)[POSTS_PATH]["fields"]["state"]
    assert (ba["b_only"], ba["a_only"]) == (ab["a_only"], ab["b_only"])
    assert ba["p_value"] == ab["p_value"]


def test_compare_rescores_from_raw_not_saved_scores() -> None:
    """Stale saved scores are ignored: comparison comes from raw outputs vs current labels."""
    a = json.loads(json.dumps(A))
    a["datasets"]["posts_dev"]["scores"] = {"is_deal_request": {"accuracy": 0.0}}
    assert ev.compare_results(a, B) == ev.compare_results(A, B)


def test_format_comparison_names_mcnemar_and_ids() -> None:
    """The printed comparison names the McNemar test and the discordant ids."""
    text = ev.format_comparison(ev.compare_results(A, B), "A.json", "B.json")
    assert "McNemar" in text
    assert "s01" in text and "s03" in text and "s04" in text


@pytest.mark.parametrize("path", EVAL_RUNS, ids=lambda p: p.name)
def test_rescore_every_results_file(path: Path) -> None:
    """--rescore works on every saved eval run, old ones included, and adds ci95s."""
    saved_run = json.loads(path.read_text())
    out = ev.rescore(saved_run)  # rescore(saved: dict); the contract leaves the return shape as before P6
    out = out.get("datasets", out)
    assert set(out) == set(saved_run["datasets"])
    for ds in out.values():
        s = ds.get("scores", ds)
        assert s["is_deal_request"]["ci95"] is not None
        assert all("ci95" in s["fields"][f] for f in KEY_FIELDS)
        assert "ci95" in s["missing_fields"]


def test_eval_runs_found() -> None:
    """The glob above found the real runs (guards against an empty parametrization)."""
    assert REGEX_RUN in EVAL_RUNS and CLAUDE_RUN in EVAL_RUNS and SUMMARY not in EVAL_RUNS


def test_cli_compare_real_files_prints_and_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--compare on two real runs prints a McNemar comparison and writes no files."""
    ev.main(["--compare", str(REGEX_RUN), str(CLAUDE_RUN)])
    out = capsys.readouterr().out
    assert "McNemar" in out and POSTS_PATH in out and HOLDOUT_PATH in out
    assert not (tmp_path / "results").exists() or not any((tmp_path / "results").iterdir())


def test_cli_compare_dev_run_with_full_run(capsys: pytest.CaptureFixture[str]) -> None:
    """A posts_dev run compares with a full run's posts dataset."""
    dev_run = next(p for p in EVAL_RUNS if list(json.loads(p.read_text())["datasets"]) == ["posts_dev"])
    ev.main(["--compare", str(dev_run), str(CLAUDE_RUN)])
    assert "McNemar" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--rescore", str(SUMMARY)],
        ["--compare", str(SUMMARY), str(REGEX_RUN)],
        ["--compare", str(REGEX_RUN), str(SUMMARY)],
    ],
)
def test_summary_file_exits_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """A JSON without datasets exits with status 2 and a one-line message, not a traceback."""
    with pytest.raises(SystemExit) as exc:
        ev.main(argv)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    msg = (captured.out + captured.err).strip()
    assert msg and "Traceback" not in msg
