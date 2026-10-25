"""Regression tests for the adversarial review of the fix loop (review/fixes/POSTREVIEW.md, ADV-n)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deal_board.cli import eval as ev
from deal_board.extractors.claude import PROMPT_VERSION

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
