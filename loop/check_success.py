"""Success test for the extractor-improvement loop. Exits 0 only if every check passes.

  python loop/check_success.py --stage dev     # tuning gate: last 3 dev-split runs of the current prompt
  python loop/check_success.py --stage final   # one-shot confirmation on both holdout sets

The thresholds are fixed for the whole loop. The loop agent must never edit this file or any other
protected path; the guard check fails if it does. A dev pass unlocks exactly one final run.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from schema import KEY_FIELDS  # noqa: E402

LOOP_START_TAG = "loop-start"
BASELINE_TAG = "baseline-v1"
PROTECTED = [  # anything the loop could edit to make the numbers look better without improving the extractor
    "loop/check_success.py", "scoring.py", "schema.py", "scripts/eval.py", "data/",
    "extractors/__init__.py", "tests/test_data.py", "tests/test_eval.py", "tests/test_brokers.py",
    "tests/test_check_success.py", ".claude/agents/extractor-improver.md",
]
DRAFT = "loop/DRAFT.md"  # only its "(FIXED)" sections are protected; the agent edits the rest

# Dev gate (posts.jsonl, split == "dev", 24 posts). Averaged over the last REPEATS runs of one prompt.
REPEATS = 3
DEV_MIN_MEAN = 0.932  # frozen regex baseline's dev mean at baseline-v1: match it on its home turf
DEV_MIN_FIELD = 0.85  # no single field may lag
DEV_MIN_IS_DEAL = 1.0

# Final gate (holdout.jsonl is the headline; the posts.jsonl holdout split must also have zero errors).
FINAL_MIN_MEAN = 0.85  # baseline is 0.764, first Claude run 0.805 (prompt de789943)
FINAL_MAX_NON_DEAL_FILLED = 1
MAX_FINAL_RUNS = 1  # one look at the holdout; more would turn it into a tuning set


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def run_mean(scores: dict) -> float:
    """Mean of is_deal_request plus the six key fields. missing_fields is reported, not gated."""
    accs = [scores["is_deal_request"]["accuracy"]] + [scores["fields"][f]["accuracy"] for f in KEY_FIELDS]
    return sum(accs) / len(accs)


def n_errors(scores: dict) -> int:
    return scores["is_deal_request"]["error"]


def dev_checks(runs: list[dict]) -> list[Check]:
    """runs: the latest dev-split results for the current prompt version, newest first."""
    if len(runs) < REPEATS:
        return [Check("repeats", False, f"need {REPEATS} dev runs of this prompt, found {len(runs)}")]
    scores = [r["datasets"]["posts_dev"]["scores"] for r in runs[:REPEATS]]
    field_means = {f: sum(s["fields"][f]["accuracy"] for s in scores) / REPEATS for f in KEY_FIELDS}
    mean = sum(run_mean(s) for s in scores) / REPEATS
    worst_field = min(field_means, key=field_means.get)
    return [
        Check("schema errors", all(n_errors(s) == 0 for s in scores),
              f"errors per run: {[n_errors(s) for s in scores]}"),
        Check("is_deal_request", all(s["is_deal_request"]["accuracy"] >= DEV_MIN_IS_DEAL for s in scores),
              f"per run: {[round(s['is_deal_request']['accuracy'], 3) for s in scores]}"),
        Check("non-deal hallucinations", all(s["non_deal_hallucinations"]["fields_filled"] == 0 for s in scores),
              f"fields filled per run: {[s['non_deal_hallucinations']['fields_filled'] for s in scores]}"),
        Check("mean accuracy", mean >= DEV_MIN_MEAN, f"{mean:.3f} (bar {DEV_MIN_MEAN})"),
        Check("weakest field", field_means[worst_field] >= DEV_MIN_FIELD,
              f"{worst_field} {field_means[worst_field]:.3f} (bar {DEV_MIN_FIELD})"),
    ]


def final_checks(finals: list[dict], dev_passed_version: str | None) -> list[Check]:
    if not finals:
        return [Check("final run", False, "no final (--split all) run since loop-start")]
    run = finals[-1]  # oldest: the first look is the one that counts
    hold, posts = run["datasets"]["holdout"]["scores"], run["datasets"]["posts"]["scores"]
    return [
        Check("one look only", len(finals) <= MAX_FINAL_RUNS, f"{len(finals)} final runs since loop-start"),
        Check("prompt passed dev first", run["prompt_version"] == dev_passed_version,
              f"final ran {run['prompt_version']}, dev passed {dev_passed_version}"),
        Check("schema errors", n_errors(hold) == 0 and n_errors(posts) == 0,
              f"holdout {n_errors(hold)}, posts {n_errors(posts)}"),
        Check("holdout mean accuracy", run_mean(hold) >= FINAL_MIN_MEAN, f"{run_mean(hold):.3f} (bar {FINAL_MIN_MEAN})"),
        Check("holdout non-deal hallucinations",
              hold["non_deal_hallucinations"]["fields_filled"] <= FINAL_MAX_NON_DEAL_FILLED,
              f"{hold['non_deal_hallucinations']['fields_filled']} (max {FINAL_MAX_NON_DEAL_FILLED})"),
    ]


# ---- repository I/O below; the checks above are pure and unit-tested ----

def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def fixed_sections(markdown: str) -> list[str]:
    """The '## ... (FIXED)' sections of a markdown file, heading included."""
    sections = ["## " + s for s in markdown.split("\n## ")[1:]]
    return [s.strip() for s in sections if s.splitlines()[0].endswith("(FIXED)")]


def guard_checks() -> list[Check]:
    protected = git("diff", "--quiet", LOOP_START_TAG, "--", *PROTECTED).returncode == 0
    original = git("show", f"{LOOP_START_TAG}:{DRAFT}").stdout
    draft_fixed = bool(original) and fixed_sections(original) == fixed_sections((ROOT / DRAFT).read_text())
    baseline = git("diff", "--quiet", BASELINE_TAG, "--", "extractors/regex_baseline.py").returncode == 0
    tests = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True)
    return [
        Check("protected files unchanged", protected, f"vs tag {LOOP_START_TAG}: {', '.join(PROTECTED)}"),
        Check("draft FIXED sections unchanged", draft_fixed, f"{DRAFT} vs tag {LOOP_START_TAG}"),
        Check("baseline frozen", baseline, f"regex_baseline.py vs {BASELINE_TAG}"),
        Check("tests pass", tests.returncode == 0, (tests.stdout.strip().splitlines() or ["no output"])[-1]),
    ]


def loop_runs(split: str) -> list[dict]:
    """Claude runs from a clean tree at or after loop-start, newest first."""
    runs = []
    for path in sorted((ROOT / "results").glob("claude_*.json"), reverse=True):
        r = json.loads(path.read_text())
        commit = r["git"]["commit"]
        after_start = git("merge-base", "--is-ancestor", LOOP_START_TAG, commit).returncode == 0
        if r.get("split", "all") == split and not r["git"]["dirty"] and after_start:
            runs.append({**r, "_file": path.name})
    return runs


def dev_runs_for(version: str) -> list[dict]:
    return [r for r in loop_runs("dev") if r["prompt_version"] == version]


def current_prompt_version() -> str:
    from extractors.claude import PROMPT_VERSION
    return PROMPT_VERSION


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["dev", "final"], required=True)
    ap.add_argument("--json", action="store_true", help="print machine-readable results")
    args = ap.parse_args(argv)

    version = current_prompt_version()
    dev = dev_checks(dev_runs_for(version))
    if args.stage == "dev":
        checks = guard_checks() + dev
    else:
        dev_ok = all(c.passed for c in dev)
        checks = guard_checks() + final_checks(loop_runs("all"), version if dev_ok else None)

    ok = all(c.passed for c in checks)
    if args.json:
        print(json.dumps({"stage": args.stage, "prompt_version": version, "passed": ok,
                          "checks": [c.__dict__ for c in checks]}, indent=2))
    else:
        print(f"stage={args.stage} prompt={version}")
        for c in checks:
            print(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
        print("SUCCESS" if ok else "NOT YET")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
