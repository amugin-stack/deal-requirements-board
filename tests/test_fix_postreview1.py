"""Post-review round 1 (EVAL-1..EVAL-6): gaps in the loop guard (loop/check_success.py) and in eval's rescore.

Written blind to the implementation, from the contracts. Helpers follow tests/test_fix_set_c2.py (copied, not
imported: tests/ is not a package under --import-mode=importlib). Every repo is a tmp git repo under tmp_path;
the real label files are only read. No API calls: Claude runs use a fake client.

Assumed names: the new checks 'runs reachable from HEAD' and 'final run clean'; final_checks' third parameter
`rescore`; the drift key 'relabeled'; the top-level result key 'raw_check'.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace as NS
from typing import TYPE_CHECKING

import pytest

from deal_board.cli import eval as ev
from deal_board.extractors import claude as claude_mod
from deal_board.extractors.claude import PROMPT_VERSION, ClaudeExtractor
from deal_board.schema import JSONObject
from deal_board.scoring import load_posts, score

CHECK_SUCCESS = Path(__file__).resolve().parent.parent / "loop" / "check_success.py"


def _load_check_success() -> ModuleType:
    """loop/ is not a package; load the script by path (same recipe as tests/test_check_success.py)."""
    spec = importlib.util.spec_from_file_location("check_success", CHECK_SUCCESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if TYPE_CHECKING:
    import check_success as cs
else:
    cs = _load_check_success()

IDENTITY = ("v1", "claude-test-model", "sha-v1")
GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
RESCORE_CHECK = "stored scores match rescore"
REACHABLE_CHECK = "runs reachable from HEAD"
DRIFT_KEYS = {"changed", "relabeled", "missing", "new"}


def failed(checks: Iterable[cs.Check]) -> set[str]:
    return {c.name for c in checks if not c.passed}


def detail(checks: Iterable[cs.Check], name: str) -> str:
    return next(c.detail for c in checks if c.name == name)


def git(root: Path, *args: str) -> str:
    out = subprocess.run([*GIT, *args], cwd=root, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def init_repo(root: Path, files: dict[str, str]) -> str:
    """A git repo holding `files`, tagged loop-start and baseline-v1 at its only commit; returns HEAD."""
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    git(root, "tag", "loop-start")
    git(root, "tag", "baseline-v1")
    return git(root, "rev-parse", "HEAD")


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


# ---- honest fake runs built from the real labels, with post hashes as eval writes them ----


def posts_for(dataset: str, split: str) -> list[JSONObject]:
    posts = load_posts(ev.ROOT / ev.DATASETS[dataset])
    return posts if split == "all" else [p for p in posts if p.get("split") == split]


def records_for(
    posts: list[JSONObject], *, edit: Callable[[JSONObject], JSONObject] | None = None
) -> dict[str, JSONObject]:
    """One fresh record per post whose raw output is the post's label (optionally edited), with its hashes."""
    call = {"cached": False, "input_tokens": 100, "output_tokens": 20, "latency_s": 0.1}
    out = {}
    for p in posts:
        label = dict(p["label"])
        raw = {"notes": None, **(edit(label) if edit else label)}
        out[p["id"]] = {**ev.to_record(raw), "call": dict(call), **ev.post_hashes(p)}
    return out


def result(commit: str, datasets: JSONObject, *, split: str = "dev", dirty: bool = False) -> JSONObject:
    version, model, request_sha = IDENTITY
    return {
        "extractor": "claude",
        "model": model,
        "prompt_version": version,
        "request_sha": request_sha,
        "split": split,
        "timestamp": "20261101T000000Z",
        "git": {"commit": commit, "dirty": dirty},
        "usage": {"cache_hits": 0, "api_calls": 0},
        "datasets": datasets,
    }


def dev_result(commit: str = "c", *, dirty: bool = False) -> JSONObject:
    posts = posts_for("posts", "dev")
    records = records_for(posts)
    ds = {"posts_dev": {"path": ev.DATASETS["posts"], "records": records, "scores": score(posts, records)}}
    return result(commit, ds, dirty=dirty)


