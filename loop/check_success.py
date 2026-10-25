"""Success test for the extractor-improvement loop. Exits 0 only if every check passes.

  python loop/check_success.py --stage dev     # tuning gate: last 3 dev-split runs of the current prompt
  python loop/check_success.py --stage final   # one-shot confirmation on both holdout sets

The thresholds are fixed for the whole loop. The loop agent must never edit this file or any other
protected path; the guard check fails if it does. A dev pass unlocks exactly one final run.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from deal_board.schema import KEY_FIELDS, JSONObject

# Worked out from this file, not from deal_board.paths: the guard must not trust a module it
# doesn't protect to tell it where the repository is.
ROOT = Path(__file__).resolve().parent.parent

LOOP_START_TAG = "loop-start"
BASELINE_TAG = "baseline-v1"
# The paths the guard used to diff. The allow-list below supersedes them: every one is outside
# it, and so is everything else the loop could use to game the numbers (a conftest.py, a
# pytest.ini, a scoring/ package shadowing scoring.py, ...). Kept as the named high-value set.
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
# P10 moved the frozen baseline into the deal_board package; it is compared with its blob at
# baseline-v1 under the path it had then, so the move by itself is not counted as an edit.
PATH_AT_TAG = {
    "deal_board/extractors/frozen/regex_baseline.py": "extractors/regex_baseline.py",
}
BASELINE = "deal_board/extractors/frozen/regex_baseline.py"
DRAFT = "loop/DRAFT.md"  # only its "(FIXED)" sections are protected; the agent edits the rest

# The guard is an allow-list over every change since loop-start (committed, uncommitted and
# untracked) and over every commit since then: the loop may change these paths. That includes
# the extractor's pre-P10 path, which the loop's own commits edited and P10's move deleted.
# Other P10 moves still count as changes, which is fine: the loop is done.
ALLOWED_CHANGES = frozenset(
    {"deal_board/extractors/claude.py", "extractors/claude.py", DRAFT, "loop/LESSONS.md", "loop/state.json"}
)
# Beyond those it may only add new files here: (directory, name patterns). Exact parent directory, so
# tests/test_x/conftest.py does not slip through as "tests/test_*".
ALLOWED_ADDITIONS = (("results", ("claude_*.json", "claude_*.md")), ("tests", ("test_*.py",)))
MAX_LISTED = 25  # offenders shown in a check's detail; the rest are counted
# The suite's one known skip: tests/test_packaging_compat.py skips results/baseline-v1_comparison.json
# because it is not an eval-run file. Any further skip, or any deselected test, fails 'tests pass'.
MAX_SKIPPED = 1

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


def is_fresh(run: JSONObject) -> bool:
    """True if no record was replayed from the eval cache.

    A replayed record is an earlier run's output, so three runs with cache hits can be one
    sample counted three times. A record without a `call` block (or with `cached` anything but
    False) can't show it was fresh and counts as replayed.
    """
    usage = run.get("usage")
    if isinstance(usage, dict) and usage.get("cache_hits", 0) != 0:
        return False
    records = [rec for ds in run["datasets"].values() for rec in ds.get("records", {}).values()]
    return all(isinstance(rec.get("call"), dict) and rec["call"].get("cached") is False for rec in records)


def fresh_check(runs: list[JSONObject]) -> Check:
    stale = [r for r in runs if not is_fresh(r)]
    return Check("fresh runs", not stale, f"runs with cached records: {run_files(stale)}")


Rescorer = Callable[[JSONObject], JSONObject]  # a saved result -> its datasets re-scored against current labels


def gated_metrics(scores: JSONObject) -> dict[str, float]:
    """Every stored number a gate reads, by name, plus how many posts it covers.

    The counts matter as much as the rates: a run with its misses deleted (records and scores
    block trimmed together) reproduces its own rates, but not the full split's post count.
    """
    metrics: dict[str, float] = {
        "n_posts": scores["n_posts"],
        "n_deal_posts": scores["n_deal_posts"],
        "is_deal_request.accuracy": scores["is_deal_request"]["accuracy"],
        "is_deal_request.error": scores["is_deal_request"]["error"],
        "non_deal_hallucinations.fields_filled": scores["non_deal_hallucinations"]["fields_filled"],
    }
    metrics.update({f"fields.{f}.accuracy": scores["fields"][f]["accuracy"] for f in KEY_FIELDS})
    return metrics


DRIFT_KEYS = ("changed", "relabeled", "missing", "new")


def drift_problem(drift: object) -> str | None:
    """What moved in the posts since the run (per cli.eval.post_drift), or None if nothing did.

    Inside the loop nothing may move: data/ is protected, so a relabeled, edited, missing or never
    scored post means the run was made against other data, or had records removed.
    """
    if not isinstance(drift, dict):
        return None  # a rescorer that reports no drift (legacy files, test stubs)
    moved = {k: drift.get(k) or [] for k in DRIFT_KEYS}
    return "; ".join(f"{k} {ids}" for k, ids in moved.items() if ids) or None


def rescore_mismatch(run: JSONObject, rescore: Rescorer, datasets: tuple[str, ...] = ("posts_dev",)) -> str | None:
    """None if re-scoring the run's raw outputs against the current labels reproduces every gated
    metric it stores, over the same posts; otherwise what differs.

    Stored scores are only as good as the labels and scoring code they were computed with: a
    label edited, run against and reverted, or a hand-edited scores block, would otherwise pass.
    Drift fails too, because the rescore drops or re-scores moved posts, which would hide exactly
    such an edit. A run that can't be re-scored at all fails closed.
    """
    problems: list[str] = []
    try:
        rescored = rescore(run)
        for ds in datasets:
            stored = gated_metrics(run["datasets"][ds]["scores"])
            fresh = gated_metrics(rescored[ds]["scores"])
            differ = [k for k in stored if not math.isclose(stored[k], fresh[k], rel_tol=0.0, abs_tol=1e-9)]
            problems += [f"{ds} {k} stored {stored[k]} rescored {fresh[k]}" for k in differ]
            if (moved := drift_problem(rescored[ds].get("drift"))) is not None:
                problems.append(f"{ds} posts moved since the run: {moved}")
    except Exception as exc:  # any failure to reproduce the scores is a failure of the run
        return f"cannot rescore ({type(exc).__name__}: {exc})"
    return ", ".join(problems) or None


def rescore_check(runs: list[JSONObject], rescore: Rescorer, datasets: tuple[str, ...] = ("posts_dev",)) -> Check:
    bad = [(r, why) for r in runs if (why := rescore_mismatch(r, rescore, datasets)) is not None]
    detail = "; ".join(f"{r.get('_file', '?')}: {why}" for r, why in bad) or f"{len(runs)} runs reproduce"
    return Check("stored scores match rescore", not bad, detail)


def dev_checks(runs: list[JSONObject], rescore: Rescorer | None = None) -> list[Check]:
    """runs: every clean dev-split result for the current request identity, newest first.

    rescore defaults to deal_board.cli.eval.rescore (it reads the current label files); tests
    pass their own.
    """
    if len(runs) < REPEATS:
        return [repeats_check(runs)]
    scores = [r["datasets"]["posts_dev"]["scores"] for r in runs[:REPEATS]]
    field_means: dict[str, float] = {f: sum(s["fields"][f]["accuracy"] for s in scores) / REPEATS for f in KEY_FIELDS}
    mean = sum(run_mean(s) for s in scores) / REPEATS
    worst_field = min(field_means, key=field_means.__getitem__)
    return [
        repeats_check(runs),
        fresh_check(runs[:REPEATS]),
        rescore_check(runs[:REPEATS], rescore_saved if rescore is None else rescore),
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


def final_checks(
    finals: list[JSONObject], dev_passed: RunIdentity | None, rescore: Rescorer | None = None
) -> list[Check]:
    """finals: every final look since loop-start, newest first, dirty ones included (a dirty
    run still looked at the holdout). dev_passed: the identity the dev gate passed for, or None
    if it did not pass. rescore defaults to deal_board.cli.eval.rescore; tests pass their own.

    The counted look gets the dev gate's integrity checks too: it must be fresh (no cache
    replays of an earlier, uncounted look) and its stored scores must reproduce on rescore.
    """
    if not finals:
        return [Check("final run", False, "no final (--split all) run since loop-start")]
    run = finals[-1]  # oldest: the first look is the one that counts
    hold, posts = run["datasets"]["holdout"]["scores"], run["datasets"]["posts"]["scores"]
    dirty = [r for r in finals if r["git"]["dirty"]]
    return [
        Check("one look only", len(finals) <= MAX_FINAL_RUNS, f"{len(finals)} final runs since loop-start"),
        Check("final run clean", not dirty, f"final runs from a dirty tree: {run_files(dirty)}"),
        fresh_check([run]),
        rescore_check([run], rescore_saved if rescore is None else rescore, ("holdout", "posts")),
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


def change_allowed(status: str, path: str) -> bool:
    """status: a `git diff --name-status --no-renames` letter (A, M, D, T, ...); untracked is A."""
    if path in ALLOWED_CHANGES:
        return True
    p = PurePosixPath(path)
    return status == "A" and any(
        str(p.parent) == folder and any(fnmatchcase(p.name, pat) for pat in patterns)
        for folder, patterns in ALLOWED_ADDITIONS
    )


def disallowed_changes(changes: Iterable[tuple[str, str]]) -> list[str]:
    """'STATUS path' for each change outside the allow-list, sorted and deduplicated."""
    return sorted({f"{status} {path}" for status, path in changes if not change_allowed(status, path)})


def parse_name_status(z_output: str) -> list[tuple[str, str]]:
    """(status, path) pairs from `git diff --name-status --no-renames -z` output."""
    parts = z_output.split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    return [(parts[i][:1], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]


def listing(items: list[str]) -> str:
    shown = ", ".join(items[:MAX_LISTED])
    return shown + (f" (+{len(items) - MAX_LISTED} more)" if len(items) > MAX_LISTED else "")


def allow_list_check(changes: list[tuple[str, str]]) -> Check:
    offenders = disallowed_changes(changes)
    detail = f"changed outside the allow-list: {listing(offenders)}" if offenders else "only allowed paths changed"
    return Check("protected files unchanged", not offenders, f"vs tag {LOOP_START_TAG}: {detail}")


def history_check(commits: list[tuple[str, list[tuple[str, str]]]]) -> Check:
    """commits: (sha, changes) for each commit since loop-start. The net diff misses a protected
    edit that was committed, used for runs, and reverted, so every commit must stay within the
    allow-list too."""
    bad = [(sha, offenders) for sha, changes in commits if (offenders := disallowed_changes(changes))]
    detail = (
        listing(
            [f"{sha[:7]} ({listing(offenders[:3])}{', ...' if len(offenders) > 3 else ''})" for sha, offenders in bad]
        )
        if bad
        else f"{len(commits)} commits within the allow-list"
    )
    return Check("protected files untouched in history", not bad, f"since tag {LOOP_START_TAG}: {detail}")


def tests_check(returncode: int, output: str) -> Check:
    """Pass only on exit 0 with at least one pass, no deselected tests and at most MAX_SKIPPED skips.

    Exit 0 alone is not enough: a conftest or config that skips or deselects tests also exits 0.
    """
    last = (output.strip().splitlines() or ["no output"])[-1]
    counts = {word: int(n) for n, word in re.findall(r"(\d+) (\w+)", last)}
    ok = (
        returncode == 0
        and counts.get("passed", 0) > 0
        and counts.get("deselected", 0) == 0
        and counts.get("skipped", 0) <= MAX_SKIPPED
    )
    return Check("tests pass", ok, f"{last} (max {MAX_SKIPPED} skipped, 0 deselected)")


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


def changes_since(tag: str) -> list[tuple[str, str]] | None:
    """Every change from `tag` to the working tree, untracked files as additions; None if git fails.

    `git diff <tag>` covers committed, staged and unstaged edits. Untracked files come from
    `git ls-files --others --exclude-standard` (the files `git status --porcelain
    --untracked-files=all` reports as ??), which is unambiguous to parse with -z.
    """
    diff = git("diff", "--name-status", "--no-renames", "-z", tag)
    untracked = git("ls-files", "--others", "--exclude-standard", "-z")
    if diff.returncode != 0 or untracked.returncode != 0:
        return None
    return parse_name_status(diff.stdout) + [("A", p) for p in untracked.stdout.split("\0") if p]


def commits_since(tag: str) -> list[tuple[str, list[tuple[str, str]]]] | None:
    """(sha, changes) for every commit in tag..HEAD, oldest first; None if git fails.

    Merge commits list no changes of their own (diff-tree shows none without -m); the commits
    they bring in are in the range themselves.
    """
    revs = git("rev-list", "--reverse", f"{tag}..HEAD")
    if revs.returncode != 0:
        return None
    out: list[tuple[str, list[tuple[str, str]]]] = []
    for sha in revs.stdout.split():
        tree = git("diff-tree", "--no-commit-id", "--name-status", "--no-renames", "-r", "-z", sha)
        if tree.returncode != 0:
            return None
        out.append((sha, parse_name_status(tree.stdout)))
    return out


def reachability_check(unreachable: list[str]) -> Check:
    """Every loop run must sit on HEAD's history.

    A run made at a commit later dropped by `git reset` or a rebase keeps counting (its commit
    still descends from loop-start), while the history check, which walks loop-start..HEAD, no
    longer sees the protected edit it was made on. Failing here, rather than skipping such runs,
    also stops an orphaned final look from disappearing from the look count.
    """
    return Check(
        "runs reachable from HEAD",
        not unreachable,
        f"runs not on HEAD's history: {listing(unreachable)}" if unreachable else "every loop run is on HEAD's history",
    )


def guard_checks() -> list[Check]:
    changes = changes_since(LOOP_START_TAG)
    protected = (
        Check("protected files unchanged", False, f"git diff against tag {LOOP_START_TAG} failed")
        if changes is None
        else allow_list_check(changes)
    )
    commits = commits_since(LOOP_START_TAG)
    history = (
        Check("protected files untouched in history", False, f"git log since tag {LOOP_START_TAG} failed")
        if commits is None
        else history_check(commits)
    )
    original = git("show", f"{LOOP_START_TAG}:{DRAFT}").stdout
    draft_fixed = bool(original) and fixed_sections(original) == fixed_sections((ROOT / DRAFT).read_text())
    baseline = unchanged_since(BASELINE_TAG, BASELINE)
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True
    )
    return [
        protected,
        history,
        Check("draft FIXED sections unchanged", draft_fixed, f"{DRAFT} vs tag {LOOP_START_TAG}"),
        Check("baseline frozen", baseline, f"regex_baseline.py vs {BASELINE_TAG}"),
        tests_check(tests.returncode, tests.stdout),
        reachability_check(unreachable_runs()),
    ]


def rescore_saved(saved: JSONObject) -> JSONObject:
    """deal_board.cli.eval.rescore, imported at call time so the pure checks above load without it."""
    from deal_board.cli.eval import rescore

    return rescore(saved)


def made_runs() -> list[JSONObject]:
    """Every Claude run in results/, newest first, with its file name. Rescores (they carry
    "rescored_from") are skipped: a rescore makes no API calls, so it is the same sample as its
    source run, not a new one, and a rescore of the final run is not a second look."""
    runs: list[JSONObject] = []
    for path in sorted((ROOT / "results").glob("claude_*.json"), reverse=True):
        r = json.loads(path.read_text())
        if "rescored_from" not in r:
            runs.append({**r, "_file": path.name})
    return runs


def after_loop_start(run: JSONObject) -> bool:
    return git("merge-base", "--is-ancestor", LOOP_START_TAG, run["git"]["commit"]).returncode == 0


def loop_runs(split: str) -> list[JSONObject]:
    """Claude runs from a clean tree at or after loop-start, newest first: the dev gate's sample."""
    return [r for r in made_runs() if r.get("split", "all") == split and not r["git"]["dirty"] and after_loop_start(r)]


