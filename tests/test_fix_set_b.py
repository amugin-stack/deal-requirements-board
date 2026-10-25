"""Blind regression tests for fix set B (API contract, compliance hardening, red-team smuggling).

Written from the findings' claims and the agreed contract notes in review/fixes/dispositions.json,
without seeing the fix. Everything is offline: fake extractors, fake keys that never leave the
process, and no request is ever sent to Anthropic.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

import anthropic
import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from deal_board import api, config, matcher
from deal_board.brokers import Broker, load_brokers
from deal_board.extractors import ExtractionError
from deal_board.schema import DealRequirement, PropertyType, TransactionType

AS_OF = date(2026, 10, 20)
# Distinctive enough that a substring hit in logs or a response can only be a leak.
POST = "Need a TX broker for a 12-unit multifamily in Austin, ~$3.2M, closing in 60 days. ref OCELOT-4417"
CRASH_SECRET = "crash-secret-QUOKKA-9921"
TX_DEAL = DealRequirement(
    is_deal_request=True,
    state="TX",
    city="Austin",
    property_type=PropertyType.multifamily,
    transaction_type=TransactionType.buy,
    deal_value_usd=3_200_000,
)
EXTRACTION_422 = {"detail": "could not extract a deal requirement from this post"}

JSON = dict[str, Any]


# --- fakes and helpers ----------------------------------------------------------------------


class FakeExtractor:
    """Returns `build()` (or raises it); `name` is a valid MatchResponse.extractor value."""

    def __init__(self, build: Callable[[], DealRequirement], name: str = "regex") -> None:
        self.name = name
        self._build = build

    def extract(self, text: str) -> DealRequirement:
        return self._build()


def smuggled(**fields: Any) -> DealRequirement:
    """A requirement only a buggy extractor could produce: `model_construct` skips validation."""
    base: dict[str, Any] = dict.fromkeys(DealRequirement.model_fields)
    base.update(is_deal_request=True, missing_fields=[])
    base.update(fields)
    return DealRequirement.model_construct(**base)


def raise_extraction_error() -> DealRequirement:
    raise ExtractionError("no tool call")


@pytest.fixture
def client() -> Iterator[TestClient]:
    try:
        yield TestClient(api.app, raise_server_exceptions=False)
    finally:
        api.app.dependency_overrides.clear()


def wire(
    extractor: FakeExtractor, match: Callable[..., Any] | None = None, brokers: list[Broker] | None = None
) -> None:
    """Override the dependencies; the real matcher and real broker data unless told otherwise."""
    overrides = api.app.dependency_overrides
    overrides[api.get_extractor] = lambda: extractor
    overrides[api.get_brokers] = lambda: load_brokers() if brokers is None else brokers
    overrides[api.get_as_of] = lambda: AS_OF
    overrides[api.get_matcher] = lambda: matcher.match if match is None else match


def clear_extractor_caches() -> None:
    """Clear whatever lru_caches api.py keeps for extractors (names may change with the fix)."""
    for name in dir(api):
        cache_clear = getattr(getattr(api, name), "cache_clear", None)
        if callable(cache_clear) and name != "get_brokers":
            cache_clear()


@pytest.fixture
def fresh_caches() -> Iterator[None]:
    clear_extractor_caches()
    try:
        yield
    finally:
        clear_extractor_caches()


# --- a tiny JSON-schema checker (no jsonschema dependency is installed) ---------------------


def resolve(schema: JSON, root: JSON) -> JSON:
    """Follow local `#/...` $refs until a concrete schema."""
    while "$ref" in schema:
        node: Any = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        schema = node
    return schema


def fits(value: object, schema: JSON, root: JSON) -> bool:
    """Does `value` fit `schema`? Covers the keywords FastAPI's OpenAPI output uses."""
    schema = resolve(schema, root)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            hits = sum(fits(value, alt, root) for alt in schema[key])
            if hits == 0 or (key == "oneOf" and hits != 1):
                return False
    if "allOf" in schema and not all(fits(value, alt, root) for alt in schema["allOf"]):
        return False
    kind = schema.get("type")
    checks: dict[str, Callable[[object], bool]] = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(checks[k](value) for k in kinds):
            return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if "const" in schema and value != schema["const"]:
        return False
    if isinstance(value, dict):
        if any(req not in value for req in schema.get("required", [])):
            return False
        props: JSON = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                if not fits(v, props[k], root):
                    return False
            elif schema.get("additionalProperties") is False:
                return False
    items = schema.get("items")
    if isinstance(value, list) and items is not None and not all(fits(v, items, root) for v in value):
        return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            return False
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return False
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False
    return True


