"""Success test for the compliance & security review loop. Exits 0 only if every check passes.

  python review/compliance-security/check_success.py

This file is the single definition of "done" for the review. It is committed first, before any
reviewing, and must never change afterwards (check `frozen` enforces that). Checks:

  (a) schema     findings.json parses; every entry has the required fields with allowed values
  (b) snippets   every confirmed finding's snippet appears verbatim at file:line (+/- 3 lines)
  (c) log        LOG.md records >= 2 rounds and the last round says `new_confirmed: 0`
  (d) diff       `git diff main..HEAD` touches only review/compliance-security/
  (e) frozen     this file is identical to its version in the branch's first commit
  (f) pytest     the suite still reports 837 passed and 5 xfailed
  (g) secrets    no file in this directory contains an Anthropic key or a key assignment
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
REL_DIR = "review/compliance-security/"
SELF = REL_DIR + "check_success.py"
FINDINGS = HERE / "findings.json"
LOG = HERE / "LOG.md"
BASE = "main"

EXPECTED_PASSED = 837
EXPECTED_XFAILED = 5
SNIPPET_WINDOW = 3  # lines either side of `line`
MIN_ROUNDS = 2

REQUIRED = ("id", "dimension", "title", "severity", "file", "line", "snippet", "claim",
            "reproduction", "status", "verifier_note")
REQUIRED_IF_CONFIRMED = ("proposed_fix", "proposed_regression_test")
DIMENSIONS = {"compliance", "security_data"}
SEVERITIES = {"high", "medium", "low"}
STATUSES = {"confirmed", "rejected"}

# Built by concatenation so this file does not match its own patterns.
SECRET_PATTERNS = (
    re.compile(re.escape("sk-" + "ant-")),
    re.compile(re.escape("ANTHROPIC_API_KEY" + "=") + r"\S+"),
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run git in the worktree root and capture text output."""
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def load_findings() -> list[dict]:
    """findings.json as a list of dicts; raises ValueError if it is not one."""
    data = json.loads(FINDINGS.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(f, dict) for f in data):
        raise ValueError("findings.json must be a JSON list of objects")
    return data


def _nonempty(v: object) -> bool:
    return v is not None and (not isinstance(v, str) or v.strip() != "")


def check_schema() -> Check:
    """(a) Every finding has the required fields; confirmed ones also carry a fix and a test."""
    try:
        findings = load_findings()
    except (OSError, ValueError) as e:
        return Check("schema", False, f"findings.json unreadable: {type(e).__name__}: {e}")
    if not findings:
        return Check("schema", False, "findings.json is empty")
    problems: list[str] = []
    ids: set[str] = set()
    for i, f in enumerate(findings):
        fid = str(f.get("id", f"#{i}"))
        missing = [k for k in REQUIRED if not _nonempty(f.get(k))]
        if f.get("status") == "confirmed":
            missing += [k for k in REQUIRED_IF_CONFIRMED if not _nonempty(f.get(k))]
        if missing:
            problems.append(f"{fid} missing {missing}")
        if f.get("dimension") not in DIMENSIONS:
            problems.append(f"{fid} dimension={f.get('dimension')!r}")
        if f.get("severity") not in SEVERITIES:
            problems.append(f"{fid} severity={f.get('severity')!r}")
        if f.get("status") not in STATUSES:
            problems.append(f"{fid} status={f.get('status')!r}")
        if not isinstance(f.get("line"), int) or isinstance(f.get("line"), bool) or f["line"] < 1:
            problems.append(f"{fid} line={f.get('line')!r} is not a positive int")
        if fid in ids:
            problems.append(f"duplicate id {fid}")
        ids.add(fid)
    n_conf = sum(f.get("status") == "confirmed" for f in findings)
    detail = "; ".join(problems[:5]) if problems else f"{len(findings)} findings, {n_conf} confirmed"
    return Check("schema", not problems, detail)


def snippet_at(file: str, line: int, snippet: str) -> bool:
    """True if `snippet` (possibly multi-line) occurs verbatim within +/- SNIPPET_WINDOW of `line`."""
    path = (ROOT / file).resolve()
    if ROOT not in path.parents or not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    span = snippet.count("\n")
    lo = max(line - 1 - SNIPPET_WINDOW, 0)
    hi = min(line - 1 + SNIPPET_WINDOW + span, len(lines) - 1)
    window = "\n".join(lines[lo:hi + 1])
    return snippet in window


