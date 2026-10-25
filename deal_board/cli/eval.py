"""Run extractors on the labeled sets and score them. The independent holdout is the headline number.

  python -m deal_board.cli.eval                      # baseline, plus Claude if ANTHROPIC_API_KEY is set
  python -m deal_board.cli.eval --extractor regex
  python -m deal_board.cli.eval --rescore results/claude_20261024T120000Z.json   # no API calls
  python -m deal_board.cli.eval --compare results/A.json results/B.json         # paired test, writes nothing
  python -m deal_board.cli.eval --extractor claude --repeats 3                  # run-to-run variance
  python -m deal_board.cli.eval --extractor claude --no-cache                   # bypass the raw-output cache

Each run writes results/<extractor>_<UTC timestamp>.json (provenance, every raw output, every
prediction, scores, token usage and cost) and a matching .md table. Scoring rules live in deal_board/scoring.py.
"""

import argparse
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Protocol, runtime_checkable

from deal_board import config, paths
from deal_board.extractors import ExtractionError, Extractor, parse_requirement
from deal_board.schema import KEY_FIELDS, JSONObject
from deal_board.scoring import (
    DEAL_VALUE_TOLERANCE,
    FIELD_OUTCOMES,
    SCORED_FIELDS,
    SCORING_VERSION,
    load_posts,
    paired_comparison,
    post_correctness,
    score,
)

ROOT = paths.REPO_ROOT
RESULTS_DIR = paths.RESULTS_DIR
CACHE_DIR = ROOT / ".cache" / "extractor"
DATASETS = {  # headline first
    "holdout": "data/holdout.jsonl",
    "posts": "data/posts.jsonl",
}
SCORING_NOTE = (
    "is_deal_request is scored on all posts; the other rows only on posts labeled as deal requests. "
    f"deal_value_usd matches within ±{DEAL_VALUE_TOLERANCE:.0%}. city ignores case, spacing, periods and "
    "commas (no spelling tolerance). missing_fields is exact set equality. notes is free text and NOT "
    "scored. Errors never count as correct and stay in the accuracy denominator. A field's hallucination "
    "rate is guesses / n_null, the non-errored deal posts whose label for that field is empty; the "
    "non-deal posts-with-any rate divides by the non-errored non-deal posts. Brackets are 95% Wilson intervals."
)

# Anthropic first-party API list prices for prompts under 100K tokens, from the Claude API reference
# model table cached 2026-10-06; checked 2026-10-24. Update when prices change.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {  # model -> (input $, output $) per 1M tokens
    "claude-haiku-5-5": (0.10, 0.50),
}
PRICES_AS_OF = "2026-10-06"


def cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    """Dollar cost of the given tokens, or None when the model has no known price.

    None rather than a guess: a made-up cost in a results file reads as fact months later.
    Zero tokens cost 0.0 whatever the model, so regex and fully cached runs report a real zero.
    """
    if input_tokens == 0 and output_tokens == 0:
        return 0.0
    price = PRICES_PER_MTOK.get(model) if model is not None else None
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def text_sha256(text: str) -> str:
    """sha256 of the text's UTF-8 bytes. surrogatepass, so a lone surrogate (which json.loads can
    produce from a post) hashes instead of crashing the run; normal text hashes as plain UTF-8."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_sha256(obj: object) -> str:
    """sha256 of the source file that defines type(obj): the cache's request fingerprint."""
    source = inspect.getsourcefile(type(obj))
    if source is None:  # a builtin or C extension; never true for the extractors here
        raise RuntimeError(f"no source file for {type(obj).__name__}, so the cache can't fingerprint it")
    return file_sha256(Path(source))


def cache_key(model: str, prompt_version: str, text: str) -> str:
    """Hash of (model, prompt version, post text).

    The prompt version is part of the key so an output from an old prompt can never be served
    for a new one. Hashing the JSON list (not a joined string) means no two different triples can
    collide by shifting a separator, and no part of a model name ever becomes a path.
    """
    return hashlib.sha256(json.dumps([model, prompt_version, text_sha256(text)]).encode()).hexdigest()


ENTRY_FIELDS = ("raw", "error", "input_tokens", "output_tokens", "latency_s")


