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

from deal_board.schema import KEY_FIELDS, JSONObject

# Worked out from this file, not from deal_board.paths: the guard must not trust a module it
# doesn't protect to tell it where the repository is.
ROOT = Path(__file__).resolve().parent.parent

LOOP_START_TAG = "loop-start"
BASELINE_TAG = "baseline-v1"
PROTECTED = [  # anything the loop could edit to make the numbers look better without improving the extractor
    "loop/check_success.py",
    "deal_board/scoring.py",
    "deal_board/schema.py",
    "deal_board/cli/eval.py",
    "data/",
    "deal_board/extractors/__init__.py",
    "tests/test_data.py",
    "tests/test_eval.py",
    "tests/test_brokers.py",
    "tests/test_check_success.py",
    ".claude/agents/extractor-improver.md",
    "deal_board/paths.py",  # P10: where eval reads data from
    "pyproject.toml",  # P10: pytest config (addopts could deselect the tests the guard runs)
]
# P10 moved files into the deal_board package. The guard compares each moved file's current
# content with its content at the tag under the path it had then, so a rename by itself is not
# counted as an edit. (P10 also reformatted most of them, so they do differ from loop-start.)
PATH_AT_TAG = {
    "deal_board/scoring.py": "scoring.py",
    "deal_board/schema.py": "schema.py",
    "deal_board/cli/eval.py": "scripts/eval.py",
    "deal_board/extractors/__init__.py": "extractors/__init__.py",
    "deal_board/extractors/frozen/regex_baseline.py": "extractors/regex_baseline.py",
}
BASELINE = "deal_board/extractors/frozen/regex_baseline.py"
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


@dataclass(frozen=True)
class RunIdentity:
    """What makes two runs samples of the same request.

    PROMPT_VERSION hashes only SYSTEM_PROMPT and TOOL, so on its own it would pool runs made
    with another model, max_tokens, tool_choice or message template. request_sha covers those
    and the model; the model is kept as well so a mismatch reads plainly in the gate output.
    """

    prompt_version: str
    model: str
    request_sha: str

    def __str__(self) -> str:
        return f"{self.prompt_version}/{self.model}/{self.request_sha}"


def run_identity(run: JSONObject) -> RunIdentity | None:
    """The run's identity, or None if any part is missing (runs saved before request_sha existed)."""
    parts = (run.get("prompt_version"), run.get("model"), run.get("request_sha"))
    if not all(isinstance(p, str) and p for p in parts):
        return None
    version, model, sha = (str(p) for p in parts)
    return RunIdentity(version, model, sha)


def same_request(run: JSONObject, identity: RunIdentity | None) -> bool:
    """Fail closed: an unidentified run, or an unknown current identity, matches nothing."""
    return identity is not None and run_identity(run) == identity


def run_mean(scores: JSONObject) -> float:
    """Mean of is_deal_request plus the six key fields. missing_fields is reported, not gated."""
    accs: list[float] = [scores["is_deal_request"]["accuracy"]] + [scores["fields"][f]["accuracy"] for f in KEY_FIELDS]
    return sum(accs) / len(accs)


def n_errors(scores: JSONObject) -> int:
    errors: int = scores["is_deal_request"]["error"]
    return errors


def run_files(runs: list[JSONObject]) -> str:
    return ", ".join(str(r.get("_file", "?")) for r in runs) or "none"


def repeats_check(runs: list[JSONObject]) -> Check:
    """Exactly REPEATS runs per request. More would let reruns replace bad runs until REPEATS
    good ones come up (optional stopping), so a rerun needs a new prompt version instead."""
    return Check(
        "repeats",
        len(runs) == REPEATS,
        f"need exactly {REPEATS} dev runs of this request, found {len(runs)}: {run_files(runs)}",
    )


