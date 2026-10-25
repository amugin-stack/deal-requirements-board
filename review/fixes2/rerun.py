"""Rerun mutant diffs against the current tree, each on a scratchpad copy, and save the failures.

  python review/fixes2/rerun.py setup --work DIR [--workers 4]
  python review/fixes2/rerun.py survivors --work DIR [--ids A B ...]   # -> review/fixes2/rerun.json
  python review/fixes2/rerun.py post --work DIR                        # postreview.json -> postreview_rerun.json

Why not `review/mutants/mutants.py run`: it applies the mutant edits written in its own source
(against c47ee1f), not the stored diffs; it writes into review/mutants/results.json, which this
pass must not touch; and its `setup` checks a hard-coded old baseline (1002 passed). So this
runner borrows only its copy and suite helpers (`setup_copy`, `run_suite`): a copy of the
worktree without .git, with its own venv and editable install, checked to import deal_board
from the copy.

Each mutant's diff is applied to a copy with `git apply` (a plain patch, outside any repo),
the full suite runs there, and the touched files are restored from the worktree. A diff that
does not apply is recorded as `applied: false`, never as a kill. Diffs come from
results.json, or from dispositions.json's rewritten `diff` when the stored one no longer
applies to the current tree. The output records the git tree hashes of the code, tests and data
it ran on; the runner refuses to run with uncommitted changes there, since copies take the
working tree and the record names HEAD.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RESULTS = ROOT / "review" / "mutants" / "results.json"
DISPOSITIONS = HERE / "dispositions.json"
TREE_PATHS = ("deal_board", "tests", "data", "pyproject.toml")
OUTPUTS = {"survivors": HERE / "rerun.json", "post": HERE / "postreview_rerun.json"}
BLOCKING = frozenset({"high", "medium"})


def _load_helpers() -> ModuleType:
    """review/mutants/mutants.py as a module, for its copy and suite helpers only."""
    spec = importlib.util.spec_from_file_location("gate_mutants", ROOT / "review" / "mutants" / "mutants.py")
    if spec is None or spec.loader is None:
        raise ImportError("cannot load review/mutants/mutants.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while the class is built
    spec.loader.exec_module(module)
    return module


HELPERS = _load_helpers()


def git(*args: str, cwd: Path = ROOT, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, input=stdin)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tree() -> dict[str, str]:
    """The HEAD tree hash of each path the suite depends on."""
    return {p: git("rev-parse", f"HEAD:{p}").stdout.strip() for p in TREE_PATHS}


def require_clean() -> None:
    dirty = git("status", "--porcelain", "--untracked-files=all", "--", *TREE_PATHS).stdout.strip()
    if dirty:
        raise SystemExit(f"commit first; uncommitted changes under {TREE_PATHS}:\n{dirty}")


def applies(diff: str, cwd: Path) -> bool:
    return git("apply", "--check", "-", cwd=cwd, stdin=diff).returncode == 0


def diff_files(diff: str) -> list[str]:
    return sorted({ln[6:].strip() for ln in diff.splitlines() if ln.startswith("+++ b/")})


def survivor_diffs(ids: list[str] | None) -> dict[str, tuple[str, str]]:
    """id -> (diff, source) for every high/medium survivor: the stored diff if it applies, else the rewrite."""
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    disp = json.loads(DISPOSITIONS.read_text(encoding="utf-8")) if DISPOSITIONS.is_file() else {}
    chosen: dict[str, tuple[str, str]] = {}
    for m in results:
        if m.get("status") != "survived" or m.get("severity") not in BLOCKING or (ids and m["id"] not in ids):
            continue
        if applies(m["diff"], ROOT):
            chosen[m["id"]] = (m["diff"], "original")
        else:
            chosen[m["id"]] = (disp.get(m["id"], {}).get("diff", ""), "rewritten")
    return chosen


def post_diffs() -> dict[str, tuple[str, str]]:
    spec = json.loads((HERE / "postreview.json").read_text(encoding="utf-8"))
    return {m["id"]: (m["diff"], "post") for m in spec["mutants"]}


def run_one(key: str, diff: str, copy: Path) -> dict[str, Any]:
    """Apply `diff` to `copy`, run the suite, restore the touched files from the worktree."""
    files = diff_files(diff)
    entry: dict[str, Any] = {"diff_sha256": sha256(diff), "files": files, "applied": False, "failed": []}
    if not diff or not applies(diff, copy):
        entry["note"] = "diff does not apply to the copy"
        return entry
    try:
        if git("apply", "-", cwd=copy, stdin=diff).returncode != 0:
            entry["note"] = "git apply failed"
            return entry
        entry["applied"] = True
        for f in files:
            shutil.rmtree((copy / f).parent / "__pycache__", ignore_errors=True)
        out = HELPERS.run_suite(copy, key)
        entry.update(failed=sorted(out.failed), counts=out.counts, note=out.note)
    finally:
        for f in files:
            shutil.copy2(ROOT / f, copy / f)
    entry["killed_by_count"] = len(entry["failed"])
    return entry


def run_all(diffs: dict[str, tuple[str, str]], work: Path) -> dict[str, Any]:
    copies = HELPERS.copies(work)
    for c in copies:  # the copies must hold today's tree, not the one `setup` saw
        for p in ("deal_board", "tests", "data"):
            shutil.rmtree(c / p)
            shutil.copytree(ROOT / p, c / p, ignore=HELPERS.COPY_IGNORE)
        shutil.copy2(ROOT / "pyproject.toml", c / "pyproject.toml")
        # Copies leave out .git so nothing can touch the real repo, but one test asks git which
        # files .gitignore covers. An empty repo of the copy's own answers that and holds nothing.
        if not (c / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=c, check=True)
    base = HELPERS.run_suite(copies[0], "baseline")
    print(f"baseline (unpatched copy): {base.note}", flush=True)
    if base.failed:  # a failure here would be counted as a kill for every mutant
        raise SystemExit(f"unpatched copy is not green: {base.failed[:5]}")
    pool: Queue[Path] = Queue()
    for c in copies:
        pool.put(c)

    def task(item: tuple[str, tuple[str, str]]) -> tuple[str, dict[str, Any]]:
        key, (diff, source) = item
        copy = pool.get()
        try:
            entry = {"diff_source": source, **run_one(key, diff, copy)}
            state = "KILLED" if entry["failed"] else ("NOT APPLIED" if not entry["applied"] else "survived")
            print(f"  {key}: {state} ({len(entry['failed'])} failing) {entry.get('note', '')}", flush=True)
            return key, entry
        finally:
            pool.put(copy)

    with ThreadPoolExecutor(max_workers=len(copies)) as ex:
        mutants = dict(ex.map(task, sorted(diffs.items())))
    baseline = {"failed": sorted(base.failed), "counts": base.counts, "note": base.note}
    return {"tree": tree(), "baseline": baseline, "mutants": mutants}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--work", type=Path, required=True)
    s.add_argument("--workers", type=int, default=4)
    for name in ("survivors", "post"):
        p = sub.add_parser(name)
        p.add_argument("--work", type=Path, required=True)
        p.add_argument("--ids", nargs="*")
    args = ap.parse_args(argv)
    if args.work.resolve().is_relative_to(ROOT.resolve()):
        raise SystemExit("--work must be outside the worktree (use the scratchpad)")

    if args.cmd == "setup":
        dests = [args.work / f"w{k}" for k in range(args.workers)]
        with ThreadPoolExecutor(max_workers=len(dests)) as ex:
            list(ex.map(HELPERS.setup_copy, dests))
        print(f"prepared {len(dests)} copies under {args.work}")
        return 0

    require_clean()
    diffs = survivor_diffs(args.ids) if args.cmd == "survivors" else post_diffs()
    if args.ids and args.cmd == "post":
        diffs = {k: v for k, v in diffs.items() if k in args.ids}
    report = run_all(diffs, args.work)
    out = OUTPUTS[args.cmd]
    if args.ids and out.is_file():  # a partial rerun updates only those ids
        old = json.loads(out.read_text(encoding="utf-8"))
        if old.get("tree") == report["tree"]:
            report["mutants"] = {**old.get("mutants", {}), **report["mutants"]}
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    killed = sum(1 for e in report["mutants"].values() if e["failed"])
    print(f"wrote {out.relative_to(ROOT)}: {killed}/{len(report['mutants'])} killed")
    for key, e in sorted(report["mutants"].items()):
        print(f"  {key}: killed_by {len(e['failed'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