def final_looks() -> list[JSONObject]:
    """Every final (--split all) run at or after loop-start, newest first, dirty or not.

    Not loop_runs("all"): a dirty final still looked at the holdout, so dropping it would let a
    second, clean final pass as the only look. final_checks fails the dirty one instead.
    """
    return [r for r in made_runs() if r.get("split", "all") == "all" and after_loop_start(r)]


def unreachable_runs() -> list[str]:
    """Runs added since loop-start whose commit is not on HEAD's history, or whose commit git
    can't resolve at all (e.g. garbage-collected after a reset): either way the guard can't see
    what tree the run was made on.

    Results files already in the tag are pre-loop history; the allow-list already fails any edit
    to them, so only files added since are checked here.
    """
    bad: list[str] = []
    for r in made_runs():
        if git("cat-file", "-e", f"{LOOP_START_TAG}:results/{r['_file']}").returncode == 0:
            continue
        commit = str(r.get("git", {}).get("commit", ""))
        if git("cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
            bad.append(f"{r['_file']} (unknown commit {commit[:12] or '?'})")
        elif after_loop_start(r) and git("merge-base", "--is-ancestor", commit, "HEAD").returncode != 0:
            bad.append(f"{r['_file']} ({commit[:7]})")
    return bad


def dev_runs_for(identity: RunIdentity | None) -> list[JSONObject]:
    return [r for r in loop_runs("dev") if same_request(r, identity)]


def current_prompt_version() -> str:
    from deal_board.extractors.claude import PROMPT_VERSION

    return PROMPT_VERSION


def current_request_sha(model: str) -> str | None:
    """deal_board.extractors.claude.request_sha(model): what a run made now would record.

    Imported at call time, like PROMPT_VERSION, so loading this script never imports the SDK.
    An empty value fails closed (no run can match it).
    """
    from deal_board.extractors.claude import request_sha

    return request_sha(model) or None


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
        checks = guard_checks() + final_checks(final_looks(), identity if dev_ok else None)

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
