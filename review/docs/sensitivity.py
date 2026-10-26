"""Secondary sensitivity line: city accuracy on holdout v3 with the two known-ambiguous city labels excluded.

Reads only the two final results JSONs and data/holdout_v3.jsonl. No API calls. Uses the project's own
scoring (deal_board.scoring) on the stored predictions, which validate.py's raw_consistent check
confirms match the raw outputs. Output is committed as review/docs/sensitivity.txt and quoted in README.

Run: .venv/bin/python review/docs/sensitivity.py > review/docs/sensitivity.txt
"""

from __future__ import annotations

import json
from pathlib import Path

from deal_board.cli.eval import with_ci
from deal_board.scoring import load_posts, paired_comparison, post_correctness, score

ROOT = Path(__file__).resolve().parents[2]
REGEX = "results/regex_20261026T003559Z.json"
CLAUDE = "results/claude_20261026T003610Z.json"
LABELS = "data/holdout_v3.jsonl"
EXCLUDE = ("v3-09", "v3-16")  # city "near Amarillo" / "outside Bend", labeled null (owner: kept as shipped)


def records(path: str) -> dict[str, dict[str, object]]:
    data = json.loads((ROOT / path).read_text())
    recs: dict[str, dict[str, object]] = data["datasets"]["holdout_v3"]["records"]
    return recs


def city_line(name: str, posts: list[dict[str, object]], recs: dict[str, dict[str, object]]) -> str:
    cell = score(posts, recs)["fields"]["city"]  # type: ignore[arg-type]
    n = sum(1 for p in posts if p["label"]["is_deal_request"])  # type: ignore[index]
    good = cell["correct"] + cell["correct_missing"]
    return f"{name} city {with_ci(cell['accuracy'], cell['ci95'])} ({good}/{n} deal posts)"


def main() -> None:
    all_posts = load_posts(ROOT / LABELS)
    kept = [p for p in all_posts if p["id"] not in EXCLUDE]
    rx, cl = records(REGEX), records(CLAUDE)
    cmp = paired_comparison(post_correctness(kept, rx)["city"], post_correctness(kept, cl)["city"])
    print(f"Sensitivity (secondary): holdout v3 city accuracy with {', '.join(EXCLUDE)} excluded")
    print(f"Sources: {REGEX}, {CLAUDE}, {LABELS}")
    for label, posts in (("all 25 posts", all_posts), (f"{len(kept)} posts", kept)):
        print(f"- {label}: {city_line('regex', posts, rx)}; {city_line('Claude', posts, cl)}")
    print(
        f"- paired exact McNemar on the {cmp['n']} remaining deal posts: p = {cmp['p_value']:.3g} "
        f"(Claude-only correct: {', '.join(cmp['b_only']) or 'none'}; "
        f"regex-only correct: {', '.join(cmp['a_only']) or 'none'})"
    )


if __name__ == "__main__":
    main()
