"""Frozen success check for the final-eval tooling (tools/final_eval/). Never edit after commit 1.

Why frozen: the bar is set before any tool exists, so the tools are built to meet it rather than
the bar being moved to meet the tools. Criterion (g) fails if this file changes after commit 1.

Run from the worktree with its own venv:  .venv/bin/python tools/final_eval/check_success.py
Exit 0 only when every criterion (a)-(h) passes. Each prints PASS/FAIL with a reason.

It never runs an extractor on data/holdout_v3.jsonl: the smoke run (c) uses a fake extractor on
25 synthetic posts in a temp dir, through run.py's injectable hooks (see PROTOCOL.md).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BIN = Path(sys.executable).parent
TOOLS = "tools/final_eval"
TEST_FILE = "tests/test_final_eval_tools.py"
SELF = f"{TOOLS}/check_success.py"
OWNED = {
    f"{TOOLS}/__init__.py",
    f"{TOOLS}/run.py",
    f"{TOOLS}/validate.py",
    f"{TOOLS}/tables.py",
    f"{TOOLS}/check_success.py",
    f"{TOOLS}/graph.md",
    f"{TOOLS}/PROTOCOL.md",
    TEST_FILE,
}
V2_REGEX = "results/regex_20261024T224817Z.json"
V2_CLAUDE = "results/claude_20261024T234359Z.json"
SCORED_FIELDS = (
    "is_deal_request",
    "state",
    "city",
    "property_type",
    "transaction_type",
    "deal_value_usd",
    "timeline",
    "missing_fields",
)
KNOWN_XFAILS = 5
FAKE_SECRET = "sk-ant-FAKE-smoke-secret-0000"

# Runs in a fresh interpreter so the smoke run can't leak state into this process.
SMOKE_DRIVER = r"""
import dataclasses, importlib.util, json, subprocess, sys
from pathlib import Path

root, tmp, secret = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]

