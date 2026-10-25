"""HTTP surface for the deal board: one endpoint, `POST /match`.

A broker posts free text; we extract a DealRequirement, run the licensing-gated matcher, and
return ranked brokers with reasons plus at most one follow-up question.

Design notes:
- Every collaborator (extractor, brokers, matcher, clock) is a FastAPI dependency, so tests swap
  in fakes with `app.dependency_overrides` and never touch the network or a real API key.
- `matcher.py` is built on a separate branch. It's imported lazily inside `get_matcher`, so this
  module imports and runs without it.
- The post text is user content and may hold client details. It is never logged; we log a short
  HMAC-SHA256 prefix under a random per-process key, plus the length. That correlates the log
  lines of one request (and repeats of a post within one process) without storing the post, and
  because the key never leaves memory, a log reader can't confirm a guessed or templated post by
  hashing it offline. Ids don't match across processes or restarts.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Annotated, Any, Final, Literal, Protocol, cast, get_args
from zoneinfo import ZoneInfo

import anthropic
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, ValidationError, WithJsonSchema
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from deal_board import brokers as broker_store
from deal_board.brokers import Broker
from deal_board.config import FORCE_CLAUDE, FORCE_REGEX, Settings, load_settings
from deal_board.extractors import ExtractionError, Extractor
from deal_board.followup import follow_up_question
from deal_board.schema import DealRequirement, ExtractedFields, State

logger = logging.getLogger(__name__)

# The Anthropic SDK logs full request options, post text included, at DEBUG (enabled by
# ANTHROPIC_LOG=debug or a DEBUG root logger). Pin it and its HTTP client to WARNING so turning
# on debug logging for this app can never write a post to the logs. Tracing is the other channel:
# the client is built with `open_telemetry=False` (see `_claude_extractor`), so the SDK's
# ANTHROPIC_OPEN_TELEMETRY_* env vars can't turn on spans that carry the post text either.
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

# One character the server does not strip. The class lists exactly the characters
# `StringConstraints(strip_whitespace=True)` strips (pydantic-core trims Unicode White_Space,
# which is not quite `str.isspace()` or JSON Schema's `\s`), so "contains a match" in the
# published schema means "non-empty after stripping" on the server.
NON_WHITESPACE_PATTERN = r"[^\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]"

# The published schema for `MatchRequest.text`. It replaces the generated one, whose raw
# minLength/maxLength disagree with the server both ways: the bounds apply after stripping, so
# 10 spaces + 4000 chars is accepted and "   \n" is rejected. The raw size bound is the 64 KiB
# body cap (413), not a string length.
_TEXT_JSON_SCHEMA: dict[str, str] = {
    "type": "string",
    "title": "Text",
    "pattern": NON_WHITESPACE_PATTERN,
    "description": (
        f"The deal post. Surrounding whitespace is stripped first; the stripped text must be "
        f"1-{MAX_POST_CHARS} characters, so it needs at least one non-whitespace character."
    ),
}


class MatchRequest(BaseModel):
    """A broker's free-text deal post.

    Whitespace is stripped before the length check, so a post of only spaces is rejected (422)
    rather than sent to the extractor. The upper bound caps model cost and latency per request.
    """

    text: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_POST_CHARS),
        WithJsonSchema(_TEXT_JSON_SCHEMA),
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


# --- Re-validating extractor output ------------------------------------------------------------

# The two-letter codes `schema.State` allows.
STATE_CODES: Final = frozenset(get_args(State))


def revalidate_requirement(extracted: DealRequirement) -> DealRequirement:
    """A freshly validated copy of an extractor's output, failing closed on the gate's inputs.

    The `Extractor` protocol promises a validated `DealRequirement`, but nothing enforces it: a
    buggy extractor can hand back a `model_construct`ed one whose fields were never checked.
    The matcher and the response would then trust a state like "ZZ" (a 200 body that breaks its
    own schema) or a truthy `is_deal_request="no"` (matches for a non-deal). So the API checks
    again, before matching:

    - `state` is kept only if it is a str that is exactly a State code. Anything else, including
      "fl" or " AZ " that validation would normalize, becomes None: no state, so nobody is
      licensed and the follow-up asks for it. Normalizing would trust a value the extractor
      should never have produced.
    - `is_deal_request` counts only if it `is True`; anything else is a non-deal (no matches).
    - every other field goes through `DealRequirement.model_validate`, which also recomputes
      `missing_fields`; an invalid value raises `pydantic.ValidationError` (the route's 422).

    Pure. A real extractor's output comes back equal to what went in.
    """
    fields: dict[str, Any] = {name: getattr(extracted, name, None) for name in ExtractedFields.model_fields}
    state = fields["state"]
    fields["state"] = state if isinstance(state, str) and state in STATE_CODES else None
    fields["is_deal_request"] = fields["is_deal_request"] is True
    return DealRequirement.model_validate(fields)


# --- Matcher contract ---------------------------------------------------------------------------
# Structural types for matcher.MatchResult, so this module type-checks without importing it.
# Read-only properties, because this module only reads them: that makes them covariant, so
# MatchResult's `list[Match]` fits `Sequence[MatchLike]` (a mutable attribute would be invariant).


class MatchLike(Protocol):
    @property
    def broker(self) -> Broker: ...

    @property
    def score(self) -> int: ...

    @property
    def reasons(self) -> Sequence[str]: ...


class MatchResultLike(Protocol):
    @property
    def matches(self) -> Sequence[MatchLike]: ...

    @property
    def excluded_for_licensing(self) -> int: ...


MatchFn = Callable[[DealRequirement, list[Broker], date], MatchResultLike]


# --- Dependencies -------------------------------------------------------------------------------


class ExtractorUnavailable(RuntimeError):
    """The extractor config is unusable (DEAL_EXTRACTOR invalid, or `claude` with no API key).

    A deployment problem, not something the caller did, so it maps to a fixed 503.
    """


def get_extractor() -> Extractor:
    """Claude when `ANTHROPIC_API_KEY` is set, unless `DEAL_EXTRACTOR=regex`; otherwise regex.

    `DEAL_EXTRACTOR=claude` without a key raises `ExtractorUnavailable` (a fixed 503, see the
    handler below) instead of serving regex: the operator asked for Claude, and the regex
    baseline routes some posts to the wrong state. An invalid DEAL_EXTRACTOR (config raises
    ValueError) gets the same 503 rather than a bare 500. Auto mode falling back to regex logs
    one WARNING per process, so the downgrade is visible without flooding the logs.

    Settings are read on every call so the choice follows the current config, but the
    Claude extractor itself is cached: it owns an HTTP client with a connection pool, and
    building one per request would throw that pool away each time. The cache is keyed on the
    current key's fingerprint and model, so a rotated key or a new DEAL_MODEL builds a fresh
    client instead of the process keeping the first one it saw.
    """
    try:
        settings = load_settings()
    except ValueError as e:
        logger.error("invalid extractor configuration: %s", type(e).__name__)
        raise ExtractorUnavailable from e
    if settings.use_claude:
        return _claude_extractor(_ClaudeKey.of(settings), settings.deal_model)
    if settings.deal_extractor == FORCE_CLAUDE:
        logger.error("DEAL_EXTRACTOR=claude but no ANTHROPIC_API_KEY is set")
        raise ExtractorUnavailable
    if settings.deal_extractor != FORCE_REGEX:
        _warn_auto_fell_back_to_regex()
    return _regex_extractor()


@lru_cache(maxsize=1)
def _warn_auto_fell_back_to_regex() -> None:
    # lru_cache makes this run once per process (cache_clear() re-arms it in tests).
    logger.warning(
        "no ANTHROPIC_API_KEY set: serving the regex baseline extractor (set DEAL_EXTRACTOR=regex to silence)"
    )


@dataclass(frozen=True)
class _ClaudeKey:
    """An API key as a cache key: equality and hash use only `fingerprint`.

    `secret` rides along (the client needs it) but takes no part in comparisons, so the cache
    looks keys up by an HMAC fingerprint under the per-process key, never by the raw key, and
    `repr` shows neither.
    """

    fingerprint: str = field(repr=False)
    secret: SecretStr = field(compare=False, repr=False)

    @classmethod
    def of(cls, settings: Settings) -> _ClaudeKey:
        """The current settings' key. Call only when `settings.use_claude` (so a key is set)."""
        secret = SecretStr(settings.api_key_value() or "")
        digest = hmac.new(_POST_ID_KEY, b"api-key\0" + secret.get_secret_value().encode(), hashlib.sha256)
        return cls(fingerprint=digest.hexdigest(), secret=secret)


@lru_cache(maxsize=1)
def _claude_extractor(key: _ClaudeKey, model: str) -> Extractor:
    """The Claude extractor for `key` and `model`; maxsize 1, so a change replaces the old one."""
    # Imported here so the regex-only path never constructs an Anthropic client.
    from deal_board.extractors.claude import ClaudeExtractor

    # open_telemetry=False: with the default (None) the SDK reads ANTHROPIC_OPEN_TELEMETRY_* env
    # vars, and ..._TRACES_CONTENT_MODE=content would export the full post text in span
    # attributes. False disables the client's spans outright, whatever the environment says.
    client = anthropic.Anthropic(
        api_key=key.secret.get_secret_value(),
        timeout=CLAUDE_TIMEOUT_SECONDS,
        max_retries=CLAUDE_MAX_RETRIES,
        open_telemetry=False,
    )
    return ClaudeExtractor(client=client, model=model)


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


# The easternmost time zone of any US state's licensing authority (Maine to Florida, plus DC).
# Every state's local date is on or before the date here, so checking expirations against it
# can only be stricter than the deal state's own date, never more lenient (fail closed).
LICENSING_TZ: Final = ZoneInfo("America/New_York")


def licensing_date(now: datetime) -> date:
    """The date license expirations are checked against at the instant `now`: today in `LICENSING_TZ`.

    Not the server's local date: on a host west of a deal's state, that date would still be
    "yesterday" for the first hours of the state's day, so a license that expired yesterday
    there would pass. Pure, so tests pass an instant instead of patching the clock. A naive
    `now` is ambiguous about which instant it means, so it is rejected with ValueError.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("licensing_date needs a timezone-aware datetime")
    return now.astimezone(LICENSING_TZ).date()


def get_as_of() -> date:
    """Today's licensing date (`licensing_date` of the current instant). A dependency so tests can pin it."""
    return licensing_date(datetime.now(UTC))


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

_REF = "#/components/schemas/"


def _document_both_422_bodies(schema: dict[str, Any]) -> None:
    """Make `POST /match`'s documented 422 accept both bodies the route can return.

    FastAPI documents 422 as `HTTPValidationError` (detail: a list of errors), but an extraction
    failure returns `ErrorDetail` (detail: a fixed string), which a generated client would then
    fail to parse. The 422 is patched after generation rather than declared in `responses=`:
    declaring 422 there makes FastAPI drop its own entry and with it the HTTPValidationError
    component the first body needs. Mutates `schema` in place.
    """
    response = schema["paths"]["/match"]["post"]["responses"]["422"]
    response["description"] = "Invalid request body, or no deal requirement could be extracted from the post"
    response["content"]["application/json"]["schema"] = {
        "anyOf": [{"$ref": _REF + "HTTPValidationError"}, {"$ref": _REF + "ErrorDetail"}]
    }


class _DealBoardApp(FastAPI):
    """FastAPI whose generated OpenAPI document gets `_document_both_422_bodies` applied once."""

    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is None:
            _document_both_422_bodies(super().openapi())  # super() caches into openapi_schema
        return super().openapi()


app = _DealBoardApp(
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

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and not _declared_length_ok(declared, self.max_bytes):
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

        replay: list[Message] = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]
        await self.app(scope, _replay(replay, then=receive), send)

    @staticmethod
    async def _reject(send: Send) -> None:
        response = JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={"detail": "request body too large"},
        )
        await send({"type": "http.response.start", "status": response.status_code, "headers": response.raw_headers})
        await send({"type": "http.response.body", "body": response.body})


