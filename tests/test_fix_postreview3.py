"""Post-review round 3 (R3-EVAL-2, R3-EVAL-3): history the guard can't see, and a trusted loop start.

Written blind to the implementation, from the contracts. Helpers are copied from tests/test_fix_postreview2.py
(tests/ is not a package). Every repo is a tmp git repo under tmp_path laid out like the real one; its own
pyproject points pytest at its own tests/, so the guard's pytest never reaches the real suite.

Assumed names: cs.commits_since(rev), cs.history_check(commits), cs.changes_since(rev), cs.allow_list_check,
module constants LOOP_START_REV / BASELINE_REV, and main()'s new `--loop-start <sha>` option.
"""

import importlib.util
import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from deal_board.schema import JSONObject

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
UNRELATED_PIN = "f" * 40  # a file pin that names nothing in the tmp repo


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


def minimal_run(commit: str, *, split: str = "dev") -> JSONObject:
    return {"extractor": "claude", "split": split, "git": {"commit": commit, "dirty": False}, "datasets": {}}


def history(start: str) -> cs.Check:
    commits = cs.commits_since(start)
    assert commits is not None
    return cs.history_check(commits)


def net_diff(start: str) -> cs.Check:
    changes = cs.changes_since(start)
    assert changes is not None
    return cs.allow_list_check(changes)


# ---- R3-EVAL-2: merges and unrelated roots are seen by the history check ----


def merge_side(root: Path, main: str, side: str) -> None:
    """Start a --no-ff merge of a fresh side branch (one empty commit) into `main`, left uncommitted."""
    git(root, "checkout", "-q", "-b", side)
    git(root, "commit", "-q", "--allow-empty", "-m", side)
    git(root, "checkout", "-q", main)
    git(root, "merge", "-q", "--no-ff", "--no-commit", side)


def test_r3_eval2_evil_merge_is_seen_in_history(guard_repo: tuple[Path, str]) -> None:
    """postreview-3/R3-EVAL-2: a protected edit inside a merge commit, later restored, fails history naming M1."""
    root, start = guard_repo
    main = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    merge_side(root, main, "side")
    with (root / "deal_board/scoring.py").open("a") as f:
        f.write("TOLERANCE = 1.0\n")
    m1 = commit_paths(root, "M1", "deal_board/scoring.py")
    merge_side(root, main, "side2")
    git(root, "checkout", start, "--", "deal_board/scoring.py")
    commit_paths(root, "M2", "deal_board/scoring.py")

    assert net_diff(start).passed  # the net diff is clean
    check = history(start)
    assert not check.passed
    assert m1[:7] in check.detail


def test_r3_eval2_unrelated_root_merged_then_removed_fails_history(guard_repo: tuple[Path, str]) -> None:
    """postreview-3/R3-EVAL-2: an unrelated root adding conftest.py, merged in and later removed (in a merge), fails history."""
    root, start = guard_repo
    main = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    git(root, "checkout", "-q", "--orphan", "other")
    git(root, "rm", "-rfq", ".")
    (root / "conftest.py").write_text("collect_ignore = ['tests']\n")
    commit_paths(root, "root", "conftest.py")
    git(root, "checkout", "-q", main)
    git(root, "merge", "-q", "--no-edit", "--allow-unrelated-histories", "other")
    merge_side(root, main, "side")  # removed inside a merge, so no plain commit shows a conftest.py change
    git(root, "rm", "-q", "conftest.py")
    git(root, "commit", "-qm", "drop conftest")

    assert net_diff(start).passed  # the net diff is clean
    assert not history(start).passed


def test_r3_eval2_control_honest_revert_pair_and_new_test_pass(guard_repo: tuple[Path, str]) -> None:
    """postreview-3/R3-EVAL-2 control: an extractor change and its revert plus an added tests/test_x.py pass history."""
    root, start = guard_repo
    (root / "deal_board/extractors/claude.py").write_text('PROMPT = "b"\n')
    commit_paths(root, "try b", "deal_board/extractors/claude.py")
    git(root, "revert", "--no-edit", "HEAD")
    (root / "tests/test_x.py").write_text(PASSING_TEST)
    commit_paths(root, "add test", "tests/test_x.py")

    check = history(start)
    assert check.passed, check.detail
    assert net_diff(start).passed


def test_r3_eval2_control_modified_results_file_still_fails(guard_repo: tuple[Path, str]) -> None:
    """postreview-3/R3-EVAL-2 control: a committed results/claude_*.json later modified still fails history."""
    root, start = guard_repo
    path = root / "results" / "claude_20261101T000001Z.json"
    path.parent.mkdir()
    path.write_text(json.dumps(minimal_run(start)))
    commit_paths(root, "run", "results")
    path.write_text(json.dumps({**minimal_run(start), "datasets": {"x": {}}}))
    commit_paths(root, "doctor run", "results")

    assert not history(start).passed


