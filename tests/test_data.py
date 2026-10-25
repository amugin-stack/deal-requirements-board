"""The eval sets are only useful if their labels are valid. Catch label typos here, not in eval scores."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from deal_board.extractors import ExtractionError, parse_requirement
from deal_board.schema import KEY_FIELDS, ExtractedFields, JSONObject

DATA = Path(__file__).parent.parent / "data"
SETS = {
    "posts.jsonl": {"sources": {"real", "synthetic"}, "splits": {"dev", "holdout"}},
    "holdout.jsonl": {"sources": {"synthetic-holdout"}, "splits": {"holdout_v2"}},
}


def load(name: str) -> list[JSONObject]:
    return [json.loads(line) for line in (DATA / name).read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("name", SETS)
def test_ids_unique_and_metadata_valid(name: str) -> None:
    posts = load(name)
    assert len({p["id"] for p in posts}) == len(posts)
    for p in posts:
        assert p["source"] in SETS[name]["sources"], p["id"]
        assert p["split"] in SETS[name]["splits"], p["id"]


def label_roundtrips(label: JSONObject) -> bool:
    """True if the label validates and comes back unchanged (eval-quality/CQ-22).

    The scorer reads the raw label, so a label that only passes after the schema's validators
    normalise it (blank city -> None, say) is not what gets compared, and must be caught here.
    """
    try:
        parsed = ExtractedFields.model_validate(label).model_dump(mode="json", exclude={"notes"})
    except ValidationError:
        return False
    return bool(parsed == label)


@pytest.mark.parametrize("name", SETS)
def test_every_label_matches_schema(name: str) -> None:
    for p in load(name):
        assert set(p["label"]) == {"is_deal_request", *KEY_FIELDS}, p["id"]
        ExtractedFields.model_validate(p["label"])  # raises on a bad state code, enum, etc.
        assert label_roundtrips(p["label"]), p["id"]


GOOD_LABEL: JSONObject = {
    "is_deal_request": True,
    "state": "TX",
    "city": "Austin",
    "property_type": "office",
    "transaction_type": "buy",
    "deal_value_usd": 2_000_000,
    "timeline": "31_to_90_days",
}


def test_cq22_good_label_roundtrips() -> None:
    """eval-quality/CQ-22 (control): a clean label round-trips."""
    assert label_roundtrips(GOOD_LABEL)


@pytest.mark.parametrize(("field", "value"), [("city", ""), ("city", "  "), ("state", 5), ("state", "")])
def test_cq22_bad_label_fails_roundtrip(field: str, value: object) -> None:
    """eval-quality/CQ-22: blank or non-string city/state labels fail the round-trip helper."""
    assert not label_roundtrips({**GOOD_LABEL, field: value})


def test_cq22_parse_requirement_rejects_int_state() -> None:
    """eval-quality/CQ-22: parse_requirement raises on a non-string state instead of turning it into None."""
    with pytest.raises(ExtractionError):
        parse_requirement({"is_deal_request": True, "state": 123})


@pytest.mark.parametrize("name", SETS)
def test_non_deal_posts_have_no_fields(name: str) -> None:
    for p in load(name):
        if not p["label"]["is_deal_request"]:
            assert all(p["label"][f] is None for f in KEY_FIELDS), p["id"]


def test_no_text_shared_between_sets() -> None:
    assert not {p["text"] for p in load("posts.jsonl")} & {p["text"] for p in load("holdout.jsonl")}
