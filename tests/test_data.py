"""The eval sets are only useful if their labels are valid. Catch label typos here, not in eval scores."""
import json
from pathlib import Path

import pytest

from schema import ExtractedFields, KEY_FIELDS

DATA = Path(__file__).parent.parent / "data"
SETS = {
    "posts.jsonl": {"sources": {"real", "synthetic"}, "splits": {"dev", "holdout"}},
    "holdout.jsonl": {"sources": {"synthetic-holdout"}, "splits": {"holdout_v2"}},
}


def load(name):
    return [json.loads(l) for l in (DATA / name).read_text().splitlines() if l.strip()]


@pytest.mark.parametrize("name", SETS)
def test_ids_unique_and_metadata_valid(name):
    posts = load(name)
    assert len({p["id"] for p in posts}) == len(posts)
    for p in posts:
        assert p["source"] in SETS[name]["sources"], p["id"]
        assert p["split"] in SETS[name]["splits"], p["id"]


@pytest.mark.parametrize("name", SETS)
def test_every_label_matches_schema(name):
    for p in load(name):
        assert set(p["label"]) == {"is_deal_request", *KEY_FIELDS}, p["id"]
        ExtractedFields.model_validate(p["label"])  # raises on a bad state code, enum, etc.


@pytest.mark.parametrize("name", SETS)
def test_non_deal_posts_have_no_fields(name):
    for p in load(name):
        if not p["label"]["is_deal_request"]:
            assert all(p["label"][f] is None for f in KEY_FIELDS), p["id"]


def test_no_text_shared_between_sets():
    assert not {p["text"] for p in load("posts.jsonl")} & {p["text"] for p in load("holdout.jsonl")}