def final_result(commit: str = "c", *, dirty: bool = False, perfect_but_wrong_in: str | None = None) -> JSONObject:
    """A final run. perfect_but_wrong_in: that dataset gets one wrong city but keeps a perfect scores block."""
    datasets = {}
    for name in ("holdout", "posts"):
        posts = posts_for(name, "all")
        records = records_for(posts)
        stored = score(posts, records)
        if name == perfect_but_wrong_in:
            target = next(p for p in posts if p["label"]["is_deal_request"] and p["label"].get("city"))
            raw = {"notes": None, **target["label"], "city": "Nowhereville"}
            records[target["id"]] = {**records[target["id"]], **ev.to_record(raw)}
            assert score(posts, records) != stored  # fixture sanity: the edit costs accuracy
        datasets[name] = {"path": ev.DATASETS[name], "records": records, "scores": stored}
    return result(commit, datasets, split="all", dirty=dirty)


def write_run(root: Path, n: int, run: JSONObject) -> Path:
    """Save as results/claude_<stamp>.json; a larger n is a newer run."""
    path = root / "results" / f"claude_20261101T{n:06d}Z.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**run, "timestamp": path.stem.removeprefix("claude_")}, indent=2))
    return path


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A tmp repo at loop-start with copies of the label files; cs points at it, guard stubbed out."""
    root = tmp_path / "repo"
    for name in ("posts", "holdout"):
        dest = root / ev.DATASETS[name]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ev.ROOT / ev.DATASETS[name], dest)
    head = init_repo(root, {"README.md": "tmp\n"})
    monkeypatch.setattr(cs, "ROOT", root)
    monkeypatch.setattr(cs, "guard_checks", list)
    monkeypatch.setattr(cs, "current_identity", lambda: cs.RunIdentity(*IDENTITY))
    monkeypatch.setattr(cs, "current_prompt_version", lambda: IDENTITY[0])
    return root, head


def gate(capsys: pytest.CaptureFixture[str], stage: str) -> set[str]:
    """Run check_success main() and return the names of the checks that failed."""
    capsys.readouterr()
    code = cs.main(["--stage", stage, "--json"])
    out = json.loads(capsys.readouterr().out)
    names = {c["name"] for c in out["checks"] if not c["passed"]}
    assert (code == 0) == (not names)
    return names


def three_good_dev_runs(root: Path, head: str) -> None:
    for n in range(1, cs.REPEATS + 1):
        write_run(root, n, dev_result(head))


def doctored_state_post() -> tuple[JSONObject, JSONObject]:
    """A dev deal post with a state, and the same post with its label's state doctored."""
    post = next(p for p in posts_for("posts", "dev") if p["label"]["is_deal_request"] and p["label"].get("state"))
    state = "VT" if post["label"]["state"] != "VT" else "WY"
    return post, {**post, "label": {**post["label"], "state": state}}


def relabeled_dev_run() -> tuple[JSONObject, str]:
    """A hashed dev run made against one doctored label (output, label_sha256 and scores all from it)."""
    real, doctored = doctored_state_post()
    posts = [doctored if p["id"] == real["id"] else p for p in posts_for("posts", "dev")]
    records = records_for(posts)
    ds = {"posts_dev": {"path": ev.DATASETS["posts"], "records": records, "scores": score(posts, records)}}
    return result("c", ds), str(real["id"])


# ---- EVAL-1 (eval side): a label-only change is re-scored, not excluded ----


def test_eval1_relabeled_post_is_rescored_against_the_current_label() -> None:
    """postreview-1/EVAL-1: same text, new label hash -> drift 'relabeled', scored against the current label."""
    run, pid = relabeled_dev_run()
    out = ev.rescore(run)["posts_dev"]
    assert out["drift"]["relabeled"] == [pid]
    assert out["drift"]["changed"] == []
    assert pid in out["records"]
    assert out["scores"]["n_posts"] == len(posts_for("posts", "dev"))
    assert out["scores"]["fields"]["state"]["accuracy"] < 1.0  # the doctored-label answer is now wrong


