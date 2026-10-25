"""One follow-up question for a deal post that is missing key fields.

The question is a fixed template, never model-generated: the same requirement
always yields the same string, so it can be tested exactly, cached, and shown to
a poster without review. We ask about one field at a time because a single short
question gets answered; a list of six gets ignored.
"""

from collections.abc import Callable

from deal_board.schema import KEY_FIELDS, DealRequirement, PropertyType

# Most important first. State leads because the license gate depends on it:
# without a state we cannot match any broker at all, whereas every other gap
# only makes the match less precise. This order deliberately differs from
# KEY_FIELDS, which is the schema's display order, not a priority.
PRIORITY: tuple[str, ...] = (
    "state",
    "transaction_type",
    "property_type",
    "city",
    "deal_value_usd",
    "timeline",
)

# Fail at import, not silently at runtime, if a key field is added to or renamed
# in the schema without updating the priority. (Not `assert`: that vanishes under -O.)
if sorted(PRIORITY) != sorted(KEY_FIELDS):
    raise RuntimeError(f"PRIORITY {PRIORITY} is not a permutation of KEY_FIELDS {KEY_FIELDS}")

_PROPERTY_LABELS: dict[PropertyType, str] = {
    PropertyType.single_family: "single-family",
    PropertyType.condo_townhome: "condo/townhome",
    PropertyType.multifamily: "multifamily",
    PropertyType.retail: "retail",
    PropertyType.office: "office",
    PropertyType.industrial: "industrial",
    PropertyType.land: "land",
    PropertyType.mixed_use: "mixed-use",
    PropertyType.other: "",  # "other" adds nothing to the question
}

# Same reasoning as above: a new PropertyType must get a label, or questions would KeyError.
if set(_PROPERTY_LABELS) != set(PropertyType):
    raise RuntimeError("_PROPERTY_LABELS must cover every PropertyType")


def _where(req: DealRequirement) -> str:
    """' in Austin' when the city is known, else ''. Keeps questions anchored to the post."""
    return f" in {req.city}" if req.city else ""


def _property(req: DealRequirement) -> str:
    """'office property' when the type is known, else 'property'."""
    label = _PROPERTY_LABELS[req.property_type] if req.property_type else ""
    return f"{label} property" if label else "property"


def _ask_state(req: DealRequirement) -> str:
    # We never infer state from a city (Portland is in OR and ME), so we name the
    # city to make clear we read the post and only need the state.
    subject = f"the {req.city} property" if req.city else "the property"
    return f"Which state is {subject} in? We only match brokers licensed in that state."


def _ask_transaction_type(req: DealRequirement) -> str:
    return f"Is the client buying, selling, leasing, or doing a 1031 exchange for the {_property(req)}{_where(req)}?"


def _ask_property_type(req: DealRequirement) -> str:
    return (
        f"What type of property is it{_where(req)}: single-family, condo/townhome, "
        f"multifamily, retail, office, industrial, land, or mixed-use?"
    )


def _ask_city(req: DealRequirement) -> str:
    return f"Which city is the {_property(req)} in?"


def _ask_deal_value(req: DealRequirement) -> str:
    return f"What is the client's price or budget for the {_property(req)}{_where(req)}?"


def _ask_timeline(req: DealRequirement) -> str:
    return (
        f"When does the client need to close on the {_property(req)}{_where(req)}: "
        f"within 30 days, in 1-3 months, or later?"
    )


_QUESTIONS: dict[str, Callable[[DealRequirement], str]] = {
    "state": _ask_state,
    "transaction_type": _ask_transaction_type,
    "property_type": _ask_property_type,
    "city": _ask_city,
    "deal_value_usd": _ask_deal_value,
    "timeline": _ask_timeline,
}


def follow_up_question(req: DealRequirement) -> str | None:
    """The question to send the poster about their most important gap, or None.

    Driven by `req.missing_fields`, which the schema computes from what came back
    empty, so this never disagrees with the gaps shown elsewhere. Non-deal posts
    have no missing fields and therefore get None, as do complete deals.
    """
    if not req.is_deal_request:
        return None
    missing = set(req.missing_fields)
    for field in PRIORITY:
        if field in missing:
            return _QUESTIONS[field](req)
    return None