class RawCache:
    """Raw extractor outputs on disk, one JSON file per (model, prompt version, post text).

    Lets an interrupted Claude run resume without paying again for posts it already did.

    request_fingerprint guards what PROMPT_VERSION doesn't cover: it hashes only the system prompt
    and tool, not max_tokens, tool_choice or the <post> wrapper. The CLI passes the sha256 of the
    extractor's source file, so any edit there turns old entries into misses instead of replaying
    outputs of a request that no longer exists. None means the caller doesn't fingerprint.
    """

    def __init__(
        self, directory: Path, model: str, prompt_version: str, request_fingerprint: str | None = None
    ) -> None:
        self.directory, self.model, self.prompt_version = Path(directory), model, prompt_version
        self.request_fingerprint = request_fingerprint
        self._looked_up: set[str] = set()

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def _identity(self, text: str) -> JSONObject:
        return {
            "model": self.model,
            "prompt_version": self.prompt_version,
            "text_sha256": text_sha256(text),
            "request_sha256": self.request_fingerprint,
        }

    def get(self, text: str) -> JSONObject | None:
        """The cached entry for text, or None (a miss).

        Only the first lookup of a key on this instance can hit. The CLI shares one instance
        across all repeats, so run 1 may resume from disk while runs 2..N always call the API:
        repeats stay independent samples instead of replaying run 1's outputs. A file that is
        unreadable, or whose stored identity doesn't match the request, is treated as a miss
        rather than trusted on the strength of its filename; so is one missing an entry field
        (hand-edited, or written by an older entry format), which would otherwise crash the run.
        """
        key = cache_key(self.model, self.prompt_version, text)
        if key in self._looked_up:
            return None
        self._looked_up.add(key)
        try:
            entry = json.loads(self._path(key).read_text())
        except OSError, ValueError:  # missing file, or not valid JSON
            return None
        identity = self._identity(text)
        if not isinstance(entry, dict) or any(entry.get(k) != v for k, v in identity.items()):
            return None
        return entry if all(k in entry for k in ENTRY_FIELDS) else None

    def put(self, text: str, entry: JSONObject) -> None:
        """Store entry for text (latest wins), stamped with the identity that get() verifies.

        Written to a temp file then os.replace'd, so a crash mid-write never leaves a
        half-written entry that a resumed run would have to recognise as corrupt.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({**entry, **self._identity(text)}, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            os.replace(tmp, self._path(cache_key(self.model, self.prompt_version, text)))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


RawCheck = Callable[[JSONObject], None]


def raw_check(extractor_name: str | None) -> RawCheck | None:
    """The shape check an extractor's raw outputs must pass before validation, or None.

    Claude's raw is the model's tool input, so it gets claude.check_claude_raw (exact keys, strict
    JSON types): the same gate ClaudeExtractor.extract applies, here on fresh runs and on rescores
    of saved Claude runs alike. The regex baseline's raw is already a validated model dump.
    """
    if extractor_name != "claude":
        return None
    from deal_board.extractors.claude import check_claude_raw

    return check_claude_raw


def to_record(raw: JSONObject | None, error: str | None = None, check: RawCheck | None = None) -> JSONObject:
    """Turn one raw output into a scored record. Used for fresh runs and for re-scoring."""
    if raw is None:
        return {"raw": None, "prediction": None, "error": error}
    try:
        if check is not None:
            check(raw)
        return {"raw": raw, "prediction": parse_requirement(raw).model_dump(mode="json"), "error": None}
    except ExtractionError as e:
        return {"raw": raw, "prediction": None, "error": str(e)}


@runtime_checkable
class RawExtractor(Protocol):
    """An extractor that can return its output before validation (Claude). isinstance() on this
    checks only that `extract_raw` exists, the same test as hasattr."""

    def extract_raw(self, text: str) -> dict[str, object]: ...


def call_extractor(extractor: Extractor, text: str) -> JSONObject:
    if isinstance(extractor, RawExtractor):  # Claude: keep the output before validation
        return extractor.extract_raw(text)
    return extractor.extract(text).model_dump(mode="json")  # baseline output is already validated


def fresh_entry(extractor: Extractor, text: str) -> JSONObject:
    """Call the extractor once and describe the outcome as a cache entry (minus its identity).

    Model failures (refusal, no tool call) become an entry with `error` set: they are outcomes
    to score and to cache, so a resumed run replays them instead of re-rolling only the failures.
    API/network exceptions are not caught; they propagate and nothing is cached.
    """
    raw, error = None, None
    start = time.perf_counter()
    try:
        raw = call_extractor(extractor, text)
    except ExtractionError as e:
        error = str(e)
    latency = time.perf_counter() - start
    usage = getattr(extractor, "last_usage", None)  # regex has none
    return {
        "raw": raw,
        "error": error,
        "input_tokens": usage.input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
        "latency_s": latency,
    }


def call_info(entry: JSONObject, cached: bool) -> JSONObject:
    return {
        "cached": cached,
        "input_tokens": entry["input_tokens"],
        "output_tokens": entry["output_tokens"],
        "latency_s": entry["latency_s"],
    }


def run_post(extractor: Extractor, text: str, cache: RawCache | None) -> JSONObject:
    """One scored record with its `call` provenance; a cache hit keeps the original call's numbers."""
    entry = cache.get(text) if cache is not None else None
    cached = entry is not None
    if entry is None:
        entry = fresh_entry(extractor, text)
        if cache is not None:
            cache.put(text, entry)
    record = to_record(entry["raw"], entry["error"], raw_check(extractor.name))
    return {**record, "call": call_info(entry, cached)}


