"""Fix set C2: the loop guard (loop/check_success.py) must count only fresh, independent, matching runs,
trust no stored score it can recompute, and allow-list what the loop may change.

Written blind to the implementation, from the contracts in review/fixes/dispositions.json.
Everything runs in tmp git repos under tmp_path: cs.ROOT points there. The real data files are only
read (dev and holdout labels, to build honest fake runs) and copied into the tmp repo. No API calls.

Seam for run identity: `cs.current_identity() -> RunIdentity(prompt_version, model, request_sha)`.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from deal_board.cli import eval as ev
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
OTHER_MODEL = "claude-other-model"
GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]


def failed(checks: Iterable[cs.Check]) -> set[str]:
    return {c.name for c in checks if not c.passed}


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


# ---- honest fake runs built from the real labels ----


def posts_for(dataset: str, split: str) -> list[JSONObject]:
    posts = load_posts(ev.ROOT / ev.DATASETS[dataset])
    return posts if split == "all" else [p for p in posts if p.get("split") == split]


def records_for(
    posts: list[JSONObject], *, cached: bool = False, edit: Callable[[JSONObject], JSONObject] | None = None
) -> dict[str, JSONObject]:
    """One record per post whose raw output is the post's label (optionally edited), as eval writes it."""
    call = {"cached": cached, "input_tokens": 100, "output_tokens": 20, "latency_s": 0.1}
    out = {}
    for p in posts:
        raw = dict(p["label"])
        out[p["id"]] = {**ev.to_record(edit(raw) if edit else raw), "call": dict(call)}
    return out


def result(
    commit: str,
    datasets: JSONObject,
    *,
    split: str = "dev",
    identity: tuple[str, str, str] = IDENTITY,
    with_request_sha: bool = True,
    cache_hits: int = 0,
) -> JSONObject:
    version, model, request_sha = identity
    r: JSONObject = {
        "extractor": "claude",
        "model": model,
        "prompt_version": version,
        "split": split,
        "timestamp": "20261101T000000Z",
        "git": {"commit": commit, "dirty": False},
        "usage": {"cache_hits": cache_hits, "api_calls": 0},
        "datasets": datasets,
    }
    if with_request_sha:
        r["request_sha"] = request_sha
    return r


def dev_result(
    commit: str,
    *,
    cached: bool = False,
    cache_hits: int = 0,
    edit: Callable[[JSONObject], JSONObject] | None = None,
    stored_against: list[JSONObject] | None = None,
    stored_scores: JSONObject | None = None,
    identity: tuple[str, str, str] = IDENTITY,
    with_request_sha: bool = True,
) -> JSONObject:
    """A dev run. Its stored scores are honest unless computed against other labels or overridden."""
    posts = posts_for("posts", "dev")
    records = records_for(posts, cached=cached, edit=edit)
    stored = stored_scores if stored_scores is not None else score(stored_against or posts, records)
    ds = {"posts_dev": {"path": ev.DATASETS["posts"], "records": records, "scores": stored}}
    return result(commit, ds, identity=identity, with_request_sha=with_request_sha, cache_hits=cache_hits)


def final_result(commit: str, *, identity: tuple[str, str, str] = IDENTITY) -> JSONObject:
    datasets = {}
    for name in ("holdout", "posts"):
        posts = posts_for(name, "all")
        records = records_for(posts)
        datasets[name] = {"path": ev.DATASETS[name], "records": records, "scores": score(posts, records)}
    return result(commit, datasets, split="all", identity=identity)


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


def test_c2_control_honest_runs_pass_dev_and_final(repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]) -> None:
    """Control for eval-quality/CQ-3, CQ-7, EM-1, EM-5, EM-21: three fresh matching dev runs and one final pass."""
    root, head = repo
    three_good_dev_runs(root, head)
    assert gate(capsys, "dev") == set()
    write_run(root, 100, final_result(head))
    assert gate(capsys, "final") == set()


# ---- eval-quality/EM-1: cache replays are not runs ----


