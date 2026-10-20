"""The eval set is only useful if its labels are valid. Catch label typos here, not in eval scores."""
import json
from pathlib import Path

from schema import ExtractedFields, KEY_FIELDS

POSTS = [json.loads(l) for l in (Path(__file__).parent.parent / "data/posts.jsonl").read_text().splitlines() if l.strip()]


def test_ids_unique_and_metadata_valid():
    assert len({p["id"] for p in POSTS}) == len(POSTS)
    for p in POSTS:
        assert p["source"] in {"real", "synthetic"}, p["id"]
        assert p["split"] in {"dev", "holdout"}, p["id"]


def test_every_label_matches_schema():
    for p in POSTS:
        assert set(p["label"]) == {"is_deal_request", *KEY_FIELDS}, p["id"]
        ExtractedFields.model_validate(p["label"])  # raises on a bad state code, enum, etc.


def test_non_deal_posts_have_no_fields():
    for p in POSTS:
        if not p["label"]["is_deal_request"]:
            assert all(p["label"][f] is None for f in KEY_FIELDS), p["id"]
