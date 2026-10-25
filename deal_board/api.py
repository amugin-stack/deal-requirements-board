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

from deal_board import brokers as broker_store
from deal_board.brokers import Broker
from deal_board.extractors import ExtractionError, Extractor
from deal_board.followup import follow_up_question
from deal_board.schema import DealRequirement

logger = logging.getLogger(__name__)

# The Anthropic SDK logs full request options, post text included, at DEBUG (enabled by
# ANTHROPIC_LOG=debug or a DEBUG root logger). Pin it and its HTTP client to WARNING so turning
# on debug logging for this app can never write a post to the logs.
for _sdk_logger in ("anthropic", "httpx", "httpx2"):
    logging.getLogger(_sdk_logger).setLevel(logging.WARNING)

MAX_POST_CHARS = 4000
# Raw body cap, checked before JSON parsing. `MAX_POST_CHARS` applies only after stripping, so
# without this a post padded with megabytes of whitespace would be read and parsed in full.
# 4000 chars of 4-byte UTF-8 plus JSON escaping and the key fits well inside 64 KiB.
MAX_BODY_BYTES = 64 * 1024
# Upper bound on one Claude call, retries included. The SDK default is 600s with 2 retries, which
# would let a slow upstream pin threadpool workers for many minutes. A timeout surfaces as
# anthropic.APIConnectionError, so it maps to the same 503 as any other upstream failure.
CLAUDE_TIMEOUT_SECONDS = 30.0
CLAUDE_MAX_RETRIES = 1

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


class ErrorDetail(BaseModel):
    """The body of every non-validation error. `detail` is always a fixed, safe message."""

    detail: str


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
    forced = os.environ.get("DEAL_EXTRACTOR", "").strip().lower()
    if os.environ.get("ANTHROPIC_API_KEY") and forced != "regex":
        return _claude_extractor()
    return _regex_extractor()


@lru_cache(maxsize=1)
def _claude_extractor() -> Extractor:
    # Imported here so the regex-only path never constructs an Anthropic client.
    from deal_board.extractors.claude import ClaudeExtractor

    client = anthropic.Anthropic(timeout=CLAUDE_TIMEOUT_SECONDS, max_retries=CLAUDE_MAX_RETRIES)
    return ClaudeExtractor(client=client)


@lru_cache(maxsize=1)
def _regex_extractor() -> Extractor:
    from deal_board.extractors.regex_baseline import RegexExtractor

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

    FastAPI resolves dependencies before validating the body, so while the matcher is missing
    even an invalid post gets this 503 rather than a 422. That is intended: the service is down,
    and failing here also avoids spending an extraction call on a request that can't be matched.
    Only ImportError is mapped; any other error while importing is a bug and stays a 500.
    """
    try:
        from deal_board.matcher import match
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


class BodySizeLimit:
    """ASGI middleware: reject request bodies over `max_bytes` with 413 before they are parsed.

    It rejects an oversize `Content-Length` up front, then reads the body itself, stopping as
    soon as it passes the cap, so a chunked upload without a length header can't get around it.
    The (at most `max_bytes`) body is then replayed to the app. Reading here rather than
    raising from a wrapped `receive` matters: FastAPI turns any error while reading the body
    into a 400, which would hide the 413.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await self._reject(send)
            return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":  # client disconnected
                await self.app(scope, _replay([message]), send)
                return
            body = message.get("body", b"")
            size += len(body)
            if size > self.max_bytes:
                await self._reject(send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        replay = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]
        await self.app(scope, _replay(replay, then=receive), send)

    @staticmethod
    async def _reject(send) -> None:
        response = JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={"detail": "request body too large"},
        )
        await send({"type": "http.response.start", "status": response.status_code,
                    "headers": response.raw_headers})
        await send({"type": "http.response.body", "body": response.body})


def _replay(messages: list[dict], then=None):
    """An ASGI `receive` that returns `messages` in order, then defers to `then` (disconnects)."""
    pending = list(messages)

    async def receive():
        if pending:
            return pending.pop(0)
        if then is not None:
            return await then()
        return {"type": "http.disconnect"}

    return receive


app.add_middleware(BodySizeLimit, max_bytes=MAX_BODY_BYTES)


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


@app.post(
    "/match",
    response_model=MatchResponse,
    responses={
        413: {"model": ErrorDetail, "description": "Request body over the size limit"},
        500: {"model": ErrorDetail, "description": "The extractor failed unexpectedly"},
        503: {"model": ErrorDetail, "description": "Extraction service or matcher unavailable"},
    },
)
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
    except Exception as e:
        # An extractor bug (e.g. the frozen regex baseline overflows on a 400-digit number).
        # Mapped here, not left to the server, so the fixed detail and post-id log line hold and
        # no exception text, which might quote the post, reaches the response or a traceback.
        logger.error("extractor crashed post=%s extractor=%s error=%s",
                     post_id, extractor.name, type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="extraction failed unexpectedly",
        ) from None

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