def check_snippets() -> Check:
    """(b) Confirmed findings point at code that really exists where they say."""
    try:
        findings = load_findings()
    except (OSError, ValueError) as e:
        return Check("snippets", False, f"findings.json unreadable: {type(e).__name__}")
    confirmed = [f for f in findings if f.get("status") == "confirmed"]
    bad = [str(f.get("id")) for f in confirmed
           if not (isinstance(f.get("file"), str) and isinstance(f.get("line"), int)
                   and isinstance(f.get("snippet"), str) and f["snippet"]
                   and snippet_at(f["file"], f["line"], f["snippet"]))]
    if bad:
        return Check("snippets", False, f"snippet not found near file:line for {bad}")
    return Check("snippets", True, f"{len(confirmed)} confirmed snippets found in place")


def parse_rounds(text: str) -> list[tuple[str, int | None]]:
    """[(round heading, new_confirmed or None)] for each `## Round ...` section of LOG.md."""
    sections = re.split(r"(?m)^##\s+(Round\b[^\n]*)$", text)
    rounds: list[tuple[str, int | None]] = []
    for heading, body in zip(sections[1::2], sections[2::2]):
        m = re.findall(r"new_confirmed:\s*`?(\d+)", body)
        rounds.append((heading.strip(), int(m[-1]) if m else None))
    return rounds


def check_log() -> Check:
    """(c) The loop ran at least twice and stopped on a round with nothing new."""
    try:
        rounds = parse_rounds(LOG.read_text(encoding="utf-8"))
    except OSError as e:
        return Check("log", False, f"LOG.md unreadable: {type(e).__name__}")
    if len(rounds) < MIN_ROUNDS:
        return Check("log", False, f"{len(rounds)} rounds recorded (need >= {MIN_ROUNDS})")
    missing = [h for h, n in rounds if n is None]
    if missing:
        return Check("log", False, f"rounds without new_confirmed: {missing}")
    last_heading, last_new = rounds[-1]
    return Check("log", last_new == 0,
                 f"{len(rounds)} rounds; last ({last_heading}) new_confirmed: {last_new}")


def check_diff() -> Check:
    """(d) The branch changes only this review directory."""
    r = git("diff", "--name-only", f"{BASE}..HEAD", "--")
    if r.returncode != 0:
        return Check("diff", False, f"git diff failed: {r.stderr.strip()}")
    paths = [p for p in r.stdout.splitlines() if p]
    outside = [p for p in paths if not p.startswith(REL_DIR)]
    if not paths:
        return Check("diff", False, f"no changes on {BASE}..HEAD")
    if outside:
        return Check("diff", False, f"files outside {REL_DIR}: {outside[:5]}")
    return Check("diff", True, f"{len(paths)} files, all under {REL_DIR}")


def check_frozen() -> Check:
    """(e) This file matches the first commit on the branch, in HEAD and in the working tree."""
    r = git("rev-list", "--reverse", f"{BASE}..HEAD")
    commits = r.stdout.split()
    if r.returncode != 0 or not commits:
        return Check("frozen", False, "no commits on the branch")
    first = commits[0]
    if git("cat-file", "-e", f"{first}:{SELF}").returncode != 0:
        return Check("frozen", False, f"{SELF} not in first commit {first[:7]}")
    committed = git("diff", "--quiet", first, "HEAD", "--", SELF).returncode == 0
    working = git("diff", "--quiet", first, "--", SELF).returncode == 0
    return Check("frozen", committed and working,
                 f"vs first commit {first[:7]}: HEAD {'same' if committed else 'CHANGED'}, "
                 f"working tree {'same' if working else 'CHANGED'}")


def check_pytest() -> Check:
    """(f) The suite is unchanged: exactly the expected passed and xfailed counts, nothing else."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # never call the API
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True, text=True, env=env)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    summary = lines[-1] if lines else ""
    counts = {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|xfailed|xpassed|error|errors|skipped)", summary)}
    ok = (counts.get("passed") == EXPECTED_PASSED and counts.get("xfailed") == EXPECTED_XFAILED
          and set(counts) <= {"passed", "xfailed"})
    return Check("pytest", ok, summary.strip("= ") or f"no output (rc={r.returncode})")


def check_secrets() -> Check:
    """(g) None of this directory's files contains something that looks like a real key."""
    hits: list[str] = []
    for path in sorted(HERE.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(p.search(text) for p in SECRET_PATTERNS):
            hits.append(str(path.relative_to(ROOT)))
    return Check("secrets", not hits, f"matches in {hits}" if hits else "no key-like strings")


CHECKS: tuple[Callable[[], Check], ...] = (
    check_schema, check_snippets, check_log, check_diff, check_frozen, check_pytest, check_secrets,
)


def main() -> int:
    results = [fn() for fn in CHECKS]
    for c in results:
        print(f"{'PASS' if c.passed else 'FAIL'}  {c.name}: {c.detail}")
    ok = all(c.passed for c in results)
    print("SUCCESS" if ok else "NOT DONE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
