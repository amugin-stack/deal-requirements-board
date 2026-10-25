"""Is this results file a clean final look at data/holdout_v3.jsonl? PASS/FAIL per check.

Why a separate validator: run.py can only vouch for itself at run time. Whoever reads the README
later needs to check the committed file on its own terms (right split, all 25 posts, clean
committed tree, scores that really follow from the stored predictions), without trusting the
script that wrote it. Every check is printed so a FAIL is reported rather than quietly fixed;
in particular a FAIL on `errors` or `non_deal` is a *result* of the run, never a reason to rerun.
The check names and their order are fixed by tools/final_eval/PROTOCOL.md.

Exit codes: 0 every check passes; 1 some check fails; 2 the file can't be read or isn't a JSON
object (one `ERROR: ` line).
"""

import argparse
import json
import math
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deal_board import paths
from deal_board.cli import eval as ev
from deal_board.schema import KEY_FIELDS, JSONObject
from deal_board.scoring import SCORED_FIELDS, load_posts, score

ROOT = paths.REPO_ROOT
SPLIT = "holdout_v3"
DEFAULT_PROMPT = "ac1a145b"
EXPECTED_POSTS = 25
HEX_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass(frozen=True)
class Check:
    """One verdict; kept as data so tests and callers can inspect it without parsing output."""

    name: str
    ok: bool
    detail: str


def _git_ok(*args: str) -> subprocess.CompletedProcess[str] | None:
    """Run git in ROOT; None if git itself can't run, which must read as "not verified"."""
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    except OSError:
        return None


def is_ancestor(commit: str) -> bool:
    """Whether the run's commit is in main's history (or is HEAD, before main is updated).

    A commit that isn't on main could carry an edited extractor or scorer. Only well-formed hex
    reaches git, so a crafted string can never be read as an option or revision expression.
    """
    if not HEX_COMMIT.fullmatch(commit):
        return False
    r = _git_ok("merge-base", "--is-ancestor", commit, "main")
    if r is not None and r.returncode == 0:
        return True
    head = _git_ok("rev-parse", "HEAD")
    return head is not None and head.returncode == 0 and head.stdout.strip() == commit


@dataclass(frozen=True)
class Hooks:
    """The validator's two outside dependencies, injectable so tests use synthetic labels and git."""

    data_path: Path
    is_ancestor: Callable[[str], bool]


def default_hooks() -> Hooks:
    """The real label file and the real repository history."""
    return Hooks(data_path=ROOT / "data/holdout_v3.jsonl", is_ancestor=is_ancestor)


def dataset_path_string(path: Path) -> str:
    """Formatted exactly as run.py (and cli/eval.py) record it, so the comparison is like for like."""
    absolute = Path(path).absolute()
    return absolute.relative_to(ROOT).as_posix() if absolute.is_relative_to(ROOT) else str(absolute)


def _dataset(result: JSONObject) -> JSONObject:
    """The holdout_v3 dataset block; raises on a malformed file so the calling check FAILs."""
    ds = result["datasets"][SPLIT]
    if not isinstance(ds, dict):
        raise TypeError("datasets.holdout_v3 is not an object")
    return ds


def _records(result: JSONObject) -> dict[str, JSONObject]:
    """The stored records; raises on a malformed file so the calling check FAILs."""
    records = _dataset(result)["records"]
    if not isinstance(records, dict) or not all(isinstance(r, dict) for r in records.values()):
        raise TypeError("records is not an object of objects")
    return records


def _ids(xs: list[str]) -> str:
    """Sorted, so the detail line is stable across runs and diffable in the README."""
    return ev.ids(sorted(xs))


def _is_number(x: Any) -> bool:
    """A real, finite number: bool is an int subclass and NaN compares false both ways."""
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def _check_extractor(result: JSONObject, ctx: _Context) -> tuple[bool, str]:
    """Only the two extractors the protocol allows produce a final number."""
    value = result["extractor"]
    return value in ("regex", "claude"), f"extractor is {value!r}"


def _check_split(result: JSONObject, ctx: _Context) -> tuple[bool, str]:
    """A file from any other split is not a look at holdout_v3 at all."""
    value = result["split"]
    return value == SPLIT, f"split is {value!r}"


