"""Single definition of success for the fix loop over the three review loops' findings.

  python review/fixes/check_success.py            # compare against branch `main`
  python review/fixes/check_success.py --base X   # compare against another base ref

Exits 0 only when every check passes; prints one PASS/FAIL line per check, like the other
`review/*/check_success.py` scripts. The checks:

  (a) dispositions.json has exactly one entry per confirmed finding across the three
      review/*/findings.json files, keyed by (review, id) because ids collide across reviews;
      every entry is fixed/wontfix/deferred with a note; every `fixed` entry names an existing
      pytest node id that passes and a commit on this branch;
  (b) every medium (or high) finding is fixed, or deferred with a note starting
      "owner decision" or "changes PROMPT_VERSION";
  (c) tests/test_red_team.py contains no xfail and the five formerly-xfailed cases pass;
  (d) the full suite: 0 failed, 0 errors, 0 xfailed, 0 xpassed, at least 1003 passed;
  (e) ruff check, ruff format --check and mypy --strict are clean;
  (f) the frozen baseline is byte-identical to tag baseline-v1 (committed and on disk);
  (g) PROMPT_VERSION is still ac1a145b;
  (h) results/, data/ and the three review loops' directories are untouched vs base
      (committed, staged, unstaged and untracked);
  (i) this script is unchanged since the first commit on the branch;
  (j) POSTREVIEW.md records at least one post-fix review round and the last one says
      `new_medium_or_high: 0`.

Run it with the worktree's interpreter (`.venv/bin/python`); pytest, ruff and mypy are run as
modules of that same interpreter. This file is frozen after the branch's first commit; (i)
enforces that.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
THIS_SCRIPT = "review/fixes/check_success.py"
DISPOSITIONS = HERE / "dispositions.json"
POSTREVIEW = HERE / "POSTREVIEW.md"
RED_TEAM = ROOT / "tests" / "test_red_team.py"

REVIEWS = ("correctness-api", "compliance-security", "eval-quality")
FINAL_DISPOSITIONS = frozenset({"fixed", "wontfix", "deferred"})
MEDIUM_DEFERRAL_PREFIXES = ("owner decision", "changes PROMPT_VERSION")
BLOCKING_SEVERITIES = frozenset({"medium", "high"})
MIN_PASSED = 1003
EXPECTED_PROMPT_VERSION = "ac1a145b"
FROZEN_TAG_PATH = "baseline-v1:extractors/regex_baseline.py"
FROZEN_PATH = "deal_board/extractors/frozen/regex_baseline.py"
UNTOUCHED = ("results/", "data/", "review/correctness-api", "review/compliance-security", "review/eval-quality")

# The five strict xfails on main; each must now pass outright.
FORMER_XFAILS = (
    "tests/test_red_team.py::test_regex_post_never_returns_unlicensed_broker[rt-20]",
    "tests/test_red_team.py::test_hostile_extractor_cannot_slip_past_gate[smuggled-state-not-a-state]",
    "tests/test_red_team.py::test_hostile_extractor_cannot_slip_past_gate[injection-in-city-with-follow-up]",
    "tests/test_red_team.py::test_hostile_extractor_cannot_slip_past_gate[injection-in-city-no-state]",
    "tests/test_red_team.py::test_hostile_extractor_cannot_slip_past_gate[smuggled-is-deal-request-no]",
)

ROUND_HEADING = re.compile(r"^##\s+Round\s+(\d+)\b", re.MULTILINE)
NEW_MEDIUM_OR_HIGH = re.compile(r"new_medium_or_high:\s*`?(\d+)`?")
SUMMARY_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|xfailed|xpassed|skipped)")


@dataclass(frozen=True)
class Check:
    """One named success criterion and why it passed or failed."""

    name: str
    passed: bool
    detail: str


def run(*cmd: str) -> subprocess.CompletedProcess[str]:
    """Run a command in the worktree root and capture its output."""
    return subprocess.run(list(cmd), cwd=ROOT, capture_output=True, text=True)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run git in the worktree root."""
    return run("git", *args)


