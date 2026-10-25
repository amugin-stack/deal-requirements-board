"""Single definition of success for the correctness / API-contract review loop.

  python review/correctness-api/check_success.py            # compare against branch `main`
  python review/correctness-api/check_success.py --base X   # compare against another base ref

Exits 0 only when every check passes; prints one PASS/FAIL line per check, like
`loop/check_success.py`. The checks:

  (a) findings.json parses and every entry has the required, well-typed fields;
  (b) every confirmed finding's `snippet` appears verbatim in `file` within +/-3 lines of `line`;
  (c) LOG.md records >= 2 rounds and the last round says `new_confirmed: 0`;
  (d) `git diff main..HEAD` touches only files inside review/correctness-api/;
  (e) this script is unchanged since the first commit on the branch;
  (f) the test suite still reports exactly 837 passed and 5 xfailed.

This file is frozen after the branch's first commit. Check (e) enforces that.
Run it with an interpreter that has the project's dependencies (e.g. the main checkout's
`.venv/bin/python`); the test suite is run with that same interpreter.
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
REVIEW_DIR = "review/correctness-api/"
ALLOWED_FILES = frozenset(
    REVIEW_DIR + name for name in ("graph.md", "check_success.py", "findings.json", "findings.md", "LOG.md")
)
FINDINGS = HERE / "findings.json"
LOG = HERE / "LOG.md"
THIS_SCRIPT = REVIEW_DIR + "check_success.py"

REQUIRED_FIELDS = (
    "id", "dimension", "title", "severity", "file", "line", "snippet",
    "claim", "reproduction", "status", "verifier_note",
)
DIMENSIONS = frozenset({"correctness", "api_contract"})
SEVERITIES = frozenset({"high", "medium", "low"})
STATUSES = frozenset({"confirmed", "rejected"})
SNIPPET_WINDOW = 3  # lines either side of `line`
MIN_ROUNDS = 2
EXPECTED_PASSED = 837
EXPECTED_XFAILED = 5

# LOG.md format: one "## Round <n>" heading per round, with a "new_confirmed: <k>" line in it.
ROUND_HEADING = re.compile(r"^##\s+Round\s+(\d+)\b", re.MULTILINE)
NEW_CONFIRMED = re.compile(r"new_confirmed:\s*`?(\d+)`?")


@dataclass(frozen=True)
class Check:
    """One named success criterion and why it passed or failed."""

    name: str
    passed: bool
    detail: str


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run git in the worktree root and capture its output."""
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def entry_problems(entry: Any) -> list[str]:
    """Schema problems with one findings.json entry; an empty list means it is valid."""
    if not isinstance(entry, dict):
        return ["entry is not an object"]
    ident = entry.get("id", "?")
    problems = [f"{ident}: missing {f}" for f in REQUIRED_FIELDS if f not in entry]
    if problems:
        return problems
    for f in ("id", "title", "file", "snippet", "claim", "reproduction", "verifier_note"):
        if not _nonempty_str(entry[f]):
            problems.append(f"{ident}: {f} must be a non-empty string")
    if entry["dimension"] not in DIMENSIONS:
        problems.append(f"{ident}: dimension {entry['dimension']!r} not in {sorted(DIMENSIONS)}")
    if entry["severity"] not in SEVERITIES:
        problems.append(f"{ident}: severity {entry['severity']!r} not in {sorted(SEVERITIES)}")
    if entry["status"] not in STATUSES:
        problems.append(f"{ident}: status {entry['status']!r} not in {sorted(STATUSES)}")
    line = entry["line"]
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        problems.append(f"{ident}: line must be a positive integer")
    if entry["status"] == "confirmed":
        if not _nonempty_str(entry.get("proposed_fix")):
            problems.append(f"{ident}: confirmed finding needs proposed_fix")
        test = entry.get("proposed_regression_test")
        if not (isinstance(test, dict) and _nonempty_str(test.get("name")) and _nonempty_str(test.get("asserts"))):
            problems.append(f"{ident}: confirmed finding needs proposed_regression_test {{name, asserts}}")
    return problems