def _check_datasets(result: JSONObject, ctx: _Context) -> tuple[bool, str]:
    """A second dataset or a different file would mix sets or score the wrong labels."""
    datasets = result["datasets"]
    if not isinstance(datasets, dict) or list(datasets) != [SPLIT]:
        keys = list(datasets) if isinstance(datasets, dict) else datasets
        return False, f"datasets keys are {keys!r}, expected exactly [{SPLIT!r}]"
    path = _dataset(result)["path"]
    return path == ctx.expected_path, f"path is {path!r}, expected {ctx.expected_path!r}"


def _check_records(result: JSONObject, ctx: _Context) -> tuple[bool, str]:
    """A missing or extra post means the number isn't over the frozen 25-post set."""
    got, want = set(_records(result)), ctx.label_ids
    n = len(_records(result))
    if n != EXPECTED_POSTS:
        return False, f"{n} records, expected {EXPECTED_POSTS}"
    if got != want:
        return (
            False,
            f"ids differ from the label file: missing {_ids(list(want - got))}, extra {_ids(list(got - want))}",
        )
    return True, f"{n} records, ids match the label file"


def _check_errors(result: JSONObject, ctx: _Context) -> tuple[bool, str]:
    """Errors count as wrong; listing them keeps a low score explainable, never rerun."""
    bad = [pid for pid, r in _records(result).items() if r.get("error")]
    return not bad, f"{len(bad)} records with an error ({_ids(bad)})"


def _check_complete(result: JSONObject, ctx: _Context) -> tuple[bool, str]:
    """A partial run is still a look, but its number is not the full-set result."""
    fe = result["final_eval"]
    complete, aborted = fe["complete"], fe["aborted"]
    return complete is True and aborted is None, f"complete={complete!r}, aborted={aborted!r}"


def _check_git_clean(result: JSONObject, ctx: _Context) -> tuple[bool, str]:
    """Uncommitted code at run time means nobody can reproduce what produced the number."""
    dirty = result["git"]["dirty"]
    return dirty is False, f"git.dirty is {dirty!r}"


def _check_git_ancestor(result: JSONObject, ctx: _Context) -> tuple[bool, str]:
    """The run must come from code that is (or will be) on main, not a side branch."""
    commit = result["git"]["commit"]
    if not isinstance(commit, str) or not commit:
        return False, f"git.commit is {commit!r}, not a commit id"
    ok = ctx.is_ancestor(commit)
    return ok, f"commit {commit[:12]} {'is' if ok else 'is NOT'} in main's history"


def _check_prompt(result: JSONObject, ctx: _Context) -> tuple[bool, str]:
    """The headline names a prompt version; the file must be from that prompt."""
    extractor, version = result["extractor"], result["prompt_version"]
    if extractor == "claude":
        return version == ctx.expect_prompt, f"prompt_version is {version!r}, expected {ctx.expect_prompt!r}"
    if extractor == "regex":
        return version is None, f"prompt_version is {version!r}, expected null for regex"
    return False, f"unknown extractor {extractor!r}"


def _check_scores(result: JSONObject, ctx: _Context) -> tuple[bool, str]:
    """Stored scores could be edited by hand; only predictions-plus-labels are trusted."""
    records = _records(result)
    if set(records) != ctx.label_ids:
        return False, "record ids don't match the label file, so the scores can't be recomputed"
    # The JSON round trip makes the recomputed scores compare like the stored (parsed) ones.
    recomputed = json.loads(json.dumps(score(ctx.posts, records)))
    stored = _dataset(result)["scores"]
    return stored == recomputed, (
        "stored scores match a recomputation" if stored == recomputed else "stored scores differ from a recomputation"
    )


def _check_intervals(result: JSONObject, ctx: _Context) -> tuple[bool, str]:
    """The README reports every rate with its interval; a missing or inverted one is a bug."""
    scores = _dataset(result)["scores"]
    bad = []
    for field in SCORED_FIELDS:
        cell = ev.field_cell(scores, field)
        acc, ci = cell.get("accuracy"), cell.get("ci95")
        ok = (
            _is_number(acc)
            and isinstance(ci, list)
            and len(ci) == 2
            and all(_is_number(x) for x in ci)
            and 0 <= ci[0] <= acc <= ci[1] <= 1
        )
        if not ok:
            bad.append(field)
    return not bad, (
        f"all {len(SCORED_FIELDS)} fields have accuracy inside its ci95" if not bad else f"bad intervals: {_ids(bad)}"
    )


