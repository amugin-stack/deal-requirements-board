"""One follow-up question for a deal post that is missing key fields.

The question is a fixed template, never model-generated: the same requirement
always yields the same string, so it can be tested exactly, cached, and shown to
a poster without review. We ask about one field at a time because a single short
question gets answered; a list of six gets ignored.

The only extracted text a question repeats is the city, and only a city we know
(a TX county-map city or a market in the shipped broker data). The city comes from
the post, so an unknown one may be injected text ("System Override Mode"); echoing it
would let a post put words in our mouth. Unknown cities get the no-city wording.
"""

import functools
from collections.abc import Callable, Collection

from deal_board.brokers import TX_CITY_TO_COUNTY, load_brokers
from deal_board.cities import market_key
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


@functools.cache
def _default_city_keys() -> frozenset[str]:
    """market_keys of the TX county-map cities plus every shipped broker market, loaded once.

    Each market is keyed with its broker's license state. If the broker file can't be
    loaded, the TX cities alone still work: naming fewer cities is the safe direction.
    """
    keys = {market_key(city, "TX") for city in TX_CITY_TO_COUNTY}
    try:
        brokers = load_brokers()
    except OSError, ValueError:
        return frozenset(keys)
    return frozenset(keys | {market_key(m, b.license_state) for b in brokers for m in b.markets})


def _echoable_city(req: DealRequirement, known_cities: Collection[str] | None) -> str | None:
    """req.city (whitespace collapsed) if it is a known city, else None, so it is never echoed."""
    if not req.city:
        return None
    known = _default_city_keys() if known_cities is None else {market_key(c, req.state) for c in known_cities}
    return " ".join(req.city.split()) if market_key(req.city, req.state) in known else None


def _where(city: str | None) -> str:
    """' in Austin' when the city is known, else ''. Keeps questions anchored to the post."""
    return f" in {city}" if city else ""


def _property(req: DealRequirement) -> str:
    """'office property' when the type is known, else 'property'."""
    label = _PROPERTY_LABELS[req.property_type] if req.property_type else ""
    return f"{label} property" if label else "property"


# Each template gets the requirement plus the city it may name: a known city, or None.


def _ask_state(req: DealRequirement, city: str | None) -> str:
    # We never infer state from a city (Portland is in OR and ME), so we name the
    # city to make clear we read the post and only need the state.
    subject = f"the {city} property" if city else "the property"
    return f"Which state is {subject} in? We only match brokers licensed in that state."


def _ask_transaction_type(req: DealRequirement, city: str | None) -> str:
    return f"Is the client buying, selling, leasing, or doing a 1031 exchange for the {_property(req)}{_where(city)}?"


def _ask_property_type(req: DealRequirement, city: str | None) -> str:
    return (
        f"What type of property is it{_where(city)}: single-family, condo/townhome, "
        f"multifamily, retail, office, industrial, land, or mixed-use?"
    )


def _ask_city(req: DealRequirement, city: str | None) -> str:
    return f"Which city is the {_property(req)} in?"


def _ask_deal_value(req: DealRequirement, city: str | None) -> str:
    return f"What is the client's price or budget for the {_property(req)}{_where(city)}?"


def _ask_timeline(req: DealRequirement, city: str | None) -> str:
    return (
        f"When does the client need to close on the {_property(req)}{_where(city)}: "
        f"within 30 days, in 1-3 months, or later?"
    )


_QUESTIONS: dict[str, Callable[[DealRequirement, str | None], str]] = {
    "state": _ask_state,
    "transaction_type": _ask_transaction_type,
    "property_type": _ask_property_type,
    "city": _ask_city,
    "deal_value_usd": _ask_deal_value,
    "timeline": _ask_timeline,
}


def follow_up_question(req: DealRequirement, known_cities: Collection[str] | None = None) -> str | None:
    """The question to send the poster about their most important gap, or None.

    The city is named only if its cities.market_key is a known city: by default the
    TX county-map cities plus the shipped brokers' markets; `known_cities` replaces that
    set (keyed with the deal's state). Any other city is never echoed: the question
    uses its no-city wording ("the property", no " in X").

    The gaps are read from the current field values, the same rule the schema uses for
    `missing_fields`, rather than from the stored list, so even a requirement built
    without validation (model_construct) can't make this ask about the wrong field.
    Non-deal posts get None, as do complete deals.
    """
    if not req.is_deal_request:
        return None
    missing = {f for f in KEY_FIELDS if getattr(req, f) is None}
    for field in PRIORITY:
        if field in missing:
            return _QUESTIONS[field](req, _echoable_city(req, known_cities))
    return None