def dev_checks(runs: list[JSONObject]) -> list[Check]:
    """runs: every clean dev-split result for the current request identity, newest first."""
    if len(runs) < REPEATS:
        return [repeats_check(runs)]
    scores = [r["datasets"]["posts_dev"]["scores"] for r in runs[:REPEATS]]
    field_means: dict[str, float] = {f: sum(s["fields"][f]["accuracy"] for s in scores) / REPEATS for f in KEY_FIELDS}
    mean = sum(run_mean(s) for s in scores) / REPEATS
    worst_field = min(field_means, key=field_means.__getitem__)
    return [
        repeats_check(runs),
        Check(
            "schema errors", all(n_errors(s) == 0 for s in scores), f"errors per run: {[n_errors(s) for s in scores]}"
        ),
        Check(
            "is_deal_request",
            all(s["is_deal_request"]["accuracy"] >= DEV_MIN_IS_DEAL for s in scores),
            f"per run: {[round(s['is_deal_request']['accuracy'], 3) for s in scores]}",
        ),
        Check(
            "non-deal hallucinations",
            all(s["non_deal_hallucinations"]["fields_filled"] == 0 for s in scores),
            f"fields filled per run: {[s['non_deal_hallucinations']['fields_filled'] for s in scores]}",
        ),
        Check("mean accuracy", mean >= DEV_MIN_MEAN, f"{mean:.3f} (bar {DEV_MIN_MEAN})"),
        Check(
            "weakest field",
            field_means[worst_field] >= DEV_MIN_FIELD,
            f"{worst_field} {field_means[worst_field]:.3f} (bar {DEV_MIN_FIELD})",
        ),
    ]


def final_checks(finals: list[JSONObject], dev_passed: RunIdentity | None) -> list[Check]:
    """finals: every final run since loop-start, newest first. dev_passed: the identity the dev
    gate passed for, or None if it did not pass."""
    if not finals:
        return [Check("final run", False, "no final (--split all) run since loop-start")]
    run = finals[-1]  # oldest: the first look is the one that counts
    hold, posts = run["datasets"]["holdout"]["scores"], run["datasets"]["posts"]["scores"]
    return [
        Check("one look only", len(finals) <= MAX_FINAL_RUNS, f"{len(finals)} final runs since loop-start"),
        Check(
            "prompt passed dev first",
            same_request(run, dev_passed),
            f"final ran {run_identity(run) or 'an unidentified request'}, dev passed {dev_passed}",
        ),
        Check(
            "schema errors",
            n_errors(hold) == 0 and n_errors(posts) == 0,
            f"holdout {n_errors(hold)}, posts {n_errors(posts)}",
        ),
        Check(
            "holdout mean accuracy", run_mean(hold) >= FINAL_MIN_MEAN, f"{run_mean(hold):.3f} (bar {FINAL_MIN_MEAN})"
        ),
        Check(
            "holdout non-deal hallucinations",
            hold["non_deal_hallucinations"]["fields_filled"] <= FINAL_MAX_NON_DEAL_FILLED,
            f"{hold['non_deal_hallucinations']['fields_filled']} (max {FINAL_MAX_NON_DEAL_FILLED})",
        ),
    ]


# ---- repository I/O below; the checks above are pure and unit-tested ----


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def unchanged_since(tag: str, path: str) -> bool:
    """True if `path` in the working tree matches `tag`, following a P10 move if there was one.

    Unmoved paths (directories included) use `git diff --quiet`, as before. A moved file must
    equal, byte for byte, the blob at its old path in the tag; a missing file or blob is a change.
    """
    old = PATH_AT_TAG.get(path)
    if old is None:
        return git("diff", "--quiet", tag, "--", path).returncode == 0
    at_tag = subprocess.run(["git", "show", f"{tag}:{old}"], cwd=ROOT, capture_output=True)
    current = ROOT / path
    return at_tag.returncode == 0 and current.is_file() and current.read_bytes() == at_tag.stdout


def fixed_sections(markdown: str) -> list[str]:
    """The '## ... (FIXED)' sections of a markdown file, heading included."""
    sections = ["## " + s for s in markdown.split("\n## ")[1:]]
    return [s.strip() for s in sections if s.splitlines()[0].endswith("(FIXED)")]


