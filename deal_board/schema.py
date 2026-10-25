"""The structured form of a deal post.

Two models:
- ExtractedFields: what an extractor is allowed to fill. Its JSON schema is the
  Claude tool's input_schema, so the model sees exactly these fields and rules.
- DealRequirement: ExtractedFields plus `missing_fields`, which code computes.
  The model never reports its own gaps; we derive them from what came back empty,
  and re-derive them whenever a field changes, so the list can't go stale.
"""

from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# A parsed `json.loads` object (posts, labels, raw extractor output, results files). Values are
# arbitrary JSON, so they stay `Any`; code that needs guarantees validates into a model instead.
type JSONObject = dict[str, Any]

State = Literal[
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
]


class PropertyType(str, Enum):
    single_family = "single_family"
    condo_townhome = "condo_townhome"
    multifamily = "multifamily"  # 2+ units: duplex, fourplex, apartments
    retail = "retail"
    office = "office"
    industrial = "industrial"  # warehouse, flex, distribution
    land = "land"
    mixed_use = "mixed_use"
    other = "other"


class TransactionType(str, Enum):
    buy = "buy"
    sell = "sell"
    lease = "lease"  # tenant or landlord side
    exchange_1031 = "1031_exchange"  # client is buying replacement property in a 1031


class Timeline(str, Enum):
    within_30_days = "within_30_days"
    days_31_to_90 = "31_to_90_days"
    over_90_days = "over_90_days"


# Fields a broker needs to act on a post. If one is empty we may ask about it.
KEY_FIELDS = ("state", "city", "property_type", "transaction_type", "deal_value_usd", "timeline")


# The fields an extractor fills, validated without repair. The only normalisation: an ASCII
# state is stripped and upper-cased, city and notes are stripped, and a blank state/city/notes
# becomes null. Everything else that doesn't fit (a non-string, a non-ASCII state, an unknown
# code or enum value) raises. (A comment, not a docstring: pydantic would put a docstring into
# the JSON schema, which is the Claude tool's input_schema and part of PROMPT_VERSION.)
class ExtractedFields(BaseModel):
    is_deal_request: bool = Field(
        description="True only if the post asks for a broker, agent, buyer, or property for a "
        "specific deal. False for news, hiring, celebrations, CE questions, lender requests."
    )
    state: State | None = Field(
        None,
        description="Two-letter code of the state where the property is. Only if the post names "
        "the state. Never infer it from a city, neighborhood, or metro name.",
    )
    city: str | None = Field(
        None,
        description="The one city where the property is. Null if none, or if several are named, "
        "or only a region/metro is given (e.g. 'DFW', 'Inland Empire').",
    )
    property_type: PropertyType | None = Field(
        None, description="Null if the post doesn't say. 'NNN' alone is a lease type, not a property type."
    )
    transaction_type: TransactionType | None = Field(
        None,
        description="What the client is doing. Any 1031 replacement purchase is 1031_exchange. "
        "Null for requests that aren't a transaction (e.g. property management).",
    )
    deal_value_usd: int | None = Field(
        None,
        ge=0,
        description="The client's price or budget in dollars. For a range use the upper bound; for "
        "'$4M+' use 4000000. Ignore NOI, rent, $/sf rates, earnest money, appraisals/BOVs, cap rates.",
    )
    timeline: Timeline | None = Field(
        None,
        description="Bucket for a stated relative deadline ('ASAP', '2 weeks', '45 days', '6 months'). "
        "Null for vague timing ('soon', 'no rush') or calendar dates ('Q3', 'year end', 'spring'), "
        "since we don't know when the post was written.",
    )
    notes: str | None = Field(None, description="Other useful details in a short phrase, or null.")

    @field_validator("state", mode="before")
    @classmethod
    def _upper_state(cls, v: object) -> str | None:
        """Normalise only an ASCII string's case and surrounding whitespace; blank means null.

        Anything else raises, so the eval counts it as an error instead of a quiet repair:
        a non-string (48, ['TX'], b'TX') is not a state, and a non-ASCII string must not
        case-fold into one ('\u0131l'.upper() == 'IL'). Codes that aren't states then fail
        the State literal.
        """
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError(f"state must be a string or null, got {type(v).__name__}")
        v = v.strip()
        if not v:
            return None
        if not v.isascii():
            raise ValueError(f"state must be an ASCII two-letter code, got {v!r}")
        return v.upper()

    @field_validator("city", "notes", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> str | None:
        """Strip surrounding whitespace and turn a blank string into null; a non-string raises.

        A string that can't be encoded as UTF-8 (a lone surrogate from a JSON '\\ud800' escape)
        raises too: it would validate here and then fail only when the API serializes its
        response, past the route's error handling.
        """
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError(f"must be a string or null, got {type(v).__name__}")
        try:
            v.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("must be valid Unicode text (no lone surrogates)") from None
        return v.strip() or None


class DealRequirement(ExtractedFields):
    """ExtractedFields plus the derived `missing_fields`.

    `missing_fields` is always recomputed from the current field values: on construction, on
    attribute assignment (validate_assignment) and on `model_copy(update=...)`, which pydantic
    would otherwise do without running validators. A stale list would make the follow-up ask
    about the wrong gap, e.g. skip "Which state?" after state was cleared.
    """

    # Only DealRequirement validates on assignment; ExtractedFields' config is part of the tool schema.
    model_config = ConfigDict(validate_assignment=True)

    missing_fields: list[str] = []

    @model_validator(mode="after")
    def _compute_missing(self) -> Self:
        # A non-deal post has nothing to follow up on.
        missing = [f for f in KEY_FIELDS if getattr(self, f) is None] if self.is_deal_request else []
        # Written to __dict__, not assigned: with validate_assignment on, `self.missing_fields = ...`
        # would re-run this validator forever.
        self.__dict__["missing_fields"] = missing
        return self

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """Copy, re-validating when `update` is given so updated values are checked and gaps recomputed.

        pydantic's model_copy(update=...) skips validation, which would let a cleared field keep
        its old missing_fields. A validated copy builds fresh objects, so `deep` needs no extra work.
        """
        if not update:
            return super().model_copy(deep=deep)
        return self.model_validate({**self.model_dump(), **update})
