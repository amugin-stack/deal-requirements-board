"""Post-review round 2 (R2-EVAL-1, R2-EVAL-2): look counting and tag pinning in the loop guard.

Written blind to the implementation, from the contracts. Helpers follow tests/test_fix_postreview1.py and
tests/test_fix_set_c2.py (copied, not imported: tests/ is not a package). Every repo is a tmp git repo under
tmp_path whose own tests/ is what the guard's pytest runs; the real label files are only read.

Assumed names: module constants LOOP_START_REV and BASELINE_REV (set here with monkeypatch, raising=False),
the new guard check 'tags unmoved', and the existing checks 'one look only' and 'runs reachable from HEAD'.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Iterable
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
GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
TAGS_CHECK = "tags unmoved"
PROTECTED_CHECK = "protected files unchanged"
HISTORY_CHECK = "protected files untouched in history"
REACHABLE_CHECK = "runs reachable from HEAD"
ONE_LOOK = "one look only"


def failed(checks: Iterable[cs.Check]) -> set[str]:
    return {c.name for c in checks if not c.passed}


def detail(checks: Iterable[cs.Check], name: str) -> str:
    return next(c.detail for c in checks if c.name == name)


def git(root: Path, *args: str) -> str:
    out = subprocess.run([*GIT, *args], cwd=root, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def init_repo(root: Path, files: dict[str, str]) -> str:
    """A git repo holding `files`, tagged loop-start and baseline-v1 at its only commit; returns HEAD."""
    write_files(root, files)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    git(root, "tag", "loop-start")
    git(root, "tag", "baseline-v1")
    return git(root, "rev-parse", "HEAD")


def commit_paths(root: Path, message: str, *paths: str) -> str:
    """Commit only `paths` (untracked results files stay untracked); returns HEAD."""
    git(root, "add", "--", *paths)
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def pin(monkeypatch: pytest.MonkeyPatch, loop_start: str, baseline: str) -> None:
    """Point the (not yet existing on the base) pinned revisions at the tmp repo's commits."""
    monkeypatch.setattr(cs, "LOOP_START_REV", loop_start, raising=False)
    monkeypatch.setattr(cs, "BASELINE_REV", baseline, raising=False)


# ---- honest fake runs built from the real labels, with post hashes as eval writes them ----


def posts_for(dataset: str, split: str) -> list[JSONObject]:
    posts = load_posts(ev.ROOT / ev.DATASETS[dataset])
    return posts if split == "all" else [p for p in posts if p.get("split") == split]


def records_for(posts: list[JSONObject]) -> dict[str, JSONObject]:
    call = {"cached": False, "input_tokens": 100, "output_tokens": 20, "latency_s": 0.1}
    out = {}
    for p in posts:
        raw = {"notes": None, **dict(p["label"])}
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


def dev_result(commit: str) -> JSONObject:
    posts = posts_for("posts", "dev")
    records = records_for(posts)
    ds = {"posts_dev": {"path": ev.DATASETS["posts"], "records": records, "scores": score(posts, records)}}
    return result(commit, ds)


def final_result(commit: str, *, dirty: bool = False) -> JSONObject:
    datasets = {}
    for name in ("holdout", "posts"):
        posts = posts_for(name, "all")
        records = records_for(posts)
        datasets[name] = {"path": ev.DATASETS[name], "records": records, "scores": score(posts, records)}
    return result(commit, datasets, split="all", dirty=dirty)


def run_path(root: Path, n: int) -> Path:
    return root / "results" / f"claude_20261101T{n:06d}Z.json"


def write_run(root: Path, n: int, run: JSONObject) -> Path:
    """Save as results/claude_<stamp>.json; a larger n is a newer run."""
    path = run_path(root, n)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**run, "timestamp": path.stem.removeprefix("claude_")}, indent=2))
    return path


def three_good_dev_runs(root: Path, head: str) -> None:
    for n in range(1, cs.REPEATS + 1):
        write_run(root, n, dev_result(head))


def gate(capsys: pytest.CaptureFixture[str], stage: str) -> set[str]:
    """Run check_success main() and return the names of the checks that failed."""
    capsys.readouterr()
    code = cs.main(["--stage", stage, "--json"])
    out = json.loads(capsys.readouterr().out)
    names = {c["name"] for c in out["checks"] if not c["passed"]}
    assert (code == 0) == (not names)
    return names