# ---- R3-EVAL-3: main() --loop-start <sha> replaces the file pin ----


@pytest.fixture
def unpinned_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """Guard repo tagged at its only commit C, but cs.LOOP_START_REV pinned elsewhere; dev side of main() stubbed.

    Returns (root, C). BASELINE_REV is C, so only the loop-start pin is wrong.
    """
    root = tmp_path / "unpinned"
    start = init_repo(root, guard_files())
    monkeypatch.setattr(cs, "ROOT", root)
    pin(monkeypatch, UNRELATED_PIN, start)
    monkeypatch.setattr(cs, "current_prompt_version", lambda: IDENTITY[0])
    monkeypatch.setattr(cs, "current_identity", lambda: cs.RunIdentity(*IDENTITY))
    monkeypatch.setattr(cs, "dev_runs_for", lambda identity: [])
    monkeypatch.setattr(cs, "dev_checks", lambda runs, rescore=None: [])
    return root, start


def run_main(capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, dict[str, bool]]:
    """main(['--stage', 'dev', '--json', *extra]) -> (exit code, {check name: passed})."""
    capsys.readouterr()
    code = cs.main(["--stage", "dev", "--json", *extra])
    out = json.loads(capsys.readouterr().out)
    return code, {c["name"]: c["passed"] for c in out["checks"]}


def test_r3_eval3_loop_start_option_overrides_file_pin(
    unpinned_repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """postreview-3/R3-EVAL-3: with --loop-start C (file pin elsewhere) every guard check passes, main exits 0."""
    _, start = unpinned_repo
    code, checks = run_main(capsys, "--loop-start", start)
    assert checks[PROTECTED_CHECK] and checks[HISTORY_CHECK] and checks[TAGS_CHECK]
    assert [name for name, ok in checks.items() if not ok] == []
    assert code == 0


def test_r3_eval3_moved_tag_fails_against_given_loop_start(
    unpinned_repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """postreview-3/R3-EVAL-3: `git tag -f loop-start <other>` fails 'tags unmoved' against --loop-start C."""
    root, start = unpinned_repo
    (root / "loop/LESSONS.md").write_text("# Lessons\n- x\n")
    commit_paths(root, "lesson", "loop/LESSONS.md")
    git(root, "tag", "-f", "loop-start", "HEAD")
    code, checks = run_main(capsys, "--loop-start", start)
    assert not checks[TAGS_CHECK]
    assert checks[PROTECTED_CHECK] and checks[HISTORY_CHECK]
    assert code != 0


def test_r3_eval3_protected_commit_fails_against_given_loop_start(
    unpinned_repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """postreview-3/R3-EVAL-3: a committed scoring.py edit fails both protected checks against --loop-start C."""
    root, start = unpinned_repo
    (root / "deal_board/scoring.py").write_text("TOLERANCE = 1.0\n")
    commit_paths(root, "loosen scoring", "deal_board/scoring.py")
    code, checks = run_main(capsys, "--loop-start", start)
    assert not checks[PROTECTED_CHECK] and not checks[HISTORY_CHECK]
    assert checks[TAGS_CHECK]
    assert code != 0


@pytest.mark.parametrize("kind", ["ref", "short", "unknown", "tree"])
def test_r3_eval3_loop_start_must_be_full_commit_sha(
    unpinned_repo: tuple[Path, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """postreview-3/R3-EVAL-3: a ref name, short sha, unknown sha or non-commit object exits non-zero, no checks run."""
    root, start = unpinned_repo
    value = {
        "ref": "loop-start",
        "short": start[:12],
        "unknown": "0123456789abcdef" * 2 + "01234567",
        "tree": git(root, "rev-parse", f"{start}^{{tree}}"),
    }[kind]
    ran: list[bool] = []

    def no_guard(*args: object, **kwargs: object) -> list[cs.Check]:
        ran.append(True)
        return []

    monkeypatch.setattr(cs, "guard_checks", no_guard)
    capsys.readouterr()
    try:
        code: object = cs.main(["--stage", "dev", "--loop-start", value])
    except SystemExit as exc:
        code = exc.code
    assert code not in (0, None)
    assert not ran
    assert "[PASS]" not in capsys.readouterr().out


def test_r3_eval3_control_default_uses_file_pin(
    unpinned_repo: tuple[Path, str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """postreview-3/R3-EVAL-3 control: without --loop-start, main uses cs.LOOP_START_REV (pinned to C here)."""
    _, start = unpinned_repo
    monkeypatch.setattr(cs, "LOOP_START_REV", start)
    code, checks = run_main(capsys)
    assert checks[PROTECTED_CHECK] and checks[HISTORY_CHECK] and checks[TAGS_CHECK]
    assert code == 0