def run_posts(extractor: Extractor, posts: list[JSONObject], cache: RawCache | None = None) -> dict[str, JSONObject]:
    return {p["id"]: run_post(extractor, p["text"], cache) for p in posts}


def select(split: str) -> dict[str, str]:
    """'all' scores every set, holdout first. 'dev' scores only the tuning posts, so no holdout is seen."""
    return DATASETS if split == "all" else {"posts_dev": DATASETS["posts"]}


def load_split(path: str, split: str) -> list[JSONObject]:
    posts = load_posts(ROOT / path)
    return posts if split == "all" else [p for p in posts if p.get("split") == split]


def evaluate(extractor: Extractor, split: str = "all", cache: RawCache | None = None) -> JSONObject:
    out = {}
    for name, path in select(split).items():
        posts = load_split(path, split)
        records = run_posts(extractor, posts, cache)
        out[name] = {"path": path, "records": records, "scores": score(posts, records)}
    return out


def usage_totals(datasets: JSONObject, model: str | None) -> JSONObject:
    """What this run cost. Counts only fresh calls: a cache hit was paid for by an earlier run.

    Records without a `call` key (files saved before usage was tracked) are skipped.
    """
    calls = [r["call"] for ds in datasets.values() for r in ds["records"].values() if "call" in r]
    fresh = [c for c in calls if not c["cached"]]
    input_tokens = sum(c["input_tokens"] or 0 for c in fresh)
    output_tokens = sum(c["output_tokens"] or 0 for c in fresh)
    latencies = [c["latency_s"] for c in fresh if c["latency_s"] is not None]
    price = PRICES_PER_MTOK.get(model) if model is not None else None
    return {
        "api_calls": sum(1 for c in fresh if c["input_tokens"] is not None),
        "cache_hits": len(calls) - len(fresh),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd(model, input_tokens, output_tokens),
        "latency_s_total": sum(latencies, 0.0),
        "latency_s_mean": sum(latencies) / len(latencies) if latencies else None,
        "price_per_mtok": list(price) if price is not None else None,
        "prices_as_of": PRICES_AS_OF,
    }


def rescore_records(records: dict[str, JSONObject], check: RawCheck | None = None) -> dict[str, JSONObject]:
    """Rebuild records from their raw outputs, keeping each one's `call` provenance if it has one."""
    return {
        pid: {**to_record(r["raw"], r["error"], check), **({"call": r["call"]} if "call" in r else {})}
        for pid, r in records.items()
    }


def rescore(saved: JSONObject) -> JSONObject:
    """Re-score a saved run against the current labels, from its raw outputs. No API calls."""
    out = {}
    for name, ds in saved["datasets"].items():
        posts = [p for p in load_posts(ROOT / ds["path"]) if p["id"] in ds["records"]]
        records = rescore_records(ds["records"], raw_check(saved.get("extractor")))
        out[name] = {"path": ds["path"], "records": records, "scores": score(posts, records)}
    return out


# The regex baseline run, scored deal-only, that replaces the legacy all-posts summary.
SUPERSEDING_BASELINE_RUN = "results/regex_20261024T224817Z.json"

