"""Frozen success check for the docs pass (branch docs-v1). Never edit after commit 1.

Run from the docs worktree: .venv/bin/python review/docs/check_success.py
Prints one PASS/FAIL line per check, then SUCCESS or FAILURE (<n> failed). Exit 0 iff SUCCESS.
Makes no API calls: it runs only offline tools (tables.py, validate.py, the review/docs scripts,
pytest, ruff, mypy) and git.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = str(ROOT / ".venv" / "bin" / "python")
BIN = ROOT / ".venv" / "bin"
README = ROOT / "README.md"

OWNED_EXACT = {"README.md", "docs/agent-graphs/docs-pass.md"}
OWNED_PREFIX = "review/docs/"
FROZEN = ["review/docs/check_success.py", "review/docs/check_blind.py"]
PROTECTED = ["results", "data", "deal_board", "tests", "tools", "loop"]

TABLES_ARGS = [
    "tools/final_eval/tables.py",
    "results/regex_20261026T003559Z.json",
    "results/claude_20261026T003610Z.json",
    "--holdout-v2",
    "results/claude_20261024T234359Z.json",
    "results/regex_20261024T224817Z.json",
]
VALIDATE_REGEX = ["tools/final_eval/validate.py", "results/regex_20261026T003559Z.json"]
VALIDATE_CLAUDE = [
    "tools/final_eval/validate.py",
    "results/claude_20261026T003610Z.json",
    "--expect-prompt",
    "ac1a145b",
    "--expect-model",
    "claude-haiku-5-5",
]

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


def run(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=check)


def git(*args: str) -> str:
    return run(["git", *args], check=True).stdout


def owned(path: str) -> bool:
    return path in OWNED_EXACT or path.startswith(OWNED_PREFIX)


def porcelain_paths(*pathspec: str) -> list[str]:
    out = run(["git", "status", "--porcelain", "--untracked-files=all", "--", *pathspec]).stdout
    return [line[3:].split(" -> ")[-1] for line in out.splitlines() if line.strip()]


def chunks_by_heading(text: str) -> list[str]:
    """Split markdown at lines starting '### '; each chunk stripped, empty ones dropped."""
    parts = re.split(r"(?m)^(?=### )", text)
    return [p.strip() for p in parts if p.strip()]


def in_order(haystack: str, needles: list[str]) -> tuple[bool, str]:
    pos = 0
    for i, n in enumerate(needles):
        at = haystack.find(n, pos)
        if at < 0:
            first = n.splitlines()[0] if n else ""
            return False, f"chunk {i + 1}/{len(needles)} not found verbatim (after earlier chunks): {first[:80]!r}"
        pos = at + len(n)
    return True, f"{len(needles)} chunks verbatim, in order"


def main() -> int:
    readme = README.read_text() if README.exists() else ""
    record("readme_exists", bool(readme), str(README))

    # (a) only owned files differ from main (committed or in the worktree)
    committed = [p for p in git("diff", "--name-only", "main...HEAD").splitlines() if p]
    pending = porcelain_paths()
    stray = sorted({p for p in committed + pending if not owned(p)})
    record("a_owned_files_only", not stray, f"outside owned files: {stray}" if stray else f"{len(committed)} files")

    # (b) tables.py stdout verbatim (split at ### headings), regenerated now
    t = run([PY, *TABLES_ARGS])
    if t.returncode != 0 or not t.stdout.strip():
        record("b_tables_verbatim", False, f"tables.py exit {t.returncode}: {t.stderr.strip()[-200:]}")
    else:
        ok, detail = in_order(readme, chunks_by_heading(t.stdout))
        record("b_tables_verbatim", ok, detail)

    # (b2) validate.py stdout verbatim for both final files, regenerated now
    vr = run([PY, *VALIDATE_REGEX])
    vc = run([PY, *VALIDATE_CLAUDE])
    record(
        "b2_validate_regex_verbatim",
        bool(vr.stdout.strip()) and vr.stdout.strip() in readme,
        f"exit {vr.returncode}; {vr.stdout.strip().splitlines()[-1] if vr.stdout.strip() else 'no output'}",
    )
    record(
        "b2_validate_claude_verbatim",
        bool(vc.stdout.strip()) and vc.stdout.strip() in readme,
        f"exit {vc.returncode}; {vc.stdout.strip().splitlines()[-1] if vc.stdout.strip() else 'no output'}",
    )
    record(
        "b2_regex_non_deal_fail_reported",
        "FAIL non_deal: 10 fields filled on non-deal posts (v3-20, v3-22, v3-23, v3-24)" in readme,
        "regex FAIL non_deal line present",
    )

    # (c) sensitivity.txt verbatim in README, and the script reproduces it; same for iterations
    generated: dict[str, str] = {}
    for stem in ("sensitivity", "iterations"):
        script = ROOT / "review" / "docs" / f"{stem}.py"
        txt = ROOT / "review" / "docs" / f"{stem}.txt"
        if not script.exists() or not txt.exists():
            record(f"c_{stem}", False, f"missing {script.name} or {txt.name}")
            continue
        out = run([PY, str(script)])
        saved = txt.read_text()
        generated[stem] = saved
        record(f"c_{stem}_reproduces", out.returncode == 0 and out.stdout == saved, f"exit {out.returncode}")
        record(f"c_{stem}_in_readme", bool(saved.strip()) and saved.strip() in readme, txt.name)

    # (d) required mentions; EM fixes come from the blind assertions
    low = readme.lower()
    for needle in ["v3-09", "v3-15", "v3-16", "first state", "second look"]:
        record(f"d_mentions_{needle.replace(' ', '_')}", needle in low, needle)

    blind_path = ROOT / "review" / "docs" / "check_blind.py"
    blind = None
    if blind_path.exists():
        spec = importlib.util.spec_from_file_location("docs_check_blind", blind_path)
        assert spec and spec.loader
        blind = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(blind)
    if blind is None:
        record("d_blind_module", False, "review/docs/check_blind.py missing")
    else:
        for name, ok, detail in blind.em_checks(readme):
            record(f"d_{name}", bool(ok), detail)

    # (e) every results/...json path named in README exists
    paths = sorted(set(re.findall(r"results/[A-Za-z0-9_.\-/]+\.json", readme)))
    missing = [p for p in paths if not (ROOT / p).is_file()]
    record("e_results_paths_exist", bool(paths) and not missing, f"{len(paths)} paths; missing {missing}")

    # (f) pytest, ruff, format, mypy
    pt = run([str(BIN / "pytest"), "-q", "-p", "no:cacheprovider"])
    summary = pt.stdout.strip().splitlines()[-1] if pt.stdout.strip() else ""

    def count(word: str) -> int:
        m = re.search(rf"(\d+) {word}", summary)
        return int(m.group(1)) if m else 0

    passed, failed, errors, skipped = count("passed"), count("failed"), count("error"), count("skipped")
    secs_m = re.search(r"in ([\d.]+)s", summary)
    secs = float(secs_m.group(1)) if secs_m else -1.0
    record(
        "f_pytest",
        pt.returncode == 0 and failed == 0 and errors == 0 and passed >= 1514,
        summary,
    )
    for name, args in [
        ("f_ruff_check", [str(BIN / "ruff"), "check", "."]),
        ("f_ruff_format", [str(BIN / "ruff"), "format", "--check", "."]),
        ("f_mypy_strict", [str(BIN / "mypy"), "--strict"]),
    ]:
        r = run(args)
        last = (r.stdout.strip() or r.stderr.strip()).splitlines()
        record(name, r.returncode == 0, last[-1] if last else "")

    # (g) frozen baseline byte-identical to baseline-v1
    frozen = (ROOT / "deal_board/extractors/frozen/regex_baseline.py").read_bytes()
    tagged = subprocess.run(
        ["git", "show", "baseline-v1:extractors/regex_baseline.py"], cwd=ROOT, capture_output=True, check=False
    ).stdout
    record("g_baseline_identical", bool(tagged) and frozen == tagged, f"{len(frozen)} bytes")

    # (h) PROMPT_VERSION
    pv = run([PY, "-c", "from deal_board.extractors.claude import PROMPT_VERSION; print(PROMPT_VERSION)"])
    record("h_prompt_version", pv.stdout.strip() == "ac1a145b", pv.stdout.strip())

    # (i) protected dirs unchanged vs main (committed, worktree, untracked)
    d = run(["git", "diff", "--name-only", "main", "--", *PROTECTED]).stdout.split()
    u = porcelain_paths(*PROTECTED)
    record("i_protected_unchanged", not d and not u, f"diff {d} untracked/modified {u}")

    # (j) frozen files unchanged since commit 1 of this branch
    revs = git("rev-list", "--reverse", "main..HEAD").split()
    if not revs:
        record("j_check_frozen", False, "no commits on branch")
    else:
        c1 = revs[0]
        in_c1 = all(run(["git", "cat-file", "-e", f"{c1}:{f}"]).returncode == 0 for f in FROZEN)
        changed = run(["git", "diff", "--name-only", c1, "--", *FROZEN]).stdout.split()
        record("j_check_frozen", in_c1 and not changed, f"commit 1 {c1[:7]}; changed since: {changed}")

    # number-trace and other blind assertions
    if blind is not None:
        ctx = {
            "root": ROOT,
            "tables_stdout": t.stdout,
            "validate_regex": vr.stdout,
            "validate_claude": vc.stdout,
            "sensitivity_txt": generated.get("sensitivity", ""),
            "iterations_txt": generated.get("iterations", ""),
            "pytest_summary": summary,
            "pytest_passed": passed,
            "pytest_skipped": skipped,
            "pytest_seconds": secs,
        }
        for name, ok, detail in blind.trace_checks(readme, ctx):
            record(f"trace_{name}", bool(ok), detail)

    # (k) clean tree
    dirty = porcelain_paths()
    record("k_clean_tree", not dirty, f"{dirty}" if dirty else "clean")

    n_fail = 0
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
        n_fail += not ok
    print("SUCCESS" if n_fail == 0 else f"FAILURE ({n_fail} failed)")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
