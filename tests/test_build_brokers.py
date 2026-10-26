"""Guards for the one-time TREC builder, deal_board/cli/build_brokers.py.

The raw TREC download is not in the repo, so these tests build a synthetic raw CSV in
tmp_path. `main` runs with ROOT monkeypatched to tmp_path, so the real data/brokers.csv is
never written.
"""

import csv
from datetime import date
from pathlib import Path

import pytest

from deal_board.brokers import Broker, load_brokers
from deal_board.cli import build_brokers
from deal_board.cli.build_brokers import AS_OF, is_valid, parse_date, sample_trec
from deal_board.matcher import is_licensed
from deal_board.paths import REPO_ROOT

RAW_FIELDS = ["License Type", "License Number", "Status", "License Expiration Date", "County"]
SOURCE = "trec_2026-10-20"
PEOPLE = {"Sales Agent", "Broker Individual"}

# A small raw file where every person row lands in the output (N_VALID=3, N_INVALID=6).
# Dates are chosen so a day/month swap moves them across AS_OF (2026-10-20).
SMALL_RAW: list[tuple[str, str, str, str, str]] = [
    # (type, number, status, expiration, county)
    ("Sales Agent", "900001-SA", "Active", "12/01/26", "Travis"),  # Dec 1 2026 valid; swapped Jan 12 expired
    ("Broker Individual", "900002", "Active", "11/05/27", "Harris"),
    ("Sales Agent", "900003-SA", "Active", "01/31/28", "Pending"),
    ("Sales Agent", "900004-SA", "Active", "05/11/26", "Bexar"),  # May 11 expired; swapped Nov 5 valid
    ("Broker Individual", "900005", "Active", "", "Dallas"),  # unknown expiry
    ("Sales Agent", "900006-SA", "Probation - Active", "11/05/27", "Collin"),
    ("Broker Individual", "900007", "Probation - Active", "03/31/28", "Tarrant"),
    ("Sales Agent", "900008-SA", "Inactive", "06/30/27", ""),
    ("Sales Agent", "900009-SA", "Suspended", "01/31/27", "El Paso"),
    # Companies are not people. Expired-Active, so without the PEOPLE filter it would always be kept.
    ("Broker LLC", "900010", "Active", "05/12/26", "Travis"),
    ("Broker Corporation", "900011", "Active", "10/31/27", "Harris"),
]


def _write_raw(path: Path, rows: list[tuple[str, str, str, str, str]]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(RAW_FIELDS)
        w.writerows(rows)
    return path


def _row(status: str, expiration: str) -> dict[str, str]:
    return {"Status": status, "License Expiration Date": expiration, "License Type": "Sales Agent"}


@pytest.fixture
def small(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, str]]:
    monkeypatch.setattr(build_brokers, "N_VALID", 3)
    monkeypatch.setattr(build_brokers, "N_INVALID", 6)
    return sample_trec(str(_write_raw(tmp_path / "raw.csv", SMALL_RAW)))


