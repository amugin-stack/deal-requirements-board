"""HTTP contract for POST /match.

Every collaborator (extractor, broker list, clock, matcher) is swapped through
FastAPI dependency overrides, so these tests pin the API's own behavior: request
validation, response shape, follow-up wording, error mapping, and log hygiene.
The last test runs the real matcher against the real broker data when it exists.
"""
import logging
from dataclasses import dataclass, field
from datetime import date

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

import api
from brokers import Broker, load_brokers
from extractors import ExtractionError
from schema import DealRequirement

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
    def __init__(self, result: DealRequirement | Exception, name: str = "claude"):
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
    calls: list[tuple] = field(default_factory=list)

    def __call__(self, req: DealRequirement, brokers: list[Broker], as_of: date) -> MatchResult:
        self.calls.append((req, brokers, as_of))
        if not req.is_deal_request:
            return MatchResult(matches=[], excluded_for_licensing=0)
        if req.state is None:
            return MatchResult(matches=[], excluded_for_licensing=len(brokers))
        return MatchResult(matches=self.matches, excluded_for_licensing=self.excluded)


def make_broker(broker_id: str, name: str, license_number: str, license_type: str = "Broker Individual") -> Broker:
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
    property_type="multifamily",
    transaction_type="buy",
    deal_value_usd=3_200_000,
    timeline="31_to_90_days",
    notes="12 units",
)
# Missing state AND transaction_type: the state question must win on priority.
PORTLAND_NO_STATE = DealRequirement(is_deal_request=True, city="Portland", property_type="retail")
NOT_A_DEAL = DealRequirement(is_deal_request=False)


def api_status_error() -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(UPSTREAM_SECRET, response=httpx.Response(500, request=request), body=None)


def api_connection_error() -> anthropic.APIConnectionError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(message=UPSTREAM_SECRET, request=request)


# --- fixtures ----------------------------------------------------------------

@pytest.fixture
def client():
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


@pytest.fixture
def matcher():
    return FakeMatcher()


def wire(extractor: FakeExtractor, matcher: FakeMatcher | None = None, brokers: list[Broker] = BROKERS):
    overrides = api.app.dependency_overrides
    overrides[api.get_extractor] = lambda: extractor
    overrides[api.get_brokers] = lambda: brokers
    overrides[api.get_as_of] = lambda: AS_OF
    if matcher is not None:
        overrides[api.get_matcher] = lambda: matcher


# --- success paths -----------------------------------------------------------

def test_full_deal_returns_matches_in_matcher_order(client, matcher):
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


def test_extractor_name_is_reported(client, matcher):
    wire(FakeExtractor(FULL_TX_DEAL, name="regex"), matcher)

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 200
    assert resp.json()["extractor"] == "regex"


def test_missing_state_excludes_everyone_and_asks_for_state(client, matcher):
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


def test_follow_up_targets_remaining_gap_when_state_is_known(client, matcher):
    # Re-validate so missing_fields is recomputed; model_copy skips validators.
    no_timeline = DealRequirement.model_validate({**FULL_TX_DEAL.model_dump(), "timeline": None})
    wire(FakeExtractor(no_timeline), matcher)

    body = client.post("/match", json={"text": POST}).json()

    assert body["requirement"]["missing_fields"] == ["timeline"]
    question = body["follow_up_question"]
    assert isinstance(question, str) and question.strip()
    assert "licensed in that state" not in question


def test_non_deal_post_has_no_matches_and_no_follow_up(client, matcher):
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
def test_invalid_body_is_rejected_before_extraction(client, matcher, payload):
    extractor = FakeExtractor(FULL_TX_DEAL)
    wire(extractor, matcher)

    resp = client.post("/match", json=payload)

    assert resp.status_code == 422
    assert extractor.calls == []
    assert matcher.calls == []


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("x", id="1-char"),
        pytest.param("x" * 4000, id="4000-chars"),
        pytest.param("   " + "x" * 4000 + "\n\n", id="4000-chars-padded"),
    ],
)
def test_length_limit_applies_after_stripping(client, matcher, text):
    extractor = FakeExtractor(FULL_TX_DEAL)
    wire(extractor, matcher)

    resp = client.post("/match", json={"text": text})

    assert resp.status_code == 200
    assert len(extractor.calls) == 1


# --- extractor failures ------------------------------------------------------

def test_extraction_error_is_422_with_readable_detail(client, matcher):
    wire(FakeExtractor(ExtractionError("model returned no tool call")), matcher)

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str) and detail.strip()
    assert matcher.calls == []


@pytest.mark.parametrize("error", [api_status_error, api_connection_error], ids=["status", "connection"])
def test_upstream_api_error_is_503_without_leaking_upstream_text(client, matcher, error):
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
def test_post_text_never_reaches_logs(client, matcher, caplog, outcome, status):
    caplog.set_level(logging.DEBUG)
    wire(FakeExtractor(outcome), matcher)

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == status
    assert "ZEBRA-7731" not in caplog.text
    assert POST not in caplog.text


# --- integration with the real matcher ---------------------------------------

def test_real_matcher_only_returns_brokers_licensed_in_the_deal_state(client):
    pytest.importorskip("matcher")
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