RUN_ONLY_KEYS = ("usage", "repeat", "cache_enabled")


def source_label(source: Path) -> str:
    """How a rescore names its source: repo-relative POSIX when under ROOT, else the bare file name.

    Results are committed, so the caller's path as typed (often absolute, with the local username
    and directory layout in it) must never be written into them.
    """
    resolved = Path(source).resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:  # outside the repo
        return resolved.name


def rescored_result(saved: JSONObject, source: Path, stamp: str, git: JSONObject) -> JSONObject:
    """A rescore's own results dict. It drops the source run's usage, repeat and cache_enabled:
    a rescore makes no API calls, and copying them would report the old run's cost (and name the
    file as a repeat) as if this run had spent it. The source file keeps them."""
    kept = {k: v for k, v in saved.items() if k not in RUN_ONLY_KEYS}
    return {
        **kept,
        "request_sha": saved.get("request_sha"),  # the source run's, or None for runs that predate it
        "timestamp": stamp,
        "git": git,
        "scoring_version": SCORING_VERSION,
        "rescored_from": source_label(source),
        "datasets": rescore(saved),
    }


def compare_results(a: JSONObject, b: JSONObject) -> JSONObject:
    """Post-by-post paired comparison of two saved runs, per field.

    Both runs are re-scored from their raw outputs against the current labels, so a label fixed
    between the two runs can't masquerade as a model change. Datasets are matched by path (a
    dev-split `posts_dev` run compares with a full run's `posts`) and only posts present in both
    runs are compared, because a paired test is only valid on the same posts.
    """
    a_by_path = {ds["path"]: ds["records"] for ds in a["datasets"].values()}
    b_by_path = {ds["path"]: ds["records"] for ds in b["datasets"].values()}
    a_check, b_check = raw_check(a.get("extractor")), raw_check(b.get("extractor"))
    out = {}
    for path, a_records in a_by_path.items():
        if path not in b_by_path:
            continue
        b_records = b_by_path[path]
        posts = [p for p in load_posts(ROOT / path) if p["id"] in a_records and p["id"] in b_records]
        a_ok = post_correctness(posts, rescore_records(a_records, a_check))
        b_ok = post_correctness(posts, rescore_records(b_records, b_check))
        out[path] = {
            "n_common_posts": len(posts),
            "fields": {f: paired_comparison(a_ok[f], b_ok[f]) for f in SCORED_FIELDS},
        }
    return out


def ids(xs: list[str]) -> str:
    return ", ".join(xs) if xs else "–"


def format_comparison(cmp: JSONObject, a_name: str, b_name: str) -> str:
    lines = [
        f"# Paired comparison: A = `{a_name}`, B = `{b_name}`",
        "",
        "Both runs re-scored from raw outputs against the current labels. p is the two-sided "
        "exact McNemar test (binomial sign test on the posts where exactly one run is right).",
        "",
    ]
    if not cmp:
        return "\n".join([*lines, "No dataset path in common: nothing to compare."])
    for path, c in cmp.items():
        lines += [
            f"## `{path}`: {c['n_common_posts']} common posts",
            "",
            "| field | n | A correct | B correct | B only (B right, A wrong) | A only (A right, B wrong) "
            "| p (exact McNemar) |",
            "|---|---:|---:|---:|---|---|---:|",
        ]
        for field, r in c["fields"].items():
            lines.append(
                f"| {field} | {r['n']} | {r['a_correct']} | {r['b_correct']} | {ids(r['b_only'])} "
                f"| {ids(r['a_only'])} | {r['p_value']:.3g} |"
            )
        lines.append("")
    return "\n".join(lines)


def field_cell(scores: JSONObject, field: str) -> JSONObject:
    cell: JSONObject = scores[field] if field in ("is_deal_request", "missing_fields") else scores["fields"][field]
    return cell


def summarize_repeats(results: list[JSONObject]) -> dict[str, dict[str, JSONObject]]:
    """Mean and range of each field's accuracy across repeated runs: the run-to-run noise floor
    that a prompt change has to beat before it means anything."""
    names = list(dict.fromkeys(name for r in results for name in r["datasets"]))
    out: dict[str, dict[str, JSONObject]] = {}
    for name in names:
        out[name] = {}
        for field in SCORED_FIELDS:
            accs = [
                field_cell(r["datasets"][name]["scores"], field)["accuracy"] for r in results if name in r["datasets"]
            ]
            accs = [x for x in accs if x is not None]
            out[name][field] = {
                "mean": sum(accs) / len(accs) if accs else None,
                "min": min(accs, default=None),
                "max": max(accs, default=None),
                "runs": len(accs),
            }
    return out


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def whole(x: float) -> str:
    return f"{x * 100:.0f}"