def test_eval1_text_changed_post_is_still_excluded() -> None:
    """postreview-1/EVAL-1: a post whose text hash changed is excluded and listed under 'changed', not relabeled."""
    run = dev_result()
    pid = next(iter(run["datasets"]["posts_dev"]["records"]))
    run["datasets"]["posts_dev"]["records"][pid]["text_sha256"] = ev.text_sha256("some other text")
    out = ev.rescore(run)["posts_dev"]
    assert out["drift"]["changed"] == [pid]
    assert out["drift"]["relabeled"] == []
    assert pid not in out["records"]
    assert out["scores"]["n_posts"] == len(posts_for("posts", "dev")) - 1


def test_eval1_every_rescore_drift_dict_has_all_four_keys() -> None:
    """postreview-1/EVAL-1: rescore drift has keys changed, relabeled, missing, new, even when nothing moved."""
    for run in (dev_result(), final_result()):
        for ds in ev.rescore(run).values():
            assert set(ds["drift"]) == DRIFT_KEYS
            assert all(ds["drift"][k] == [] for k in DRIFT_KEYS)


def test_eval1_legacy_records_without_hashes_are_scored_as_before() -> None:
    """postreview-1/EVAL-1 control: records without hashes are never changed or relabeled; all are scored."""
    run, _ = relabeled_dev_run()
    for rec in run["datasets"]["posts_dev"]["records"].values():
        del rec["text_sha256"], rec["label_sha256"]
    out = ev.rescore(run)["posts_dev"]
    assert out["drift"]["changed"] == [] and out["drift"].get("relabeled", []) == []
    assert out["scores"]["n_posts"] == len(posts_for("posts", "dev"))


# ---- EVAL-1 / EVAL-2 (guard side): rescore drift and post counts fail the rescore check ----


def test_eval1_guard_fails_a_hashed_run_made_against_a_doctored_label() -> None:
    """postreview-1/EVAL-1: hashed dev runs scored against one doctored (then restored) label fail the rescore check."""
    run, pid = relabeled_dev_run()
    assert run["datasets"]["posts_dev"]["scores"]["fields"]["state"]["accuracy"] == 1.0  # fixture sanity
    checks = cs.dev_checks([run] * cs.REPEATS)
    assert RESCORE_CHECK in failed(checks)
    why = detail(checks, RESCORE_CHECK)
    assert "cannot rescore" not in why
    assert pid in why


def test_eval2_guard_fails_a_run_with_a_dropped_record() -> None:
    """postreview-1/EVAL-2: a dev run missing one post's record (scores recomputed without it) fails the rescore check."""
    run = dev_result()
    ds = run["datasets"]["posts_dev"]
    pid = next(p["id"] for p in posts_for("posts", "dev") if not p["label"]["is_deal_request"])
    del ds["records"][pid]
    ds["scores"] = score([p for p in posts_for("posts", "dev") if p["id"] != pid], ds["records"])
    checks = cs.dev_checks([run] * cs.REPEATS)
    assert RESCORE_CHECK in failed(checks)
    assert pid in detail(checks, RESCORE_CHECK)


@pytest.mark.parametrize("key", ["n_posts", "n_deal_posts"])
def test_eval2_guard_fails_stored_post_counts_that_differ_from_the_rescore(key: str) -> None:
    """postreview-1/EVAL-2: stored n_posts / n_deal_posts that differ from the rescored ones fail the rescore check."""
    run = dev_result()
    run["datasets"]["posts_dev"]["scores"][key] += 1
    assert failed(cs.dev_checks([run] * cs.REPEATS)) == {RESCORE_CHECK}


