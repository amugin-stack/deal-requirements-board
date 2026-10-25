"""The structured form of a deal post.

Two models:
- ExtractedFields: what an extractor is allowed to fill. Its JSON schema is the
  Claude tool's input_schema, so the model sees exactly these fields and rules.
- DealRequirement: ExtractedFields plus `missing_fields`, which code computes.
  The model never reports its own gaps; we derive them from what came back empty.
"""
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

State = Literal[
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
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
    def _upper_state(cls, v):
        return v.strip().upper() if isinstance(v, str) and v.strip() else None

    @field_validator("city", "notes", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        return v.strip() if isinstance(v, str) and v.strip() else None


class DealRequirement(ExtractedFields):
    missing_fields: list[str] = []

    @model_validator(mode="after")
    def _compute_missing(self):
        # A non-deal post has nothing to follow up on.
        self.missing_fields = (
            [f for f in KEY_FIELDS if getattr(self, f) is None] if self.is_deal_request else []
        )
        return self