def stub_gate(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(cs, "ROOT", root)
    monkeypatch.setattr(cs, "guard_checks", list)
    monkeypatch.setattr(cs, "current_identity", lambda: cs.RunIdentity(*IDENTITY))
    monkeypatch.setattr(cs, "current_prompt_version", lambda: IDENTITY[0])


def copy_labels(root: Path) -> None:
    for name in ("posts", "holdout"):
        dest = root / ev.DATASETS[name]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ev.ROOT / ev.DATASETS[name], dest)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A tmp repo at loop-start (its only commit) with the label files; revs pinned there, guard stubbed."""
    root = tmp_path / "repo"
    copy_labels(root)
    head = init_repo(root, {"README.md": "tmp\n"})
    stub_gate(monkeypatch, root)
    pin(monkeypatch, head, head)
    return root, head


@pytest.fixture
def late_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, str]:
    """Like `repo`, but loop-start is the second commit, so a commit before it exists.

    A pre-loop final (recorded at that first commit) is committed into the loop-start tree.
    Returns (root, first commit, loop-start).
    """
    root = tmp_path / "late"
    copy_labels(root)
    first = init_repo(root, {"README.md": "tmp\n"})
    write_run(root, 50, final_result(first))
    (root / "README.md").write_text("tmp 2\n")
    start = commit_paths(root, "second", "README.md", "results")
    git(root, "tag", "-f", "loop-start", start)
    git(root, "tag", "-f", "baseline-v1", start)
    stub_gate(monkeypatch, root)
    pin(monkeypatch, start, start)
    return root, first, start


def peek_commit(root: Path, parent: str) -> str:
    """A commit with HEAD's tree whose parent is `parent` (not a descendant of loop-start)."""
    return git(root, "commit-tree", "HEAD^{tree}", "-p", parent, "-m", "peek")


# ---- guard fixture: a tiny repo laid out like the real one at loop-start ----

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
    """Its own pyproject points pytest at its own tests/, so the guard's pytest never reaches the real suite."""
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
    }
    for name in ("test_data", "test_eval", "test_brokers", "test_check_success"):
        files[f"tests/{name}.py"] = PASSING_TEST
    for new, old in MOVED.items():
        files[old] = files[new]
    return files


@pytest.fixture
def guard_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """Guard repo with both tags (and both pinned revs) at its only commit; returns (root, loop-start)."""
    root = tmp_path / "guarded"
    start = init_repo(root, guard_files())
    monkeypatch.setattr(cs, "ROOT", root)
    pin(monkeypatch, start, start)
    return root, start


def minimal_run(commit: str, *, split: str = "all", dirty: bool = False) -> JSONObject:
    return {"extractor": "claude", "split": split, "git": {"commit": commit, "dirty": dirty}, "datasets": {}}


@pytest.fixture
def late_guard_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, str]:
    """Guard repo whose loop-start is its second commit; a pre-loop final at the first commit is in the
    loop-start tree. Returns (root, first commit, loop-start)."""
    root = tmp_path / "late_guarded"
    first = init_repo(root, guard_files())
    write_run(root, 50, minimal_run(first))
    (root / "loop/LESSONS.md").write_text("# Lessons\n- one\n")
    start = commit_paths(root, "second", "loop/LESSONS.md", "results")
    git(root, "tag", "-f", "loop-start", start)
    git(root, "tag", "-f", "baseline-v1", start)
    monkeypatch.setattr(cs, "ROOT", root)
    pin(monkeypatch, start, start)
    return root, first, start


# ---- R2-EVAL-2: comparisons use the pinned revisions, and moved tags / replace refs fail ----


def protected_commit(root: Path) -> str:
    (root / "deal_board/scoring.py").write_text("TOLERANCE = 1.0\n")
    return commit_paths(root, "loosen scoring", "deal_board/scoring.py")


def test_r2_eval2_moving_loop_start_does_not_hide_a_protected_commit(guard_repo: tuple[Path, str]) -> None:
    """postreview-2/R2-EVAL-2: a protected commit then `git tag -f loop-start HEAD` still fails both protected checks."""
    root, _ = guard_repo
    protected_commit(root)
    git(root, "tag", "-f", "loop-start", "HEAD")
    checks = cs.guard_checks()
    assert {PROTECTED_CHECK, HISTORY_CHECK, TAGS_CHECK} <= failed(checks)
    assert "deal_board/scoring.py" in detail(checks, PROTECTED_CHECK)