def guard_checks() -> list[Check]:
    protected = all(unchanged_since(LOOP_START_TAG, path) for path in PROTECTED)
    original = git("show", f"{LOOP_START_TAG}:{DRAFT}").stdout
    draft_fixed = bool(original) and fixed_sections(original) == fixed_sections((ROOT / DRAFT).read_text())
    baseline = unchanged_since(BASELINE_TAG, BASELINE)
    tests = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True)
    return [
        Check("protected files unchanged", protected, f"vs tag {LOOP_START_TAG}: {', '.join(PROTECTED)}"),
        Check("draft FIXED sections unchanged", draft_fixed, f"{DRAFT} vs tag {LOOP_START_TAG}"),
        Check("baseline frozen", baseline, f"regex_baseline.py vs {BASELINE_TAG}"),
        Check("tests pass", tests.returncode == 0, (tests.stdout.strip().splitlines() or ["no output"])[-1]),
    ]


def loop_runs(split: str) -> list[JSONObject]:
    """Claude runs from a clean tree at or after loop-start, newest first.

    Rescores (they carry "rescored_from") are skipped: a rescore makes no API calls, so it is
    the same sample as its source run, not a new one. Counted, it could fill the dev gate from
    one run, and a rescore of the final run would count as a second look.
    """
    runs: list[JSONObject] = []
    for path in sorted((ROOT / "results").glob("claude_*.json"), reverse=True):
        r = json.loads(path.read_text())
        if "rescored_from" in r:
            continue
        commit = r["git"]["commit"]
        after_start = git("merge-base", "--is-ancestor", LOOP_START_TAG, commit).returncode == 0
        if r.get("split", "all") == split and not r["git"]["dirty"] and after_start:
            runs.append({**r, "_file": path.name})
    return runs


def dev_runs_for(identity: RunIdentity | None) -> list[JSONObject]:
    return [r for r in loop_runs("dev") if same_request(r, identity)]


def current_prompt_version() -> str:
    from deal_board.extractors.claude import PROMPT_VERSION

    return PROMPT_VERSION


def current_request_sha(model: str) -> str | None:
    """deal_board.extractors.claude.request_sha(model), or None if the extractor has none.

    Looked up with getattr so this file typechecks and runs against an extractor that predates
    request_sha; None then makes every run fail to match, so the gate fails closed.
    """
    from deal_board.extractors import claude

    fn = getattr(claude, "request_sha", None)
    if not callable(fn):
        return None
    sha = fn(model)
    return sha if isinstance(sha, str) and sha else None


def current_identity() -> RunIdentity | None:
    """The identity a run made now would carry. The model comes from the same setting eval uses
    (DEAL_MODEL, read at call time); only the process environment is consulted, so a model set
    only in .env must be exported, or nothing matches."""
    from deal_board.config import load_settings

    model = load_settings().deal_model
    sha = current_request_sha(model)
    return None if sha is None else RunIdentity(current_prompt_version(), model, sha)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["dev", "final"], required=True)
    ap.add_argument("--json", action="store_true", help="print machine-readable results")
    args = ap.parse_args(argv)

    version = current_prompt_version()
    identity = current_identity()
    dev = dev_checks(dev_runs_for(identity))
    if args.stage == "dev":
        checks = guard_checks() + dev
    else:
        dev_ok = all(c.passed for c in dev)
        checks = guard_checks() + final_checks(loop_runs("all"), identity if dev_ok else None)

    ok = all(c.passed for c in checks)
    if args.json:
        print(
            json.dumps(
                {
                    "stage": args.stage,
                    "prompt_version": version,
                    "identity": None if identity is None else identity.__dict__,
                    "passed": ok,
                    "checks": [c.__dict__ for c in checks],
                },
                indent=2,
            )
        )
    else:
        print(f"stage={args.stage} prompt={version} identity={identity or 'unknown (no request_sha): no run matches'}")
        for c in checks:
            print(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
        print("SUCCESS" if ok else "NOT YET")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
