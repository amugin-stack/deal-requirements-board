"""HTTP contract for POST /match.

Every collaborator (extractor, broker list, clock, matcher) is swapped through
FastAPI dependency overrides, so these tests pin the API's own behavior: request
validation, response shape, follow-up wording, error mapping, and log hygiene.
The last test runs the real matcher against the real broker data when it exists.
"""

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from deal_board import api
from deal_board import brokers as broker_store
from deal_board.brokers import Broker, LicenseType, load_brokers
from deal_board.extractors import ExtractionError
from deal_board.schema import DealRequirement, PropertyType, Timeline, TransactionType

AS_OF = date(2026, 10, 20)
# Distinctive enough that a substring hit in logs or a response can only be a leak.
POST = "Need a TX broker for a 12-unit multifamily in Austin, ~$3.2M, closing in 60 days. ref ZEBRA-7731"
TOP_LEVEL_KEYS = {"extractor", "requirement", "matches", "excluded_for_licensing", "follow_up_question"}
MATCH_KEYS = {"broker_id", "name", "source", "license_number", "license_type", "score", "reasons"}
UPSTREAM_SECRET = "upstream secret sk-ant-XXXX"


# --- fakes -------------------------------------------------------------------


class Match(BaseModel):
    broker: Broker
    score: int
    reasons: list[str]


class MatchResult(BaseModel):
    matches: list[Match]
    excluded_for_licensing: int


class FakeExtractor:
    def __init__(self, result: DealRequirement | Exception, name: str = "claude") -> None:
        self.name = name
        self._result = result
        self.calls: list[str] = []

    def extract(self, text: str) -> DealRequirement:
        self.calls.append(text)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@dataclass
class FakeMatcher:
    """Mirrors matcher.match's gates; returns `matches` for any deal with a state."""

    matches: list[Match] = field(default_factory=list)
    excluded: int = 0
    calls: list[tuple[DealRequirement, list[Broker], date]] = field(default_factory=list)

    def __call__(self, req: DealRequirement, brokers: list[Broker], as_of: date) -> MatchResult:
        self.calls.append((req, brokers, as_of))
        if not req.is_deal_request:
            return MatchResult(matches=[], excluded_for_licensing=0)
        if req.state is None:
            return MatchResult(matches=[], excluded_for_licensing=len(brokers))
        return MatchResult(matches=self.matches, excluded_for_licensing=self.excluded)


def make_broker(
    broker_id: str, name: str, license_number: str, license_type: LicenseType = "Broker Individual"
) -> Broker:
    return Broker(
        broker_id=broker_id,
        name=name,
        source="trec_2026-10-20",
        license_state="TX",
        license_number=license_number,
        license_type=license_type,
        license_status="Active",
        license_expiration=date(2027, 5, 31),
        county="Travis",
        markets=[],
        property_types=[],
        transaction_types=[],
        min_deal_usd=None,
        max_deal_usd=None,
    )


BROKER_A = make_broker("tx-004", "TX Licensee 004", "359131-B")
BROKER_B = make_broker("tx-017", "TX Licensee 017", "612004-SA", license_type="Sales Agent")
BROKER_C = make_broker("tx-031", "TX Licensee 031", "700112-B")
BROKERS = [BROKER_A, BROKER_B, BROKER_C]

FULL_TX_DEAL = DealRequirement(
    is_deal_request=True,
    state="TX",
    city="Austin",
    property_type=PropertyType.multifamily,
    transaction_type=TransactionType.buy,
    deal_value_usd=3_200_000,
    timeline=Timeline.days_31_to_90,
    notes="12 units",
)
# Missing state AND transaction_type: the state question must win on priority.
PORTLAND_NO_STATE = DealRequirement(is_deal_request=True, city="Portland", property_type=PropertyType.retail)
NOT_A_DEAL = DealRequirement(is_deal_request=False)


def api_status_error() -> anthropic.APIStatusError:
    # httpx2, not httpx: it is the HTTP library anthropic (and starlette's TestClient) are built on.
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(UPSTREAM_SECRET, response=httpx2.Response(500, request=request), body=None)


def api_connection_error() -> anthropic.APIConnectionError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(message=UPSTREAM_SECRET, request=request)


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


@pytest.fixture
def matcher() -> FakeMatcher:
    return FakeMatcher()


