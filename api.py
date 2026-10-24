"""HTTP surface for the deal board: one endpoint, `POST /match`.

A broker posts free text; we extract a DealRequirement, run the licensing-gated matcher, and
return ranked brokers with reasons plus at most one follow-up question.

Design notes:
- Every collaborator (extractor, brokers, matcher, clock) is a FastAPI dependency, so tests swap
  in fakes with `app.dependency_overrides` and never touch the network or a real API key.
- `matcher.py` is built on a separate branch. It's imported lazily inside `get_matcher`, so this
  module imports and runs without it.
- The post text is user content and may hold client details. It is never logged; we log a short
  SHA-256 prefix and the length, which is enough to correlate a request without storing it.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from typing import Annotated, Literal, Protocol

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from brokers import Broker
from extractors import Extractor
from schema import DealRequirement

MAX_POST_CHARS = 4000

# --- Request / response models -----------------------------------------------------------------


class MatchRequest(BaseModel):
    """A broker's free-text deal post.

    Whitespace is stripped before the length check, so a post of only spaces is rejected (422)
    rather than sent to the extractor. The upper bound caps model cost and latency per request.
    """

    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_POST_CHARS)
    ]


class BrokerMatch(BaseModel):
    """One ranked broker.

    A flat, public subset of `Broker`: enough to contact and verify the licensee. It leaves out
    internal fields (markets, deal ranges) so the response shape doesn't change when they do.
    """

    broker_id: str
    name: str
    source: str
    license_number: str
    license_type: str
    score: int
    reasons: list[str]


class MatchResponse(BaseModel):
    """The result of `POST /match`.

    `excluded_for_licensing` is reported even when there are no matches, so the caller can tell
    "nobody is licensed there" apart from "no post details to match on".
    """

    model_config = ConfigDict(use_enum_values=True)

    extractor: Literal["claude", "regex"]
    requirement: DealRequirement
    matches: list[BrokerMatch]
    excluded_for_licensing: int = Field(ge=0)
    follow_up_question: str | None


# --- Matcher contract ---------------------------------------------------------------------------
# Structural types for matcher.MatchResult, so this module type-checks without importing it.


class MatchLike(Protocol):
    broker: Broker
    score: int
    reasons: list[str]


class MatchResultLike(Protocol):
    matches: Sequence[MatchLike]
    excluded_for_licensing: int


MatchFn = Callable[[DealRequirement, list[Broker], date], MatchResultLike]


# --- Dependencies -------------------------------------------------------------------------------


def get_extractor() -> Extractor:
    """Claude when `ANTHROPIC_API_KEY` is set, unless `DEAL_EXTRACTOR=regex`; otherwise regex."""
    raise NotImplementedError


def get_brokers() -> list[Broker]:
    """All broker records, loaded from CSV once per process and cached."""
    raise NotImplementedError


def get_as_of() -> date:
    """The date license expirations are checked against. A dependency so tests can pin it."""
    raise NotImplementedError


def get_matcher() -> MatchFn:
    """The real `matcher.match`, imported lazily so this branch runs before matcher.py exists."""
    raise NotImplementedError


# --- App ----------------------------------------------------------------------------------------

app = FastAPI(
    title="Deal Requirements Board",
    summary="Turn a broker's deal post into licensed, ranked broker matches.",
    version="0.1.0",
)
