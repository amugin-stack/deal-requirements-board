"""Single definition of success for the eval-method / code-quality review on branch review-eval-quality.

    python review/eval-quality/check_success.py

Exits 0 only when every check below passes, printing one PASS/FAIL line per check (same style as
loop/check_success.py). The checks exist so a reviewer cannot "finish" by asserting things: every
confirmed finding must point at text that is really in the tree, the loop must have converged, and
the review must not have touched anything outside its own directory (above all results/).

Run it with an interpreter that has pytest installed (the repo's .venv); check (f) runs the suite
with sys.executable. This file is frozen after the branch's first commit; check (e) enforces that.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
REVIEW_DIR = "review/eval-quality/"
SELF = REVIEW_DIR + "check_success.py"
FINDINGS = HERE / "findings.json"
LOG = HERE / "LOG.md"
BASE = "main"

REQUIRED_KEYS = ("id", "dimension", "title", "severity", "file", "line", "snippet", "claim",
                 "reproduction", "status", "verifier_note")
CONFIRMED_KEYS = ("proposed_fix", "proposed_regression_test")
DIMENSIONS = {"eval_method", "code_quality"}
SEVERITIES = {"high", "medium", "low"}
STATUSES = {"confirmed", "rejected"}  # "candidate" is a working state and must not survive to the end
LINE_TOLERANCE = 3
EXPECTED_PASSED = 837
EXPECTED_XFAILED = 5
MIN_ROUNDS = 2


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def load_findings() -> tuple[list[dict] | None, str]:
    """Parse findings.json; returns (findings, error). A top-level list or {"findings": [...]} is accepted."""
    try:
        data = json.loads(FINDINGS.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"cannot parse {FINDINGS.name}: {exc}"
    if isinstance(data, dict):
        data = data.get("findings")
    if not isinstance(data, list):
        return None, "findings.json must be a list (or an object with a 'findings' list)"
    return data, ""


def schema_problems(findings: list[dict]) -> list[str]:
    """Every problem with the shape of the findings, as human-readable strings (empty means valid)."""
    problems: list[str] = []
    seen: set[str] = set()
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            problems.append(f"entry {i} is not an object")
            continue
        tag = str(f.get("id", f"#{i}"))
        missing = [k for k in REQUIRED_KEYS if f.get(k) in (None, "")]
        if f.get("status") == "confirmed":
            missing += [k for k in CONFIRMED_KEYS if f.get(k) in (None, "")]
        if missing:
            problems.append(f"{tag}: missing {missing}")
        if tag in seen:
            problems.append(f"{tag}: duplicate id")
        seen.add(tag)
        if f.get("dimension") not in DIMENSIONS:
            problems.append(f"{tag}: dimension {f.get('dimension')!r}")
        if f.get("severity") not in SEVERITIES:
            problems.append(f"{tag}: severity {f.get('severity')!r}")
        if f.get("status") not in STATUSES:
            problems.append(f"{tag}: status {f.get('status')!r}")
        if not isinstance(f.get("line"), int) or isinstance(f.get("line"), bool):
            problems.append(f"{tag}: line must be an int")
    return problems


def snippet_line_starts(text: str, snippet: str) -> list[int]:
    """1-based line numbers on which each verbatim occurrence of snippet starts."""
    starts, pos = [], text.find(snippet)
    while pos != -1:
        starts.append(text.count("\n", 0, pos) + 1)
        pos = text.find(snippet, pos + 1)
    return starts


def snippet_problems(findings: list[dict]) -> list[str]:
    """Confirmed findings whose snippet is not verbatim in the worktree file within ±LINE_TOLERANCE lines."""
    problems: list[str] = []
    for f in findings:
        if not isinstance(f, dict) or f.get("status") != "confirmed":
            continue
        path = ROOT / str(f.get("file", ""))
        snippet, line = f.get("snippet"), f.get("line")
        if not path.is_file() or not isinstance(snippet, str) or not snippet or not isinstance(line, int):
            problems.append(f"{f.get('id')}: file/snippet/line unusable ({f.get('file')})")
            continue
        starts = snippet_line_starts(path.read_text(), snippet)
        if not any(abs(s - line) <= LINE_TOLERANCE for s in starts):
            where = f"found at {starts}" if starts else "not found"
            problems.append(f"{f.get('id')}: snippet {where}, claimed line {line} in {f.get('file')}")
    return problems


def log_rounds(markdown: str) -> list[int | None]:
    """new_confirmed per '## Round N' section of LOG.md, in order (None when the section has no count)."""
    sections = re.split(r"(?m)^## Round \d+", markdown)[1:]
    counts: list[int | None] = []
    for s in sections:
        m = re.search(r"new_confirmed:\s*\**\s*(\d+)", s)
        counts.append(int(m.group(1)) if m else None)
    return counts


def pytest_counts(summary: str) -> dict[str, int]:
    """{'passed': n, 'xfailed': n, 'failed': n, ...} from a pytest -q summary line."""
    return {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|xfailed|xpassed|errors?|skipped)", summary)}


def first_branch_commit() -> str | None:
    commits = git("rev-list", "--reverse", f"{BASE}..HEAD").stdout.split()
    return commits[0] if commits else None


def checks() -> list[Check]:
    out: list[Check] = []

    findings, err = load_findings()
    if findings is None:
        out.append(Check("(a) findings.json schema", False, err))
        out.append(Check("(b) confirmed snippets verbatim", False, "no findings to check"))
    else:
        probs = schema_problems(findings)
        n_conf = sum(1 for f in findings if isinstance(f, dict) and f.get("status") == "confirmed")
        out.append(Check("(a) findings.json schema", not probs,
                         f"{len(findings)} findings, {n_conf} confirmed" if not probs else "; ".join(probs[:5])))
        sprobs = snippet_problems(findings)
        out.append(Check("(b) confirmed snippets verbatim", not sprobs,
                         f"{n_conf} confirmed snippets located" if not sprobs else "; ".join(sprobs[:5])))

    try:
        rounds = log_rounds(LOG.read_text())
    except OSError as exc:
        rounds, err = [], str(exc)
    ok = len(rounds) >= MIN_ROUNDS and rounds[-1] == 0
    out.append(Check("(c) LOG.md converged", ok, f"rounds={len(rounds)} new_confirmed per round={rounds}"))

    changed = [p for p in git("diff", "--name-only", f"{BASE}..HEAD", "--").stdout.splitlines() if p]
    outside = [p for p in changed if not p.startswith(REVIEW_DIR)]
    out.append(Check("(d) diff only in review dir", bool(changed) and not outside,
                     f"{len(changed)} files changed" if not outside else f"outside: {outside[:5]}"))

    first = first_branch_commit()
    in_first = first is not None and git("cat-file", "-e", f"{first}:{SELF}").returncode == 0
    frozen = in_first and git("diff", "--quiet", first, "--", SELF).returncode == 0
    out.append(Check("(e) check_success.py frozen", frozen,
                     f"vs first branch commit {first[:7] if first else None}" + ("" if in_first else " (absent there)")))

    tests = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                           cwd=ROOT, capture_output=True, text=True)
    summary = (tests.stdout.strip().splitlines() or ["no output"])[-1]
    counts = pytest_counts(summary)
    tests_ok = (counts.get("passed") == EXPECTED_PASSED and counts.get("xfailed") == EXPECTED_XFAILED
                and set(counts) <= {"passed", "xfailed"})
    out.append(Check("(f) pytest unchanged", tests_ok, summary))

    status = git("status", "--porcelain", "--", "results/").stdout.strip()
    same = git("diff", "--quiet", BASE, "--", "results/").returncode == 0
    out.append(Check("(g) results/ untouched", not status and same,
                     "clean and identical to main" if not status and same else f"status={status!r} same_as_main={same}"))
    return out


def main() -> int:
    results = checks()
    for c in results:
        print(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
    ok = all(c.passed for c in results)
    print("SUCCESS" if ok else "NOT YET")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
