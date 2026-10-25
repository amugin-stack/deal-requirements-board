"""Properties of data/brokers.csv that must hold before the matcher can trust it."""

import re
from datetime import date

from deal_board.brokers import load_brokers

BROKERS = load_brokers()
REAL = [b for b in BROKERS if not b.is_fictional]
FICTIONAL = [b for b in BROKERS if b.is_fictional]
AS_OF = date(2026, 10, 20)


def test_every_row_is_labeled_real_or_fictional() -> None:
    assert all(b.source == "fictional" or b.source.startswith("trec_") for b in BROKERS)
    assert all(b.license_number.startswith("FICT-") for b in FICTIONAL)
    assert all(b.name.startswith("Fictional ") for b in FICTIONAL)


def test_real_names_are_removed() -> None:
    assert all(re.fullmatch(r"TX Licensee \d{3}", b.name) for b in REAL)


def test_real_rows_have_no_invented_profile_fields() -> None:
    for b in REAL:
        assert not (b.markets or b.property_types or b.transaction_types), b.broker_id
        assert b.min_deal_usd is None and b.max_deal_usd is None, b.broker_id


def test_sample_is_stratified() -> None:
    valid = [b for b in REAL if b.license_status == "Active" and b.license_expiration and b.license_expiration >= AS_OF]
    assert len(REAL) == 200 and len(valid) == 150
    # The hardest case for a status-only filter must be in the sample.
    assert any(
        b.license_status == "Active" and b.license_expiration is not None and b.license_expiration < AS_OF for b in REAL
    )
