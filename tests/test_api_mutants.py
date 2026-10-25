"""Tests aimed at mutants of deal_board/api.py that the rest of the suite let through.

Each test pins one behavior a deliberate bug would change: the production clock behind
`get_as_of`, the fail-closed `get_matcher` import handling, the body-size cap's boundary, and
`get_brokers` handing the matcher every record so the excluded count stays honest.
"""

import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import ClassVar, Self
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from deal_board import api
from deal_board import brokers as broker_store
from deal_board.brokers import Broker
from deal_board.matcher import match
from deal_board.schema import DealRequirement, PropertyType, Timeline, TransactionType

AS_OF = date(2026, 10, 20)
POST = "Need a TX broker for a 12-unit multifamily in Austin, ~$3.2M, closing in 60 days."
NEW_YORK = ZoneInfo("America/New_York")
# A server west of New York: its local date lags the licensing date for part of each day.
SERVER_TZ = ZoneInfo("America/Chicago")

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


# --- helpers -----------------------------------------------------------------


class FakeExtractor:
    def __init__(self, result: DealRequirement, name: str = "claude") -> None:
        self.name = name
        self._result = result
        self.calls: list[str] = []

    def extract(self, text: str) -> DealRequirement:
        self.calls.append(text)
        return self._result


def make_broker(broker_id: str, expiration: date | None, status: str = "Active") -> Broker:
    # "fictional" source: exempt from the snapshot-age check, so only status/state/expiration gate it.
    return Broker(
        broker_id=broker_id,
        name=f"Fictional {broker_id}",
        source="fictional",
        license_state="TX",
        license_number=f"{broker_id}-B",
        license_type="Broker Individual",
        license_status=status,
        license_expiration=expiration,
        county="Travis",
        markets=[],
        property_types=[],
        transaction_types=[],
        min_deal_usd=None,
        max_deal_usd=None,
    )


class FrozenDatetime(datetime):
    """`datetime` with a pinned current instant; naive reads see a Chicago-local server clock."""

    instant: ClassVar[datetime] = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        if tz is None:
            return cls.fromtimestamp(cls.instant.timestamp(), SERVER_TZ).replace(tzinfo=None)
        return cls.fromtimestamp(cls.instant.timestamp(), tz)

    @classmethod
    def today(cls) -> Self:
        return cls.now()

    @classmethod
    def utcnow(cls) -> Self:
        return cls.fromtimestamp(cls.instant.timestamp(), UTC).replace(tzinfo=None)


class FrozenDate(date):
    """`date` whose today() is the frozen instant's date on a Chicago-local server."""

    @classmethod
    def today(cls) -> Self:
        local = FrozenDatetime.instant.astimezone(SERVER_TZ)
        return cls(local.year, local.month, local.day)


@pytest.fixture
def freeze(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FrozenDatetime]]:
    """Pin the clock api.py reads (both its `datetime` and `date` names); restored by monkeypatch."""
    monkeypatch.setattr(api, "datetime", FrozenDatetime)
    monkeypatch.setattr(api, "date", FrozenDate)
    original = FrozenDatetime.instant
    yield FrozenDatetime
    FrozenDatetime.instant = original


@pytest.fixture
def client() -> Iterator[TestClient]:
    try:
        yield TestClient(api.app)
    finally:
        api.app.dependency_overrides.clear()


def override_extractor_and_brokers(extractor: FakeExtractor, brokers: list[Broker]) -> None:
    api.app.dependency_overrides[api.get_extractor] = lambda: extractor
    api.app.dependency_overrides[api.get_brokers] = lambda: brokers


def ny_date(instant: datetime) -> date:
    return instant.astimezone(NEW_YORK).date()


# --- get_as_of: the production clock -----------------------------------------


def test_get_as_of_is_today(freeze: type[FrozenDatetime]) -> None:
    """Guards against get_as_of returning yesterday or a date a year back: either lets a license
    that expired within that window pass the gate in production (fail open)."""
    freeze.instant = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)  # 13:00 in New York, mid-day everywhere in the US
    assert api.get_as_of() == date(2026, 10, 25)


def test_get_as_of_matches_the_real_clock() -> None:
    """Same guard without any patching: the unpatched get_as_of must equal today's licensing date,
    not an earlier one. Two reads bracket the call so a midnight rollover can't flake it."""
    before = ny_date(datetime.now(UTC))
    got = api.get_as_of()
    after = ny_date(datetime.now(UTC))
    assert got in {before, after}