def test_em1_dev_checks_fail_a_run_with_a_cached_record() -> None:
    """eval-quality/EM-1: any record replayed from the cache fails 'fresh runs'."""
    posts = posts_for("posts", "dev")
    fresh = dev_result("c")
    replay = dev_result("c", cached=True)
    assert failed(cs.dev_checks([fresh] * cs.REPEATS)) == set()
    assert failed(cs.dev_checks([fresh, replay, fresh])) == {"fresh runs"}
    one_cached = dev_result("c")
    one_cached["datasets"]["posts_dev"]["records"][posts[0]["id"]]["call"]["cached"] = True
    assert failed(cs.dev_checks([fresh, fresh, one_cached])) == {"fresh runs"}


def test_em1_dev_checks_fail_a_run_with_cache_hits_in_usage() -> None:
    """eval-quality/EM-1: usage.cache_hits > 0 fails 'fresh runs' even if no record says cached."""
    fresh = dev_result("c")
    assert failed(cs.dev_checks([dev_result("c", cache_hits=1), fresh, fresh])) == {"fresh runs"}


def test_em1_gate_rejects_replayed_dev_runs(repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]) -> None:
    """eval-quality/EM-1: runs 2 and 3 replaying run 1 from the cache do not pass the dev gate."""
    root, head = repo
    write_run(root, 1, dev_result(head))
    n_posts = len(posts_for("posts", "dev"))
    write_run(root, 2, dev_result(head, cached=True, cache_hits=n_posts))
    write_run(root, 3, dev_result(head, cached=True, cache_hits=n_posts))
    assert "fresh runs" in gate(capsys, "dev")


# ---- eval-quality/EM-21: no optional stopping ----


def test_em21_extra_reruns_fail_repeats_even_if_newest_three_are_good() -> None:
    """eval-quality/EM-21: 5 runs, oldest with a non-deal fill: the gate must not pass on the newest 3."""
    good = dev_result("c")
    posts = posts_for("posts", "dev")
    non_deal = next(p for p in posts if not p["label"]["is_deal_request"])
    hallucinated = dev_result("c", edit=lambda raw: {**raw, "state": "TX"} if raw == non_deal["label"] else raw)
    assert hallucinated["datasets"]["posts_dev"]["scores"]["non_deal_hallucinations"]["fields_filled"] > 0
    names = failed(cs.dev_checks([good] * 4 + [hallucinated]))
    assert "repeats" in names


def test_em21_gate_fails_with_more_than_repeats_runs(
    repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """eval-quality/EM-21: a prompt with REPEATS + 1 clean dev runs fails 'repeats' (rerun needs a new prompt)."""
    root, head = repo
    for n in range(1, cs.REPEATS + 2):
        write_run(root, n, dev_result(head))
    assert "repeats" in gate(capsys, "dev")


# ---- eval-quality/EM-5: rescores are not runs ----


def test_em5_rescores_do_not_fill_the_dev_gate(repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]) -> None:
    """eval-quality/EM-5: one dev run plus two rescores of it is one run, so 'repeats' fails."""
    root, head = repo
    source = write_run(root, 1, dev_result(head))
    saved = json.loads(source.read_text())
    git_stamp = {"commit": head, "dirty": False}
    for n in (2, 3):
        write_run(root, n, ev.rescored_result(saved, source, f"stamp{n}", git_stamp))
    assert len(cs.loop_runs("dev")) == 1
    assert "repeats" in gate(capsys, "dev")