def test_r2_eval2_moving_baseline_tag_fails_tags_unmoved(guard_repo: tuple[Path, str]) -> None:
    """postreview-2/R2-EVAL-2: moving baseline-v1 alone (to an allowed commit) fails 'tags unmoved'."""
    root, _ = guard_repo
    (root / "loop/LESSONS.md").write_text("# Lessons\n- x\n")
    commit_paths(root, "lesson", "loop/LESSONS.md")
    git(root, "tag", "-f", "baseline-v1", "HEAD")
    assert TAGS_CHECK in failed(cs.guard_checks())


def test_r2_eval2_earlier_final_still_counts_after_loop_start_moves(
    repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """postreview-2/R2-EVAL-2: a final made before loop-start was re-tagged forward still counts: 'one look only' fails."""
    root, head = repo
    write_run(root, 100, final_result(head))
    (root / "README.md").write_text("moved on\n")
    later = commit_paths(root, "later", "README.md")
    git(root, "tag", "-f", "loop-start", later)
    three_good_dev_runs(root, later)
    write_run(root, 101, final_result(later))
    assert ONE_LOOK in gate(capsys, "final")


def test_r2_eval2_replace_ref_does_not_hide_a_protected_commit(guard_repo: tuple[Path, str]) -> None:
    """postreview-2/R2-EVAL-2: `git replace <loop-start> <other>` fails 'tags unmoved'; protected checks see the real diff."""
    root, start = guard_repo
    edited = protected_commit(root)
    fake = git(root, "commit-tree", f"{edited}^{{tree}}", "-m", "fake loop-start")  # same tree as HEAD, no parent
    git(root, "replace", start, fake)
    checks = cs.guard_checks()
    assert {PROTECTED_CHECK, HISTORY_CHECK, TAGS_CHECK} <= failed(checks)
    assert "deal_board/scoring.py" in detail(checks, PROTECTED_CHECK)


def test_r2_eval2_control_untouched_tags_pass(guard_repo: tuple[Path, str]) -> None:
    """postreview-2/R2-EVAL-2 control: untouched tags and no replace refs -> 'tags unmoved' passes, nothing fails."""
    checks = cs.guard_checks()
    assert TAGS_CHECK in {c.name for c in checks}
    assert failed(checks) == set()


# ---- R2-EVAL-1: a final added since loop-start is a look even off loop-start's descendants ----


@pytest.mark.parametrize("dirty", [False, True])
def test_r2_eval1_off_branch_peek_counts_as_a_look(
    late_repo: tuple[Path, str, str], capsys: pytest.CaptureFixture[str], dirty: bool
) -> None:
    """postreview-2/R2-EVAL-1: a final at a commit not descending from loop-start plus a normal final fails 'one look only'."""
    root, first, start = late_repo
    peek = peek_commit(root, first)
    write_run(root, 100, final_result(peek, dirty=dirty))
    three_good_dev_runs(root, start)
    write_run(root, 101, final_result(start))
    assert ONE_LOOK in gate(capsys, "final")


@pytest.mark.parametrize("dirty", [False, True])
def test_r2_eval1_off_branch_peek_fails_reachable(late_guard_repo: tuple[Path, str, str], dirty: bool) -> None:
    """postreview-2/R2-EVAL-1: the guard's 'runs reachable from HEAD' fails naming an off-branch peek file."""
    root, first, _ = late_guard_repo
    path = write_run(root, 100, minimal_run(peek_commit(root, first), dirty=dirty))
    checks = cs.guard_checks()
    assert REACHABLE_CHECK in failed(checks)
    assert path.name in detail(checks, REACHABLE_CHECK)
    assert run_path(root, 50).name not in detail(checks, REACHABLE_CHECK)


def test_r2_eval1_control_pre_loop_final_in_tree_is_not_a_look(
    late_repo: tuple[Path, str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """postreview-2/R2-EVAL-1 control: a final in the loop-start tree is pre-loop history; one normal final passes."""
    root, _, start = late_repo
    three_good_dev_runs(root, start)
    write_run(root, 101, final_result(start))
    assert gate(capsys, "final") == set()


def test_r2_eval1_control_pre_loop_final_in_tree_is_not_flagged(late_guard_repo: tuple[Path, str, str]) -> None:
    """postreview-2/R2-EVAL-1 control: the guard flags nothing for a results file already in the loop-start tree."""
    checks = cs.guard_checks()
    assert REACHABLE_CHECK in {c.name for c in checks}
    assert failed(checks) == set()
