"""One-time build of data/brokers.csv from the raw TREC download plus data/fictional_brokers.csv.

Run: python -m deal_board.cli.build_brokers data/raw/<TREC file>.csv

The raw file stays in data/raw/ (gitignored). The output keeps only license fields and county.
Names are replaced with "TX Licensee 001" and so on. License numbers are kept, and they are
public lookups on TREC's site, so this removes names; it does not anonymize.
"""

import csv
import random
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


def parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%m/%d/%y").date()
    except ValueError:
        return None  # an unreadable date is treated as no date, which fails the license check


def is_valid(row: dict[str, str]) -> bool:
    exp = parse_date(row["License Expiration Date"])
    return row["Status"] == "Active" and exp is not None and exp >= AS_OF


def sample_trec(raw_path: str) -> list[dict[str, str]]:
    with open(raw_path, newline="", encoding="utf-8-sig") as f:
        people = [r for r in csv.DictReader(f) if r["License Type"] in PEOPLE]
    valid = [r for r in people if is_valid(r)]
    invalid = [r for r in people if not is_valid(r)]
    # "Active but expired" is rare and the most useful test of the filter, so keep every one.
    active_expired = [r for r in invalid if r["Status"] == "Active"][:N_INVALID]
    other_invalid = [r for r in invalid if r["Status"] != "Active"]

    rng = random.Random(SEED)
    chosen = rng.sample(valid, N_VALID) + active_expired + rng.sample(other_invalid, N_INVALID - len(active_expired))
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
                "license_type": r["License Type"].strip(),
                "license_status": r["Status"].strip(),  # copied verbatim, never edited
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
