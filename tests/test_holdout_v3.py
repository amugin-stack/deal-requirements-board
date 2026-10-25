"""Checks for data/holdout_v3.jsonl, the fresh double-labeled holdout.

The final eval reads these labels as ground truth, so a malformed record would
silently skew a score instead of failing loudly. These tests pin the shape and
the labeling invariants that schema.py states, so a bad hand edit (by the owner
overruling an adjudication, say) is caught before it reaches an eval.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from deal_board.schema import ExtractedFields

HOLDOUT_PATH = Path(__file__).resolve().parent.parent / "data" / "holdout_v3.jsonl"
RECORD_KEYS = {"id", "source", "split", "tags", "text", "label", "label_note", "adjudication"}
LABEL_KEYS = {
    "is_deal_request",
    "state",
    "city",
    "property_type",
    "transaction_type",
    "deal_value_usd",
    "timeline",
}
EXPECTED_IDS = [f"v3-{i:02d}" for i in range(1, 26)]


def _load() -> list[dict[str, Any]]:
    with HOLDOUT_PATH.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


RECORDS = _load()


def test_ids_are_unique_and_complete() -> None:
    """Ids join model outputs to labels; a duplicate would score one post twice."""
    ids = [r["id"] for r in RECORDS]
    assert ids == EXPECTED_IDS


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
def test_record_shape(record: dict[str, Any]) -> None:
    """Same keys as earlier holdouts plus `adjudication`, so loaders need no special case."""
    assert set(record) == RECORD_KEYS
    assert record["source"] == "synthetic-holdout-v3"
    assert record["split"] == "holdout_v3"
    assert isinstance(record["text"], str) and record["text"].strip()
    assert isinstance(record["label_note"], str) and record["label_note"].strip()
    assert record["adjudication"] in {"agreed", "lead-resolved"}


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
def test_tags_present(record: dict[str, Any]) -> None:
    """Tags drive per-slice error analysis; an untagged post drops out of every slice."""
    tags = record["tags"]
    assert isinstance(tags, list) and tags
    assert all(isinstance(t, str) and t.strip() for t in tags)


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
def test_label_parses_through_schema(record: dict[str, Any]) -> None:
    """A typo'd enum or state code must fail here, not quietly count as a miss in the eval.

    We also check the parsed value round-trips unchanged, because the schema's
    validators normalize input (upper-casing state, trimming city) and a label
    that only passes after normalization is not what the scorer will compare.
    """
    label = record["label"]
    assert set(label) == LABEL_KEYS
    parsed = ExtractedFields.model_validate(label).model_dump(mode="json", exclude={"notes"})
    assert parsed == label


@pytest.mark.parametrize("record", [r for r in RECORDS if not r["label"]["is_deal_request"]], ids=lambda r: r["id"])
def test_non_deal_posts_have_no_fields(record: dict[str, Any]) -> None:
    """A non-deal post has nothing to route or follow up on, so every field is null."""
    filled = {k: v for k, v in record["label"].items() if k != "is_deal_request" and v is not None}
    assert filled == {}


def test_set_covers_both_classes() -> None:
    """The brief requires at least 5 non-deal posts so precision on is_deal_request means something."""
    non_deals = sum(not r["label"]["is_deal_request"] for r in RECORDS)
    assert non_deals >= 5
    assert len(RECORDS) - non_deals >= 5