def test_eval2_control_honest_hashed_runs_pass() -> None:
    """postreview-1/EVAL-1/EVAL-2 control: honest hashed dev runs pass every dev check."""
    assert failed(cs.dev_checks([dev_result()] * cs.REPEATS)) == set()


# ---- EVAL-3: the counted final is fresh-checked and re-scored ----


def final_failures(run: JSONObject) -> set[str]:
    return failed(cs.final_checks([run], cs.RunIdentity(*IDENTITY)))


def test_eval3_final_with_a_cached_record_fails_fresh_runs() -> None:
    """postreview-1/EVAL-3: a counted final with a record replayed from the cache fails 'fresh runs'."""
    run = final_result()
    next(iter(run["datasets"]["holdout"]["records"].values()))["call"]["cached"] = True
    assert "fresh runs" in final_failures(run)


def test_eval3_final_with_cache_hits_fails_fresh_runs() -> None:
    """postreview-1/EVAL-3: a counted final with usage.cache_hits > 0 fails 'fresh runs'."""
    run = final_result()
    run["usage"]["cache_hits"] = 1
    assert "fresh runs" in final_failures(run)


@pytest.mark.parametrize("dataset", ["holdout", "posts"])
def test_eval3_final_with_hand_edited_perfect_scores_fails_rescore(dataset: str) -> None:
    """postreview-1/EVAL-3: a final whose stored scores were edited to perfect fails 'stored scores match rescore'."""
    assert RESCORE_CHECK in final_failures(final_result(perfect_but_wrong_in=dataset))


def test_eval3_control_honest_final_passes() -> None:
    """postreview-1/EVAL-3 control: an honest fresh final passes every final check."""
    assert final_failures(final_result()) == set()


# ---- EVAL-4: counted runs must be reachable from HEAD ----

PASSING_TEST = "def test_ok() -> None:\n    assert True\n"
DRAFT = "# Loop\n\n## Goal (FIXED)\nwin\n\n## Backlog (EDITABLE)\n1. a\n"
MOVED = {  # P10 moves: the guard compares these with their pre-move path at the tag
    "deal_board/scoring.py": "scoring.py",
    "deal_board/schema.py": "schema.py",
    "deal_board/cli/eval.py": "scripts/eval.py",
    "deal_board/extractors/__init__.py": "extractors/__init__.py",
    "deal_board/extractors/frozen/regex_baseline.py": "extractors/regex_baseline.py",
}


def guard_files() -> dict[str, str]:
    """A tiny repo laid out like the real one at loop-start (as in test_fix_set_c2); its own tests/ is what runs."""
    files = {
        "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = ["-p", "no:cacheprovider"]\n',
        ".gitignore": "__pycache__/\n.pytest_cache/\n",
        "loop/check_success.py": "GATE = 1\n",
        "loop/DRAFT.md": DRAFT,
        "loop/LESSONS.md": "# Lessons\n",
        "loop/state.json": "{}\n",
        "deal_board/__init__.py": "",
        "deal_board/scoring.py": "TOLERANCE = 0.01\n",
        "deal_board/schema.py": "SCHEMA = 1\n",
        "deal_board/cli/eval.py": "EVAL = 1\n",
        "deal_board/extractors/__init__.py": "EXTRACTORS = 1\n",
        "deal_board/extractors/frozen/regex_baseline.py": "BASELINE = 1\n",
        "deal_board/extractors/claude.py": 'PROMPT = "a"\n',
        "deal_board/matcher.py": "MATCH = 1\n",
        "deal_board/paths.py": "PATHS = 1\n",
        "data/posts.jsonl": "{}\n",
        ".claude/agents/extractor-improver.md": "# agent\n",
        "results/claude_20260101T000000Z.json": "{}\n",
    }
    for name in ("test_data", "test_eval", "test_brokers", "test_check_success"):
        files[f"tests/{name}.py"] = PASSING_TEST
    for new, old in MOVED.items():
        files[old] = files[new]
    return files


