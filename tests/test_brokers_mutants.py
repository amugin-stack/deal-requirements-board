"""Loader and county-map guards for deal_board/brokers.py (mutation-testing survivors).

Every fixture broker here is fictional and lives in a tmp_path CSV or an in-test Broker.
"""

from datetime import date
from pathlib import Path

from deal_board.brokers import TX_CITY_TO_COUNTY, Broker, load_brokers
from deal_board.matcher import match
from deal_board.schema import DealRequirement

AS_OF = date(2026, 10, 25)
HEADER = (
    "broker_id,name,source,license_state,license_number,license_type,license_status,"
    "license_expiration,county,markets,property_types,transaction_types,min_deal_usd,max_deal_usd"
)


def _write(tmp_path: Path, *rows: str) -> Path:
    """Write raw CSV lines verbatim (no csv module), so whitespace lands exactly as given."""
    path = tmp_path / "brokers.csv"
    path.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
    return path


def _fl_row(broker_id: str, name: str, number: str, status: str, expiration: str) -> str:
    return (
        f"{broker_id},{name},fictional,FL,{number},Broker Individual,{status},{expiration},,"
        "Tampa,retail,buy,1000000,10000000"
    )


FL_DEAL = DealRequirement(is_deal_request=True, state="FL")


def test_padded_status_is_kept_verbatim_and_fails_closed(tmp_path: Path) -> None:
    """gate-25: a validator that strips strings would turn 'Active ' into 'Active' and pass the gate.

    The status is verbatim from the source; a padded value is not exactly "Active", so the
    fail-closed gate must reject it rather than have the loader quietly repair it.
    """
    path = _write(tmp_path, _fl_row("fx-mut-01", "Fictional Broker FL-Padded", "FICT-FL-9001", "Active ", "2028-06-30"))
    brokers = load_brokers(path)
    assert [b.license_status for b in brokers] == ["Active "]
    result = match(FL_DEAL, brokers, AS_OF)
    assert result.matches == []
    assert result.excluded_for_licensing == 1


def test_leading_space_status_is_not_trimmed_on_load(tmp_path: Path) -> None:
    """gate-28: reading with skipinitialspace=True would drop the space in ', Active' and pass the gate.

    The space sits right after the delimiter, unquoted, which is exactly what skipinitialspace eats.
    """
    path = _write(
        tmp_path, _fl_row("fx-mut-02", "Fictional Broker FL-LeadingSpace", "FICT-FL-9002", " Active", "2028-06-30")
    )
    assert ", Active," in path.read_text(encoding="utf-8")
    brokers = load_brokers(path)
    assert [b.license_status for b in brokers] == [" Active"]
    # The broker id is also a field right after a delimiter; prove other fields are untouched too.
    assert brokers[0].name == "Fictional Broker FL-LeadingSpace"
    result = match(FL_DEAL, brokers, AS_OF)
    assert result.matches == []
    assert result.excluded_for_licensing == 1


def test_load_brokers_keeps_rows_sharing_a_broker_id(tmp_path: Path) -> None:
    """gate-53: deduping by broker_id (keep last) would silently drop one of two records.

    Checked in both orders, so a keep-last and a keep-first dedup are each caught: the loader
    must return every row, and the gate must decide on each record separately.
    """
    expired = _fl_row("fx-mut-03", "Fictional Broker FL-Twin", "FICT-FL-9003", "Expired", "2025-01-31")
    active = _fl_row("fx-mut-03", "Fictional Broker FL-Twin", "FICT-FL-9003", "Active", "2028-06-30")
    for order in ((expired, active), (active, expired)):
        brokers = load_brokers(_write(tmp_path, *order))
        assert len(brokers) == 2
        assert sorted(b.license_status for b in brokers) == ["Active", "Expired"]
        result = match(FL_DEAL, brokers, AS_OF)
        assert [m.broker.license_status for m in result.matches] == ["Active"]
        assert result.excluded_for_licensing == 1


def _tx_broker(broker_id: str, county: str) -> Broker:
    return Broker(
        broker_id=broker_id,
        name=f"Fictional Broker TX-{county}",
        source="fictional",
        license_state="TX",
        license_number=f"FICT-TX-{broker_id[-4:]}",
        license_type="Broker Individual",
        license_status="Active",
        license_expiration=date(2028, 6, 30),
        county=county,
        markets=[],
        property_types=[],
        transaction_types=[],
        min_deal_usd=None,
        max_deal_usd=None,
    )


def test_fort_worth_maps_to_tarrant_county() -> None:
    """match-41: mapping Fort Worth to Dallas County would give the local-fit point to the wrong brokers.

    A Tarrant broker must get +2 for a Fort Worth deal and a Dallas broker must get nothing.
    """
    assert TX_CITY_TO_COUNTY["fort worth"] == "Tarrant"
    tarrant, dallas = _tx_broker("fx-mut-9101", "Tarrant"), _tx_broker("fx-mut-9102", "Dallas")
    deal = DealRequirement(is_deal_request=True, state="TX", city="Fort Worth")
    result = match(deal, [dallas, tarrant], AS_OF)
    by_id = {m.broker.broker_id: m for m in result.matches}
    assert by_id["fx-mut-9101"].score == 2
    assert by_id["fx-mut-9101"].reasons == ["+2 based in Tarrant County, which covers Fort Worth"]
    assert by_id["fx-mut-9102"].score == 0
    assert by_id["fx-mut-9102"].reasons == []
    assert [m.broker.broker_id for m in result.matches] == ["fx-mut-9101", "fx-mut-9102"]


def test_tx_city_to_county_table_is_exact() -> None:
    """match-43: an extra non-Texas city (e.g. 'oklahoma city' -> Tarrant) would hand out county points.

    The table is pinned exactly, and a TX deal naming Oklahoma City gives a Tarrant broker nothing.
    """
    assert TX_CITY_TO_COUNTY == {
        "austin": "Travis",
        "houston": "Harris",
        "dallas": "Dallas",
        "san antonio": "Bexar",
        "fort worth": "Tarrant",
        "arlington": "Tarrant",
        "plano": "Collin",
        "el paso": "El Paso",
    }
    deal = DealRequirement(is_deal_request=True, state="TX", city="Oklahoma City")
    result = match(deal, [_tx_broker("fx-mut-9103", "Tarrant")], AS_OF)
    assert len(result.matches) == 1
    assert result.matches[0].score == 0
    assert result.matches[0].reasons == []