def load_findings() -> tuple[list[dict[str, Any]] | None, str]:
    """The parsed findings list, or None plus the reason it could not be loaded."""
    try:
        data = json.loads(FINDINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"cannot load {FINDINGS.name}: {type(e).__name__}: {e}"
    if not isinstance(data, list) or not data:
        return None, f"{FINDINGS.name} must be a non-empty JSON array"
    return data, ""


def check_schema(findings: list[dict[str, Any]] | None, load_error: str) -> Check:
    """(a) every entry has the required fields with allowed values; ids are unique."""
    name = "(a) findings.json schema"
    if findings is None:
        return Check(name, False, load_error)
    problems = [p for e in findings for p in entry_problems(e)]
    ids = [e.get("id") for e in findings if isinstance(e, dict)]
    dupes = sorted({i for i in ids if ids.count(i) > 1}, key=str)
    if dupes:
        problems.append(f"duplicate ids: {dupes}")
    if problems:
        return Check(name, False, "; ".join(problems[:5]) + (" ..." if len(problems) > 5 else ""))
    confirmed = sum(e["status"] == "confirmed" for e in findings)
    return Check(name, True, f"{len(findings)} entries ({confirmed} confirmed, {len(findings) - confirmed} rejected)")


def snippet_problem(entry: dict[str, Any]) -> str | None:
    """Why a confirmed finding's snippet isn't pinned to its file and line, or None if it is."""
    path = (ROOT / entry["file"]).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        return f"{entry['id']}: {entry['file']} is not a file in the worktree"
    lines = path.read_text(encoding="utf-8").splitlines()
    start = max(entry["line"] - 1 - SNIPPET_WINDOW, 0)
    window = "\n".join(lines[start: entry["line"] + SNIPPET_WINDOW])
    if entry["snippet"] not in window:
        return f"{entry['id']}: snippet not found at {entry['file']}:{entry['line']}+/-{SNIPPET_WINDOW}"
    return None


def check_snippets(findings: list[dict[str, Any]] | None) -> Check:
    """(b) confirmed findings point at code that really exists where they say."""
    name = "(b) confirmed snippets pinned to code"
    if findings is None or any(entry_problems(e) for e in findings):
        return Check(name, False, "skipped: findings.json fails the schema check")
    confirmed = [e for e in findings if e["status"] == "confirmed"]
    problems = [p for e in confirmed if (p := snippet_problem(e)) is not None]
    if problems:
        return Check(name, False, "; ".join(problems))
    return Check(name, True, f"{len(confirmed)} confirmed snippets found within +/-{SNIPPET_WINDOW} lines")


def parse_rounds(markdown: str) -> list[tuple[int, int | None]]:
    """(round number, new_confirmed or None) for each '## Round n' section of LOG.md."""
    heads = list(ROUND_HEADING.finditer(markdown))
    rounds = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(markdown)
        found = NEW_CONFIRMED.search(markdown, head.end(), end)
        rounds.append((int(head.group(1)), int(found.group(1)) if found else None))
    return rounds


def check_log() -> Check:
    """(c) at least two rounds, numbered 1..n, each with new_confirmed; the last one is 0."""
    name = "(c) LOG.md converged"
    try:
        rounds = parse_rounds(LOG.read_text(encoding="utf-8"))
    except OSError as e:
        return Check(name, False, f"cannot read {LOG.name}: {type(e).__name__}")
    numbers = [n for n, _ in rounds]
    if len(rounds) < MIN_ROUNDS:
        return Check(name, False, f"{len(rounds)} rounds logged, need >= {MIN_ROUNDS}")
    if numbers != list(range(1, len(rounds) + 1)):
        return Check(name, False, f"rounds must be numbered 1..n, got {numbers}")
    if any(k is None for _, k in rounds):
        return Check(name, False, "every round needs a 'new_confirmed: <k>' line")
    last = rounds[-1][1]
    return Check(name, last == 0, f"{len(rounds)} rounds, new_confirmed per round {[k for _, k in rounds]}")


def check_scope(base: str) -> Check:
    """(d) the branch changes only this review's own files."""
    name = "(d) diff limited to review/correctness-api/"
    diff = git("diff", "--name-only", f"{base}..HEAD", "--")
    if diff.returncode != 0:
        return Check(name, False, f"git diff failed: {diff.stderr.strip()}")
    changed = [p for p in diff.stdout.splitlines() if p]
    outside = [p for p in changed if p not in ALLOWED_FILES]
    if outside:
        return Check(name, False, f"files outside the allowed set: {outside}")
    return Check(name, True, f"{len(changed)} files changed vs {base}, all in {REVIEW_DIR}")


def check_frozen(base: str) -> Check:
    """(e) this script (committed and on disk) matches its version in the branch's first commit."""
    name = "(e) check_success.py unchanged since commit 1"
    commits = git("rev-list", "--reverse", f"{base}..HEAD").stdout.split()
    if not commits:
        return Check(name, False, f"no commits on the branch beyond {base}")
    first = commits[0]
    if git("cat-file", "-e", f"{first}:{THIS_SCRIPT}").returncode != 0:
        return Check(name, False, f"{THIS_SCRIPT} is not in the first commit {first[:7]}")
    unchanged = git("diff", "--quiet", first, "--", THIS_SCRIPT).returncode == 0
    return Check(name, unchanged, f"vs first branch commit {first[:7]} (working tree included)")


def check_tests() -> Check:
    """(f) the suite result is exactly what main had: 837 passed, 5 xfailed, nothing failing."""
    name = "(f) pytest unchanged"
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                         cwd=ROOT, capture_output=True, text=True)
    summary = (run.stdout.strip().splitlines() or ["no output"])[-1]
    counts = {k: int(n) for n, k in re.findall(r"(\d+) (passed|xfailed|failed|errors?|xpassed)", summary)}
    ok = (
        run.returncode == 0
        and counts.get("passed") == EXPECTED_PASSED
        and counts.get("xfailed") == EXPECTED_XFAILED
        and not any(counts.get(k) for k in ("failed", "error", "errors", "xpassed"))
    )
    return Check(name, ok, f"{summary} (expected {EXPECTED_PASSED} passed, {EXPECTED_XFAILED} xfailed)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="main", help="base ref the branch was cut from (default: main)")
    args = ap.parse_args(argv)

    findings, load_error = load_findings()
    checks = [
        check_schema(findings, load_error),
        check_snippets(findings),
        check_log(),
        check_scope(args.base),
        check_frozen(args.base),
        check_tests(),
    ]
    for c in checks:
        print(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
    ok = all(c.passed for c in checks)
    print("SUCCESS" if ok else "NOT YET")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
