"""Single definition of success for the licensing-gate mutation-testing loop.

  python review/mutants/check_success.py            # compare against branch `main`
  python review/mutants/check_success.py --base X   # compare against another base ref

Exits 0 only when every check passes; prints one PASS/FAIL line per check, like the other
`review/*/check_success.py` scripts. The checks:

  (a) results.json lists >= 25 non-equivalent mutants (status killed or survived), each with
      id, target {function, file}, description, diff, status; killed ones name the failing test
      node ids in `killed_by`; survived ones carry severity, proposed_test {name, asserts},
      proposed_fixture (null or an obviously fictional `fx-*` broker row) and verifier_note.
      Mutants judged equivalent by a verifier may be listed with status `equivalent` and a
      verifier_note; they do not count towards the 25.
  (b) no `high` survivor is left without a proposed test;
  (c) every mutant's `diff` applies cleanly to a temp copy of the current worktree file(s) and
      the patched file differs from the original, so every mutant is real;
  (d) LOG.md records >= 2 rounds ("## Round <n>") and the last round says
      `new_high_medium_survivors: 0`;
  (e) `git diff main..HEAD` touches only files inside review/mutants/;
  (f) the worktree is clean and `pytest -q` in it still reports exactly
      1003 passed, 1 skipped, 5 xfailed;
  (g) this script is unchanged since the first commit on the branch.

This file is frozen after the branch's first commit. Check (g) enforces that.
Run it with the worktree's interpreter (`.venv/bin/python`); pytest runs with that same one.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
REVIEW_DIR = "review/mutants/"
RESULTS = HERE / "results.json"
LOG = HERE / "LOG.md"
THIS_SCRIPT = REVIEW_DIR + "check_success.py"

MIN_MUTANTS = 25
MIN_ROUNDS = 2
EXPECTED = {"passed": 1003, "skipped": 1, "xfailed": 5}
STATUSES = frozenset({"killed", "survived", "equivalent"})
SEVERITIES = frozenset({"high", "medium", "low"})
BROKER_COLUMNS = (
    "broker_id",
    "name",
    "source",
    "license_state",
    "license_number",
    "license_type",
    "license_status",
    "license_expiration",
    "county",
    "markets",
    "property_types",
    "transaction_types",
    "min_deal_usd",
    "max_deal_usd",
)

ROUND_HEADING = re.compile(r"^##\s+Round\s+(\d+)\b", re.MULTILINE)
NEW_HM = re.compile(r"new_high_medium_survivors:\s*`?(\d+)`?")
DIFF_FILE = re.compile(r"^\+\+\+ b/(\S+)", re.MULTILINE)


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


def fixture_problems(ident: str, fixture: Any) -> list[str]:
    """A proposed fixture must be null or a complete, obviously fictional broker row."""
    if fixture is None:
        return []
    if not isinstance(fixture, dict):
        return [f"{ident}: proposed_fixture must be null or an object"]
    problems = [f"{ident}: proposed_fixture missing {c}" for c in BROKER_COLUMNS if c not in fixture]
    if not str(fixture.get("broker_id", "")).startswith("fx-"):
        problems.append(f"{ident}: proposed_fixture broker_id must start with 'fx-'")
    if fixture.get("source") != "fictional":
        problems.append(f"{ident}: proposed_fixture source must be 'fictional'")
    if "Fictional" not in str(fixture.get("name", "")):
        problems.append(f"{ident}: proposed_fixture name must contain 'Fictional'")
    return problems


def entry_problems(entry: Any) -> list[str]:
    """Schema problems with one results.json entry; an empty list means it is valid."""
    if not isinstance(entry, dict):
        return ["entry is not an object"]
    ident = str(entry.get("id", "?"))
    problems = [f"{ident}: missing {f}" for f in ("id", "target", "description", "diff", "status") if f not in entry]
    if problems:
        return problems
    for f in ("id", "description", "diff"):
        if not _nonempty_str(entry[f]):
            problems.append(f"{ident}: {f} must be a non-empty string")
    target = entry["target"]
    if not (isinstance(target, dict) and _nonempty_str(target.get("function")) and _nonempty_str(target.get("file"))):
        problems.append(f"{ident}: target must be {{function, file}}")
    status = entry["status"]
    if status not in STATUSES:
        return [*problems, f"{ident}: status {status!r} not in {sorted(STATUSES)}"]
    if status == "killed":
        killed_by = entry.get("killed_by")
        if not (isinstance(killed_by, list) and killed_by and all(_nonempty_str(t) and "::" in t for t in killed_by)):
            problems.append(f"{ident}: killed mutant needs killed_by: non-empty list of test node ids")
    if status == "survived":
        if entry.get("severity") not in SEVERITIES:
            problems.append(f"{ident}: survivor severity must be one of {sorted(SEVERITIES)}")
        if "proposed_fixture" not in entry:
            problems.append(f"{ident}: survivor needs proposed_fixture (null if none is needed)")
        else:
            problems.extend(fixture_problems(ident, entry["proposed_fixture"]))
    if status in ("survived", "equivalent") and not _nonempty_str(entry.get("verifier_note")):
        problems.append(f"{ident}: {status} mutant needs a verifier_note")
    return problems


def _has_test(entry: dict[str, Any]) -> bool:
    test = entry.get("proposed_test")
    return isinstance(test, dict) and _nonempty_str(test.get("name")) and _nonempty_str(test.get("asserts"))


def load_results() -> tuple[list[dict[str, Any]] | None, str]:
    """The parsed mutant list, or None plus the reason it could not be loaded."""
    try:
        data = json.loads(RESULTS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"cannot load {RESULTS.name}: {type(e).__name__}: {e}"
    if not isinstance(data, list) or not data:
        return None, f"{RESULTS.name} must be a non-empty JSON array"
    return data, ""


def check_schema(mutants: list[dict[str, Any]] | None, load_error: str) -> Check:
    """(a) every entry is well formed, ids are unique, and >= 25 count."""
    name = "(a) results.json schema, >= 25 mutants"
    if mutants is None:
        return Check(name, False, load_error)
    problems = [p for e in mutants for p in entry_problems(e)]
    ids = [e.get("id") for e in mutants if isinstance(e, dict)]
    if len(ids) != len(set(ids)):
        problems.append("duplicate ids")
    for e in mutants:
        if isinstance(e, dict) and e.get("status") == "survived" and e.get("severity") != "high" and not _has_test(e):
            problems.append(f"{e.get('id')}: survivor needs proposed_test {{name, asserts}}")
    counted = [e for e in mutants if isinstance(e, dict) and e.get("status") in ("killed", "survived")]
    if len(counted) < MIN_MUTANTS:
        problems.append(f"only {len(counted)} killed/survived mutants (need >= {MIN_MUTANTS})")
    if problems:
        return Check(
            name, False, "; ".join(problems[:8]) + (f" (+{len(problems) - 8} more)" if len(problems) > 8 else "")
        )
    by = {s: sum(e["status"] == s for e in mutants) for s in sorted(STATUSES)}
    return Check(name, True, f"{len(counted)} counted mutants; {by}")


def check_high_survivors(mutants: list[dict[str, Any]] | None) -> Check:
    """(b) every high-severity survivor has a proposed test."""
    name = "(b) every high survivor has a proposed test"
    if mutants is None:
        return Check(name, False, "results.json not loaded")
    high = [e for e in mutants if isinstance(e, dict) and e.get("status") == "survived" and e.get("severity") == "high"]
    missing = [e.get("id") for e in high if not _has_test(e)]
    if missing:
        return Check(name, False, f"high survivors without proposed_test: {missing}")
    return Check(name, True, f"{len(high)} high survivors, all with proposed_test")


def diff_problem(entry: dict[str, Any]) -> str | None:
    """Apply one mutant's diff to a temp copy of its target file(s); None means it is real."""
    ident = str(entry.get("id", "?"))
    diff = entry.get("diff")
    if not isinstance(diff, str) or not diff.strip():
        return f"{ident}: no diff"
    files = sorted(set(DIFF_FILE.findall(diff)))
    if not files:
        return f"{ident}: diff names no '+++ b/<path>' file"
    with tempfile.TemporaryDirectory(prefix="mutant-check-") as tmp:
        tmp_root = Path(tmp)
        for rel in files:
            src = ROOT / rel
            if not src.is_file():
                return f"{ident}: target {rel} does not exist in the worktree"
            (tmp_root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tmp_root / rel)
        patch = tmp_root / "mutant.diff"
        patch.write_text(diff if diff.endswith("\n") else diff + "\n", encoding="utf-8")
        run = subprocess.run(["git", "apply", "--check", str(patch)], cwd=tmp_root, capture_output=True, text=True)
        if run.returncode != 0:
            return f"{ident}: diff does not apply: {run.stderr.strip()[:120]}"
        subprocess.run(["git", "apply", str(patch)], cwd=tmp_root, capture_output=True, text=True, check=False)
        if all((tmp_root / rel).read_bytes() == (ROOT / rel).read_bytes() for rel in files):
            return f"{ident}: patched file is identical to the original"
    return None