def match_operation() -> tuple[JSON, JSON]:
    root = api.app.openapi()
    return root["paths"]["/match"]["post"], root


# --- correctness-api/A-1 --------------------------------------------------------------------


def test_extraction_422_body_matches_openapi_schema(client: TestClient) -> None:
    """correctness-api/A-1: both 422 bodies (validation, extraction failure) fit the published 422 schema."""
    op, root = match_operation()
    schema = op["responses"]["422"]["content"]["application/json"]["schema"]

    wire(FakeExtractor(raise_extraction_error))
    extraction = client.post("/match", json={"text": POST})
    validation = client.post("/match", json={"text": "   "})

    assert extraction.status_code == 422 and extraction.json() == EXTRACTION_422
    assert validation.status_code == 422 and isinstance(validation.json()["detail"], list)
    assert fits(extraction.json(), schema, root), schema
    assert fits(validation.json(), schema, root), schema


def test_schema_checker_rejects_wrong_shapes() -> None:
    """correctness-api/A-1: control, the hand-rolled checker is not vacuous."""
    _, root = match_operation()
    validation_error = {"$ref": "#/components/schemas/HTTPValidationError"}
    assert not fits(EXTRACTION_422, validation_error, root)
    assert not fits({"detail": 1}, {"$ref": "#/components/schemas/ErrorDetail"}, root)
    assert fits(EXTRACTION_422, {"$ref": "#/components/schemas/ErrorDetail"}, root)


# --- correctness-api/A-2 --------------------------------------------------------------------


@dataclass
class BadMatch:
    """A match whose `score` is not an int, so building BrokerMatch raises a ValidationError
    whose message would quote the bad value."""

    broker: Broker
    score: object
    reasons: list[str]


@dataclass
class BadResult:
    matches: list[BadMatch]
    excluded_for_licensing: int


def crashing_matcher(req: DealRequirement, brokers: list[Broker], as_of: date) -> Any:
    raise ValueError(f"{CRASH_SECRET} {POST}")


def reshaping_crash_matcher(req: DealRequirement, brokers: list[Broker], as_of: date) -> Any:
    return BadResult(matches=[BadMatch(brokers[0], f"{CRASH_SECRET} {POST}", [])], excluded_for_licensing=0)


@pytest.mark.parametrize(
    "fake_match, error_type",
    [
        pytest.param(crashing_matcher, "ValueError", id="matcher-raises"),
        pytest.param(reshaping_crash_matcher, "ValidationError", id="reshaping-raises"),
    ],
)
def test_matcher_crash_returns_fixed_json_500(
    client: TestClient, caplog: pytest.LogCaptureFixture, fake_match: Callable[..., Any], error_type: str
) -> None:
    """correctness-api/A-2: a crash after extraction is a fixed JSON 500 plus a post-id log line."""
    wire(FakeExtractor(lambda: TX_DEAL), match=fake_match)

    with caplog.at_level(logging.DEBUG):
        resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"detail": "matching failed unexpectedly"}
    assert CRASH_SECRET not in resp.text and "OCELOT" not in resp.text
    lines = [r.getMessage() for r in caplog.records]
    assert any("post=" in line and error_type in line for line in lines), lines
    for record in caplog.records:
        text = record.getMessage() + (record.exc_text or "") + str(record.exc_info or "")
        assert CRASH_SECRET not in text and "OCELOT" not in text, text


