"""Tests for bugs a post-fix mutation pass found the suite missing (matcher, builder, API, extractors).

Offline only: fake Anthropic clients, synthetic raw TREC files in tmp_path (with
build_brokers.ROOT monkeypatched, so the real data/brokers.csv is never written), and
obviously fictional brokers.
"""

import asyncio
import builtins
import csv
import logging
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace as NS

import pytest
from starlette.types import Message, Receive, Scope, Send

from deal_board import api, schema
from deal_board.brokers import Broker
from deal_board.cli import build_brokers
from deal_board.cli.build_brokers import AS_OF, is_valid, sample_trec
from deal_board.config import Settings
from deal_board.extractors import ExtractionError
from deal_board.extractors.claude import ClaudeExtractor
from deal_board.extractors.regex_baseline import RegexExtractor, _scoped_import
from deal_board.matcher import is_licensed, match
from deal_board.schema import DealRequirement

RAW_FIELDS = ["License Type", "License Number", "Status", "License Expiration Date", "County"]
RawRow = tuple[str, str, str, str, str]


# --- helpers -------------------------------------------------------------------------------------


def make_broker(broker_id: str, source: str = "fictional", state: str = "TX") -> Broker:
    return Broker.model_validate(
        {
            "broker_id": broker_id,
            "name": f"Fictional Broker {broker_id}",
            "source": source,
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
    )


def _write_raw(path: Path, rows: Sequence[RawRow]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(RAW_FIELDS)
        w.writerows(rows)
    return path


def _write_fictional(root: Path) -> None:
    (root / "data").mkdir(parents=True, exist_ok=True)
    with open(root / "data/fictional_brokers.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=build_brokers.FIELDS)
        w.writeheader()
        w.writerow(
            {
                "broker_id": "fx-001",
                "name": "Fictional Broker One",
                "source": "fictional",
                "license_state": "TX",
                "license_number": "FICT-0001",
                "license_type": "Broker Individual",
                "license_status": "Active",
                "license_expiration": "2028-01-31",
                "county": "",
                "markets": "Austin",
                "property_types": "retail",
                "transaction_types": "buy",
                "min_deal_usd": "",
                "max_deal_usd": "",
            }
        )


# 30 valid rows (FICT-V...), 3 Active-but-expired (FICT-E...), 10 other invalid (FICT-O...),
# numbered in file order, sampled with N_VALID=10, N_INVALID=6.
STRATA_RAW: list[RawRow] = (
    [("Sales Agent", f"FICT-V{i:03d}", "Active", "12/01/27", "Travis") for i in range(30)]
    + [("Sales Agent", f"FICT-E{i:03d}", "Active", "01/15/26", "Harris") for i in range(3)]
    + [("Sales Agent", f"FICT-O{i:03d}", "Inactive", "06/30/27", "Bexar") for i in range(10)]
)


@pytest.fixture
def strata_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, str]]:
    monkeypatch.setattr(build_brokers, "ROOT", tmp_path)
    monkeypatch.setattr(build_brokers, "N_VALID", 10)
    monkeypatch.setattr(build_brokers, "N_INVALID", 6)
    return sample_trec(str(_write_raw(tmp_path / "raw.csv", STRATA_RAW)))


# --- matcher -------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["fictional_import", "fictional-trec_2019-01-01", "fictionally_real"])
def test_only_exact_fictional_source_skips_snapshot_gate(source: str) -> None:
    """post-04: a prefix check (startswith('fictional')) would let any 'fictional...' source skip
    the freshness gate, so a stale or unreadable real record would be vouched for forever."""
    assert is_licensed(make_broker("fx-a", source=source), "TX", date(2026, 10, 20)) is False
    assert is_licensed(make_broker("fx-b", source="fictional"), "TX", date(2026, 10, 20)) is True


def test_limit_zero_returns_no_matches() -> None:
    """post-09: treating limit=0 as 'no limit' would hand a caller who asked for zero matches every
    licensed broker; the excluded count must still reflect the gate."""
    req = DealRequirement(is_deal_request=True, state="TX")
    brokers = [make_broker("fx-1"), make_broker("fx-2"), make_broker("fx-3", state="CA")]
    result = match(req, brokers, date(2026, 10, 20), limit=0)
    assert result.matches == []
    assert result.excluded_for_licensing == 1


# --- build_brokers -------------------------------------------------------------------------------


def test_padded_active_status_is_trimmed_and_classified_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """post-18: if Status is not trimmed, a raw ' Active ' is classified invalid (and not even as
    'Active but expired') and written padded, so strata and the written value disagree."""
    monkeypatch.setattr(build_brokers, "ROOT", tmp_path)
    monkeypatch.setattr(build_brokers, "N_VALID", 1)
    monkeypatch.setattr(build_brokers, "N_INVALID", 2)
    raw: list[RawRow] = [
        ("Sales Agent", "FICT-P001", " Active ", "12/01/27", "Travis"),
        ("Sales Agent", "FICT-P002", "Inactive", "12/01/27", "Travis"),
        ("Sales Agent", "FICT-P003", "Suspended", "12/01/27", "Travis"),
    ]
    rows = sample_trec(str(_write_raw(tmp_path / "raw.csv", raw)))
    by_number = {r["license_number"]: r for r in rows}
    assert by_number["FICT-P001"]["license_status"] == "Active"
    assert {r["license_status"] for r in rows} == {"Active", "Inactive", "Suspended"}


def test_license_expiring_on_as_of_is_valid() -> None:
    """post-19: exp > AS_OF would put a license valid on the download day into the 'Active but
    expired' stratum, mislabeling it against the matcher's >= check."""
    row = {"Status": "Active", "License Expiration Date": AS_OF.strftime("%m/%d/%y"), "License Type": "Sales Agent"}
    assert is_valid(row) is True


def test_valid_stratum_is_a_random_sample_not_file_order(strata_rows: list[dict[str, str]]) -> None:
    """post-20: taking valid[:N_VALID] biases the sample to file order (and shifts every later
    rng draw); the seeded sample must not simply be the first N valid rows."""
    chosen = {r["license_number"] for r in strata_rows if r["license_number"].startswith("FICT-V")}
    assert len(chosen) == 10
    assert chosen != {f"FICT-V{i:03d}" for i in range(10)}


def test_broker_ids_do_not_reveal_stratum(strata_rows: list[dict[str, str]]) -> None:
    """post-21: without the final shuffle tx-001..tx-N are exactly the valid rows, leaking the
    label into the broker_id."""
    assert len(strata_rows) == 16
    first = [r for r in strata_rows if int(r["broker_id"].removeprefix("tx-")) <= 10]
    assert len(first) == 10
    assert not all(r["license_number"].startswith("FICT-V") for r in first)
    assert any(r["license_number"].startswith("FICT-V") for r in strata_rows[10:])


def test_build_summary_counts_license_expiring_on_as_of(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """post-22: counting with > undercounts licenses expiring on the download day in the printed
    'N valid as of AS_OF' summary."""
    monkeypatch.setattr(build_brokers, "ROOT", tmp_path)
    monkeypatch.setattr(build_brokers, "N_VALID", 2)
    monkeypatch.setattr(build_brokers, "N_INVALID", 2)
    _write_fictional(tmp_path)
    raw: list[RawRow] = [
        ("Sales Agent", "FICT-S001", "Active", AS_OF.strftime("%m/%d/%y"), "Travis"),
        ("Sales Agent", "FICT-S002", "Active", "12/01/27", "Travis"),
        ("Sales Agent", "FICT-S003", "Inactive", "12/01/27", "Travis"),
        ("Sales Agent", "FICT-S004", "Suspended", "12/01/27", "Travis"),
    ]
    build_brokers.main(str(_write_raw(tmp_path / "raw.csv", raw)))
    out = capsys.readouterr().out
    assert f"wrote 4 TREC rows (2 valid as of {AS_OF}) + 1 fictional" in out


# --- api -----------------------------------------------------------------------------------------


def test_licensing_date_rejects_naive_datetime() -> None:
    """post-26: a naive now would be read in the host's local zone, so the licensing date (and
    which licenses pass) would depend on the server's TZ."""
    with pytest.raises(ValueError):
        api.licensing_date(datetime(2026, 10, 20, 23, 30))


def test_body_limit_counts_running_total_of_chunks() -> None:
    """post-29: checking each chunk instead of the running total lets a chunked upload of many
    sub-cap chunks (no Content-Length) bypass the 64 KiB limit."""
    chunk = b"x" * (api.MAX_BODY_BYTES // 4)
    messages: list[Message] = [{"type": "http.request", "body": chunk, "more_body": True} for _ in range(5)]
    messages.append({"type": "http.request", "body": b"", "more_body": False})
    inner_called: list[bool] = []
    sent: list[Message] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        inner_called.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive() -> Message:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": "POST", "path": "/match", "headers": []}
    asyncio.run(api.BodySizeLimit(inner, max_bytes=api.MAX_BODY_BYTES)(scope, receive, send))
    assert inner_called == []
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


@pytest.fixture
def rearmed_warning() -> Iterator[None]:
    api._warn_auto_fell_back_to_regex.cache_clear()
    yield
    api._warn_auto_fell_back_to_regex.cache_clear()


def _fallback_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and "regex baseline" in r.getMessage()]


def test_fallback_warning_only_in_auto_mode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rearmed_warning: None
) -> None:
    """post-31: an inverted condition would warn on an explicit DEAL_EXTRACTOR=regex and stay
    silent on the real auto-mode downgrade to regex."""
    caplog.set_level(logging.WARNING, logger=api.logger.name)
    monkeypatch.setattr(api, "load_settings", lambda: Settings(deal_extractor="regex"))
    assert api.get_extractor().name == "regex"
    assert _fallback_warnings(caplog) == []

    api._warn_auto_fell_back_to_regex.cache_clear()
    monkeypatch.setattr(api, "load_settings", lambda: Settings())
    assert api.get_extractor().name == "regex"
    assert len(_fallback_warnings(caplog)) == 1


# --- extractors ----------------------------------------------------------------------------------

VALID_OUTPUT: dict[str, object] = {
    "is_deal_request": True,
    "state": "TX",
    "city": None,
    "property_type": "retail",
    "transaction_type": "buy",
    "deal_value_usd": None,
    "timeline": None,
    "notes": None,
}


class NamelessToolClient:
    """Returns a tool_use block whose name is None (or absent); never touches the network."""

    def __init__(self, with_name_attr: bool) -> None:
        self.with_name_attr = with_name_attr
        self.messages = self

    def create(self, **kwargs: object) -> NS:
        block = NS(type="tool_use", input=dict(VALID_OUTPUT))
        if self.with_name_attr:
            block.name = None
        return NS(stop_reason="tool_use", content=[block], usage=None)


@pytest.mark.parametrize("with_name_attr", [True, False])
def test_claude_rejects_tool_use_block_without_name(with_name_attr: bool) -> None:
    """post-35: a tool_use block with no name can't be shown to be our tool; accepting it would
    trust another (or malformed) tool call's input as a deal requirement."""
    extractor = ClaudeExtractor(client=NamelessToolClient(with_name_attr), model="fictional-model")
    with pytest.raises(ExtractionError):
        extractor.extract("Fictional client wants a retail buy in Austin, TX")


class DictReturningImpl:
    def extract(self, text: str) -> object:
        return dict(VALID_OUTPUT)


def test_regex_wrapper_rejects_non_requirement_output() -> None:
    """post-38: a check weakened to 'result is None' would pass a dict from the frozen impl
    through as if it were a validated DealRequirement."""
    extractor = RegexExtractor()
    extractor._impl = DictReturningImpl()
    with pytest.raises(TypeError):
        extractor.extract("Fictional client wants a retail buy in Austin, TX")


def test_scoped_import_aliases_only_absolute_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """post-39: aliasing 'schema' at any level would redirect a relative 'from .schema import'
    to deal_board.schema instead of resolving it normally via __import__."""
    assert _scoped_import("schema", None, None, ("DealRequirement",), 0) is schema

    real_import = builtins.__import__
    sentinel = ModuleType("fictional_relative_schema")
    calls: list[tuple[str, int]] = []

    def recorder(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] | None = (),
        level: int = 0,
    ) -> ModuleType:
        if name == "schema" and level > 0:
            calls.append((name, level))
            return sentinel
        return real_import(name, globals, locals, fromlist or (), level)

    monkeypatch.setattr(builtins, "__import__", recorder)
    result = _scoped_import("schema", None, None, ("DealRequirement",), 1)
    monkeypatch.undo()
    assert result is sentinel
    assert result is not schema
    assert calls == [("schema", 1)]
