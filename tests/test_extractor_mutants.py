"""Tests aimed at extractor bugs the earlier suite let through (guessed state, prefix cache).

Fake Anthropic clients only: no network, no API key. Brokers are fictional and built here.
"""

from collections.abc import Iterator
from datetime import date
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from deal_board import api
from deal_board.brokers import Broker
from deal_board.extractors.claude import TOOL, ClaudeExtractor
from deal_board.extractors.regex_baseline import RegexExtractor
from deal_board.matcher import match
from deal_board.schema import JSONObject

AS_OF = date(2026, 10, 20)


def make_broker(broker_id: str, state: str, **overrides: object) -> Broker:
    fields: dict[str, object] = {
        "broker_id": broker_id,
        "name": f"Fictional Broker {broker_id}",
        "source": "fictional",
        "license_state": state,
        "license_number": f"FICT-{broker_id}",
        "license_type": "Broker Individual",
        "license_status": "Active",
        "license_expiration": "2028-01-31",
        "county": "",
        "markets": "",
        "property_types": "retail",
        "transaction_types": "buy",
        "min_deal_usd": "",
        "max_deal_usd": "",
    }
    fields.update(overrides)
    return Broker.model_validate(fields)


class FakeClient:
    """Returns one canned tool_use response; never touches the network."""

    def __init__(self, output: JSONObject) -> None:
        self.output = output
        self.messages = self

    def create(self, **kwargs: object) -> NS:
        block = NS(type="tool_use", name=TOOL["name"], input=dict(self.output))
        return NS(stop_reason="tool_use", content=[block], usage=None)


NULL_STATE_OUTPUT: JSONObject = {
    "is_deal_request": True,
    "state": None,
    "city": None,
    "property_type": "retail",
    "transaction_type": "buy",
    "deal_value_usd": None,
    "timeline": None,
    "notes": None,
}


@pytest.mark.parametrize(
    "post",
    [
        "Fictional client wants a retail buy, call me IN the morning",
        "Fictional client wants a retail buy, OK with any size, ME too",
    ],
)
def test_claude_null_state_is_not_guessed_from_text(post: str) -> None:
    """Guards against extract() inferring a null state from a capitalised word like 'IN' or 'OK'.

    The post names no state; a guessed one would route the deal to brokers licensed in the
    wrong state. A null state must stay null, and the matcher then excludes everyone.
    """
    req = ClaudeExtractor(client=FakeClient(NULL_STATE_OUTPUT), model="fake-model").extract(post)
    assert req.state is None
    assert "state" in req.missing_fields
    brokers = [
        make_broker("fx-mut-69", "IN", name="Fictional Broker IN-Mut", license_number="FICT-IN-9069"),
        make_broker("fx-mut-69-ok", "OK"),
        make_broker("fx-mut-69-me", "ME"),
    ]
    result = match(req, brokers, AS_OF)
    assert result.matches == []
    assert result.excluded_for_licensing == len(brokers)


# Identical for 77 characters (well past 40); only the city/state at the end differs.
SHARED_OPENING = "Looking for a broker for my client who wants to buy a retail strip center in "
POST_TX = SHARED_OPENING + "Dallas, TX"
POST_OK = SHARED_OPENING + "Tulsa, OK"


@pytest.mark.parametrize("order", [(POST_TX, POST_OK), (POST_OK, POST_TX)])
def test_regex_extractor_does_not_reuse_results_for_shared_prefix(order: tuple[str, str]) -> None:
    """Guards against the wrapper caching results keyed on the post's opening characters.

    One instance handles both posts; a prefix cache would hand the second post the first
    post's state, sending a Tulsa deal to Texas brokers (or the reverse).
    """
    ext = RegexExtractor()
    expected = {POST_TX: ("TX", "Dallas"), POST_OK: ("OK", "Tulsa")}
    for post in order:
        req = ext.extract(post)
        assert (req.state, req.city) == expected[post]


@pytest.fixture
def regex_client() -> Iterator[TestClient]:
    brokers = [make_broker("fx-mut-71-tx", "TX"), make_broker("fx-mut-71-ok", "OK")]
    overrides = api.app.dependency_overrides
    overrides[api.get_extractor] = api._regex_extractor  # the process-wide cached instance
    overrides[api.get_brokers] = lambda: brokers
    overrides[api.get_as_of] = lambda: AS_OF
    try:
        yield TestClient(api.app)
    finally:
        overrides.clear()


def test_regex_api_does_not_reuse_results_for_shared_prefix(regex_client: TestClient) -> None:
    """Same prefix-cache bug through POST /match and the API's cached regex extractor.

    The second post must get its own state and only the OK broker, not the first post's TX broker.
    """
    first = regex_client.post("/match", json={"text": POST_TX})
    assert first.status_code == 200
    assert first.json()["requirement"]["state"] == "TX"
    assert [m["broker_id"] for m in first.json()["matches"]] == ["fx-mut-71-tx"]

    second = regex_client.post("/match", json={"text": POST_OK})
    assert second.status_code == 200
    body = second.json()
    assert body["requirement"]["state"] == "OK"
    assert body["requirement"]["city"] == "Tulsa"
    assert [m["broker_id"] for m in body["matches"]] == ["fx-mut-71-ok"]
