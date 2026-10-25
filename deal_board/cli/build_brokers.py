"""One-time build of data/brokers.csv from the raw TREC download plus data/fictional_brokers.csv.

Run: python -m deal_board.cli.build_brokers data/raw/<TREC file>.csv

The raw file stays in data/raw/ (gitignored). The output keeps only license fields and county.
Names are replaced with "TX Licensee 001" and so on. License numbers are kept, and they are
public lookups on TREC's site, so this removes names; it does not anonymize.
"""

import csv
import random
import re
import sys
from datetime import date, datetime

from deal_board.brokers import LICENSE_TYPES
from deal_board.paths import REPO_ROOT

SEED = 20261020
AS_OF = date(2026, 10, 20)  # download date; used only to pick the strata, not by the matcher
N_VALID, N_INVALID = 150, 50
PEOPLE = frozenset(LICENSE_TYPES)  # companies aren't people to match with; Broker accepts only these
ROOT = REPO_ROOT
FIELDS = [
    "broker_id",
    "name",
    "source",
    "license_state",
    "license_number",
    "license_type",
    "license_status",
    "license_expiration",
    "county",
    "markets",
    "property_types",
    "transaction_types",
    "min_deal_usd",
    "max_deal_usd",
]


# The raw columns we classify on and write out. Trimmed once on read (see _trimmed).
_TRIMMED_COLUMNS = ("Status", "License Expiration Date", "License Type")
_FOUR_DIGIT_YEAR = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")


def parse_date(s: str) -> date | None:
    """Parse TREC's MM/DD/YY expiration date; None if it is blank or unreadable.

    %y uses Python's pivot: 69-99 become 1969-1999 and 00-68 become 2000-2068, so a
    placeholder like 12/31/90 is a past date (expired), as TREC means it. A 4-digit year
    raises instead of becoming None: it means TREC changed its format, and silently
    reading every date as unknown would mark the whole download invalid.
    """
    s = s.strip()
    if _FOUR_DIGIT_YEAR.fullmatch(s):
        raise ValueError(f"expected a 2-digit year (MM/DD/YY), got {s!r}: has TREC changed its date format?")
    try:
        return datetime.strptime(s, "%m/%d/%y").date()
    except ValueError:
        return None  # an unreadable date is treated as no date, which fails the license check


def _trimmed(row: dict[str, str]) -> dict[str, str]:
    """Strip the classified columns once, so the value we classify on is the value we write."""
    return {**row, **{k: row[k].strip() for k in _TRIMMED_COLUMNS}}


def is_valid(row: dict[str, str]) -> bool:
    """Expects a _trimmed row."""
    exp = parse_date(row["License Expiration Date"])
    return row["Status"] == "Active" and exp is not None and exp >= AS_OF


def sample_trec(raw_path: str) -> list[dict[str, str]]:
    with open(raw_path, newline="", encoding="utf-8-sig") as f:
        people = [t for r in csv.DictReader(f) if (t := _trimmed(r))["License Type"] in PEOPLE]
    valid = [r for r in people if is_valid(r)]
    invalid = [r for r in people if not is_valid(r)]
    # "Active but expired" is rare and the most useful test of the filter, so keep every one up
    # to half the invalid stratum; past that, a random half, so other invalid statuses
    # (Inactive, Suspended, Probation) stay represented.
    active_expired = [r for r in invalid if r["Status"] == "Active"]
    other_invalid = [r for r in invalid if r["Status"] != "Active"]
    if len(valid) < N_VALID:
        raise ValueError(f"need {N_VALID} valid (Active, unexpired as of {AS_OF}) rows, found {len(valid)}")

    rng = random.Random(SEED)
    chosen_valid = rng.sample(valid, N_VALID)
    # Sample only when over the cap, so a download under it draws the same rows as before.
    if len(active_expired) > N_INVALID // 2:
        active_expired = rng.sample(active_expired, N_INVALID // 2)
    n_other = N_INVALID - len(active_expired)
    if len(other_invalid) < n_other:
        raise ValueError(f"need {n_other} other invalid rows (not Active), found {len(other_invalid)}")
    chosen = chosen_valid + active_expired + rng.sample(other_invalid, n_other)
    rng.shuffle(chosen)  # so the licensee number doesn't reveal which group a row came from

    rows: list[dict[str, str]] = []
    for i, r in enumerate(chosen, 1):
        exp = parse_date(r["License Expiration Date"])
        county = r["County"].strip()
        rows.append(
            {
                "broker_id": f"tx-{i:03d}",
                "name": f"TX Licensee {i:03d}",
                "source": f"trec_{AS_OF.isoformat()}",
                "license_state": "TX",
                "license_number": r["License Number"].strip(),
                "license_type": r["License Type"],
                "license_status": r["Status"],  # trimmed of surrounding whitespace, otherwise verbatim
                "license_expiration": exp.isoformat() if exp else "",
                "county": "" if county in ("", "Pending") else county,
                # TREC has no specialty or deal-size data. Left blank rather than invented.
                "markets": "",
                "property_types": "",
                "transaction_types": "",
                "min_deal_usd": "",
                "max_deal_usd": "",
            }
        )
    return rows


def main(raw_path: str) -> None:
    real = sample_trec(raw_path)
    with open(ROOT / "data/fictional_brokers.csv", newline="") as f:
        fictional = list(csv.DictReader(f))
    with open(ROOT / "data/brokers.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(real + fictional)
    n_ok = sum(1 for r in real if r["license_status"] == "Active" and r["license_expiration"] >= AS_OF.isoformat())
    print(f"wrote {len(real)} TREC rows ({n_ok} valid as of {AS_OF}) + {len(fictional)} fictional")


def run() -> None:
    """Console-script entry point (`deal-build-brokers <raw TREC csv>`)."""
    main(sys.argv[1])


if __name__ == "__main__":
    run()
