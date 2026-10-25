"""Tests for the deterministic follow-up question template."""

import pytest

from deal_board.followup import _QUESTIONS, PRIORITY, follow_up_question
from deal_board.schema import KEY_FIELDS, DealRequirement, PropertyType, Timeline, TransactionType

COMPLETE = {
    "is_deal_request": True,
    "state": "TX",
    "city": "Austin",
    "property_type": PropertyType.office,
    "transaction_type": TransactionType.buy,
    "deal_value_usd": 2_000_000,
    "timeline": Timeline.days_31_to_90,
}


def deal(**overrides: object) -> DealRequirement:
    return DealRequirement.model_validate({**COMPLETE, **overrides})


def test_priority_is_a_permutation_of_key_fields() -> None:
    assert sorted(PRIORITY) == sorted(KEY_FIELDS)
    assert len(PRIORITY) == len(set(PRIORITY))


def test_every_priority_field_has_a_question() -> None:
    assert set(_QUESTIONS) == set(PRIORITY)


def test_non_deal_post_gets_no_question() -> None:
    assert follow_up_question(DealRequirement(is_deal_request=False)) is None


def test_complete_deal_gets_no_question() -> None:
    req = deal()
    assert req.missing_fields == []
    assert follow_up_question(req) is None


def test_missing_state_names_the_city() -> None:
    req = DealRequirement(is_deal_request=True, city="Portland")
    assert follow_up_question(req) == (
        "Which state is the Portland property in? We only match brokers licensed in that state."
    )


def test_missing_state_without_city() -> None:
    req = DealRequirement(is_deal_request=True)
    assert follow_up_question(req) == ("Which state is the property in? We only match brokers licensed in that state.")


# Each case blanks the field under test plus every lower-priority field, so it is
# the highest-priority gap and must win; higher-priority fields stay known and
# show up in the wording.
@pytest.mark.parametrize(
    "field, expected",
    [
        (
            "transaction_type",
            "Is the client buying, selling, leasing, or doing a 1031 exchange for the property?",
        ),
        (
            "property_type",
            "What type of property is it: single-family, condo/townhome, "
            "multifamily, retail, office, industrial, land, or mixed-use?",
        ),
        ("city", "Which city is the office property in?"),
        ("deal_value_usd", "What is the client's price or budget for the office property in Austin?"),
        (
            "timeline",
            "When does the client need to close on the office property in Austin: "
            "within 30 days, in 1-3 months, or later?",
        ),
    ],
)
def test_highest_priority_gap_wins(field: str, expected: str) -> None:
    blanked = PRIORITY[PRIORITY.index(field) :]
    req = deal(**dict.fromkeys(blanked))
    assert req.missing_fields and set(req.missing_fields) == set(blanked)
    assert follow_up_question(req) == expected


def test_state_outranks_everything() -> None:
    req = DealRequirement(is_deal_request=True, city="Austin")
    question = follow_up_question(req)
    assert question is not None
    assert question.startswith("Which state is the Austin property in?")


def test_known_fields_make_questions_specific() -> None:
    assert follow_up_question(deal(timeline=None)) == (
        "When does the client need to close on the office property in Austin: within 30 days, in 1-3 months, or later?"
    )
    assert follow_up_question(deal(city=None, deal_value_usd=None)) == ("Which city is the office property in?")
    # "other" is not a useful label, so it is dropped.
    assert follow_up_question(deal(property_type=PropertyType.other, deal_value_usd=None)) == (
        "What is the client's price or budget for the property in Austin?"
    )


def test_uses_missing_fields_order_independently() -> None:
    # The question depends on PRIORITY, not on the order of missing_fields.
    req = deal(city=None, transaction_type=None)
    req.missing_fields = list(reversed(req.missing_fields))
    question = follow_up_question(req)
    assert question is not None
    assert question.startswith("Is the client buying")


def test_is_deterministic() -> None:
    req = DealRequirement(is_deal_request=True, city="Portland", property_type=PropertyType.land)
    first = follow_up_question(req)
    assert all(follow_up_question(req) == first for _ in range(20))
    assert follow_up_question(DealRequirement(**req.model_dump())) == first