def _declared_length_ok(declared: bytes, max_bytes: int) -> bool:
    """True if a `Content-Length` value is ASCII digits for at most `max_bytes`.

    The digit count is checked before `int()`: past Python's int max-str-digits limit (4300),
    `int()` raises ValueError, which would escape the middleware as a 500 instead of the 413.
    Any value with more digits than `max_bytes` is over the cap (or zero-padded, which no
    client needs), so it can be rejected without parsing.
    """
    return declared.isdigit() and len(declared) <= len(str(max_bytes)) and int(declared) <= max_bytes


def _replay(messages: list[Message], then: Receive | None = None) -> Receive:
    """An ASGI `receive` that returns `messages` in order, then defers to `then` (disconnects)."""
    pending = list(messages)

    async def receive() -> Message:
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
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": "matcher unavailable"})


@app.exception_handler(ExtractorUnavailable)
def _extractor_unavailable(_: Request, __: ExtractorUnavailable) -> JSONResponse:
    # Raised while resolving a dependency, like MatcherUnavailable, so it needs a handler too.
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": "extractor not configured"})


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


# Key for `_post_id` (and `_ClaudeKey` fingerprints). Random per process and never logged or read from the environment, so an id
# can't be recomputed from a candidate post outside this process (an unkeyed hash could be).
_POST_ID_KEY: bytes = secrets.token_bytes(32)


