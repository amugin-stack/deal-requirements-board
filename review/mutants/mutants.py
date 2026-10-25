"""Mutant definitions and the runner for the licensing-gate mutation-testing loop.

A mutant is one or more exact text edits to a file under `deal_board/`. Its unified diff is
derived from the edits with difflib, so the diff stored in results.json always matches the
real file. Mutants are never applied to the worktree: the runner works on throwaway copies.

  python review/mutants/mutants.py setup --work DIR [--workers 4]
      copy the worktree into DIR/w<k> (no .git, no .venv), give each copy its own venv with
      `pip install -e '.[dev]'`, and check an unpatched copy gives the baseline result.
  python review/mutants/mutants.py run --work DIR [--ids A B ...] [--round N]
      apply each mutant to a copy, run the full suite there, restore the copy, and record
      status / killed_by in results.json (annotation fields already there are kept).
  python review/mutants/mutants.py show --work DIR ID
      run one mutant and print its failure count per test file (the proof step).

Run it with the worktree's `.venv/bin/python`. Nothing here touches the network or a key.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RESULTS = HERE / "results.json"
COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", "*.egg-info"
)
# The worktree gives 1003 passed / 1 skipped / 5 xfailed. Copies have no .git on purpose (so
# nothing run in a copy can touch the real repo), so test_frozen_baseline_matches_tag skips there
# too. It checks deal_board/extractors/frozen/, which no mutant touches, so no kill is lost.
BASELINE = {"passed": 1002, "skipped": 2, "xfailed": 5}
TEST_TIMEOUT_SECONDS = 600
MATCHER, API, BROKERS = "deal_board/matcher.py", "deal_board/api.py", "deal_board/brokers.py"


@dataclass(frozen=True)
class Mutant:
    """A deliberate bug: exact `(old, new)` edits to `file`, each `old` occurring exactly once."""

    id: str
    file: str
    function: str
    description: str
    edits: tuple[tuple[str, str], ...]
    round: int = 1
    author: str = "lead"

    def mutated(self, original: str) -> str:
        """The file text with every edit applied; raises if an edit is missing or ambiguous."""
        text = original
        for old, new in self.edits:
            count = text.count(old)
            if count != 1:
                raise ValueError(f"{self.id}: edit target occurs {count} times in {self.file}: {old!r}")
            text = text.replace(old, new)
        if text == original:
            raise ValueError(f"{self.id}: edits leave {self.file} unchanged")
        return text

    def diff(self, root: Path = ROOT) -> str:
        """Unified diff (a/ b/ paths, 3 lines of context) of this mutant against `root`'s file."""
        original = (root / self.file).read_text(encoding="utf-8")
        lines = difflib.unified_diff(
            original.splitlines(keepends=True),
            self.mutated(original).splitlines(keepends=True),
            fromfile=f"a/{self.file}",
            tofile=f"b/{self.file}",
        )
        return "".join(lines)


# --- Mutant definitions ------------------------------------------------------------------------
# Filled in round by round from the mutant writers' proposals; see LOG.md.

MUTANTS: list[Mutant] = [
    Mutant(
        id="gate-00",
        file=MATCHER,
        function="is_licensed",
        description="Gate always returns True: every broker in every state passes, whatever the status or expiry.",
        edits=(("    return (\n        state is not None\n", "    return True or (\n        state is not None\n"),),
    ),
    # @@ROUND1@@
]


# --- Runner ------------------------------------------------------------------------------------


@dataclass
class RunOutcome:
    """What the full suite did on one patched copy."""

    id: str
    failed: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    note: str = ""


def base_python() -> str:
    """The interpreter the worktree venv was built from, used to build each copy's own venv."""
    return str(getattr(sys, "_base_executable", sys.executable))


def setup_copy(dest: Path) -> None:
    """Fresh copy of the worktree at `dest` with its own editable install."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(ROOT, dest, ignore=COPY_IGNORE)
    subprocess.run([base_python(), "-m", "venv", str(dest / ".venv")], check=True)
    pip = [str(dest / ".venv/bin/pip"), "install", "-q", "--disable-pip-version-check", "-e", ".[dev]"]
    subprocess.run(pip, cwd=dest, check=True)
    where = subprocess.run(
        [str(dest / ".venv/bin/python"), "-c", "import deal_board; print(deal_board.__file__)"],
        cwd="/",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not Path(where).resolve().is_relative_to(dest.resolve()):
        raise RuntimeError(f"copy {dest} imports deal_board from {where}, not from the copy")


def _nodeid(case: ET.Element, copy: Path) -> str:
    """Turn a junit testcase's dotted classname into a pytest node id like tests/x.py::Cls::name."""
    parts = case.get("classname", "").split(".")
    for i in range(len(parts), 0, -1):
        path = Path(*parts[:i]).with_suffix(".py")
        if (copy / path).is_file():
            return "::".join([path.as_posix(), *parts[i:], case.get("name", "")])
    return f"{case.get('classname')}::{case.get('name')}"