def wire(extractor: FakeExtractor, matcher: FakeMatcher | None = None, brokers: list[Broker] = BROKERS) -> None:
    overrides = api.app.dependency_overrides
    overrides[api.get_extractor] = lambda: extractor
    overrides[api.get_brokers] = lambda: brokers
    overrides[api.get_as_of] = lambda: AS_OF
    if matcher is not None:
        overrides[api.get_matcher] = lambda: matcher


# --- success paths -----------------------------------------------------------


def test_full_deal_returns_matches_in_matcher_order(client: TestClient, matcher: FakeMatcher) -> None:
    matcher.matches = [
        Match(broker=BROKER_A, score=87, reasons=["licensed in TX", "Travis County"]),
        Match(broker=BROKER_B, score=61, reasons=["licensed in TX"]),
    ]
    matcher.excluded = 1
    extractor = FakeExtractor(FULL_TX_DEAL, name="claude")
    wire(extractor, matcher)

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == TOP_LEVEL_KEYS
    assert body["extractor"] == "claude"
    assert DealRequirement.model_validate(body["requirement"]) == FULL_TX_DEAL
    assert body["requirement"]["missing_fields"] == []
    assert body["matches"] == [
        {
            "broker_id": "tx-004",
            "name": "TX Licensee 004",
            "source": "trec_2026-10-20",
            "license_number": "359131-B",
            "license_type": "Broker Individual",
            "score": 87,
            "reasons": ["licensed in TX", "Travis County"],
        },
        {
            "broker_id": "tx-017",
            "name": "TX Licensee 017",
            "source": "trec_2026-10-20",
            "license_number": "612004-SA",
            "license_type": "Sales Agent",
            "score": 61,
            "reasons": ["licensed in TX"],
        },
    ]
    assert body["excluded_for_licensing"] == 1
    assert body["follow_up_question"] is None

    assert extractor.calls == [POST]
    [(req, brokers, as_of)] = matcher.calls
    assert req == FULL_TX_DEAL
    assert brokers == BROKERS
    assert as_of == AS_OF


def test_extractor_name_is_reported(client: TestClient, matcher: FakeMatcher) -> None:
    wire(FakeExtractor(FULL_TX_DEAL, name="regex"), matcher)

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 200
    assert resp.json()["extractor"] == "regex"


def test_missing_state_excludes_everyone_and_asks_for_state(client: TestClient, matcher: FakeMatcher) -> None:
    wire(FakeExtractor(PORTLAND_NO_STATE), matcher)

    resp = client.post("/match", json={"text": "Retail space wanted in Portland, who can help?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["matches"] == []
    assert body["excluded_for_licensing"] == len(BROKERS)
    assert body["requirement"]["state"] is None
    assert body["requirement"]["missing_fields"][0] == "state"
    assert body["follow_up_question"] == (
        "Which state is the Portland property in? We only match brokers licensed in that state."
    )


def test_follow_up_targets_remaining_gap_when_state_is_known(client: TestClient, matcher: FakeMatcher) -> None:
    # Re-validate so missing_fields is recomputed; model_copy skips validators.
    no_timeline = DealRequirement.model_validate({**FULL_TX_DEAL.model_dump(), "timeline": None})
    wire(FakeExtractor(no_timeline), matcher)

    body = client.post("/match", json={"text": POST}).json()

    assert body["requirement"]["missing_fields"] == ["timeline"]
    question = body["follow_up_question"]
    assert isinstance(question, str) and question.strip()
    assert "licensed in that state" not in question


def test_non_deal_post_has_no_matches_and_no_follow_up(client: TestClient, matcher: FakeMatcher) -> None:
    wire(FakeExtractor(NOT_A_DEAL), matcher)

    resp = client.post("/match", json={"text": "Congrats to our team on a record Q3!"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["requirement"]["is_deal_request"] is False
    assert body["requirement"]["missing_fields"] == []
    assert body["matches"] == []
    assert body["excluded_for_licensing"] == 0
    assert body["follow_up_question"] is None


# --- request validation ------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"text": ""}, id="empty"),
        pytest.param({"text": " \n\t  "}, id="whitespace-only"),
        pytest.param({"text": "x" * 4001}, id="4001-chars"),
        pytest.param({}, id="missing-text"),
        pytest.param({"body": POST}, id="wrong-field"),
    ],
)
def test_invalid_body_is_rejected_before_extraction(
    client: TestClient, matcher: FakeMatcher, payload: dict[str, str]
) -> None:
    extractor = FakeExtractor(FULL_TX_DEAL)
    wire(extractor, matcher)

    resp = client.post("/match", json=payload)

    assert resp.status_code == 422
    assert extractor.calls == []
    assert matcher.calls == []
    # Regression: FastAPI's default 422 echoes the rejected value; ours must not.
    assert all("input" not in err for err in resp.json()["detail"])
    if payload.get("text", "").strip():
        assert payload["text"] not in resp.text


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("x", id="1-char"),
        pytest.param("x" * 4000, id="4000-chars"),
        pytest.param("   " + "x" * 4000 + "\n\n", id="4000-chars-padded"),
    ],
)
def test_length_limit_applies_after_stripping(client: TestClient, matcher: FakeMatcher, text: str) -> None:
    extractor = FakeExtractor(FULL_TX_DEAL)
    wire(extractor, matcher)

    resp = client.post("/match", json={"text": text})

    assert resp.status_code == 200
    assert extractor.calls == [text.strip()]