def check_diffs(mutants: list[dict[str, Any]] | None) -> Check:
    """(c) every diff applies cleanly to the current worktree file and changes it."""
    name = "(c) every mutant diff applies and changes the file"
    if mutants is None:
        return Check(name, False, "results.json not loaded")
    problems = [p for e in mutants if isinstance(e, dict) and (p := diff_problem(e))]
    if problems:
        return Check(name, False, "; ".join(problems[:5]))
    return Check(name, True, f"{len(mutants)} diffs applied to temp copies, each changed its file")


def parse_rounds(markdown: str) -> list[tuple[int, int | None]]:
    """(round number, new_high_medium_survivors or None) for each round section in LOG.md."""
    heads = list(ROUND_HEADING.finditer(markdown))
    rounds = []
    for i, h in enumerate(heads):
        body = markdown[h.end() : heads[i + 1].start() if i + 1 < len(heads) else len(markdown)]
        m = NEW_HM.search(body)
        rounds.append((int(h.group(1)), int(m.group(1)) if m else None))
    return rounds


def check_log() -> Check:
    """(d) >= 2 rounds, and the last one adds no high or medium survivor."""
    name = "(d) LOG.md >= 2 rounds, last adds 0 high/medium survivors"
    try:
        rounds = parse_rounds(LOG.read_text(encoding="utf-8"))
    except OSError as e:
        return Check(name, False, f"cannot read LOG.md: {e}")
    if len(rounds) < MIN_ROUNDS:
        return Check(name, False, f"{len(rounds)} rounds logged (need >= {MIN_ROUNDS})")
    last_n, last_new = rounds[-1]
    if last_new is None:
        return Check(name, False, f"round {last_n} has no 'new_high_medium_survivors: <k>' line")
    return Check(name, last_new == 0, f"{len(rounds)} rounds; round {last_n} new_high_medium_survivors={last_new}")


