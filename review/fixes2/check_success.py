"""Single definition of success for fix pass 2: closing the gate-mutant survivors.

  python review/fixes2/check_success.py            # compare against branch `main`
  python review/fixes2/check_success.py --base X   # compare against another base ref

Exits 0 only when every check passes; prints one PASS/FAIL line per check, like the other
`review/*/check_success.py` scripts. The checks:

  (a) dispositions.json has exactly one entry per high/medium survivor in
      review/mutants/results.json (status "survived", severity high or medium), each with a
      disposition in {killed, wontfix, deferred} and a note; `killed` also names `test`, the
      pytest node id that now fails on the mutant;
  (b) every high survivor is killed; every medium is killed, or deferred with a note starting
      "owner decision" or "needs data/raw";
  (c) rerun.json was produced by rerunning every survivor's mutant on a scratchpad copy of the
      tree as committed now (tree hashes match, no uncommitted changes in those paths). The
      stored diff is used whenever it still applies; otherwise dispositions.json must give a
      rewritten `diff` (plus `rewrite_reason`) that applies, and the rerun must have used that
      exact diff. The unpatched copy had 0 failures, and every `killed` survivor has >= 1
      failing test, its named `test` among them;
  (d) the full suite: 0 failed, 0 errors, 0 xfailed, 0 xpassed, at least 1451 passed;
  (e) ruff check, ruff format --check and mypy --strict are clean;
  (f) the frozen baseline is byte-identical to tag baseline-v1 (committed and on disk);
  (g) PROMPT_VERSION is still ac1a145b;
  (h) the protected paths are unchanged vs base (committed, staged, unstaged, untracked);
  (i) data/fictional_brokers.csv: header and every base row unchanged and in order; any added
      row has a broker_id starting "fx-", source "fictional", a name starting
      "Fictional Broker" and a license number starting "FICT-";
  (j) this script is unchanged since the first commit on the branch;
  (k) POSTREVIEW.md records a post-fix mutation round: postreview.json holds >= 15 new mutants
      (ids not in results.json, an author, a description, a severity, a diff that applies to
      the current tree and touches only the survivors' files, `verified_real: true` with a
      verifier note); postreview_rerun.json reran exactly those diffs on the current tree; no
      high or medium one survived; and POSTREVIEW.md says `new_high_medium_survivors: 0`.

Run it with the worktree's interpreter (`.venv/bin/python`); pytest, ruff and mypy are run as
modules of that same interpreter. Nothing here touches the network or a key. This file is
frozen after the branch's first commit; (j) enforces that.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
THIS_SCRIPT = "review/fixes2/check_success.py"
RESULTS = ROOT / "review" / "mutants" / "results.json"
DISPOSITIONS = HERE / "dispositions.json"
RERUN = HERE / "rerun.json"
POSTREVIEW_MD = HERE / "POSTREVIEW.md"
POSTREVIEW_JSON = HERE / "postreview.json"
POSTREVIEW_RERUN = HERE / "postreview_rerun.json"

FINAL_DISPOSITIONS = frozenset({"killed", "wontfix", "deferred"})
MEDIUM_DEFERRAL_PREFIXES = ("owner decision", "needs data/raw")
BLOCKING = frozenset({"high", "medium"})
SEVERITIES = frozenset({"high", "medium", "low"})
MIN_PASSED = 1451
MIN_POST_MUTANTS = 15
EXPECTED_PROMPT_VERSION = "ac1a145b"
FROZEN_TAG_PATH = "baseline-v1:extractors/regex_baseline.py"
FROZEN_PATH = "deal_board/extractors/frozen/regex_baseline.py"
FICTIONAL = "data/fictional_brokers.csv"
# Paths whose committed content the rerun must match: the code, the tests and their data.
TREE_PATHS = ("deal_board", "tests", "data", "pyproject.toml")
UNTOUCHED = (
    "results/",
    "data/brokers.csv",
    "data/posts.jsonl",
    "data/holdout.jsonl",
    "data/holdout_v3.jsonl",
    "data/red_team.jsonl",
    ":(glob)data/*.jsonl",
    "review/mutants",
    "review/fixes/",
    "tools",
    "README.md",
    "loop",
)

NEW_HIGH_MEDIUM = re.compile(r"new_high_medium_survivors:\s*`?(\d+)`?")
SUMMARY_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|xfailed|xpassed|skipped)")
DIFF_FILE = re.compile(r"^(?:---|\+\+\+) (?:a|b)/(\S+)", re.MULTILINE)


@dataclass(frozen=True)
class Check:
    """One named success criterion and why it passed or failed."""

    name: str
    passed: bool
    detail: str


def run(*cmd: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command in the worktree root and capture its output."""
    return subprocess.run(list(cmd), cwd=ROOT, capture_output=True, text=True, input=stdin)