def test_followup_crash_returns_fixed_json_500(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """correctness-api/A-2: a follow_up_question crash is the same fixed JSON 500."""

    def broken_follow_up(req: DealRequirement) -> str | None:
        raise KeyError(CRASH_SECRET)

    monkeypatch.setattr(api, "follow_up_question", broken_follow_up)
    wire(FakeExtractor(lambda: TX_DEAL))

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 500
    assert resp.json() == {"detail": "matching failed unexpectedly"}


def test_extractor_crash_500_body_is_unchanged(client: TestClient) -> None:
    """correctness-api/A-2: control, an extractor crash keeps its own fixed message."""

    def boom() -> DealRequirement:
        raise RuntimeError(CRASH_SECRET)

    wire(FakeExtractor(boom))

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 500
    assert resp.json() == {"detail": "extraction failed unexpectedly"}


# --- correctness-api/A-3 --------------------------------------------------------------------


def published_text_schema() -> tuple[JSON, JSON]:
    op, root = match_operation()
    body = resolve(op["requestBody"]["content"]["application/json"]["schema"], root)
    return body["properties"]["text"], root


def test_openapi_text_constraints_match_stripping_semantics(client: TestClient) -> None:
    """correctness-api/A-3: a padded post the server accepts fits the published MatchRequest.text."""
    padded = " " * 10 + "a" * api.MAX_POST_CHARS
    wire(FakeExtractor(lambda: TX_DEAL))

    resp = client.post("/match", json={"text": padded})

    assert resp.status_code == 200
    schema, root = published_text_schema()
    assert fits(padded, schema, root), schema


def test_openapi_text_rejects_whitespace_only_like_the_server(client: TestClient) -> None:
    """correctness-api/A-3: '   \\n' is rejected by both the server and the published schema."""
    wire(FakeExtractor(lambda: TX_DEAL))

    resp = client.post("/match", json={"text": "   \n"})

    assert resp.status_code == 422
    schema, root = published_text_schema()
    assert not fits("   \n", schema, root), schema
    assert fits(" x ", schema, root), schema


# --- compliance-security/C-3 ----------------------------------------------------------------

# Runs in a fresh interpreter with TZ set by the caller. The clock is frozen at INSTANT for
# every source a fix could read: time.time (date.today, fromtimestamp) and datetime.datetime's
# now/utcnow/today. Third-party libraries are imported first so only deal_board sees the fake.
CLOCK_SCRIPT = r"""
import datetime as _dt, json, sys, time
import anthropic, fastapi, pydantic, starlette
from fastapi.testclient import TestClient

INSTANT = _dt.datetime.fromisoformat(sys.argv[1])
T = INSTANT.timestamp()
time.time = lambda: T
time.time_ns = lambda: int(T * 1_000_000_000)
_Real = _dt.datetime


class FrozenDatetime(_Real):
    @classmethod
    def now(cls, tz=None):
        return _Real.fromtimestamp(T, tz)

    @classmethod
    def utcnow(cls):
        return _Real.fromtimestamp(T, _dt.UTC).replace(tzinfo=None)

    @classmethod
    def today(cls):
        return _Real.fromtimestamp(T)


_dt.datetime = FrozenDatetime

from deal_board import api, matcher
from deal_board.brokers import Broker
from deal_board.schema import DealRequirement

as_of = api.get_as_of()
broker = Broker(
    broker_id="tx-900", name="TX Licensee 900", source="test", license_state="TX",
    license_number="900900-B", license_type="Broker Individual", license_status="Active",
    license_expiration=_dt.date.fromisoformat(sys.argv[2]), county="Travis", markets=[],
    property_types=[], transaction_types=[], min_deal_usd=None, max_deal_usd=None,
)


class Extractor:
    name = "regex"

    def extract(self, text):
        return DealRequirement(is_deal_request=True, state="TX", city="Austin")


api.app.dependency_overrides[api.get_extractor] = lambda: Extractor()
api.app.dependency_overrides[api.get_brokers] = lambda: [broker]
api.app.dependency_overrides[api.get_matcher] = lambda: matcher.match
body = TestClient(api.app).post("/match", json={"text": "TX deal in Austin"}).json()
print(json.dumps({
    "as_of": as_of.isoformat(), "type": type(as_of).__name__,
    "matches": len(body["matches"]), "excluded": body["excluded_for_licensing"],
}))
"""


def run_frozen_clock(tz: str, instant: str, expiration: str) -> JSON:
    env = {**os.environ, "TZ": tz}
    proc = subprocess.run(
        [sys.executable, "-c", CLOCK_SCRIPT, instant, expiration],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    result: JSON = json.loads(proc.stdout.strip().splitlines()[-1])
    return result


@pytest.mark.parametrize(
    "tz, instant, new_york_date",
    [
        # 00:30 CST / 01:30 EST on Jan 2; the Honolulu host still says Jan 1.
        pytest.param("Pacific/Honolulu", "2027-01-02T06:30:00+00:00", "2027-01-02", id="honolulu-behind"),
        # 22:00 EST on Jan 1; UTC and Tokyo hosts already say Jan 2.
        pytest.param("UTC", "2027-01-02T03:00:00+00:00", "2027-01-01", id="utc-ahead"),
        pytest.param("Asia/Tokyo", "2027-01-02T03:00:00+00:00", "2027-01-01", id="tokyo-ahead"),
    ],
)
def test_get_as_of_uses_new_york_date(tz: str, instant: str, new_york_date: str) -> None:
    """compliance-security/C-3: get_as_of is today's date in America/New_York, whatever the host TZ."""
    result = run_frozen_clock(tz, instant, expiration="2030-01-01")

    assert result["type"] == "date"
    assert result["as_of"] == new_york_date


def test_license_expired_yesterday_in_texas_is_excluded_on_a_honolulu_host() -> None:
    """compliance-security/C-3: with the real get_as_of, a TX license that expired Jan 1 can't match on Jan 2."""
    result = run_frozen_clock("Pacific/Honolulu", "2027-01-02T06:30:00+00:00", expiration="2027-01-01")

    assert result["matches"] == 0
    assert result["excluded"] == 1


# --- compliance-security/C-7 ----------------------------------------------------------------


@pytest.mark.parametrize("value", ["rgex", "anthropic", "Claud", "regexx"])
def test_unknown_deal_extractor_is_rejected(value: str) -> None:
    """compliance-security/C-7: an unknown DEAL_EXTRACTOR is a ValueError, not a silent 'auto'."""
    with pytest.raises(ValueError):
        config.load_settings({"DEAL_EXTRACTOR": value})


@pytest.mark.parametrize("value", ["", "auto", "claude", "regex", " Regex ", "CLAUDE"])
def test_known_deal_extractor_values_are_accepted(value: str) -> None:
    """compliance-security/C-7: '', auto, claude and regex (any case, padded) still load."""
    config.load_settings({"DEAL_EXTRACTOR": value})


def test_deal_extractor_claude_without_key_does_not_fall_back_to_regex(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fresh_caches: None
) -> None:
    """compliance-security/C-7: DEAL_EXTRACTOR=claude with no key is a JSON 503, never regex."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DEAL_EXTRACTOR", "claude")
    overrides = api.app.dependency_overrides
    overrides[api.get_brokers] = load_brokers
    overrides[api.get_as_of] = lambda: AS_OF
    overrides[api.get_matcher] = lambda: matcher.match

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert isinstance(body.get("detail"), str) and body["detail"]
    assert body.get("extractor") != "regex"
    assert "matches" not in body


# --- compliance-security/S-1 ----------------------------------------------------------------

HUGE_LENGTH = "9" * 5000


def test_huge_digit_content_length_is_413(client: TestClient) -> None:
    """compliance-security/S-1: a 5000-digit Content-Length is the 413 JSON, not a 500."""
    wire(FakeExtractor(lambda: TX_DEAL))

    resp = client.post(
        "/match",
        content=b'{"text":"x"}',
        headers={"content-type": "application/json", "content-length": HUGE_LENGTH},
    )

    assert resp.status_code == 413
    assert resp.json() == {"detail": "request body too large"}


def test_body_size_limit_handles_huge_digit_header_directly() -> None:
    """compliance-security/S-1: BodySizeLimit itself raises nothing and sends a 413."""
    sent: list[Message] = []
    app_called: list[bool] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        app_called.append(True)

    async def receive() -> Message:
        return {"type": "http.request", "body": b'{"text":"x"}', "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/match",
        "headers": [(b"content-type", b"application/json"), (b"content-length", HUGE_LENGTH.encode())],
    }
    middleware = api.BodySizeLimit(inner, max_bytes=api.MAX_BODY_BYTES)

    asyncio.run(middleware(scope, receive, send))

    assert app_called == []
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 413
    assert json.loads(sent[1]["body"]) == {"detail": "request body too large"}


# --- compliance-security/S-2 ----------------------------------------------------------------


def test_claude_client_tracing_never_records_content(monkeypatch: pytest.MonkeyPatch, fresh_caches: None) -> None:
    """compliance-security/S-2: ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE=content can't make api's client record post text."""
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-s2-never-sent")
    monkeypatch.delenv("DEAL_EXTRACTOR", raising=False)
    monkeypatch.delenv("ANTHROPIC_OPEN_TELEMETRY", raising=False)

    extractor = api.get_extractor()
    sdk_client = getattr(extractor, "client", None)

    assert isinstance(sdk_client, anthropic.Anthropic)
    assert sdk_client._otel.records_content is False


def test_default_claude_extractor_client_never_records_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """compliance-security/S-2: the ClaudeExtractor's own default client ignores the content-mode env var too."""
    from deal_board.extractors.claude import ClaudeExtractor

    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-s2b-never-sent")

    sdk_client = ClaudeExtractor().client

    assert isinstance(sdk_client, anthropic.Anthropic)
    assert sdk_client._otel.records_content is False


def test_tracing_env_var_would_otherwise_record_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """compliance-security/S-2: control, an SDK-default client does record content under that env var."""
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")

    assert anthropic.Anthropic(api_key="fake-key-control")._otel.records_content is True


# --- compliance-security/S-3 ----------------------------------------------------------------


def test_post_id_is_keyed() -> None:
    """compliance-security/S-3: _post_id is 12 hex chars, stable in-process, not an unkeyed SHA-256 prefix."""
    post_id = api._post_id(POST)

    assert re.fullmatch(r"[0-9a-f]{12}", post_id)
    assert api._post_id(POST) == post_id
    assert api._post_id(POST + " ") != post_id
    assert post_id != hashlib.sha256(POST.encode()).hexdigest()[:12]


def test_post_id_differs_across_processes() -> None:
    """compliance-security/S-3: another process can't reproduce the id from the text alone."""
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; from deal_board import api; print(api._post_id(sys.argv[1]))", POST],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() != api._post_id(POST)


# --- red-team/RT-2, red-team/RT-5 -----------------------------------------------------------


def test_smuggled_state_not_a_state_is_revalidated(client: TestClient) -> None:
    """red-team/RT-2: a model_construct'd state 'ZZ' gives a schema-valid 200 with state None."""
    wire(FakeExtractor(lambda: smuggled(state="ZZ", city="Austin")))

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 200
    body = resp.json()
    api.MatchResponse.model_validate(body)
    assert body["requirement"]["state"] is None
    assert body["matches"] == []
    assert isinstance(body["follow_up_question"], str) and body["follow_up_question"].startswith("Which state")


def test_smuggled_lowercase_state_still_matches_nobody(client: TestClient) -> None:
    """red-team/RT-2: control, a smuggled lowercase 'fl' still yields no matches."""
    wire(FakeExtractor(lambda: smuggled(state="fl")))

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 200
    assert resp.json()["matches"] == []


def test_smuggled_truthy_non_deal_gets_no_matches(client: TestClient) -> None:
    """red-team/RT-5: is_deal_request='no' with state TX is a non-deal: no matches, nothing excluded."""
    wire(FakeExtractor(lambda: smuggled(is_deal_request="no", state="TX", city="Austin")))

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 200
    body = resp.json()
    api.MatchResponse.model_validate(body)
    assert body["requirement"]["is_deal_request"] is False
    assert body["matches"] == []
    assert body["excluded_for_licensing"] == 0


# --- p10/P10-3 ------------------------------------------------------------------------------


@pytest.fixture
def fake_anthropic(monkeypatch: pytest.MonkeyPatch, fresh_caches: None) -> list[dict[str, object]]:
    """Replace the Anthropic client class so building an extractor records its kwargs, nothing more."""
    built: list[dict[str, object]] = []

    class FakeAnthropic:
        def __init__(self, **kwargs: object) -> None:
            built.append(kwargs)

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.delenv("DEAL_EXTRACTOR", raising=False)
    monkeypatch.delenv("DEAL_MODEL", raising=False)
    return built


def test_claude_extractor_rebuilt_when_key_or_model_changes(
    monkeypatch: pytest.MonkeyPatch, fake_anthropic: list[dict[str, object]]
) -> None:
    """p10/P10-3: the cached Claude extractor follows a changed key or DEAL_MODEL, and is reused otherwise."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-one")
    first = api.get_extractor()
    assert api.get_extractor() is first

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-two")
    second = api.get_extractor()
    assert second is not first
    assert api.get_extractor() is second
    assert [b.get("api_key") for b in fake_anthropic] == ["fake-key-one", "fake-key-two"]

    monkeypatch.setenv("DEAL_MODEL", "claude-fake-model-p10")
    third = api.get_extractor()
    assert third is not second
    assert getattr(third, "model", None) == "claude-fake-model-p10"
    assert api.get_extractor() is third
    assert len(fake_anthropic) == 3