def load(name):
    spec = importlib.util.spec_from_file_location(f"final_eval_smoke_{name}", root / "tools/final_eval" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod

run, validate = load("run"), load("validate")
head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()

posts = []
for i in range(1, 26):
    deal = i <= 19
    label = {
        "is_deal_request": deal,
        "state": "TX" if deal else None,
        "city": "Austin" if deal else None,
        "property_type": None,
        "transaction_type": None,
        "deal_value_usd": 100000 * i if deal else None,
        "timeline": None,
    }
    posts.append({"id": f"smoke-{i:02d}", "split": "holdout_v3", "text": f"synthetic smoke post number {i}", "label": label})
data = tmp / "smoke_holdout_v3.jsonl"
data.write_text("".join(json.dumps(p) + "\n" for p in posts))
by_text = {p["text"]: p["label"] for p in posts}

class Fake:
    name = "fake"
    def extract_raw(self, text):
        return {**by_text[text], "notes": None}
    def extract(self, text):
        raise AssertionError("run.py should call extract_raw on a raw-capable extractor")

hooks = dataclasses.replace(
    run.default_hooks(),
    make_extractor=lambda name: (Fake(), {"model": "fake-model", "prompt_version": "ac1a145b"}),
    git_state=lambda: run.GitState(branch="main", commit=head, dirty=False),
    has_api_key=lambda: True,
    prompt_version=lambda: "ac1a145b",
    secret=lambda: secret,
    data_path=data,
    guard_results_dirs=(),
    guard_cache_dirs=(),
)
results, cache = tmp / "results", tmp / "cache"
argv = ["--extractor", "claude", "--expect-prompt", "ac1a145b", "--results-dir", str(results), "--cache-dir", str(cache)]

rc1 = run.main(argv, hooks)
files = sorted(results.glob("*.json"))
assert rc1 == 0, f"first run exit {rc1}"
assert len(files) == 1, f"expected 1 results file, got {files}"
saved = json.loads(files[0].read_text())
assert saved["split"] == "holdout_v3" and list(saved["datasets"]) == ["holdout_v3"], "wrong split/datasets"

vhooks = dataclasses.replace(validate.default_hooks(), data_path=data, is_ancestor=lambda commit: True)
rcv = validate.main([str(files[0])], vhooks)
assert rcv == 0, f"validate rejected the smoke file (exit {rcv})"

dirty = tmp / "dirty.json"
dirty.write_text(json.dumps({**saved, "git": {**saved["git"], "dirty": True}}))
assert validate.main([str(dirty)], vhooks) != 0, "validate accepted a dirty run"

rc2 = run.main(argv, hooks)
assert rc2 == 2, f"second look not refused (exit {rc2})"
assert len(sorted(results.glob("*.json"))) == 1, "second look wrote a file"
assert not cache.exists() or not any(cache.iterdir()), "run.py wrote to the cache"
print("SMOKE OK")
"""


def sh(*cmd: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def tool(name: str) -> str:
    return str(BIN / name)


def holdout_v3_hashes() -> set[str]:
    path = ROOT / "data" / "holdout_v3.jsonl"
    texts = [json.loads(line)["text"] for line in path.read_text().splitlines() if line.strip()]
    return {hashlib.sha256(t.encode("utf-8", "surrogatepass")).hexdigest() for t in texts}


def cache_snapshot() -> dict[str, str]:
    cache = ROOT / ".cache"
    if not cache.exists():
        return {}
    return {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in cache.rglob("*") if p.is_file()
    }


def check_a() -> tuple[bool, str]:
    collected = sh(tool("pytest"), "--collect-only", "-q", TEST_FILE)
    n = sum(1 for line in collected.stdout.splitlines() if "::" in line)
    if n < 20:
        return False, f"{n} tests collected in {TEST_FILE}, need >= 20"
    r = sh(tool("pytest"), "-q", TEST_FILE)
    tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:]
    return r.returncode == 0, f"{n} tests; {tail}"


def check_b() -> tuple[bool, str]:
    targets = ["tools", TEST_FILE]
    runs = {
        "ruff check": sh(tool("ruff"), "check", *targets),
        "ruff format --check": sh(tool("ruff"), "format", "--check", *targets),
        "mypy --strict": sh(tool("mypy"), "--strict", *targets),
    }
    bad = [f"{k}: {(r.stdout + r.stderr).strip()[-400:]}" for k, r in runs.items() if r.returncode != 0]
    return not bad, "; ".join(bad) if bad else "ruff, ruff format and mypy --strict clean"


def check_c() -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(
            [sys.executable, "-c", SMOKE_DRIVER, str(ROOT), tmp, FAKE_SECRET],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
    out = r.stdout + r.stderr
    if FAKE_SECRET in out:
        return False, "the (fake) API key appeared in run.py/validate.py output"
    ok = r.returncode == 0 and "SMOKE OK" in r.stdout
    return (
        ok,
        "smoke run accepted by validate.py, dirty copy rejected, second look refused, cache untouched"
        if ok
        else out.strip()[-800:],
    )


def check_d() -> tuple[bool, str]:
    r = sh(sys.executable, f"{TOOLS}/tables.py", V2_REGEX, V2_CLAUDE)
    if r.returncode != 0:
        return False, f"tables.py exit {r.returncode}: {(r.stdout + r.stderr).strip()[-400:]}"
    rows = [line for line in r.stdout.splitlines() if re.match(r"^\|\s*(" + "|".join(SCORED_FIELDS) + r")\b", line)]
    counts = {f: sum(1 for line in rows if re.match(rf"^\|\s*{f}\b", line)) for f in SCORED_FIELDS}
    ok = len(rows) == 8 and all(c == 1 for c in counts.values())
    return ok, f"{len(rows)} field rows ({counts})"


def check_e(before: dict[str, str]) -> tuple[bool, str]:
    problems = []
    status = sh("git", "status", "--porcelain", "results/").stdout.strip()
    if status:
        problems.append(f"results/ changed: {status}")
    after = cache_snapshot()
    if after != before:
        problems.append(f"cache changed: {sorted(set(after.items()) ^ set(before.items()))[:5]}")
    hashes = holdout_v3_hashes()
    for p in (ROOT / ".cache").rglob("*.json") if (ROOT / ".cache").exists() else []:
        try:
            if json.loads(p.read_text()).get("text_sha256") in hashes:
                problems.append(f"cache entry for a holdout_v3 post: {p}")
        except ValueError, OSError, AttributeError:
            continue
    for p in sorted((ROOT / "results").rglob("*.json")):
        try:
            data = json.loads(p.read_text())
        except ValueError:
            continue
        if isinstance(data, dict) and (data.get("split") == "holdout_v3" or "holdout_v3" in data.get("datasets", {})):
            problems.append(f"holdout_v3 results file exists: {p.relative_to(ROOT)}")
    return not problems, "; ".join(problems) if problems else "results/ and .cache/ untouched, no holdout_v3 look"


def check_f() -> tuple[bool, str]:
    committed = set(sh("git", "diff", "--name-only", "main...HEAD").stdout.split())
    working = {line[3:].strip() for line in sh("git", "status", "--porcelain", "-uall").stdout.splitlines()}
    extra = sorted((committed | working) - OWNED)
    return not extra, f"outside owned paths: {extra}" if extra else f"{len(committed)} committed paths, all owned"


def check_g() -> tuple[bool, str]:
    commits = sh("git", "rev-list", "--reverse", "main..HEAD").stdout.split()
    if not commits:
        return False, "no commits on the branch"
    first = commits[0]
    added = sh("git", "show", "--name-status", "--format=", first).stdout
    if not re.search(rf"^A\s+{re.escape(SELF)}$", added, re.M):
        return False, f"commit 1 ({first[:7]}) did not add {SELF}"
    diff = sh("git", "diff", first, "--", SELF).stdout
    return not diff, f"changed since commit 1 ({first[:7]})" if diff else f"unchanged since commit 1 ({first[:7]})"


def check_h() -> tuple[bool, str]:
    r = sh(tool("pytest"), "-q", timeout=1200)
    tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    m = re.search(r"(\d+) xfailed", tail)
    xfailed = int(m.group(1)) if m else 0
    bad = re.search(r"\d+ (failed|errors?|xpassed)", tail)
    ok = r.returncode == 0 and not bad and xfailed <= KNOWN_XFAILS
    return ok, tail or (r.stderr[-400:])


def main() -> int:
    before = cache_snapshot()
    results = {
        "(a) >= 20 tool tests, passing": check_a(),
        "(b) ruff + format + mypy --strict": check_b(),
        "(c) smoke run, validate, one-look refusal": check_c(),
        "(d) tables.py on real holdout-v2 files": check_d(),
        "(f) diff limited to owned paths": check_f(),
        "(g) check_success.py frozen": check_g(),
        "(h) full pytest suite": check_h(),
    }
    results["(e) results/ and .cache/ untouched"] = check_e(before)
    for name in sorted(results):
        ok, why = results[name]
        print(f"{'PASS' if ok else 'FAIL'} {name}: {why}")
    passed = all(ok for ok, _ in results.values())
    print("SUCCESS" if passed else "NOT DONE")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