def git(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run git in the worktree root."""
    return run("git", *args, stdin=stdin)


def nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json(path: Path) -> tuple[Any, str]:
    """Parsed JSON, or None plus why it failed to load."""
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except (OSError, ValueError) as e:
        return None, f"cannot load {path.relative_to(ROOT)}: {type(e).__name__}"


def diff_applies(diff: str) -> bool:
    """True if `diff` applies cleanly to the working tree (checked only, nothing is written)."""
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8") as f:
        f.write(diff)
        name = f.name
    try:
        return git("apply", "--check", name).returncode == 0
    finally:
        Path(name).unlink(missing_ok=True)


def diff_files(diff: str) -> set[str]:
    return set(DIFF_FILE.findall(diff))


def current_tree() -> dict[str, str]:
    """`git rev-parse HEAD:<path>` for each of TREE_PATHS: what the rerun must have run on."""
    return {p: git("rev-parse", f"HEAD:{p}").stdout.strip() for p in TREE_PATHS}


def tree_is_clean() -> bool:
    """No staged, unstaged or untracked change under TREE_PATHS, so HEAD is what is on disk."""
    out = git("status", "--porcelain", "--untracked-files=all", "--", *TREE_PATHS)
    return out.returncode == 0 and not out.stdout.strip()


def load_survivors() -> tuple[dict[str, dict[str, Any]] | None, str]:
    """id -> results.json entry for every high/medium survivor."""
    data, error = load_json(RESULTS)
    if data is None:
        return None, error
    return {m["id"]: m for m in data if m.get("status") == "survived" and m.get("severity") in BLOCKING}, ""


def load_dispositions() -> tuple[dict[str, dict[str, Any]] | None, str]:
    data, error = load_json(DISPOSITIONS)
    if data is None:
        return None, error
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        return None, "dispositions.json must be an object of id -> entry objects"
    return data, ""


def check_dispositions(
    survivors: dict[str, dict[str, Any]] | None, disp: dict[str, dict[str, Any]] | None, error: str
) -> Check:
    """(a) one complete entry per high/medium survivor."""
    name = "(a) dispositions complete"
    if survivors is None or disp is None:
        return Check(name, False, error)
    problems: list[str] = []
    if missing := sorted(set(survivors) - set(disp)):
        problems.append(f"missing {missing}")
    if extra := sorted(set(disp) - set(survivors)):
        problems.append(f"not high/medium survivors {extra}")
    for key in sorted(set(survivors) & set(disp)):
        entry = disp[key]
        if entry.get("disposition") not in FINAL_DISPOSITIONS:
            problems.append(f"{key}: disposition {entry.get('disposition')!r}")
        if not nonempty_str(entry.get("note")):
            problems.append(f"{key}: no note")
        if entry.get("disposition") == "killed" and not (nonempty_str(entry.get("test")) and "::" in entry["test"]):
            problems.append(f"{key}: killed needs `test` as a pytest node id")
    detail = f"{len(survivors)} survivors; " + ("; ".join(problems[:6]) if problems else "all have an entry")
    return Check(name, not problems, detail)


def check_severities(survivors: dict[str, dict[str, Any]] | None, disp: dict[str, dict[str, Any]] | None) -> Check:
    """(b) highs all killed; mediums killed or deferred for a stated reason."""
    name = "(b) highs killed, mediums killed/deferred"
    if survivors is None or disp is None:
        return Check(name, False, "inputs did not load")
    bad: list[str] = []
    for key, m in sorted(survivors.items()):
        entry = disp.get(key, {})
        d = entry.get("disposition")
        if m["severity"] == "high" and d != "killed":
            bad.append(f"{key} (high) is {d}")
        if m["severity"] == "medium" and d != "killed":
            note = str(entry.get("note", "")).strip().lower()
            if not (d == "deferred" and note.startswith(MEDIUM_DEFERRAL_PREFIXES)):
                bad.append(f"{key} (medium) is {d} without an allowed reason")
    highs = sum(1 for m in survivors.values() if m["severity"] == "high")
    return Check(name, not bad, "; ".join(bad[:6]) if bad else f"{highs} high killed; mediums ok")


def expected_diff(key: str, survivor: dict[str, Any], entry: dict[str, Any]) -> tuple[str | None, str, str]:
    """(diff the rerun must have used, its source, problem) for one survivor."""
    if diff_applies(survivor["diff"]):
        return survivor["diff"], "original", ""
    rewritten = entry.get("diff")
    if not nonempty_str(rewritten):
        return None, "", f"{key}: stored diff no longer applies and no rewritten `diff` given"
    if not nonempty_str(entry.get("rewrite_reason")):
        return None, "", f"{key}: rewritten diff needs a `rewrite_reason`"
    if not diff_applies(rewritten):
        return None, "", f"{key}: rewritten diff does not apply to the current tree"
    if diff_files(rewritten) != diff_files(survivor["diff"]):
        return None, "", f"{key}: rewritten diff touches {sorted(diff_files(rewritten))}, not the original's file"
    return rewritten, "rewritten", ""


def node_failed(node: str, failed: list[str]) -> bool:
    """A node id failed itself, or (for an unparametrised name) some param of it failed."""
    return node in failed or any(f.startswith(node + "[") for f in failed)


def check_rerun_meta(data: Any, label: str) -> list[str]:
    """Problems with a rerun file's provenance: wrong tree, dirty tree, or a broken baseline."""
    if not isinstance(data, dict) or not isinstance(data.get("mutants"), dict):
        return [f"{label}: expected an object with `mutants`"]
    problems: list[str] = []
    if data.get("tree") != current_tree():
        problems.append(f"{label}: ran on another tree than HEAD's {', '.join(TREE_PATHS)}")
    if not tree_is_clean():
        problems.append("uncommitted changes under " + ", ".join(TREE_PATHS))
    base = data.get("baseline") or {}
    if base.get("failed") != [] or int((base.get("counts") or {}).get("passed", 0)) < MIN_PASSED - 1:
        problems.append(f"{label}: unpatched copy was not green: {base.get('note')!r}")
    return problems


def check_rerun(survivors: dict[str, dict[str, Any]] | None, disp: dict[str, dict[str, Any]] | None) -> Check:
    """(c) every survivor rerun against the current tree; every killed one has failing tests."""
    name = "(c) rerun.json: killed mutants now fail"
    if survivors is None or disp is None:
        return Check(name, False, "inputs did not load")
    data, error = load_json(RERUN)
    if data is None:
        return Check(name, False, error)
    problems = check_rerun_meta(data, "rerun.json")
    rerun: dict[str, Any] = data.get("mutants", {}) if isinstance(data, dict) else {}
    killed = 0
    for key, m in sorted(survivors.items()):
        entry = disp.get(key, {})
        diff, source, problem = expected_diff(key, m, entry)
        if diff is None:
            problems.append(problem)
            continue
        if "deal_board/extractors/frozen/regex_baseline.py" in diff_files(diff):
            problems.append(f"{key}: a mutant may not touch the frozen baseline")
        got = rerun.get(key)
        if not isinstance(got, dict):
            problems.append(f"{key}: not in rerun.json")
            continue
        if got.get("diff_sha256") != sha256(diff) or got.get("diff_source") != source or got.get("applied") is not True:
            problems.append(f"{key}: rerun did not apply the expected {source} diff")
            continue
        failed = got.get("failed")
        if not isinstance(failed, list):
            problems.append(f"{key}: no failed list")
            continue
        if entry.get("disposition") == "killed":
            if not failed:
                problems.append(f"{key}: marked killed but 0 tests fail on the mutant")
            elif not node_failed(str(entry.get("test")), failed):
                problems.append(f"{key}: named test {entry.get('test')} is not among the {len(failed)} failing")
            else:
                killed += 1
    detail = "; ".join(problems[:6]) if problems else f"{killed} killed mutants each fail >= 1 test"
    return Check(name, not problems, detail)


def summary_counts(output: str) -> dict[str, int]:
    """Outcome counts from pytest's final summary line ('3 passed, 1 skipped in 0.1s')."""
    lines = [ln for ln in output.strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    counts: dict[str, int] = {}
    for n, kind in SUMMARY_COUNTS.findall(last):
        counts["errors" if kind.startswith("error") else kind] = int(n)
    return counts


def check_suite() -> Check:
    """(d) the whole suite is green with nothing hidden behind xfail."""
    name = "(d) pytest green"
    result = run(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")
    counts = summary_counts(result.stdout)
    bad = {k: counts.get(k, 0) for k in ("failed", "errors", "xfailed", "xpassed") if counts.get(k, 0)}
    ok = result.returncode == 0 and not bad and counts.get("passed", 0) >= MIN_PASSED
    return Check(name, ok, f"{counts} (need >= {MIN_PASSED} passed, 0 failed/errors/xfailed/xpassed)")


def check_lint() -> Check:
    """(e) ruff lint, ruff format and mypy --strict all clean."""
    name = "(e) ruff + format + mypy --strict"
    commands = {
        "ruff check": (sys.executable, "-m", "ruff", "check", "."),
        "ruff format --check": (sys.executable, "-m", "ruff", "format", "--check", "."),
        "mypy --strict": (sys.executable, "-m", "mypy", "--strict"),
    }
    failed = [label for label, cmd in commands.items() if run(*cmd).returncode != 0]
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
    """(g) the extractor's prompt identity is unchanged, so saved results stay comparable."""
    name = "(g) PROMPT_VERSION == ac1a145b"
    code = (
        f"from deal_board.extractors.claude import PROMPT_VERSION; assert PROMPT_VERSION=={EXPECTED_PROMPT_VERSION!r}"
    )
    result = run(sys.executable, "-c", code)
    return Check(name, result.returncode == 0, "unchanged" if result.returncode == 0 else result.stderr.strip()[-200:])


def check_untouched(base: str) -> Check:
    """(h) saved results, real data, eval sets and earlier loops' records are untouched."""
    name = "(h) protected paths untouched"
    quiet = git("diff", "--quiet", base, "--", *UNTOUCHED).returncode == 0
    names = git("diff", "--name-only", base, "--", *UNTOUCHED).stdout.split()
    untracked = git("status", "--porcelain", "--untracked-files=all", "--", *UNTOUCHED).stdout.splitlines()
    changed = names + [p for p in untracked if p]
    if not quiet or changed:
        return Check(name, False, f"changed vs {base}: {changed[:8]}")
    return Check(name, True, f"no changes vs {base}")


def read_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(text))
    return list(reader.fieldnames or []), list(reader)


def check_fictional(base: str) -> Check:
    """(i) fictional brokers only grow, and only by obviously fictional rows."""
    name = "(i) fictional_brokers.csv only gains fictional rows"
    shown = git("show", f"{base}:{FICTIONAL}")
    if shown.returncode != 0:
        return Check(name, False, f"cannot read {base}:{FICTIONAL}")
    try:
        now_text = (ROOT / FICTIONAL).read_text(encoding="utf-8")
    except OSError as e:
        return Check(name, False, f"cannot read {FICTIONAL}: {type(e).__name__}")
    head_base, rows_base = read_csv(shown.stdout)
    head_now, rows_now = read_csv(now_text)
    if head_now != head_base:
        return Check(name, False, "header changed")
    if rows_now[: len(rows_base)] != rows_base:
        return Check(name, False, "an existing row changed, moved or was removed")
    bad = [
        r.get("broker_id")
        for r in rows_now[len(rows_base) :]
        if not (
            str(r.get("broker_id", "")).startswith("fx-")
            and r.get("source") == "fictional"
            and str(r.get("name", "")).startswith("Fictional Broker")
            and str(r.get("license_number", "")).startswith("FICT-")
        )
    ]
    added = len(rows_now) - len(rows_base)
    return Check(name, not bad, f"bad added rows: {bad}" if bad else f"{added} added row(s), base rows unchanged")


def check_frozen_script(base: str) -> Check:
    """(j) this script (committed and on disk) matches the branch's first commit."""
    name = "(j) check_success.py unchanged since commit 1"
    commits = git("rev-list", "--reverse", f"{base}..HEAD").stdout.split()
    if not commits:
        return Check(name, False, f"no commits on the branch beyond {base}")
    first = commits[0]
    if git("cat-file", "-e", f"{first}:{THIS_SCRIPT}").returncode != 0:
        return Check(name, False, f"{THIS_SCRIPT} is not in the first commit {first[:7]}")
    unchanged = git("diff", "--quiet", first, "--", THIS_SCRIPT).returncode == 0
    return Check(name, unchanged, f"vs first branch commit {first[:7]} (working tree included)")


def check_postreview(survivors: dict[str, dict[str, Any]] | None) -> Check:
    """(k) a fresh post-fix mutation round on the fixed code left no high/medium survivor."""
    name = "(k) post-fix mutation round converged"
    results, error = load_json(RESULTS)
    spec, spec_error = load_json(POSTREVIEW_JSON)
    rerun, rerun_error = load_json(POSTREVIEW_RERUN)
    if results is None or spec is None or rerun is None or survivors is None:
        return Check(name, False, error or spec_error or rerun_error or "survivors did not load")
    try:
        md = POSTREVIEW_MD.read_text(encoding="utf-8")
    except OSError as e:
        return Check(name, False, f"cannot read {POSTREVIEW_MD.name}: {type(e).__name__}")
    mutants = spec.get("mutants") if isinstance(spec, dict) else None
    if not isinstance(mutants, list):
        return Check(name, False, "postreview.json needs a `mutants` list")
    old_ids = {m["id"] for m in results}
    regions = set().union(*(diff_files(m["diff"]) for m in survivors.values()))
    problems = check_rerun_meta(rerun, "postreview_rerun.json")
    ran: dict[str, Any] = rerun.get("mutants", {}) if isinstance(rerun, dict) else {}
    ids: list[str] = []
    blocking_survivors: list[str] = []
    for m in mutants:
        key = str(m.get("id", ""))
        ids.append(key)
        if not key or key in old_ids:
            problems.append(f"{key or '<no id>'}: id missing or reused from results.json")
        if not all(nonempty_str(m.get(f)) for f in ("author", "description", "diff", "verifier_note")):
            problems.append(f"{key}: needs author, description, diff and verifier_note")
            continue
        if m.get("severity") not in SEVERITIES or m.get("verified_real") is not True:
            problems.append(f"{key}: needs a severity and verified_real: true")
        files = diff_files(m["diff"])
        if not files or not files <= regions or FROZEN_PATH in files:
            problems.append(f"{key}: touches {sorted(files)}, outside the survivors' files")
        if not diff_applies(m["diff"]):
            problems.append(f"{key}: diff does not apply to the current tree")
        got = ran.get(key)
        if not isinstance(got, dict) or got.get("diff_sha256") != sha256(m["diff"]) or got.get("applied") is not True:
            problems.append(f"{key}: not rerun with this diff")
            continue
        if not got.get("failed") and m.get("severity") in BLOCKING:
            blocking_survivors.append(key)
    if len(set(ids)) != len(ids):
        problems.append("duplicate ids")
    if len(mutants) < MIN_POST_MUTANTS:
        problems.append(f"{len(mutants)} mutants, need >= {MIN_POST_MUTANTS}")
    if blocking_survivors:
        problems.append(f"high/medium survivors: {blocking_survivors}")
    stated = NEW_HIGH_MEDIUM.findall(md)
    if not stated or int(stated[-1]) != 0:
        problems.append("POSTREVIEW.md must end its record with `new_high_medium_survivors: 0`")
    killed = sum(1 for k in ids if (ran.get(k) or {}).get("failed"))
    detail = (
        "; ".join(problems[:6]) if problems else f"{len(mutants)} new mutants, {killed} killed, 0 high/medium survive"
    )
    return Check(name, not problems, detail)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--base", default="main", help="base ref the branch was cut from (default: main)")
    args = ap.parse_args(argv)

    survivors, surv_error = load_survivors()
    disp, disp_error = load_dispositions()
    checks = [
        check_dispositions(survivors, disp, surv_error or disp_error),
        check_severities(survivors, disp),
        check_rerun(survivors, disp),
        check_suite(),
        check_lint(),
        check_frozen_baseline(),
        check_prompt_version(),
        check_untouched(args.base),
        check_fictional(args.base),
        check_frozen_script(args.base),
        check_postreview(survivors),
    ]
    for c in checks:
        print(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
    ok = all(c.passed for c in checks)
    print("SUCCESS" if ok else "NOT YET")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
