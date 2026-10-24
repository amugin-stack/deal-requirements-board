"""Run extractors on the labeled sets and score them. The independent holdout is the headline number.

  python scripts/eval.py                      # baseline, plus Claude if ANTHROPIC_API_KEY is set
  python scripts/eval.py --extractor regex
  python scripts/eval.py --rescore results/claude_20261024T120000Z.json   # no API calls

Each run writes results/<extractor>_<UTC timestamp>.json (provenance, every raw output, every
prediction, scores) and a matching .md table. Scoring rules live in scoring.py.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extractors import ExtractionError, parse_requirement  # noqa: E402
from schema import KEY_FIELDS  # noqa: E402
from scoring import DEAL_VALUE_TOLERANCE, FIELD_OUTCOMES, load_posts, score  # noqa: E402

RESULTS_DIR = ROOT / "results"
DATASETS = {  # headline first
    "holdout": "data/holdout.jsonl",
    "posts": "data/posts.jsonl",
}
SCORING_NOTE = (
    "is_deal_request is scored on all posts; the other rows only on posts labeled as deal requests. "
    f"deal_value_usd matches within ±{DEAL_VALUE_TOLERANCE:.0%}. city ignores case, spacing, periods and "
    "commas (no spelling tolerance). missing_fields is exact set equality. notes is free text and NOT "
    "scored. Errors never count as correct and stay in the denominator."
)


def to_record(raw: dict | None, error: str | None = None) -> dict:
    """Turn one raw output into a scored record. Used for fresh runs and for re-scoring."""
    if raw is None:
        return {"raw": None, "prediction": None, "error": error}
    try:
        return {"raw": raw, "prediction": parse_requirement(raw).model_dump(mode="json"), "error": None}
    except ExtractionError as e:
        return {"raw": raw, "prediction": None, "error": str(e)}


def run_posts(extractor, posts: list[dict]) -> dict[str, dict]:
    records = {}
    for p in posts:
        try:
            if hasattr(extractor, "extract_raw"):  # Claude: keep the output before validation
                raw = extractor.extract_raw(p["text"])
            else:  # baseline output is already validated
                raw = extractor.extract(p["text"]).model_dump(mode="json")
            records[p["id"]] = to_record(raw)
        except ExtractionError as e:  # model failure (refusal, no tool call): scored as an error.
            records[p["id"]] = to_record(None, str(e))  # API/network errors are not caught.
    return records


def evaluate(extractor, datasets: dict = DATASETS) -> dict:
    out = {}
    for name, path in datasets.items():
        posts = load_posts(ROOT / path)
        records = run_posts(extractor, posts)
        out[name] = {"path": path, "records": records, "scores": score(posts, records)}
    return out


def rescore(saved: dict) -> dict:
    """Re-score a saved run against the current labels, from its raw outputs. No API calls."""
    out = {}
    for name, ds in saved["datasets"].items():
        posts = [p for p in load_posts(ROOT / ds["path"]) if p["id"] in ds["records"]]
        records = {pid: to_record(r["raw"], r["error"]) for pid, r in ds["records"].items()}
        out[name] = {"path": ds["path"], "records": records, "scores": score(posts, records)}
    return out


def git_info() -> dict:
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def markdown(result: dict) -> str:
    def pct(x):
        return "n/a" if x is None else f"{x:.0%}"

    lines = [f"# Eval: {result['extractor']}", ""]
    meta = {k: result[k] for k in ("model", "prompt_version", "timestamp") if result.get(k)}
    meta["commit"] = result["git"]["commit"][:7] + (" (uncommitted changes)" if result["git"]["dirty"] else "")
    lines += [", ".join(f"{k}: `{v}`" for k, v in meta.items()), "", f"_{SCORING_NOTE}_", ""]
    for name, ds in result["datasets"].items():
        s = ds["scores"]
        title = "holdout (headline)" if name == "holdout" else name
        lines += [f"## {title}: `{ds['path']}`, {s['n_posts']} posts "
                  f"({s['n_deal_posts']} deal, {s['n_non_deal_posts']} non-deal)", "",
                  "| field | accuracy | " + " | ".join(o.replace("_", "-") for o in FIELD_OUTCOMES) + " |",
                  "|---|---:|" + "---:|" * len(FIELD_OUTCOMES)]
        rows = [("is_deal_request", s["is_deal_request"]), *s["fields"].items(), ("missing_fields", s["missing_fields"])]
        for field, c in rows:
            cells = [str(c[o]) if o in c else "–" for o in FIELD_OUTCOMES]
            lines.append(f"| {field} | {pct(c['accuracy'])} | " + " | ".join(cells) + " |")
        h = s["non_deal_hallucinations"]
        on_deal = sum(s["fields"][f]["hallucinated"] for f in KEY_FIELDS)
        lines += ["", f"**Hallucinations:** {on_deal} fields on deal posts; {h['fields_filled']} fields "
                  f"filled on non-deal posts ({h['posts_with_any']} of {s['n_non_deal_posts']} non-deal posts).",
                  "", f"**Failures ({len(s['failures'])}):**", ""]
        lines += [f"- `{x['id']}` {x['field']} [{x['outcome']}]: expected `{x['expected']}`, got `{x['got']}`"
                  for x in s["failures"]]
        lines.append("")
    return "\n".join(lines)


def save(result: dict) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    stem = f"{result['extractor']}_{result['timestamp']}"
    (RESULTS_DIR / f"{stem}.json").write_text(json.dumps(result, indent=2))
    (RESULTS_DIR / f"{stem}.md").write_text(markdown(result))
    return RESULTS_DIR / f"{stem}.json"


def make_extractor(name: str):
    if name == "regex":
        from extractors.regex_baseline import RegexExtractor
        return RegexExtractor(), {}
    from extractors.claude import PROMPT_VERSION, ClaudeExtractor
    ex = ClaudeExtractor()
    return ex, {"model": ex.model, "prompt_version": PROMPT_VERSION}


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractor", choices=["regex", "claude", "all"], default="all")
    ap.add_argument("--rescore", type=Path, help="re-score a saved results JSON without re-running")
    args = ap.parse_args(argv)

    if args.rescore:
        saved = json.loads(args.rescore.read_text())
        result = {**saved, "timestamp": timestamp(), "git": git_info(),
                  "rescored_from": str(args.rescore), "datasets": rescore(saved)}
        path = save(result)
        print(markdown(result), f"\nsaved {path}", sep="\n")
        return

    names = ["regex", "claude"] if args.extractor == "all" else [args.extractor]
    if "claude" in names and not os.environ.get("ANTHROPIC_API_KEY"):
        print("NOTE: ANTHROPIC_API_KEY is not set, so the Claude extractor was skipped. "
              "Running the regex baseline only.\n")
        names = ["regex"]
    for name in names:
        extractor, meta = make_extractor(name)
        result = {"extractor": name, "model": None, "prompt_version": None, **meta,
                  "timestamp": timestamp(), "git": git_info(), "scoring": SCORING_NOTE,
                  "datasets": evaluate(extractor)}
        path = save(result)
        print(markdown(result), f"\nsaved {path}", sep="\n")


if __name__ == "__main__":
    # Loaded only when run as a script, so tests calling main() never pick up a real key.
    # A key already set in the shell wins over the file (override=False).
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    main()