# --- extractor failures ------------------------------------------------------


def test_extraction_error_is_422_with_readable_detail(client: TestClient, matcher: FakeMatcher) -> None:
    wire(FakeExtractor(ExtractionError("model returned no tool call")), matcher)

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str) and detail.strip()
    assert "no tool call" not in resp.text  # exception text is never echoed
    assert matcher.calls == []


@pytest.mark.parametrize("error", [api_status_error, api_connection_error], ids=["status", "connection"])
def test_upstream_api_error_is_503_without_leaking_upstream_text(
    client: TestClient, matcher: FakeMatcher, error: Callable[[], Exception]
) -> None:
    wire(FakeExtractor(error()), matcher)

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 503
    assert resp.json() == {"detail": "extraction service unavailable"}
    assert "sk-ant" not in resp.text
    assert UPSTREAM_SECRET not in resp.text
    assert matcher.calls == []


# --- log hygiene -------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome, status",
    [
        pytest.param(FULL_TX_DEAL, 200, id="success"),
        pytest.param(ExtractionError(f"could not parse post: {POST}"), 422, id="extraction-error"),
        pytest.param(api_status_error(), 503, id="api-error"),
    ],
)
def test_post_text_never_reaches_logs(
    client: TestClient,
    matcher: FakeMatcher,
    caplog: pytest.LogCaptureFixture,
    outcome: DealRequirement | Exception,
    status: int,
) -> None:
    caplog.set_level(logging.DEBUG)
    wire(FakeExtractor(outcome), matcher)

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == status
    assert "ZEBRA-7731" not in caplog.text
    assert POST not in caplog.text


# --- review regressions ------------------------------------------------------


def test_unexpected_extractor_crash_is_fixed_500_without_post_text(
    client: TestClient, matcher: FakeMatcher, caplog: pytest.LogCaptureFixture
) -> None:
    # The frozen regex baseline raises OverflowError on a 400-digit dollar amount.
    caplog.set_level(logging.DEBUG)
    wire(FakeExtractor(OverflowError(f"cannot convert {POST}")), matcher)

    resp = TestClient(api.app, raise_server_exceptions=False).post("/match", json={"text": POST})

    assert resp.status_code == 500
    assert resp.json() == {"detail": "extraction failed unexpectedly"}
    assert "ZEBRA-7731" not in caplog.text
    assert matcher.calls == []


def test_real_regex_extractor_overflow_is_500_not_crash(client: TestClient, matcher: FakeMatcher) -> None:
    api.app.dependency_overrides[api.get_matcher] = lambda: matcher
    api.app.dependency_overrides[api.get_extractor] = lambda: api._regex_extractor()

    resp = TestClient(api.app, raise_server_exceptions=False).post(
        "/match", json={"text": "need broker TX $" + "9" * 400}
    )

    assert resp.status_code == 500
    assert resp.json() == {"detail": "extraction failed unexpectedly"}