def run_suite(copy: Path, tag: str) -> RunOutcome:
    """Full pytest run in `copy` with its own venv; failures and errors become node ids."""
    report = copy / f".junit-{tag}.xml"
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "ANTHROPIC_API_KEY", "VIRTUAL_ENV"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [str(copy / ".venv/bin/python"), "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}"]
    outcome = RunOutcome(id=tag)
    try:
        proc = subprocess.run(cmd, cwd=copy, env=env, capture_output=True, text=True, timeout=TEST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        outcome.note = f"suite timed out after {TEST_TIMEOUT_SECONDS}s"
        outcome.failed = ["<timeout>::whole-suite"]
        return outcome
    summary = (proc.stdout.strip().splitlines() or [""])[-1]
    outcome.counts = {
        k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?)", summary)
    }
    outcome.note = summary
    if report.is_file():
        for case in ET.parse(report).getroot().iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                outcome.failed.append(_nodeid(case, copy))
        report.unlink()
    if proc.returncode != 0 and not outcome.failed:
        outcome.failed = ["<collection>::suite-error"]
        outcome.note = f"pytest exit {proc.returncode}: {summary or proc.stderr.strip()[-200:]}"
    return outcome


def run_mutant(mutant: Mutant, copy: Path) -> RunOutcome:
    """Patch `copy`, run the suite, and always restore the pristine file from the worktree."""
    target = copy / mutant.file
    original = (ROOT / mutant.file).read_text(encoding="utf-8")
    try:
        target.write_text(mutant.mutated(original), encoding="utf-8")
        shutil.rmtree(target.parent / "__pycache__", ignore_errors=True)
        outcome = run_suite(copy, mutant.id)
    finally:
        target.write_text(original, encoding="utf-8")
    outcome.id = mutant.id
    return outcome


def copies(work: Path) -> list[Path]:
    """The prepared worker copies under `work`."""
    found = sorted(p for p in work.glob("w*") if (p / ".venv/bin/python").is_file())
    if not found:
        raise SystemExit(f"no prepared copies under {work}; run `setup` first")
    return found


def run_many(mutants: list[Mutant], work: Path) -> list[RunOutcome]:
    """Run mutants in parallel, one copy per worker thread."""
    pool: Queue[Path] = Queue()
    for c in copies(work):
        pool.put(c)

    def task(m: Mutant) -> RunOutcome:
        copy = pool.get()
        try:
            out = run_mutant(m, copy)
            print(
                f"  {m.id}: {'KILLED' if out.failed else 'survived'} ({len(out.failed)} failing) {out.note}", flush=True
            )
            return out
        finally:
            pool.put(copy)

    with ThreadPoolExecutor(max_workers=pool.qsize()) as ex:
        return list(ex.map(task, mutants))


def load_results() -> list[dict[str, Any]]:
    """Current results.json, or an empty list before the first run."""
    return json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.is_file() else []


def record(outcomes: list[RunOutcome], by_id: dict[str, Mutant]) -> None:
    """Merge run outcomes into results.json, keeping verifier / proposal fields already present."""
    existing = {e["id"]: e for e in load_results()}
    for out in outcomes:
        m = by_id[out.id]
        entry = existing.get(m.id, {})
        status = "killed" if out.failed else entry.get("status") if entry.get("status") == "equivalent" else "survived"
        entry.update(
            id=m.id,
            round=m.round,
            author=m.author,
            target={"function": m.function, "file": m.file},
            description=m.description,
            diff=m.diff(),
            status=status,
            killed_by=sorted(out.failed),
            suite=out.note,
        )
        existing[m.id] = entry
    order = {m.id: i for i, m in enumerate(MUTANTS)}
    rows = sorted(existing.values(), key=lambda e: order.get(e["id"], len(order)))
    RESULTS.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--work", type=Path, required=True)
    s.add_argument("--workers", type=int, default=4)
    r = sub.add_parser("run")
    r.add_argument("--work", type=Path, required=True)
    r.add_argument("--ids", nargs="*")
    r.add_argument("--round", type=int)
    r.add_argument("--no-record", action="store_true", help="print only; leave results.json alone")
    sh = sub.add_parser("show")
    sh.add_argument("--work", type=Path, required=True)
    sh.add_argument("id")
    args = ap.parse_args(argv)
    if args.work.resolve().is_relative_to(ROOT.resolve()):
        raise SystemExit("--work must be outside the worktree (use the scratchpad)")

    if args.cmd == "setup":
        dests = [args.work / f"w{k}" for k in range(args.workers)]
        with ThreadPoolExecutor(max_workers=len(dests)) as ex:
            list(ex.map(setup_copy, dests))
        base = run_suite(dests[0], "baseline")
        ok = not base.failed and all(base.counts.get(k) == v for k, v in BASELINE.items())
        print(f"baseline on unpatched copy: {base.note} -> {'OK' if ok else 'MISMATCH'}")
        return 0 if ok else 1

    by_id = {m.id: m for m in MUTANTS}
    if args.cmd == "show":
        out = run_mutant(by_id[args.id], copies(args.work)[0])
        print(f"{args.id}: {by_id[args.id].description}")
        print(f"suite: {out.note}")
        per_file = Counter(n.split("::")[0] for n in out.failed)
        for f, n in sorted(per_file.items()):
            print(f"  {n:4d} failing in {f}")
        print(f"  {len(out.failed):4d} failing in total -> {'KILLED' if out.failed else 'SURVIVED'}")
        return 0

    chosen = [by_id[i] for i in args.ids] if args.ids else [m for m in MUTANTS if args.round in (None, m.round)]
    outcomes = run_many(chosen, args.work)
    if not args.no_record:
        record(outcomes, by_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