def test_production_as_of_excludes_license_expired_yesterday(client: TestClient, freeze: type[FrozenDatetime]) -> None:
    """End to end with no get_as_of override: a broker whose license expired yesterday must be
    excluded, while one expiring today still matches; an as_of lagging by a day or a year would
    route a referral to the expired licensee."""
    freeze.instant = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)
    today = date(2026, 10, 25)
    expired = make_broker("fx-expired", today - timedelta(days=1))
    current = make_broker("fx-current", today)
    extractor = FakeExtractor(FULL_TX_DEAL)
    override_extractor_and_brokers(extractor, [expired, current])

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 200
    body = resp.json()
    assert [m["broker_id"] for m in body["matches"]] == ["fx-current"]
    assert body["excluded_for_licensing"] == 1


def test_get_as_of_uses_licensing_date_not_utc(freeze: type[FrozenDatetime]) -> None:
    """Guards against get_as_of using the UTC calendar date: at 02:00Z it is already tomorrow in
    UTC but still today in New York, so a license expiring today would be wrongly rejected. The
    second instant also rules out the server's local (Chicago) date, which lags New York."""
    freeze.instant = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)  # 22:00 on 2026-10-25 in New York
    assert api.get_as_of() == date(2026, 10, 25)
    # Just after New York midnight (still 2026-10-25 in Chicago): the licensing date moves on.
    freeze.instant = datetime(2026, 10, 26, 4, 30, tzinfo=UTC)  # 00:30 on 2026-10-26 in New York
    assert api.get_as_of() == date(2026, 10, 26)


def test_get_as_of_follows_the_clock(freeze: type[FrozenDatetime]) -> None:
    """Guards against get_as_of returning a frozen constant (e.g. the 2026-10-20 snapshot date):
    licenses that expire after that date would then pass the gate forever."""
    freeze.instant = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)
    assert api.get_as_of() == date(2026, 10, 25)
    freeze.instant = datetime(2027, 1, 1, 17, 0, tzinfo=UTC)
    assert api.get_as_of() == date(2027, 1, 1)


# --- get_matcher: fail closed on a broken deployment -------------------------


def test_matcher_import_error_is_503_and_returns_no_brokers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards against swallowing the matcher's ImportError with an ungated pass-through: a broken
    deployment must answer 503, never a 200 listing unlicensed or expired brokers."""
    monkeypatch.setitem(sys.modules, "deal_board.matcher", None)  # makes the import raise ImportError
    extractor = FakeExtractor(FULL_TX_DEAL)
    override_extractor_and_brokers(extractor, [make_broker("fx-expired", date(2020, 1, 1), "Inactive")])
    api.app.dependency_overrides[api.get_as_of] = lambda: AS_OF

    resp = client.post("/match", json={"text": POST})

    assert resp.status_code == 503
    assert resp.json() == {"detail": "matcher unavailable"}
    assert extractor.calls == []


class _ExplodingModule:
    def __getattr__(self, name: str) -> object:
        raise RuntimeError("matcher bug at import time")


def test_matcher_non_import_error_stays_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards against get_matcher catching every Exception: a real matcher bug must stay a 500,
    not be disguised as a 503 'matcher unavailable' deployment problem."""
    monkeypatch.setitem(sys.modules, "deal_board.matcher", _ExplodingModule())
    extractor = FakeExtractor(FULL_TX_DEAL)
    override_extractor_and_brokers(extractor, [])
    api.app.dependency_overrides[api.get_as_of] = lambda: AS_OF
    try:
        resp = TestClient(api.app, raise_server_exceptions=False).post("/match", json={"text": POST})
    finally:
        api.app.dependency_overrides.clear()

    assert resp.status_code == 500
    assert extractor.calls == []


# --- BodySizeLimit boundary --------------------------------------------------


def padded_body(size: int) -> bytes:
    """A valid JSON request body of exactly `size` bytes (whitespace padding before the brace)."""
    head = b'{"text": "' + POST.encode() + b'"'
    pad = size - len(head) - 1
    assert pad >= 0
    body = head + b" " * pad + b"}"
    assert len(body) == size
    return body