def with_ci(x: float | None, ci: list[float] | None) -> str:
    """`92% [64–99]`; the interval is left off when absent (e.g. a score dict saved before P6)."""
    if x is None or ci is None:
        return pct(x)
    return f"{pct(x)} [{whole(ci[0])}–{whole(ci[1])}]"


def format_repeat_summary(summary: dict[str, dict[str, JSONObject]]) -> str:
    lines = ["# Repeat summary (accuracy across runs)", ""]
    for name, fields in summary.items():
        lines.append(f"## {name}")
        width = max(map(len, fields))
        for field, s in fields.items():
            span = "n/a" if s["min"] is None else f"{whole(s['min'])}–{whole(s['max'])}%"
            lines.append(f"{field:<{width}}  mean {pct(s['mean'])}  min–max {span}  ({s['runs']} runs)")
        lines.append("")
    return "\n".join(lines)


def usage_line(u: JSONObject) -> str:
    cost = "unknown (model not in price table)" if u["cost_usd"] is None else f"${u['cost_usd']:.4f}"
    mean = "n/a" if u["latency_s_mean"] is None else f"{u['latency_s_mean']:.2f} s"
    return (
        f"**Usage:** {u['api_calls']} API calls, {u['cache_hits']} cache hits, "
        f"{u['input_tokens']:,} input / {u['output_tokens']:,} output tokens, cost {cost} "
        f"(prices as of {u['prices_as_of']}), latency {u['latency_s_total']:.1f} s total, {mean} mean."
    )


# Paths eval itself writes; changes there are output, not code, so they never make a run dirty.
DIRTY_EXCLUDES = ("results", ".cache")


def git_info() -> JSONObject:
    """HEAD and whether the tree has uncommitted changes outside eval's own output.

    Why exclude results/ and .cache/: each run writes there, so counting them made every run after
    an uncommitted result (and repeats 2..N of one invocation) dirty with unchanged code.
    """

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()

    excludes = [f":(exclude){d}" for d in DIRTY_EXCLUDES]
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "--", ".", *excludes))}


def markdown(result: JSONObject) -> str:
    split = result.get("split", "all")
    repeat = result.get("repeat", {"index": 1, "of": 1})
    title = f"# Eval: {result['extractor']}" + ("" if split == "all" else f" (split: {split}; holdout not scored)")
    title += "" if repeat["of"] == 1 else f" (repeat {repeat['index']} of {repeat['of']})"
    lines = [title, ""]
    meta = {k: result[k] for k in ("model", "prompt_version", "timestamp") if result.get(k)}
    meta["commit"] = result["git"]["commit"][:7] + (" (uncommitted changes)" if result["git"]["dirty"] else "")
    lines += [", ".join(f"{k}: `{v}`" for k, v in meta.items()), ""]
    if "rescored_from" in result:
        lines += [f"Re-scored from `{result['rescored_from']}` against the current labels; no API calls.", ""]
    if "usage" in result:
        lines += [usage_line(result["usage"]), ""]
    lines += [f"_{SCORING_NOTE}_", ""]
    for name, ds in result["datasets"].items():
        s = ds["scores"]
        title = "holdout (headline)" if name == "holdout" else name
        lines += [
            f"## {title}: `{ds['path']}`, {s['n_posts']} posts "
            f"({s['n_deal_posts']} deal, {s['n_non_deal_posts']} non-deal)",
            "",
            "| field | accuracy | halluc. rate | " + " | ".join(o.replace("_", "-") for o in FIELD_OUTCOMES) + " |",
            "|---|---:|---:|" + "---:|" * len(FIELD_OUTCOMES),
        ]
        for field in SCORED_FIELDS:
            c = field_cell(s, field)
            halluc = with_ci(c.get("hallucination_rate"), c.get("hallucination_ci95")) if field in KEY_FIELDS else "–"
            cells = [str(c[o]) if o in c else "–" for o in FIELD_OUTCOMES]
            lines.append(
                f"| {field} | {with_ci(c['accuracy'], c.get('ci95'))} | {halluc} | " + " | ".join(cells) + " |"
            )
        h = s["non_deal_hallucinations"]
        on_deal = sum(s["fields"][f]["hallucinated"] for f in KEY_FIELDS)
        rate = with_ci(h.get("posts_with_any_rate"), h.get("posts_with_any_ci95"))
        lines += [
            "",
            f"**Hallucinations:** {on_deal} fields on deal posts; {h['fields_filled']} fields "
            f"filled on non-deal posts ({h['posts_with_any']} of {s['n_non_deal_posts']} non-deal posts, "
            f"{rate}).",
            "",
            f"**Failures ({len(s['failures'])}):**",
            "",
        ]
        lines += [
            f"- `{x['id']}` {x['field']} [{x['outcome']}]: expected `{x['expected']}`, got `{x['got']}`"
            for x in s["failures"]
        ]
        lines.append("")
    return "\n".join(lines)