@pytest.fixture
def guard_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "guarded"
    init_repo(root, guard_files())
    monkeypatch.setattr(cs, "ROOT", root)
    return root


def run_at(root: Path, commit: str) -> Path:
    """A minimal clean dev-run file recorded at `commit`; untracked, so a reset keeps it."""
    run = {"extractor": "claude", "split": "dev", "git": {"commit": commit, "dirty": False}, "datasets": {}}
    return write_run(root, 1, run)


def protected_commit_with_a_run(root: Path) -> Path:
    (root / "deal_board/scoring.py").write_text("TOLERANCE = 1.0\n")
    sha = commit_all(root, "loosen scoring")
    return run_at(root, sha)


def test_eval4_run_on_a_reset_away_commit_fails_reachable(guard_repo: Path) -> None:
    """postreview-1/EVAL-4: a run made at a protected commit that was then reset away fails 'runs reachable from HEAD'."""
    path = protected_commit_with_a_run(guard_repo)
    git(guard_repo, "reset", "-q", "--hard", "HEAD~1")
    checks = cs.guard_checks()
    assert failed(checks) == {REACHABLE_CHECK}  # history and net diff are both clean after the reset
    assert path.name in detail(checks, REACHABLE_CHECK)


def test_eval4_run_on_an_unresolvable_commit_fails_reachable(guard_repo: Path) -> None:
    """postreview-1/EVAL-4: a run whose commit git can't resolve fails 'runs reachable from HEAD'."""
    path = run_at(guard_repo, "f" * 40)
    checks = cs.guard_checks()
    assert REACHABLE_CHECK in failed(checks)
    assert path.name in detail(checks, REACHABLE_CHECK)


def test_eval4_after_revert_reachable_passes_and_history_still_fails(guard_repo: Path) -> None:
    """postreview-1/EVAL-4: after git revert the run is reachable (check passes); the history check still fails."""
    protected_commit_with_a_run(guard_repo)
    git(guard_repo, "revert", "--no-edit", "HEAD")
    checks = cs.guard_checks()
    assert REACHABLE_CHECK in {c.name for c in checks}
    assert failed(checks) == {"protected files untouched in history"}


# ---- EVAL-5: dirty finals are looks too ----


def test_eval5_older_dirty_final_counts_as_a_look(repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]) -> None:
    """postreview-1/EVAL-5: an older dirty final plus a newer clean one of the same identity fails 'one look only'."""
    root, head = repo
    three_good_dev_runs(root, head)
    write_run(root, 100, final_result(head, dirty=True))
    write_run(root, 101, final_result(head))
    assert "one look only" in gate(capsys, "final")


def test_eval5_single_dirty_final_fails_final_run_clean(
    repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """postreview-1/EVAL-5: a lone dirty final fails 'final run clean' instead of reading as no final run."""
    root, head = repo
    three_good_dev_runs(root, head)
    write_run(root, 100, final_result(head, dirty=True))
    names = gate(capsys, "final")
    assert "final run clean" in names
    assert "final run" not in names


def test_eval5_control_one_clean_final_passes(repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]) -> None:
    """postreview-1/EVAL-5 control: three good dev runs and one clean hashed final pass the final stage."""
    root, head = repo
    three_good_dev_runs(root, head)
    write_run(root, 100, final_result(head))
    assert gate(capsys, "final") == set()


# ---- EVAL-6: a strict run stays strict ----

MODEL = "claude-haiku-5-5"


class FakeClient:
    """Answers every request with one valid tool call; never touches the network."""

    def __init__(self) -> None:
        self.messages = self

    def create(self, **kwargs: object) -> NS:
        output = {
            "is_deal_request": True,
            "state": "TX",
            "city": "Austin",
            "property_type": "single_family",
            "transaction_type": "buy",
            "deal_value_usd": 650000,
            "timeline": "31_to_90_days",
            "notes": None,
        }
        return NS(
            stop_reason="tool_use",
            content=[NS(type="tool_use", name="record_deal_requirement", input=output)],
            usage=NS(input_tokens=100, output_tokens=20),
        )


