"""Pin the frozen regex baseline (git tag `baseline-v1`), known bugs included.

`golden/regex_baseline.json` holds RegexExtractor's output for every post in
data/posts.jsonl and data/holdout.jsonl. It was generated once and must NEVER be
regenerated. If this test fails, the baseline changed: revert that change to
extractors/regex_baseline.py - do not update the golden file.
"""
import json
from pathlib import Path
from typing import Any

import pytest

from extractors.regex_baseline import RegexExtractor
from scoring import load_posts

ROOT = Path(__file__).parent.parent
GOLDEN: dict[str, Any] = json.loads((Path(__file__).parent / "golden" / "regex_baseline.json").read_text())
POSTS: dict[str, str] = {
    f"{stem}/{post['id']}": post["text"]
    for stem in ("posts", "holdout")
    for post in load_posts(ROOT / "data" / f"{stem}.jsonl")
}


def test_golden_covers_exactly_the_posts() -> None:
    assert set(GOLDEN) == set(POSTS)


@pytest.mark.parametrize("key", sorted(POSTS))
def test_baseline_output_matches_golden(key: str) -> None:
    assert RegexExtractor().extract(POSTS[key]).model_dump(mode="json") == GOLDEN[key]
