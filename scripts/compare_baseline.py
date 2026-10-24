"""Score the frozen regex baseline (tag baseline-v1) on the original posts and the independent holdout.

Run: python scripts/compare_baseline.py
Writes results/baseline-v1_comparison.json and prints a side-by-side table plus every holdout mistake.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extractors.regex_baseline import RegexExtractor  # noqa: E402
from scoring import SCORED_FIELDS, load_posts, score  # noqa: E402

SETS = {"original (35, author-written)": "data/posts.jsonl", "holdout (15, independent)": "data/holdout.jsonl"}


def run(path: str) -> dict:
    posts = load_posts(ROOT / path)
    ex = RegexExtractor()
    preds = {p["id"]: ex.extract(p["text"]).model_dump(mode="json") for p in posts}
    return score(posts, preds)


def main() -> None:
    results = {name: run(path) for name, path in SETS.items()}
    names = list(results)
    print(f"| field | {' | '.join(names)} | gap |")
    print("|---|" + "---:|" * (len(names) + 1))
    for f in (*SCORED_FIELDS, "MEAN"):
        accs = [
            sum(r["accuracy"].values()) / len(r["accuracy"]) if f == "MEAN" else r["accuracy"][f]
            for r in results.values()
        ]
        print(f"| {f} | " + " | ".join(f"{a:.0%}" for a in accs) + f" | {accs[0] - accs[1]:+.0%} |")

    hold = results[names[1]]
    print(f"\nHoldout mistakes ({len(hold['failures'])}):")
    for x in hold["failures"]:
        print(f"- {x['id']} {x['field']} [{x['outcome']}]: expected {x['expected']!r}, got {x['got']!r}\n    \"{x['text']}\"")

    out = ROOT / "results/baseline-v1_comparison.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"extractor": "regex", "git_tag": "baseline-v1", "results": results}, indent=2))
    print(f"\nsaved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