def _post_id(text: str) -> str:
    """A short fingerprint for correlating log lines with a post without storing the post.

    HMAC-SHA256 under the per-process `_POST_ID_KEY`: stable within a process, not reproducible
    from the text alone.
    """
    return hmac.new(_POST_ID_KEY, text.encode(), hashlib.sha256).hexdigest()[:12]


@app.post(
    "/match",
    response_model=MatchResponse,
    responses={
        413: {"model": ErrorDetail, "description": "Request body over the size limit"},
        500: {"model": ErrorDetail, "description": "The extractor or the matching step failed unexpectedly"},
        503: {"model": ErrorDetail, "description": "Extraction service or matcher unavailable or not configured"},
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

    Extraction failures, and any error after extraction, are mapped here with `HTTPException`
    rather than global handlers, because only the route knows the post id to log alongside the
    error class. No branch logs or returns exception text: pydantic errors embed input values
    (i.e. parts of the post), and upstream API errors can carry request details.
    """
    text = body.text  # already stripped by MatchRequest
    post_id = _post_id(text)

    try:
        extracted = extractor.extract(text)
    except ExtractionError as e:
        logger.warning("extraction failed post=%s extractor=%s error=%s", post_id, extractor.name, type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="could not extract a deal requirement from this post",
        ) from e
    except anthropic.APIError as e:
        logger.error(
            "extraction service error post=%s extractor=%s error=%s status=%s",
            post_id,
            extractor.name,
            type(e).__name__,
            getattr(e, "status_code", None),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="extraction service unavailable",
        ) from e
    except Exception as e:
        # An extractor bug (e.g. the frozen regex baseline overflows on a 400-digit number).
        # Mapped here, not left to the server, so the fixed detail and post-id log line hold and
        # no exception text, which might quote the post, reaches the response or a traceback.
        logger.error("extractor crashed post=%s extractor=%s error=%s", post_id, extractor.name, type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="extraction failed unexpectedly",
        ) from None

    # The matcher, the follow-up and the response all get the re-validated requirement, never
    # the extractor's own object. An invalid field is the same failure as an ExtractionError.
    try:
        requirement = revalidate_requirement(extracted)
    except ValidationError as e:
        logger.warning("extraction failed post=%s extractor=%s error=%s", post_id, extractor.name, type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="could not extract a deal requirement from this post",
        ) from None
    except Exception as e:
        raise _matching_failed(post_id, extractor.name, e) from None

    try:
        return _respond(requirement, extractor.name, brokers, as_of, match, post_id=post_id, chars=len(text))
    except Exception as e:
        raise _matching_failed(post_id, extractor.name, e) from None


def _matching_failed(post_id: str, extractor_name: str, error: Exception) -> HTTPException:
    """Log a bug after extraction and return the fixed 500 for it.

    Covers re-validation, the matcher, the reshaping, the follow-up and the response model (e.g.
    an extractor name outside the Literal). Mapped like the extractor crash, so the documented
    JSON 500 and the post-id log line hold instead of Starlette's text/plain 500, and no
    exception text (which might quote the requirement) is logged or returned.
    """
    logger.error("matching failed post=%s extractor=%s error=%s", post_id, extractor_name, type(error).__name__)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="matching failed unexpectedly")


def _respond(
    requirement: DealRequirement,
    extractor_name: str,
    brokers: list[Broker],
    as_of: date,
    match: MatchFn,
    *,
    post_id: str,
    chars: int,
) -> MatchResponse:
    """Everything `match_post` does after extraction: match, reshape, build the response, log.

    Licensing gating, non-deal posts and missing states are the matcher's job; this only
    reshapes what it returns. Split out so the route can map any error in it to one fixed 500.
    """
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

    response = MatchResponse(
        # Extractor.name is a plain str; MatchResponse validates it against the Literal at runtime
        # (an unknown name fails validation, which the route maps to its 500), so the cast only
        # informs the type checker.
        extractor=cast(Literal["claude", "regex"], extractor_name),
        requirement=requirement,
        matches=matches,
        excluded_for_licensing=result.excluded_for_licensing,
        follow_up_question=follow_up_question(requirement),
    )
    # Logged once the response exists, so a failed request logs only the route's error line.
    logger.info(
        "matched post=%s chars=%d extractor=%s matches=%d excluded=%d",
        post_id,
        chars,
        extractor_name,
        len(matches),
        result.excluded_for_licensing,
    )
    return response