def result_stem(result: JSONObject) -> str:
    repeat = result.get("repeat", {"index": 1, "of": 1})
    stem = f"{result['extractor']}_{result['timestamp']}"
    return stem if repeat["of"] == 1 else f"{stem}_r{repeat['index']}"


def save(result: JSONObject, results_dir: Path | None = None) -> Path:
    """Write <stem>.json and <stem>.md, never overwriting: a taken stem gets -2, -3, ...

    Two runs in the same second (or a rescore of a just-written file) would otherwise silently
    replace saved evidence. Exclusive-create ("x") keeps that true even if two processes race.
    """
    directory = RESULTS_DIR if results_dir is None else Path(results_dir)
    directory.mkdir(parents=True, exist_ok=True)
    base = result_stem(result)
    for stem in (base if i == 1 else f"{base}-{i}" for i in count(1)):
        json_path, md_path = directory / f"{stem}.json", directory / f"{stem}.md"
        if md_path.exists():
            continue
        try:
            with json_path.open("x") as f:
                f.write(json.dumps(result, indent=2))
        except FileExistsError:
            continue
        with md_path.open("x") as f:
            f.write(markdown(result))
        break
    return json_path


def load_run(path: Path) -> JSONObject:
    """Load an eval-run results file, exiting with status 2 (no traceback) for anything else,
    e.g. results/baseline-v1_comparison.json, which is a summary with no raw outputs to re-score.
    That file scores the key fields on all posts (non-deal posts earn correct_missing credit), so
    its numbers disagree with every current one; the message names the run that supersedes it.

    A missing, unreadable, empty or non-JSON path (say the run's .md twin) gets the same one-line
    exit: --rescore and --compare are read-only, so a traceback would only bury the actual mistake.
    """
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError) as e:  # ValueError covers JSONDecodeError and UnicodeDecodeError
        print(f"error: can't load {path} as an eval run results file: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    if not isinstance(saved, dict):
        print(f"error: {path} is not a JSON object, so it is not an eval run results file", file=sys.stderr)
        raise SystemExit(2)
    if "datasets" not in saved:
        print(
            f"error: {path} has no 'datasets' key: it is a legacy summary that predates deal-only scoring, "
            "not an eval run results file; "
            f"results/baseline-v1_comparison.json is superseded by {SUPERSEDING_BASELINE_RUN}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return saved


def make_extractor(name: str) -> tuple[Extractor, JSONObject]:
    if name == "regex":
        from deal_board.extractors.regex_baseline import RegexExtractor

        return RegexExtractor(), {}
    from deal_board.extractors.claude import PROMPT_VERSION, ClaudeExtractor

    ex = ClaudeExtractor()
    return ex, {"model": ex.model, "prompt_version": PROMPT_VERSION}


def timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def run_request_sha(name: str, model: str | None) -> str | None:
    """claude.request_sha for a Claude run's recorded model; None for regex, which sends no request.

    Derived here from the model the result records, rather than taken from make_extractor's meta,
    so every Claude result carries one and it always describes that result's own model.
    """
    if name != "claude" or model is None:
        return None
    from deal_board.extractors.claude import request_sha

    return request_sha(model)


def run_once(
    name: str,
    extractor: Extractor,
    meta: JSONObject,
    split: str,
    cache: RawCache | None,
    stamp: str,
    index: int,
    of: int,
    git: JSONObject | None = None,
) -> JSONObject:
    """One scored run. git is main()'s snapshot taken before the first repeat, so every repeat of an
    invocation reports the tree it started from; None (direct callers) reads it now."""
    result = {
        "extractor": name,
        "model": None,
        "prompt_version": None,
        **meta,
        "request_sha": run_request_sha(name, meta.get("model")),
        "split": split,
        "timestamp": stamp,
        "git": git_info() if git is None else git,
        "scoring": SCORING_NOTE,
        "scoring_version": SCORING_VERSION,
        "repeat": {"index": index, "of": of},
        "cache_enabled": cache is not None,
    }
    datasets = evaluate(extractor, split, cache)
    return {**result, "usage": usage_totals(datasets, result["model"]), "datasets": datasets}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractor", choices=["regex", "claude", "all"], default="all")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--rescore", type=Path, help="re-score a saved results JSON without re-running")
    mode.add_argument(
        "--compare",
        type=Path,
        nargs=2,
        metavar=("A", "B"),
        help="paired per-post comparison of two saved results JSONs; writes nothing",
    )
    ap.add_argument(
        "--split",
        choices=["all", "dev"],
        default="all",
        help="'dev' scores only posts.jsonl's dev split, for prompt tuning without seeing holdout",
    )
    ap.add_argument("--repeats", type=int, default=1, help="run each extractor N times, then summarize")
    ap.add_argument("--no-cache", action="store_true", help="bypass the raw-output cache entirely")
    ap.add_argument("--results-dir", type=Path, help=f"where results are written (default {RESULTS_DIR})")
    ap.add_argument("--cache-dir", type=Path, help=f"raw-output cache directory (default {CACHE_DIR})")
    args = ap.parse_args(argv)
    if args.repeats < 1:
        ap.error("--repeats must be >= 1")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    results_dir = args.results_dir or RESULTS_DIR

    if args.compare:
        a_path, b_path = args.compare
        print(format_comparison(compare_results(load_run(a_path), load_run(b_path)), str(a_path), str(b_path)))
        return

    if args.rescore:
        result = rescored_result(load_run(args.rescore), args.rescore, timestamp(), git_info())
        path = save(result, results_dir)
        print(markdown(result), f"\nsaved {path}", sep="\n")
        return

    names = ["regex", "claude"] if args.extractor == "all" else [args.extractor]
    if args.extractor == "claude" and not config.load_settings().has_api_key:
        # Explicitly asked for Claude: substituting regex would write results nobody asked for and
        # exit 0, so a missing key looks like a finished run. Only the default 'all' falls back.
        print("error: --extractor claude needs ANTHROPIC_API_KEY, which is not set", file=sys.stderr)
        raise SystemExit(2)
    if "claude" in names and not config.load_settings().has_api_key:
        print(
            "NOTE: ANTHROPIC_API_KEY is not set, so the Claude extractor was skipped. "
            "Running the regex baseline only.\n"
        )
        names = ["regex"]
    git = git_info()  # once, before anything is written, so later repeats can't see earlier output
    for name in names:
        extractor, meta = make_extractor(name)
        # One cache instance per extractor per invocation, shared by every repeat (see RawCache.get).
        # The regex baseline is deterministic and free, so it is never cached.
        cache = (
            None
            if name == "regex" or args.no_cache
            else RawCache(
                args.cache_dir or CACHE_DIR,
                meta["model"],
                meta["prompt_version"],
                source_sha256(extractor),
            )
        )
        stamp = timestamp()
        results = []
        for k in range(1, args.repeats + 1):
            result = run_once(name, extractor, meta, args.split, cache, stamp, k, args.repeats, git)
            path = save(result, results_dir)
            print(markdown(result), f"\nsaved {path}", sep="\n")
            results.append(result)
        if args.repeats > 1:
            print(format_repeat_summary(summarize_repeats(results)))


def run() -> None:
    """Entry point for `deal-eval` and `python -m deal_board.cli.eval`.

    .env is loaded only here, so tests calling main() never pick up a real key.
    A key already set in the shell wins over the file (override=False).
    """
    config.load_dotenv_file()
    main()


if __name__ == "__main__":
    run()
