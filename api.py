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

import hashlib
import logging
import os
from collections.abc import Callable, Sequence
from datetime import date
from functools import lru_cache
from typing import Annotated, Literal, Protocol

import anthropic
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

import brokers as broker_store
from brokers import Broker
from extractors import ExtractionError, Extractor
from followup import follow_up_question
from schema import DealRequirement

logger = logging.getLogger(__name__)

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
    """Claude when `ANTHROPIC_API_KEY` is set, unless `DEAL_EXTRACTOR=regex`; otherwise regex.

    The environment is read on every call so the choice follows the current config, but the
    Claude extractor itself is cached: it owns an HTTP client with a connection pool, and
    building one per request would throw that pool away each time.
    """
    if os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("DEAL_EXTRACTOR") != "regex":
        return _claude_extractor()
    return _regex_extractor()


@lru_cache(maxsize=1)
def _claude_extractor() -> Extractor:
    # Imported here so the regex-only path never constructs an Anthropic client.
    from extractors.claude import ClaudeExtractor

    return ClaudeExtractor()


@lru_cache(maxsize=1)
def _regex_extractor() -> Extractor:
    from extractors.regex_baseline import RegexExtractor

    return RegexExtractor()


@lru_cache(maxsize=1)
def get_brokers() -> list[Broker]:
    """All broker records, loaded from CSV once per process and cached.

    The list is shared across requests, so the matcher must treat it as read-only.
    """
    return broker_store.load_brokers()


def get_as_of() -> date:
    """The date license expirations are checked against. A dependency so tests can pin it."""
    return date.today()


class MatcherUnavailable(RuntimeError):
    """matcher.py is missing or broken: a deployment problem, not something the caller did."""


def get_matcher() -> MatchFn:
    """The real `matcher.match`, imported lazily so this branch runs before matcher.py exists.

    A failed import is a server misconfiguration, so it surfaces as a 503 with a fixed message
    (see the handler below) instead of a 500 whose traceback would expose file paths.
    """
    try:
        from matcher import match
    except ImportError as e:
        logger.error("matcher import failed: %s", type(e).__name__)
        raise MatcherUnavailable from e
    return match


# --- App ----------------------------------------------------------------------------------------

app = FastAPI(
    title="Deal Requirements Board",
    summary="Turn a broker's deal post into licensed, ranked broker matches.",
    version="0.1.0",
)


@app.exception_handler(MatcherUnavailable)
def _matcher_unavailable(_: Request, __: MatcherUnavailable) -> JSONResponse:
    # A handler rather than an HTTPException because it is raised while resolving a dependency,
    # before the route body (and its try/except) runs.
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": "matcher unavailable"}
    )


@app.exception_handler(RequestValidationError)
def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422, minus each error's `input`.

    The default echoes the rejected value, which for an oversize post means returning up to the
    whole post. It's the caller's own text, but there's no reason to reflect it into responses
    (and from there into proxy or client logs); `loc`, `msg` and `ctx` say what was wrong.
    """
    errors = [{k: v for k, v in err.items() if k != "input"} for err in exc.errors()]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": jsonable_encoder(errors)},
    )


def _post_id(text: str) -> str:
    """A short, stable fingerprint for correlating logs with a post without storing the post."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


@app.post("/match", response_model=MatchResponse)
def match_post(
    body: MatchRequest,
    extractor: Annotated[Extractor, Depends(get_extractor)],
    brokers: Annotated[list[Broker], Depends(get_brokers)],
    as_of: Annotated[date, Depends(get_as_of)],
    match: Annotated[MatchFn, Depends(get_matcher)],
) -> MatchResponse:
    """Extract a requirement from the post, then return licensed brokers ranked for it.

    A plain `def`: extraction is a blocking network call, so FastAPI runs this in its threadpool
    instead of stalling the event loop.

    Extraction failures are mapped here with `HTTPException` rather than global handlers, because
    only the route knows the post id to log alongside the error class. Neither branch logs or
    returns exception text: pydantic errors embed input values (i.e. parts of the post), and
    upstream API errors can carry request details.
    """
    text = body.text  # already stripped by MatchRequest
    post_id = _post_id(text)

    try:
        requirement = extractor.extract(text)
    except ExtractionError as e:
        logger.warning("extraction failed post=%s extractor=%s error=%s",
                       post_id, extractor.name, type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="could not extract a deal requirement from this post",
        ) from e
    except anthropic.APIError as e:
        logger.error("extraction service error post=%s extractor=%s error=%s status=%s",
                     post_id, extractor.name, type(e).__name__, getattr(e, "status_code", None))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="extraction service unavailable",
        ) from e

    # Licensing gating, non-deal posts and missing states are the matcher's job; the API only
    # reshapes what it returns.
    result = match(requirement, brokers, as_of)
    matches = [
        BrokerMatch(
            broker_id=m.broker.broker_id,
            name=m.broker.name,
            source=m.broker.source,
            license_number=m.broker.license_number,
            license_type=m.broker.license_type,
            score=m.score,
            reasons=list(m.reasons),
        )
        for m in result.matches
    ]

    logger.info("matched post=%s chars=%d extractor=%s matches=%d excluded=%d",
                post_id, len(text), extractor.name, len(matches), result.excluded_for_licensing)

    return MatchResponse(
        extractor=extractor.name,
        requirement=requirement,
        matches=matches,
        excluded_for_licensing=result.excluded_for_licensing,
        follow_up_question=follow_up_question(requirement),
    )
