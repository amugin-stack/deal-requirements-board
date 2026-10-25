"""Post-review round 4 (R4-EVAL-1, R4-EVAL-2): grafts/shallow history, and honest merges from main.

Written blind to the implementation, from the contracts. Helpers are copied from tests/test_fix_postreview3.py
(tests/ is not a package). Every repo is a tmp git repo under tmp_path laid out like the real one; its own
pyproject points pytest at its own tests/, so the guard's pytest never reaches the real suite.

Assumed names: cs.commits_since(rev), cs.history_check(commits), cs.guard_checks(), module constants ROOT /
LOOP_START_REV / BASELINE_REV, and a new guard check named 'history not rewritten'.
"""

import importlib.util
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

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
HISTORY_CHECK = "protected files untouched in history"
REWRITE_CHECK = "history not rewritten"


def failed(checks: Iterable[cs.Check]) -> set[str]:
    return {c.name for c in checks if not c.passed}


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
    """Commit only `paths`; returns HEAD."""
    git(root, "add", "--", *paths)
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def pin(monkeypatch: pytest.MonkeyPatch, loop_start: str, baseline: str) -> None:
    monkeypatch.setattr(cs, "LOOP_START_REV", loop_start, raising=False)
    monkeypatch.setattr(cs, "BASELINE_REV", baseline, raising=False)


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


def history(start: str) -> cs.Check:
    commits = cs.commits_since(start)
    assert commits is not None
    return cs.history_check(commits)


def net_diff(start: str) -> cs.Check:
    changes = cs.changes_since(start)
    assert changes is not None
    return cs.allow_list_check(changes)


def checks_by_name() -> dict[str, cs.Check]:
    return {c.name: c for c in cs.guard_checks()}


# ---- R4-EVAL-1: .git/info/grafts and shallow clones must not hide history ----


def grafted_history(root: Path) -> tuple[str, str]:
    """start S -> A (edits scoring.py) -> revert of A -> B (allowed extractor edit); returns (A, B)."""
    (root / "deal_board/scoring.py").write_text("TOLERANCE = 1.0\n")
    a = commit_paths(root, "A loosen scoring", "deal_board/scoring.py")
    git(root, "revert", "--no-edit", "HEAD")
    (root / "deal_board/extractors/claude.py").write_text('PROMPT = "b"\n')
    b = commit_paths(root, "B try b", "deal_board/extractors/claude.py")
    return a, b


def test_r4_eval1_grafts_do_not_hide_protected_commit(guard_repo: tuple[Path, str]) -> None:
    """postreview-4/R4-EVAL-1: a grafts file cutting A out of history still fails history naming scoring.py and A."""
    root, start = guard_repo
    a, b = grafted_history(root)
    (root / ".git/info").mkdir(parents=True, exist_ok=True)
    (root / ".git/info/grafts").write_text(f"{start} {a}\n{b} {start}\n")

    check = history(start)
    assert not check.passed, check.detail
    assert "deal_board/scoring.py" in check.detail
    assert a[:7] in check.detail


def test_r4_eval1_grafts_fail_history_not_rewritten(guard_repo: tuple[Path, str]) -> None:
    """postreview-4/R4-EVAL-1: with .git/info/grafts present, guard_checks() fails 'history not rewritten'."""
    root, start = guard_repo
    a, b = grafted_history(root)
    (root / ".git/info").mkdir(parents=True, exist_ok=True)
    (root / ".git/info/grafts").write_text(f"{start} {a}\n{b} {start}\n")

    checks = checks_by_name()
    assert REWRITE_CHECK in checks
    assert not checks[REWRITE_CHECK].passed
    assert not checks[HISTORY_CHECK].passed


def test_r4_eval1_control_no_grafts_history_fails_rewrite_passes(guard_repo: tuple[Path, str]) -> None:
    """postreview-4/R4-EVAL-1 control: same history without grafts fails history but passes 'history not rewritten'."""
    root, start = guard_repo
    a, _ = grafted_history(root)

    check = history(start)
    assert not check.passed
    assert a[:7] in check.detail
    checks = checks_by_name()
    assert REWRITE_CHECK in checks
    assert checks[REWRITE_CHECK].passed, checks[REWRITE_CHECK].detail
    assert not checks[HISTORY_CHECK].passed


def test_r4_eval1_shallow_clone_fails_history_not_rewritten(
    guard_repo: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """postreview-4/R4-EVAL-1: a `git clone --depth 1` checkout fails 'history not rewritten'."""
    root, _ = guard_repo
    (root / "loop/LESSONS.md").write_text("# Lessons\n- x\n")
    commit_paths(root, "lesson", "loop/LESSONS.md")
    dst = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{root}", str(dst)], check=True, capture_output=True, text=True
    )
    assert (dst / ".git/shallow").is_file()
    monkeypatch.setattr(cs, "ROOT", dst)

    checks = checks_by_name()
    assert REWRITE_CHECK in checks
    assert not checks[REWRITE_CHECK].passed


# ---- R4-EVAL-2: an honest merge from main into the loop branch passes history ----


def loop_branch_with_main_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """base -> loop: S (edits loop/check_success.py, pinned); main: allowed loop/state.json change.

    Returns (root, S), with the loop branch checked out and main not yet merged.
    """
    root = tmp_path / "merging"
    init_repo(root, guard_files())
    main = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    git(root, "checkout", "-q", "-b", "loop")
    (root / "loop/check_success.py").write_text("GATE = 2\n")
    s = commit_paths(root, "S setup", "loop/check_success.py")
    git(root, "checkout", "-q", main)
    (root / "loop/state.json").write_text('{"iteration": 1}\n')
    commit_paths(root, "main: state", "loop/state.json")
    git(root, "checkout", "-q", "loop")
    monkeypatch.setattr(cs, "ROOT", root)
    pin(monkeypatch, s, s)
    return root, s


def test_r4_eval2_honest_merge_from_main_passes_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """postreview-4/R4-EVAL-2: merging main (an allowed state.json change) into the loop branch passes history."""
    root, s = loop_branch_with_main_change(tmp_path, monkeypatch)
    main = "main" if git(root, "branch", "--list", "main") else "master"
    git(root, "merge", "-q", "--no-ff", "--no-edit", main)

    check = history(s)
    assert check.passed, check.detail


def test_r4_eval2_control_evil_merge_still_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """postreview-4/R4-EVAL-2 control: a merge of main that also appends to scoring.py still fails history."""
    root, s = loop_branch_with_main_change(tmp_path, monkeypatch)
    main = "main" if git(root, "branch", "--list", "main") else "master"
    git(root, "merge", "-q", "--no-ff", "--no-commit", main)
    with (root / "deal_board/scoring.py").open("a") as f:
        f.write("TOLERANCE = 1.0\n")
    git(root, "add", "deal_board/scoring.py")
    git(root, "commit", "-qm", "evil merge")

    check = history(s)
    assert not check.passed
    assert "deal_board/scoring.py" in check.detail
