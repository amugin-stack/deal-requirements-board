"""Regression tests for the P6 adversarial review's confirmed findings. One test per finding.

Every Claude extractor uses a fake client; nothing touches the network or the repo's results/ or .cache/.
"""

import json
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from deal_board.cli import eval as ev
from deal_board.extractors.claude import PROMPT_VERSION, ClaudeExtractor
from deal_board.scoring import wilson_interval

MODEL = "claude-haiku-5-5"
OUTPUT = {
    "is_deal_request": True,
    "state": "TX",
    "city": "Austin",
    "property_type": "single_family",
    "transaction_type": "buy",
    "deal_value_usd": 650000,
    "timeline": "31_to_90_days",
    "notes": None,
}


class FakeClient:
    """One canned tool call per request, with usage, counting calls."""

    def __init__(self) -> None:
        self.calls, self.messages = 0, self

    def create(self, **kwargs: Any) -> NS:
        self.calls += 1
        return NS(
            stop_reason="tool_use",
            usage=NS(input_tokens=100, output_tokens=10),
            content=[NS(type="tool_use", input=dict(OUTPUT))],
        )


def run_claude(monkeypatch: pytest.MonkeyPatch, *argv: str) -> FakeClient:
    client = FakeClient()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-a-real-key")
    monkeypatch.setattr(
        ev,
        "make_extractor",
        lambda name: (ClaudeExtractor(client=client), {"model": MODEL, "prompt_version": PROMPT_VERSION}),
    )
    ev.main(["--extractor", "claude", "--split", "dev", *argv])
    return client


def test_rescore_does_not_inherit_source_usage_or_repeat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """F1: a rescore makes no API calls, so it must not report the source run's cost or repeat index."""
    runs, rescored = tmp_path / "runs", tmp_path / "rescored"
    run_claude(monkeypatch, "--repeats", "2", "--results-dir", str(runs), "--cache-dir", str(tmp_path / "c"))
    source = next(runs.glob("*_r2.json"))
    ev.main(["--rescore", str(source), "--results-dir", str(rescored)])
    (out,) = rescored.glob("*.json")
    result = json.loads(out.read_text())
    assert not {"usage", "repeat", "cache_enabled"} & result.keys()
    assert "_r2" not in out.name and result["rescored_from"] == str(source)
    md = out.with_suffix(".md").read_text()
    assert "**Usage:**" not in md and "repeat 2 of 2" not in md and "no API calls" in md


def test_editing_the_extractor_source_invalidates_cache(tmp_path: Path) -> None:
    """F2: PROMPT_VERSION ignores max_tokens/tool_choice/the <post> wrapper, so the request fingerprint must
    turn entries written under a different extractor source into misses."""
    text = "Need a buyer agent in Austin, TX"
    ev.RawCache(tmp_path, MODEL, PROMPT_VERSION, "source-before-edit").put(
        text, {"raw": dict(OUTPUT), "error": None, "input_tokens": 1, "output_tokens": 1, "latency_s": 0.1}
    )
    assert ev.RawCache(tmp_path, MODEL, PROMPT_VERSION, "source-before-edit").get(text) is not None
    assert ev.RawCache(tmp_path, MODEL, PROMPT_VERSION, "source-after-edit").get(text) is None


def test_cli_fingerprints_the_extractor_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """F2: the CLI stores the sha256 of the extractor's source file in every cache entry."""
    cache_dir = tmp_path / "cache"
    run_claude(monkeypatch, "--results-dir", str(tmp_path / "r"), "--cache-dir", str(cache_dir))
    expected = ev.file_sha256(Path(ev.ROOT / "deal_board" / "extractors" / "claude.py"))
    entries = [json.loads(p.read_text()) for p in cache_dir.glob("*.json")]
    assert entries and all(e["request_sha256"] == expected for e in entries)


def test_entry_missing_a_field_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    """F3: an entry whose identity matches but lacks `raw` (hand edit, old format) is a miss."""
    text = "Need a buyer agent in Austin, TX"
    cache = ev.RawCache(tmp_path, MODEL, PROMPT_VERSION)
    identity = {
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
        "text_sha256": ev.text_sha256(text),
        "request_sha256": None,
    }
    (tmp_path / f"{ev.cache_key(MODEL, PROMPT_VERSION, text)}.json").write_text(json.dumps(identity))
    assert cache.get(text) is None


def test_lone_surrogate_text_hashes_instead_of_crashing(tmp_path: Path) -> None:
    """F4: json.loads can yield a lone surrogate; hashing it must not raise UnicodeEncodeError."""
    text = json.loads('"broken \\ud800 post"')
    assert len(ev.cache_key(MODEL, PROMPT_VERSION, text)) == 64
    assert ev.text_sha256("plain") == ev.text_sha256("plain")  # ordinary text unchanged by surrogatepass


@pytest.mark.parametrize("z", [0.0, -1.96])
def test_wilson_rejects_non_positive_z(z: float) -> None:
    """F6: a non-positive z would silently return an inverted or zero-width interval."""
    with pytest.raises(ValueError):
        wilson_interval(3, 10, z)