def test_sdk_loggers_stay_above_debug_even_with_debug_root(caplog: pytest.LogCaptureFixture) -> None:
    # The Anthropic SDK logs request bodies (the post) at DEBUG.
    caplog.set_level(logging.DEBUG)
    for name in ("anthropic", "anthropic._base_client", "httpx", "httpx2"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name


def test_oversize_body_is_413_before_parsing(client: TestClient, matcher: FakeMatcher) -> None:
    extractor = FakeExtractor(FULL_TX_DEAL)
    wire(extractor, matcher)

    resp = client.post("/match", json={"text": " " * (api.MAX_BODY_BYTES + 1) + "x"})

    assert resp.status_code == 413
    assert resp.json() == {"detail": "request body too large"}
    assert extractor.calls == []


def test_oversize_chunked_body_without_length_is_413(client: TestClient, matcher: FakeMatcher) -> None:
    extractor = FakeExtractor(FULL_TX_DEAL)
    wire(extractor, matcher)
    chunk = b" " * 16_384

    def stream() -> Iterator[bytes]:
        yield b'{"text": "'
        for _ in range(api.MAX_BODY_BYTES // len(chunk) + 1):
            yield chunk
        yield b'x"}'

    resp = client.post("/match", content=stream(), headers={"content-type": "application/json"})

    assert resp.status_code == 413
    assert extractor.calls == []


def test_openapi_documents_error_responses(client: TestClient) -> None:
    responses = client.get("/openapi.json").json()["paths"]["/match"]["post"]["responses"]
    assert {"200", "413", "422", "500", "503"} <= set(responses)


@pytest.fixture
def fresh_extractor_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, object]]]:
    """Clears the extractor caches and replaces the Anthropic client so nothing hits the network."""
    built: list[dict[str, object]] = []

    class FakeAnthropic:
        def __init__(self, **kwargs: object) -> None:
            built.append(kwargs)

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)  # the module object api.py calls through
    api._claude_extractor.cache_clear()
    api._regex_extractor.cache_clear()
    yield built
    api._claude_extractor.cache_clear()
    api._regex_extractor.cache_clear()


@pytest.mark.parametrize(
    "key, forced, expected",
    [
        pytest.param("test-key", None, "claude", id="key-only"),
        pytest.param("test-key", "regex", "regex", id="key-forced-regex"),
        pytest.param("test-key", " Regex ", "regex", id="key-forced-regex-loose"),
        pytest.param(None, None, "regex", id="no-key"),
        pytest.param("", None, "regex", id="empty-key"),
    ],
)
def test_extractor_selection_follows_env(
    monkeypatch: pytest.MonkeyPatch,
    fresh_extractor_cache: list[dict[str, object]],
    key: str | None,
    forced: str | None,
    expected: str,
) -> None:
    for name, value in (("ANTHROPIC_API_KEY", key), ("DEAL_EXTRACTOR", forced)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    assert api.get_extractor().name == expected


def test_claude_client_has_bounded_timeout_and_retries(
    monkeypatch: pytest.MonkeyPatch, fresh_extractor_cache: list[dict[str, object]]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("DEAL_EXTRACTOR", raising=False)

    api.get_extractor()
    api.get_extractor()

    assert fresh_extractor_cache == [
        {
            "api_key": "test-key",
            "timeout": api.CLAUDE_TIMEOUT_SECONDS,
            "max_retries": api.CLAUDE_MAX_RETRIES,
            "open_telemetry": False,
        }
    ]  # built once, with bounds, and the key passed explicitly from deal_board.config


def test_brokers_are_loaded_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def counting_load_brokers() -> list[Broker]:
        calls.append(1)
        return BROKERS

    monkeypatch.setattr(broker_store, "load_brokers", counting_load_brokers)  # the module api.py calls through
    api.get_brokers.cache_clear()
    try:
        assert api.get_brokers() is api.get_brokers()
        assert calls == [1]
    finally:
        api.get_brokers.cache_clear()


# --- integration with the real matcher ---------------------------------------


def test_real_matcher_only_returns_brokers_licensed_in_the_deal_state(client: TestClient) -> None:
    pytest.importorskip("deal_board.matcher")
    # Real get_matcher and get_brokers; only the extractor and the clock are pinned.
    api.app.dependency_overrides[api.get_extractor] = lambda: FakeExtractor(FULL_TX_DEAL)
    api.app.dependency_overrides[api.get_as_of] = lambda: AS_OF
    by_id = {b.broker_id: b for b in load_brokers()}

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 200
    body = resp.json()
    assert body["excluded_for_licensing"] >= 0
    for m in body["matches"]:
        assert set(m) == MATCH_KEYS
        broker = by_id[m["broker_id"]]
        assert broker.license_state == "TX", m["broker_id"]
        # brokers.py: an unknown expiration counts as not licensed.
        assert broker.license_expiration is not None and broker.license_expiration >= AS_OF, m["broker_id"]
