"""Regression tests for the adversarial review of the fix loop (review/fixes/POSTREVIEW.md, ADV-n)."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deal_board import api
from deal_board.brokers import Broker
from deal_board.cli import eval as ev
from deal_board.extractors.claude import PROMPT_VERSION
from deal_board.matcher import is_licensed, match

ROOT = Path(__file__).resolve().parent.parent
PRE_STRICT_RUN = ROOT / "results" / "claude_20261024T231514Z.json"  # prompt de789943: nulls could be omitted


def error_counts(datasets: dict[str, dict[str, object]]) -> dict[str, int]:
    """is_deal_request error count per dataset of a results 'datasets' block."""
    return {name: ds["scores"]["is_deal_request"]["error"] for name, ds in datasets.items()}  # type: ignore[index]


def test_adv1_rescore_of_a_pre_strict_run_keeps_its_valid_outputs() -> None:
    """ADV-1: outputs that were valid under their own prompt (omitted null keys) stay predictions on rescore."""
    saved = json.loads(PRE_STRICT_RUN.read_text())
    assert saved["prompt_version"] != PROMPT_VERSION
    assert error_counts(ev.rescore(saved)) == error_counts(saved["datasets"])
    assert ev.raw_check_name(saved) == "legacy"


def test_adv1_current_prompt_outputs_are_still_held_to_the_strict_schema() -> None:
    """ADV-1 control: a saved run of today's prompt is still strict, so a missing key is an error."""
    assert ev.raw_check("claude", PROMPT_VERSION) is not None
    assert ev.raw_check("claude") is not None  # fresh runs
    assert ev.raw_check("claude", "de789943") is None
    assert ev.raw_check("regex", PROMPT_VERSION) is None
    check = ev.raw_check("claude", PROMPT_VERSION)
    assert check is not None
    record = ev.to_record({"is_deal_request": True, "state": "TX"}, None, check)
    assert record["error"] and record["prediction"] is None
    assert ev.raw_check_name({"extractor": "claude", "prompt_version": PROMPT_VERSION}) == "strict"
    assert ev.raw_check_name({"extractor": "claude"}) == "legacy"  # no prompt version: can't claim today's


@pytest.mark.parametrize("name", ["state", "notes"])
def test_adv1_legacy_rescore_still_rejects_invalid_values(name: str) -> None:
    """ADV-1: the legacy path is main's behaviour, so schema-invalid values still raise there."""
    bad = {"is_deal_request": True, name: 48}
    assert ev.to_record(bad, None, ev.raw_check("claude", "de789943"))["error"]


def _broker(source: str) -> Broker:
    return Broker(
        broker_id="tx-adv2",
        name="TX Licensee",
        source=source,
        license_state="TX",
        license_number="ADV-2",
        license_type="Broker Individual",
        license_status="Active",
        license_expiration=date(2027, 12, 31),
        county=None,
        markets=[],
        property_types=[],
        transaction_types=[],
        min_deal_usd=None,
        max_deal_usd=None,
    )


ADV2_AS_OF = date(2026, 11, 30)


@pytest.mark.parametrize(
    "source",
    [
        "TREC_2026-10-20",
        "Trec_2026-10-20",
        " trec_2026-10-20",
        "trec-2026-10-20",
        "tdlr_2026-10-20",
        "real",
        "trec_2026-12-01",  # snapshot after as_of: says nothing about that earlier day
        "trec_2099-01-01",
        "trec_2026-10-20 ",
    ],
)
def test_adv2_unreadable_or_future_snapshot_fails_closed(source: str) -> None:
    """ADV-2: only 'fictional' or an exact, past trec_YYYY-MM-DD within the age limit can pass the gate."""
    assert is_licensed(_broker(source), "TX", ADV2_AS_OF) is False


@pytest.mark.parametrize("source", ["fictional", "trec_2026-10-20", "trec_2026-11-30"])
def test_adv2_controls_still_pass(source: str) -> None:
    """ADV-2 control: fictional rows and fresh, well-formed snapshots (age 0 to 90 days) still pass."""
    assert is_licensed(_broker(source), "TX", ADV2_AS_OF) is True


class _WrongTypeExtractor:
    """An extractor bug: returns something that isn't a DealRequirement at all."""

    name = "regex"

    def __init__(self, value: object) -> None:
        self._value = value

    def extract(self, text: str) -> object:
        return self._value


@pytest.mark.parametrize(
    "value", [None, {"is_deal_request": True, "state": "TX"}, "TX deal", object()], ids=["none", "dict", "str", "obj"]
)
def test_adv3_non_requirement_from_extractor_is_a_logged_500(value: object, caplog: pytest.LogCaptureFixture) -> None:
    """ADV-3: a wrong-typed extractor result is the fixed 500 with a post= log line, never a 200 'not a deal'."""
    api.app.dependency_overrides[api.get_extractor] = lambda: _WrongTypeExtractor(value)
    api.app.dependency_overrides[api.get_brokers] = lambda: []
    api.app.dependency_overrides[api.get_as_of] = lambda: date(2026, 11, 30)
    api.app.dependency_overrides[api.get_matcher] = lambda: match
    try:
        with caplog.at_level(logging.INFO):
            resp = TestClient(api.app, raise_server_exceptions=False).post("/match", json={"text": "TX deal QUOKKA"})
    finally:
        api.app.dependency_overrides.clear()
    assert resp.status_code == 500
    assert resp.json() == {"detail": "matching failed unexpectedly"}
    assert any("post=" in r.getMessage() for r in caplog.records)
    assert not any("QUOKKA" in r.getMessage() for r in caplog.records)