def check_scope(base: str) -> Check:
    """(e) the branch changes only files inside review/mutants/."""
    name = f"(e) diff limited to {REVIEW_DIR}"
    diff = git("diff", "--name-only", f"{base}..HEAD", "--")
    if diff.returncode != 0:
        return Check(name, False, f"git diff failed: {diff.stderr.strip()}")
    changed = [p for p in diff.stdout.splitlines() if p]
    outside = [p for p in changed if not p.startswith(REVIEW_DIR)]
    if outside:
        return Check(name, False, f"files outside {REVIEW_DIR}: {outside}")
    return Check(name, bool(changed), f"{len(changed)} files changed vs {base}, all in {REVIEW_DIR}")


def check_clean_and_tests() -> Check:
    """(f) no uncommitted or untracked files, and the suite result matches main exactly."""
    name = "(f) worktree clean, pytest 1003 passed / 1 skipped / 5 xfailed"
    dirty = git("status", "--porcelain").stdout.strip()
    if dirty:
        return Check(name, False, f"worktree not clean: {dirty.splitlines()[:5]}")
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True
    )
    summary = (run.stdout.strip().splitlines() or ["no output"])[-1]
    counts = {k: int(n) for n, k in re.findall(r"(\d+) (passed|skipped|xfailed|failed|errors?|xpassed)", summary)}
    ok = (
        run.returncode == 0
        and all(counts.get(k) == v for k, v in EXPECTED.items())
        and not any(counts.get(k) for k in ("failed", "error", "errors", "xpassed"))
    )
    still_clean = not git("status", "--porcelain").stdout.strip()
    return Check(name, ok and still_clean, f"{summary} (expected {EXPECTED}); clean after run: {still_clean}")


def check_frozen(base: str) -> Check:
    """(g) this script (committed and on disk) matches its version in the branch's first commit."""
    name = "(g) check_success.py unchanged since commit 1"
    commits = git("rev-list", "--reverse", f"{base}..HEAD").stdout.split()
    if not commits:
        return Check(name, False, f"no commits on the branch beyond {base}")
    first = commits[0]
    if git("cat-file", "-e", f"{first}:{THIS_SCRIPT}").returncode != 0:
        return Check(name, False, f"{THIS_SCRIPT} is not in the first commit {first[:7]}")
    unchanged = git("diff", "--quiet", first, "--", THIS_SCRIPT).returncode == 0
    return Check(name, unchanged, f"vs first branch commit {first[:7]} (working tree included)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--base", default="main", help="base ref the branch was cut from (default: main)")
    args = ap.parse_args(argv)

    mutants, load_error = load_results()
    checks = [
        check_schema(mutants, load_error),
        check_high_survivors(mutants),
        check_diffs(mutants),
        check_log(),
        check_scope(args.base),
        check_clean_and_tests(),
        check_frozen(args.base),
    ]
    for c in checks:
        print(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
    ok = all(c.passed for c in checks)
    print("SUCCESS" if ok else "NOT YET")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