def _wire_fake_matcher(extractor: FakeExtractor) -> None:
    override_extractor_and_brokers(extractor, [make_broker("fx-current", date(2030, 1, 1))])
    api.app.dependency_overrides[api.get_as_of] = lambda: AS_OF


def test_body_of_exactly_max_bytes_is_accepted(client: TestClient) -> None:
    """Guards against an off-by-one (>=) in the streaming size check: a body of exactly
    MAX_BODY_BYTES is within the cap and must be served, while one byte more is a 413."""
    extractor = FakeExtractor(FULL_TX_DEAL)
    _wire_fake_matcher(extractor)
    headers = {"content-type": "application/json"}

    ok = client.post("/match", content=padded_body(api.MAX_BODY_BYTES), headers=headers)
    assert ok.status_code == 200
    assert extractor.calls == [POST]

    too_big = client.post("/match", content=padded_body(api.MAX_BODY_BYTES + 1), headers=headers)
    assert too_big.status_code == 413
    assert extractor.calls == [POST]


def test_chunked_body_of_exactly_max_bytes_is_accepted(client: TestClient) -> None:
    """Same boundary without a Content-Length header, so only the streaming check decides:
    exactly MAX_BODY_BYTES across chunks is accepted."""
    extractor = FakeExtractor(FULL_TX_DEAL)
    _wire_fake_matcher(extractor)
    body = padded_body(api.MAX_BODY_BYTES)

    def stream() -> Iterator[bytes]:
        for i in range(0, len(body), 16_384):
            yield body[i : i + 16_384]

    resp = client.post("/match", content=stream(), headers={"content-type": "application/json"})

    assert resp.status_code == 200
    assert extractor.calls == [POST]


# --- get_brokers: every record reaches the gate ------------------------------


_BROKER_HEADER = (
    "broker_id,name,source,license_state,license_number,license_type,license_status,"
    "license_expiration,county,markets,property_types,transaction_types,min_deal_usd,max_deal_usd"
)
# Two licensed TX rows and three refused ones (inactive, expired, wrong state), all fictional.
_MIXED_BROKER_ROWS = (
    "fx-gb-1,Fictional Broker GB-1,fictional,TX,FICT-TX-8001,Broker Individual,Active,2028-01-31,,,,,,",
    "fx-gb-2,Fictional Broker GB-2,fictional,TX,FICT-TX-8002,Sales Agent,Active,2027-06-30,,,,,,",
    "fx-gb-3,Fictional Broker GB-3,fictional,TX,FICT-TX-8003,Broker Individual,Inactive,2028-01-31,,,,,,",
    "fx-gb-4,Fictional Broker GB-4,fictional,TX,FICT-TX-8004,Broker Individual,Active,2026-01-31,,,,,,",
    "fx-gb-5,Fictional Broker GB-5,fictional,FL,FICT-FL-8005,Broker Individual,Active,2028-01-31,,,,,,",
)


def test_real_get_brokers_keeps_every_record_and_counts_exclusions(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards against get_brokers pre-filtering to Active records: non-Active brokers would vanish
    from excluded_for_licensing, understating how many were refused on licensing grounds.

    The store reads a tmp CSV with a known mix, so the test doesn't depend on data/brokers.csv."""
    csv_path = tmp_path / "brokers.csv"
    csv_path.write_text("\n".join((_BROKER_HEADER, *_MIXED_BROKER_ROWS)) + "\n", encoding="utf-8")
    real_load = broker_store.load_brokers
    monkeypatch.setattr(broker_store, "load_brokers", lambda: real_load(csv_path))
    api.get_brokers.cache_clear()
    try:
        loaded = api.get_brokers()
        assert [b.broker_id for b in loaded] == [f"fx-gb-{i}" for i in range(1, 6)]
        assert [b.license_status for b in loaded].count("Inactive") == 1

        api.app.dependency_overrides[api.get_extractor] = lambda: FakeExtractor(FULL_TX_DEAL)
        api.app.dependency_overrides[api.get_as_of] = lambda: AS_OF
        api.app.dependency_overrides[api.get_matcher] = lambda: match
        resp = client.post("/match", json={"text": POST})
    finally:
        api.get_brokers.cache_clear()

    assert resp.status_code == 200
    body = resp.json()
    assert sorted(m["broker_id"] for m in body["matches"]) == ["fx-gb-1", "fx-gb-2"]
    assert body["excluded_for_licensing"] == 3  # inactive + expired + wrong state, out of 5