def test_em5_rescore_of_the_final_is_not_a_second_look(
    repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """eval-quality/EM-5: a rescore of the split='all' run is not a second final run."""
    root, head = repo
    three_good_dev_runs(root, head)
    source = write_run(root, 100, final_result(head))
    saved = json.loads(source.read_text())
    write_run(root, 101, ev.rescored_result(saved, source, "stamp", {"commit": head, "dirty": False}))
    assert len(cs.loop_runs("all")) == 1
    assert gate(capsys, "final") == set()


# ---- eval-quality/CQ-3: run identity is (prompt_version, model, request_sha) ----


@pytest.mark.parametrize("which", ["model", "request_sha"])
def test_cq3_dev_gate_does_not_pool_runs_of_another_model_or_request(
    repo: tuple[Path, str], capsys: pytest.CaptureFixture[str], which: str
) -> None:
    """eval-quality/CQ-3: a run with the same prompt_version but another model/request_sha is not counted."""
    root, head = repo
    version, model, request_sha = IDENTITY
    other = (version, OTHER_MODEL, request_sha) if which == "model" else (version, model, "sha-other")
    write_run(root, 1, dev_result(head, identity=other))
    write_run(root, 2, dev_result(head))
    write_run(root, 3, dev_result(head))
    assert "repeats" in gate(capsys, "dev")


def test_cq3_legacy_runs_without_request_sha_never_match(
    repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """eval-quality/CQ-3: runs saved without request_sha cannot fill the dev gate."""
    root, head = repo
    for n in range(1, cs.REPEATS + 1):
        write_run(root, n, dev_result(head, with_request_sha=False))
    assert "repeats" in gate(capsys, "dev")


def test_cq3_final_on_another_model_did_not_pass_dev_first(
    repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """eval-quality/CQ-3: a final run on another model fails 'prompt passed dev first'."""
    root, head = repo
    three_good_dev_runs(root, head)
    write_run(root, 100, final_result(head, identity=(IDENTITY[0], OTHER_MODEL, IDENTITY[2])))
    assert "prompt passed dev first" in gate(capsys, "final")


# ---- eval-quality/CQ-7: the dev gate re-scores what it counts ----


def wrong_state(raw: JSONObject) -> JSONObject:
    """Predict a state the label does not have, on every post whose label has one."""
    if raw.get("state") is None:
        return raw
    return {**raw, "state": "VT" if raw["state"] != "VT" else "WY"}


def test_cq7_scores_stored_against_doctored_labels_fail_the_dev_gate(
    repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """eval-quality/CQ-7: stored scores computed against labels patched to match wrong outputs don't pass."""
    root, head = repo
    doctored = [{**p, "label": wrong_state(dict(p["label"]))} for p in posts_for("posts", "dev")]
    for n in range(1, cs.REPEATS + 1):
        write_run(root, n, dev_result(head, edit=wrong_state, stored_against=doctored))
    stored = json.loads((root / "results" / "claude_20261101T000001Z.json").read_text())
    assert stored["datasets"]["posts_dev"]["scores"]["fields"]["state"]["accuracy"] == 1.0  # fixture sanity
    names = gate(capsys, "dev")
    assert "stored scores match rescore" in names


def test_cq7_hand_edited_perfect_scores_block_fails(repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]) -> None:
    """eval-quality/CQ-7: a scores block edited to perfect over outputs with one wrong city fails the gate."""
    root, head = repo
    posts = posts_for("posts", "dev")
    target = next(p["label"] for p in posts if p["label"].get("city"))
    perfect = score(posts, records_for(posts))

    def one_wrong_city(raw: JSONObject) -> JSONObject:
        return {**raw, "city": "Nowhereville"} if raw == target else raw

    for n in range(1, cs.REPEATS + 1):
        write_run(root, n, dev_result(head, edit=one_wrong_city, stored_scores=perfect))
    assert gate(capsys, "dev") == {"stored scores match rescore"}


# ---- eval-quality/CQ-31 and CQ-7 (guard half): the guard is an allow-list ----

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
    """A tiny repo laid out like the real one at loop-start; its own trivial tests/ is what pytest runs."""
    root = tmp_path / "guarded"
    init_repo(root, guard_files())
    monkeypatch.setattr(cs, "ROOT", root)
    return root


def write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text)


def test_cq31_allowed_changes_pass_every_guard_check(guard_repo: Path) -> None:
    """eval-quality/CQ-31: claude.py, DRAFT backlog, LESSONS, state, new results and new tests are allowed."""
    root = guard_repo
    write(root, "deal_board/extractors/claude.py", 'PROMPT = "b"\n')
    write(root, "tests/test_new.py", PASSING_TEST)
    commit_all(root, "iteration 1")
    write(root, "loop/DRAFT.md", DRAFT.replace("1. a\n", "1. b\n2. c\n"))
    write(root, "loop/LESSONS.md", "# Lessons\n- one\n")
    write(root, "loop/state.json", '{"iteration": 1}\n')
    write(root, "results/claude_20261101T000001Z.json", "{}\n")
    write(root, "results/claude_20261101T000001Z.md", "# run\n")
    assert failed(cs.guard_checks()) == set()


def committed_scoring_package(root: Path) -> None:
    write(root, "scoring/__init__.py", "TOLERANCE = 1.0\n")
    commit_all(root, "shadow scoring.py")


def untracked_module(root: Path) -> None:
    write(root, "deal_board/sneaky.py", "X = 1\n")


def edited_unlisted_module(root: Path) -> None:
    write(root, "deal_board/matcher.py", "MATCH = 2\n")
    commit_all(root, "edit matcher")


def edited_existing_result(root: Path) -> None:
    write(root, "results/claude_20260101T000000Z.json", '{"edited": true}\n')


def new_pytest_ini(root: Path) -> None:
    write(root, "pytest.ini", "[pytest]\naddopts = -k nothing\n")


def protected_commit_then_revert(root: Path) -> None:
    write(root, "deal_board/scoring.py", "TOLERANCE = 1.0\n")
    commit_all(root, "loosen scoring")
    write(root, "deal_board/scoring.py", "TOLERANCE = 0.01\n")
    commit_all(root, "revert scoring")


@pytest.mark.parametrize(
    "change",
    [committed_scoring_package, untracked_module, edited_unlisted_module, edited_existing_result, new_pytest_ini],
)
def test_cq31_changes_outside_the_allow_list_fail_protected(guard_repo: Path, change: Callable[[Path], None]) -> None:
    """eval-quality/CQ-31: any changed or added path outside the allow-list fails 'protected files unchanged'."""
    change(guard_repo)
    assert "protected files unchanged" in failed(cs.guard_checks())


def test_cq7_guard_fails_on_a_protected_commit_later_reverted(guard_repo: Path) -> None:
    """eval-quality/CQ-7: a commit since loop-start touching a protected path fails, though the net diff is empty."""
    protected_commit_then_revert(guard_repo)
    assert git(guard_repo, "diff", "loop-start", "--", "deal_board/scoring.py") == ""  # fixture sanity
    # The net diff is clean, so the history check (not the net-diff one) is what must catch it.
    assert failed(cs.guard_checks()) == {"protected files untouched in history"}


SKIP_ALL = "import pytest\n\n\ndef pytest_collection_modifyitems(items):\n" + (
    "    for item in items:\n        item.add_marker(pytest.mark.skip(reason='hidden'))\n"
)
DESELECT_ONE = "def pytest_collection_modifyitems(config, items):\n" + (
    "    config.hook.pytest_deselected(items=items[:1])\n    items[:] = items[1:]\n"
)
TWO_SKIPS = "import pytest\n\n\n" + "".join(
    f"@pytest.mark.skip(reason='later')\ndef test_skipped_{i}() -> None:\n    assert False\n\n\n" for i in range(2)
)


@pytest.mark.parametrize("conftest", [SKIP_ALL, DESELECT_ONE], ids=["skip_all", "deselect_one"])
def test_cq31_conftest_that_hides_tests_fails_tests_pass(guard_repo: Path, conftest: str) -> None:
    """eval-quality/CQ-31: a new tests/conftest.py that skips or deselects tests fails 'tests pass'."""
    write(guard_repo, "tests/conftest.py", conftest)
    commit_all(guard_repo, "conftest")
    assert "tests pass" in failed(cs.guard_checks())


def test_cq31_extra_skips_in_an_allowed_test_file_fail_tests_pass(guard_repo: Path) -> None:
    """eval-quality/CQ-31: more skips than the one known skip fails 'tests pass', even from an allowed file."""
    write(guard_repo, "tests/test_extra.py", TWO_SKIPS)
    commit_all(guard_repo, "skipped tests")
    assert failed(cs.guard_checks()) == {"tests pass"}