def _by_number(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {r["license_number"]: r for r in rows}


def _loaded(tmp_path: Path, rows: list[dict[str, str]]) -> dict[str, Broker]:
    """Write sampled rows the way main does and load them back as Brokers, keyed by license number."""
    path = tmp_path / "sampled.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=build_brokers.FIELDS)
        w.writeheader()
        w.writerows(rows)
    return {b.license_number: b for b in load_brokers(path)}


def test_parse_date_is_month_first() -> None:
    """gate-41: reading MM/DD/YY as DD/MM/YY would make an expired 05/11/26 look valid till Nov 5.

    Both directions are pinned: 05/11/26 (May 11) is expired at AS_OF, 12/01/26 (Dec 1) is not.
    """
    assert parse_date("05/11/26") == date(2026, 5, 11)
    assert parse_date("12/01/26") == date(2026, 12, 1)
    assert is_valid(_row("Active", "05/11/26")) is False
    assert is_valid(_row("Active", "12/01/26")) is True


def test_parse_date_unreadable_is_none() -> None:
    """gate-42: defaulting an unreadable expiry to 2099-12-31 would make unknown licenses look valid."""
    for bad in ("not-a-date", "", "   ", "13/45/26"):
        assert parse_date(bad) is None, bad
        assert is_valid(_row("Active", bad)) is False, bad


def test_build_keeps_compound_status_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """gate-43: collapsing 'Probation - Active' to 'Active' would let probationary licensees pass the gate.

    Runs main end to end into tmp_path; both in-date probation rows must keep their status and fail.
    """
    monkeypatch.setattr(build_brokers, "ROOT", tmp_path)
    monkeypatch.setattr(build_brokers, "N_VALID", 3)
    monkeypatch.setattr(build_brokers, "N_INVALID", 6)
    _write_fictional(tmp_path)
    build_brokers.main(str(_write_raw(tmp_path / "raw.csv", SMALL_RAW)))
    brokers = load_brokers(tmp_path / "data/brokers.csv")
    probation = [b for b in brokers if b.license_number in ("900006-SA", "900007")]
    assert len(probation) == 2
    for b in probation:
        assert b.license_status == "Probation - Active"
        assert b.license_expiration is not None and b.license_expiration > AS_OF
        assert is_licensed(b, "TX", AS_OF) is False
    assert "wrote 9 TREC rows (3 valid as of 2026-10-20)" in capsys.readouterr().out


def test_built_row_expiration_is_iso(small: list[dict[str, str]], tmp_path: Path) -> None:
    """gate-44: writing the expiry as %Y-%d-%m would turn an expired May 11 into a valid Nov 5."""
    rows = _by_number(small)
    assert rows["900004-SA"]["license_expiration"] == "2026-05-11"
    assert rows["900001-SA"]["license_expiration"] == "2026-12-01"
    assert rows["900002"]["license_expiration"] == "2027-11-05"
    brokers = _loaded(tmp_path, small)
    assert is_licensed(brokers["900004-SA"], "TX", AS_OF) is False
    assert is_licensed(brokers["900001-SA"], "TX", AS_OF) is True


def test_built_row_unknown_expiration_blank(small: list[dict[str, str]], tmp_path: Path) -> None:
    """gate-45: writing a missing expiry as AS_OF would make an unknown-expiry licensee pass on AS_OF."""
    row = _by_number(small)["900005"]
    assert row["license_status"] == "Active"
    assert row["license_expiration"] == ""
    b = _loaded(tmp_path, small)["900005"]
    assert b.license_expiration is None
    assert is_licensed(b, "TX", AS_OF) is False


def test_small_build_exact_output(small: list[dict[str, str]], tmp_path: Path) -> None:
    """gates 41-45/49/50 together: every person row is chosen here, so the whole output is pinned.

    Statuses verbatim, ISO or blank expirations, 'Pending' county blanked, and exactly the three
    in-date Active rows licensed.
    """
    got = {
        r["license_number"]: (r["license_type"], r["license_status"], r["license_expiration"], r["county"])
        for r in small
    }
    assert got == {
        "900001-SA": ("Sales Agent", "Active", "2026-12-01", "Travis"),
        "900002": ("Broker Individual", "Active", "2027-11-05", "Harris"),
        "900003-SA": ("Sales Agent", "Active", "2028-01-31", ""),
        "900004-SA": ("Sales Agent", "Active", "2026-05-11", "Bexar"),
        "900005": ("Broker Individual", "Active", "", "Dallas"),
        "900006-SA": ("Sales Agent", "Probation - Active", "2027-11-05", "Collin"),
        "900007": ("Broker Individual", "Probation - Active", "2028-03-31", "Tarrant"),
        "900008-SA": ("Sales Agent", "Inactive", "2027-06-30", ""),
        "900009-SA": ("Sales Agent", "Suspended", "2027-01-31", "El Paso"),
    }
    assert sorted(r["broker_id"] for r in small) == [f"tx-{i:03d}" for i in range(1, 10)]
    licensed = {n for n, b in _loaded(tmp_path, small).items() if is_licensed(b, "TX", AS_OF)}
    assert licensed == {"900001-SA", "900002", "900003-SA"}


def test_build_excludes_non_person_license_types(small: list[dict[str, str]]) -> None:
    """gate-49: without the PEOPLE filter, company licenses would enter the sample as brokers.

    The expired 'Broker LLC' row would always be kept in the Active-expired stratum, so the
    check does not depend on a random draw.
    """
    assert {r["license_type"] for r in small} <= PEOPLE
    assert "900010" not in _by_number(small)
    assert "900011" not in _by_number(small)
    assert len(small) == 9


def test_build_labels_trec_rows_with_trec_source(small: list[dict[str, str]]) -> None:
    """gate-50: labeling TREC rows 'fictional' would hide the real sample from real-row checks."""
    assert [r["source"] for r in small] == [SOURCE] * len(small)
    assert all(r["license_state"] == "TX" for r in small)
    assert all(r["name"] == f"TX Licensee {r['broker_id'][3:]}" for r in small)


def _write_fictional(tmp_path: Path) -> list[str]:
    """Write a header and two fictional rows into tmp_path/data, independent of the real file."""
    lines = [
        (REPO_ROOT / "data/fictional_brokers.csv").read_text().splitlines()[0],
        "fx-bb-1,Fictional Broker BB-1,fictional,FL,FICT-FL-7001,Broker Individual,Active,2028-01-31,,,,,,",
        "fx-bb-2,Fictional Broker BB-2,fictional,AZ,FICT-AZ-7002,Sales Agent,Active,2028-01-31,,,,,,",
    ]
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data/fictional_brokers.csv").write_text("\n".join(lines) + "\n")
    return lines


def _big_raw() -> list[tuple[str, str, str, str, str]]:
    """160 valid, 30 Active-but-expired, 80 other invalid (default N_VALID=150, N_INVALID=50)."""
    rows: list[tuple[str, str, str, str, str]] = []
    for i in range(160):
        rows.append(("Sales Agent", f"91{i:04d}-SA", "Active", f"{1 + i % 12:02d}/28/{27 + i % 2}", "Travis"))
    for i in range(30):
        rows.append(("Broker Individual", f"92{i:04d}", "Active", f"{1 + i % 9:02d}/15/26", "Harris"))
    statuses = ("Inactive", "Probation - Active", "Suspended", "Expired less than 6 months")
    for i in range(80):
        rows.append(("Sales Agent", f"93{i:04d}-SA", statuses[i % 4], f"{1 + i % 12:02d}/10/27", "Bexar"))
    return rows


@pytest.fixture
def big_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[list[dict[str, str]], int, str]:
    monkeypatch.setattr(build_brokers, "ROOT", tmp_path)
    n_fictional = len(_write_fictional(tmp_path)) - 1
    build_brokers.main(str(_write_raw(tmp_path / "raw.csv", _big_raw())))
    with open(tmp_path / "data/brokers.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows, n_fictional, capsys.readouterr().out


def test_build_strata_counts(big_build: tuple[list[dict[str, str]], int, str], tmp_path: Path) -> None:
    """gate-47: ignoring expiry in is_valid would draw expired Active rows into the 150-valid stratum.

    Exact counts: 150 Active in-date, 25 Active expired (capped at N_INVALID // 2), 25 others,
    and loading the CSV the gate licenses exactly the 150.
    """
    rows, n_fictional, out = big_build
    trec = [r for r in rows if r["source"] == SOURCE]
    active_ok = [r for r in trec if r["license_status"] == "Active" and r["license_expiration"] >= AS_OF.isoformat()]
    active_expired = [
        r for r in trec if r["license_status"] == "Active" and r["license_expiration"] < AS_OF.isoformat()
    ]
    other = [r for r in trec if r["license_status"] != "Active"]
    assert (len(active_ok), len(active_expired), len(other)) == (150, 25, 25)
    assert all(r["license_number"].startswith("91") for r in active_ok)
    assert all(r["license_number"].startswith("92") for r in active_expired)
    brokers = [b for b in load_brokers(tmp_path / "data/brokers.csv") if b.source == SOURCE]
    assert sum(is_licensed(b, "TX", AS_OF) for b in brokers) == 150
    assert f"wrote 200 TREC rows (150 valid as of 2026-10-20) + {n_fictional} fictional" in out


def test_build_output_has_unique_ids(big_build: tuple[list[dict[str, str]], int, str]) -> None:
    """gate-48: writing some TREC rows twice would duplicate broker_ids, so a broker could match twice."""
    rows, n_fictional, _ = big_build
    assert len(rows) == 200 + n_fictional
    assert len([r for r in rows if r["source"] == SOURCE]) == 200
    ids = [r["broker_id"] for r in rows]
    assert len(set(ids)) == len(ids)
    assert len({r["license_number"] for r in rows}) == len(rows)
    assert sorted(i for i in ids if i.startswith("tx-")) == [f"tx-{i:03d}" for i in range(1, 201)]