def pytest(*args: str) -> subprocess.CompletedProcess[str]:
    """Run pytest with this interpreter, no cache, short summary of every outcome."""
    return run(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA", *args)


def summary_counts(output: str) -> dict[str, int]:
    """Outcome counts from pytest's final summary line ('3 passed, 1 skipped in 0.1s')."""
    lines = [ln for ln in output.strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    counts: dict[str, int] = {}
    for n, kind in SUMMARY_COUNTS.findall(last):
        counts["errors" if kind.startswith("error") else kind] = int(n)
    return counts


def passed_nodes(output: str) -> set[str]:
    """Node ids listed as PASSED in a `-rA` short summary."""
    return {ln[len("PASSED ") :].strip() for ln in output.splitlines() if ln.startswith("PASSED ")}


def node_passed(node: str, passed: set[str]) -> bool:
    """A node id passed itself, or (for an unparametrised name) every reported param of it passed."""
    return node in passed or any(p.startswith(node + "[") for p in passed)


def nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def load_confirmed() -> tuple[dict[tuple[str, str], str] | None, str]:
    """(review, id) -> severity for every confirmed finding, or None plus why it failed to load."""
    confirmed: dict[tuple[str, str], str] = {}
    for review in REVIEWS:
        path = ROOT / "review" / review / "findings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return None, f"cannot load {path.relative_to(ROOT)}: {type(e).__name__}"
        for entry in data:
            if entry.get("status") == "confirmed":
                confirmed[(review, entry["id"])] = entry["severity"]
    return confirmed, ""


def load_dispositions() -> tuple[list[dict[str, Any]] | None, str]:
    """The dispositions list, or None plus the reason it could not be loaded."""
    try:
        data = json.loads(DISPOSITIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"cannot load {DISPOSITIONS.name}: {type(e).__name__}: {e}"
    if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
        return None, f"{DISPOSITIONS.name} must be a JSON array of objects"
    return data, ""


def entry_problems(entry: dict[str, Any]) -> list[str]:
    """Shape problems with one disposition entry; empty means well-formed."""
    key = f"{entry.get('review', '?')}/{entry.get('id', '?')}"
    problems = []
    if not (nonempty_str(entry.get("review")) and nonempty_str(entry.get("id"))):
        problems.append(f"{key}: needs review and id")
    if entry.get("disposition") not in FINAL_DISPOSITIONS:
        problems.append(f"{key}: disposition {entry.get('disposition')!r} not in {sorted(FINAL_DISPOSITIONS)}")
    if not nonempty_str(entry.get("note")):
        problems.append(f"{key}: needs a note")
    if entry.get("disposition") == "fixed":
        test = entry.get("regression_test")
        if not (nonempty_str(test) and "::" in test):
            problems.append(f"{key}: fixed needs regression_test as a pytest node id")
        if not nonempty_str(entry.get("commit")):
            problems.append(f"{key}: fixed needs a commit sha")
    return problems


def branch_commits(base: str) -> set[str]:
    """Full shas of the commits on this branch beyond base."""
    return set(git("rev-list", f"{base}..HEAD").stdout.split())


def commit_problem(sha: str, on_branch: set[str]) -> str | None:
    """Why a cited commit is not a commit on this branch, or None if it is."""
    resolved = git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if resolved.returncode != 0:
        return f"commit {sha} does not exist"
    if resolved.stdout.strip() not in on_branch:
        return f"commit {sha} is not on the branch"
    return None


def check_dispositions(
    confirmed: dict[tuple[str, str], str] | None,
    dispositions: list[dict[str, Any]] | None,
    load_error: str,
    base: str,
) -> Check:
    """(a) one well-formed final disposition per confirmed finding; fixed ones are proven."""
    name = "(a) dispositions complete and proven"
    if confirmed is None or dispositions is None:
        return Check(name, False, load_error)
    problems = [p for e in dispositions for p in entry_problems(e)]
    review_keys = [(e.get("review"), e.get("id")) for e in dispositions if e.get("review") in REVIEWS]
    dupes = sorted({f"{r}/{i}" for r, i in review_keys if review_keys.count((r, i)) > 1})
    unknown = sorted(f"{r}/{i}" for r, i in set(review_keys) if (r, i) not in confirmed)
    missing = sorted(f"{r}/{i}" for r, i in confirmed if (r, i) not in set(review_keys))
    problems += [f"duplicate: {d}" for d in dupes] + [f"not a confirmed finding: {u}" for u in unknown]
    problems += [f"missing: {m}" for m in missing]
    fixed = [e for e in dispositions if e.get("disposition") == "fixed" and not entry_problems(e)]
    on_branch = branch_commits(base)
    problems += [p for e in fixed if (p := commit_problem(e["commit"], on_branch)) is not None]
    nodes = sorted({e["regression_test"] for e in fixed})
    if nodes:
        result = pytest(*nodes)
        passed = passed_nodes(result.stdout)
        if result.returncode != 0:
            problems.append(f"regression tests did not all pass (pytest exit {result.returncode})")
        problems += [f"regression test not passing: {n}" for n in nodes if not node_passed(n, passed)]
    if problems:
        return Check(name, False, "; ".join(problems[:6]) + (f" ... ({len(problems)} problems)" if len(problems) > 6 else ""))
    tally = {d: sum(e["disposition"] == d for e in dispositions) for d in sorted(FINAL_DISPOSITIONS)}
    return Check(name, True, f"{len(confirmed)} confirmed covered, {len(dispositions)} entries {tally}, {len(nodes)} regression tests pass")


def check_mediums(confirmed: dict[tuple[str, str], str] | None, dispositions: list[dict[str, Any]] | None) -> Check:
    """(b) no medium/high finding is waved off: fixed, or deferred for an allowed reason."""
    name = "(b) medium findings fixed or validly deferred"
    if confirmed is None or dispositions is None:
        return Check(name, False, "skipped: inputs failed to load")
    by_key = {(e.get("review"), e.get("id")): e for e in dispositions}
    blocking = sorted(k for k, sev in confirmed.items() if sev in BLOCKING_SEVERITIES)
    bad = []
    for key in blocking:
        entry = by_key.get(key, {})
        disp, note = entry.get("disposition"), str(entry.get("note", ""))
        ok = disp == "fixed" or (disp == "deferred" and note.startswith(MEDIUM_DEFERRAL_PREFIXES))
        if not ok:
            bad.append(f"{key[0]}/{key[1]}={disp}")
    if bad:
        return Check(name, False, f"not fixed or validly deferred: {bad}")
    n_fixed = sum(by_key[k].get("disposition") == "fixed" for k in blocking)
    return Check(name, True, f"{len(blocking)} medium/high: {n_fixed} fixed, {len(blocking) - n_fixed} deferred")


def check_red_team() -> Check:
    """(c) the red-team file has no xfail left and the five former xfails pass."""
    name = "(c) red-team xfails removed and passing"
    try:
        text = RED_TEAM.read_text(encoding="utf-8")
    except OSError as e:
        return Check(name, False, f"cannot read {RED_TEAM.name}: {type(e).__name__}")
    if "xfail" in text:
        return Check(name, False, f"'xfail' still appears {text.count('xfail')} times in {RED_TEAM.name}")
    result = pytest(*FORMER_XFAILS)
    passed = passed_nodes(result.stdout)
    failing = [n.split("::", 1)[1] for n in FORMER_XFAILS if n not in passed]
    if result.returncode != 0 or failing:
        return Check(name, False, f"pytest exit {result.returncode}; not passing: {failing}")
    return Check(name, True, f"no xfail in file; {len(FORMER_XFAILS)} former xfails pass")


def check_suite() -> Check:
    """(d) the whole suite is green with nothing expected-to-fail left."""
    name = "(d) pytest green"
    result = run(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")
    counts = summary_counts(result.stdout)
    bad = {k: counts[k] for k in ("failed", "errors", "xfailed", "xpassed") if counts.get(k)}
    ok = result.returncode == 0 and not bad and counts.get("passed", 0) >= MIN_PASSED
    return Check(name, ok, f"{counts} (need >= {MIN_PASSED} passed, 0 failed/errors/xfailed/xpassed)")


def check_lint() -> Check:
    """(e) ruff lint, ruff format and mypy --strict all clean."""
    name = "(e) ruff + format + mypy --strict"
    steps = {
        "ruff check": (sys.executable, "-m", "ruff", "check", "."),
        "ruff format --check": (sys.executable, "-m", "ruff", "format", "--check", "."),
        "mypy --strict": (sys.executable, "-m", "mypy", "--strict"),
    }
    failed = [label for label, cmd in steps.items() if run(*cmd).returncode != 0]
    return Check(name, not failed, f"failing: {failed}" if failed else "all clean")


def check_frozen_baseline() -> Check:
    """(f) the frozen regex baseline matches tag baseline-v1, in HEAD and on disk."""
    name = "(f) frozen baseline byte-identical"
    committed = git("diff", "--quiet", FROZEN_TAG_PATH, f"HEAD:{FROZEN_PATH}").returncode == 0
    tagged = git("show", FROZEN_TAG_PATH)
    try:
        on_disk = (ROOT / FROZEN_PATH).read_text(encoding="utf-8") == tagged.stdout and tagged.returncode == 0
    except OSError:
        on_disk = False
    return Check(name, committed and on_disk, f"HEAD identical: {committed}; working tree identical: {on_disk}")


def check_prompt_version() -> Check:
    """(g) the extractor's prompt identity is unchanged, so the final eval stays comparable."""
    name = "(g) PROMPT_VERSION == ac1a145b"
    code = f"from deal_board.extractors.claude import PROMPT_VERSION; assert PROMPT_VERSION=={EXPECTED_PROMPT_VERSION!r}"
    result = run(sys.executable, "-c", code)
    return Check(name, result.returncode == 0, "unchanged" if result.returncode == 0 else result.stderr.strip()[-200:])


def check_untouched(base: str) -> Check:
    """(h) saved results, data and the review loops' own records are untouched."""
    name = "(h) results/, data/, review loops untouched"
    diff = git("diff", "--name-only", base, "--", *UNTOUCHED)
    untracked = git("status", "--porcelain", "--untracked-files=all", "--", *UNTOUCHED)
    changed = [p for p in diff.stdout.splitlines() if p] + [p for p in untracked.stdout.splitlines() if p]
    if diff.returncode != 0 or changed:
        return Check(name, False, f"changed vs {base}: {changed[:8]}")
    return Check(name, True, f"no changes vs {base}")


def check_frozen_script(base: str) -> Check:
    """(i) this script (committed and on disk) matches the branch's first commit."""
    name = "(i) check_success.py unchanged since commit 1"
    commits = git("rev-list", "--reverse", f"{base}..HEAD").stdout.split()
    if not commits:
        return Check(name, False, f"no commits on the branch beyond {base}")
    first = commits[0]
    if git("cat-file", "-e", f"{first}:{THIS_SCRIPT}").returncode != 0:
        return Check(name, False, f"{THIS_SCRIPT} is not in the first commit {first[:7]}")
    unchanged = git("diff", "--quiet", first, "--", THIS_SCRIPT).returncode == 0
    return Check(name, unchanged, f"vs first branch commit {first[:7]} (working tree included)")


def parse_rounds(markdown: str) -> list[tuple[int, int | None]]:
    """(round number, new_medium_or_high or None) per '## Round n' section."""
    heads = list(ROUND_HEADING.finditer(markdown))
    rounds = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(markdown)
        found = NEW_MEDIUM_OR_HIGH.search(markdown, head.end(), end)
        rounds.append((int(head.group(1)), int(found.group(1)) if found else None))
    return rounds


def check_postreview() -> Check:
    """(j) a post-fix review round happened and the last one found nothing medium or high."""
    name = "(j) POSTREVIEW.md converged"
    try:
        rounds = parse_rounds(POSTREVIEW.read_text(encoding="utf-8"))
    except OSError as e:
        return Check(name, False, f"cannot read {POSTREVIEW.name}: {type(e).__name__}")
    if not rounds:
        return Check(name, False, "no '## Round n' sections")
    if [n for n, _ in rounds] != list(range(1, len(rounds) + 1)):
        return Check(name, False, f"rounds must be numbered 1..n, got {[n for n, _ in rounds]}")
    if any(k is None for _, k in rounds):
        return Check(name, False, "every round needs a 'new_medium_or_high: <n>' line")
    return Check(name, rounds[-1][1] == 0, f"{len(rounds)} rounds, new_medium_or_high per round {[k for _, k in rounds]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--base", default="main", help="base ref the branch was cut from (default: main)")
    args = ap.parse_args(argv)

    confirmed, confirmed_error = load_confirmed()
    dispositions, disp_error = load_dispositions()
    checks = [
        check_dispositions(confirmed, dispositions, confirmed_error or disp_error, args.base),
        check_mediums(confirmed, dispositions),
        check_red_team(),
        check_suite(),
        check_lint(),
        check_frozen_baseline(),
        check_prompt_version(),
        check_untouched(args.base),
        check_frozen_script(args.base),
        check_postreview(),
    ]
    for c in checks:
        print(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
    ok = all(c.passed for c in checks)
    print("SUCCESS" if ok else "NOT YET")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