def run_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extractor: str) -> JSONObject:
    """ev.main for one extractor on the dev split, written to tmp_path; returns the saved result."""
    if extractor == "claude":  # the regex baseline needs no key and no fake
        monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-a-real-key")
        meta = {"model": MODEL, "prompt_version": PROMPT_VERSION}
        monkeypatch.setattr(ev, "make_extractor", lambda name: (ClaudeExtractor(client=FakeClient()), meta))
    out = tmp_path / "results"
    ev.main(["--extractor", extractor, "--split", "dev", "--no-cache", "--results-dir", str(out)])
    (path,) = out.glob("*.json")
    saved: JSONObject = json.loads(path.read_text())
    return saved


def test_eval6_fresh_claude_result_records_strict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """postreview-1/EVAL-6: a fresh Claude result written by eval records top-level raw_check 'strict'."""
    assert run_main(monkeypatch, tmp_path, "claude").get("raw_check") == "strict"


def test_eval6_fresh_regex_result_is_not_strict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """postreview-1/EVAL-6 control: a regex result records raw_check 'legacy' or nothing."""
    assert run_main(monkeypatch, tmp_path, "regex").get("raw_check", "legacy") == "legacy"


def string_value_run(raw_check: str) -> tuple[JSONObject, str]:
    """A current-prompt Claude dev run with one deal_value_usd given as a string (a strict-check error)."""
    strict = ev.raw_check("claude", PROMPT_VERSION)
    assert strict is not None
    run = dev_result()
    ds = run["datasets"]["posts_dev"]
    post = next(p for p in posts_for("posts", "dev") if isinstance(p["label"].get("deal_value_usd"), int))
    raw = {"notes": None, **post["label"], "deal_value_usd": str(post["label"]["deal_value_usd"])}
    ds["records"][post["id"]] = {**ds["records"][post["id"]], **ev.to_record(raw, None, strict)}
    assert ds["records"][post["id"]]["error"]  # fixture sanity
    ds["scores"] = score(posts_for("posts", "dev"), ds["records"])
    return {**run, "prompt_version": PROMPT_VERSION, "raw_check": raw_check}, str(post["id"])


def test_eval6_recorded_strict_survives_a_prompt_version_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """postreview-1/EVAL-6: a run recorded 'strict' keeps its strict-check errors after PROMPT_VERSION changes."""
    run, pid = string_value_run("strict")
    stored_errors = run["datasets"]["posts_dev"]["scores"]["is_deal_request"]["error"]
    monkeypatch.setattr(claude_mod, "PROMPT_VERSION", "0" * 8)
    assert ev.raw_check_name(run) == "strict"
    out = ev.rescore(run)["posts_dev"]
    assert out["records"][pid]["error"]
    assert out["records"][pid]["prediction"] is None
    assert out["scores"]["is_deal_request"]["error"] == stored_errors >= 1


def test_eval6_recorded_strict_survives_in_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    """postreview-1/EVAL-6: compare also holds a recorded-strict run to the strict check after a prompt change."""
    bad, pid = string_value_run("strict")
    good = {**dev_result(), "prompt_version": PROMPT_VERSION, "raw_check": "strict"}
    monkeypatch.setattr(claude_mod, "PROMPT_VERSION", "0" * 8)
    cmp = ev.compare_results(bad, good)[ev.DATASETS["posts"]]
    assert pid in cmp["fields"]["deal_value_usd"]["b_only"]


def test_eval6_recorded_legacy_on_a_current_prompt_run_is_ignored() -> None:
    """postreview-1/EVAL-6: 'legacy' recorded on a current-prompt run is ignored; the rescore stays strict."""
    run, pid = string_value_run("legacy")
    assert ev.raw_check_name(run) == "strict"
    assert ev.rescore(run)["posts_dev"]["records"][pid]["error"]