def _check_non_deal(result: JSONObject, ctx: _Context) -> tuple[bool, str]:
    """Filling fields on non-deal posts is the hallucination the project most cares about."""
    records = _records(result)
    absent = [pid for pid in ctx.non_deal_ids if pid not in records]
    if absent:
        return False, f"no record for non-deal posts {_ids(absent)}"
    filled, posts_with_any = 0, []
    for pid in ctx.non_deal_ids:
        pred = records[pid].get("prediction")
        n = sum(1 for f in KEY_FIELDS if pred.get(f) is not None) if isinstance(pred, dict) else 0
        filled += n
        if n:
            posts_with_any.append(pid)
    return filled == 0, f"{filled} fields filled on non-deal posts ({_ids(posts_with_any)})"


@dataclass(frozen=True)
class _Context:
    """What every check may compare against, computed once from the label file."""

    posts: list[JSONObject]
    label_ids: set[str]
    non_deal_ids: list[str]
    expected_path: str
    is_ancestor: Callable[[str], bool]
    expect_prompt: str


CHECKS: tuple[tuple[str, Callable[[JSONObject, _Context], tuple[bool, str]]], ...] = (
    ("extractor", _check_extractor),
    ("split", _check_split),
    ("datasets", _check_datasets),
    ("records", _check_records),
    ("errors", _check_errors),
    ("complete", _check_complete),
    ("git_clean", _check_git_clean),
    ("git_ancestor", _check_git_ancestor),
    ("prompt", _check_prompt),
    ("scores", _check_scores),
    ("intervals", _check_intervals),
    ("non_deal", _check_non_deal),
)


def validate(
    result: JSONObject,
    posts: list[JSONObject],
    *,
    expected_path: str,
    is_ancestor: Callable[[str], bool],
    expect_prompt: str,
) -> list[Check]:
    """All checks, in order. A malformed file FAILs the affected checks instead of crashing,
    because a validator that dies on a bad file reports nothing about the rest of it."""
    ctx = _Context(
        posts=posts,
        label_ids={str(p["id"]) for p in posts},
        non_deal_ids=[str(p["id"]) for p in posts if not p["label"]["is_deal_request"]],
        expected_path=expected_path,
        is_ancestor=is_ancestor,
        expect_prompt=expect_prompt,
    )
    out: list[Check] = []
    for name, fn in CHECKS:
        try:
            ok, detail = fn(result, ctx)
        except Exception as e:
            ok, detail = False, f"malformed file: {type(e).__name__}: {e}"
        out.append(Check(name, bool(ok), detail))
    return out


def main(argv: list[str] | None = None, hooks: Hooks | None = None) -> int:
    """Print every verdict, then the overall one. Returns the exit code so tests can drive it."""
    ap = argparse.ArgumentParser(description="Validate a final-eval results file (see tools/final_eval/PROTOCOL.md).")
    ap.add_argument("results_json", type=Path)
    ap.add_argument(
        "--expect-prompt", default=DEFAULT_PROMPT, help=f"required Claude prompt version ({DEFAULT_PROMPT})"
    )
    args = ap.parse_args(argv)
    hooks = default_hooks() if hooks is None else hooks

    try:
        result = json.loads(args.results_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"ERROR: can't read {args.results_json}: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    if not isinstance(result, dict):
        print(f"ERROR: {args.results_json} is not a JSON object", file=sys.stderr)
        return 2
    try:
        posts = load_posts(hooks.data_path)
    except (OSError, ValueError) as e:
        print(f"ERROR: can't read the labels {hooks.data_path}: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    checks = validate(
        result,
        posts,
        expected_path=dataset_path_string(hooks.data_path),
        is_ancestor=hooks.is_ancestor,
        expect_prompt=args.expect_prompt,
    )
    for c in checks:
        print(f"{'PASS' if c.ok else 'FAIL'} {c.name}: {c.detail}")
    failed = sum(1 for c in checks if not c.ok)
    print("VALID" if not failed else f"INVALID ({failed} failed)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
