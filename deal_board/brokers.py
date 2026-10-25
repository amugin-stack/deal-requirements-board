"""Broker records as typed objects, loaded from data/brokers.csv.

This module only loads data. The compliance filter and scoring live in matcher.py.
"""

import csv
from datetime import date
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, field_validator

from deal_board.paths import DATA_DIR
from deal_board.schema import PropertyType, State, TransactionType

BROKERS_CSV = DATA_DIR / "brokers.csv"

# The license types we match, spelled exactly as TREC writes them. A closed set, because the
# matcher adds the sales-agent referral note by exact comparison: a padded or misspelled
# "sales agent" must fail at load rather than reach a poster without that note.
LicenseType = Literal["Broker Individual", "Sales Agent"]
LICENSE_TYPES: tuple[LicenseType, ...] = get_args(LicenseType)
SALES_AGENT: LicenseType = "Sales Agent"

# Deal posts name cities; TREC records counties. Each city maps to its main county only.
# Austin, Houston, Dallas, and San Antonio spill into neighboring counties; we ignore that.
# The lookup is used only when the deal's state is TX.
TX_CITY_TO_COUNTY = {
    "austin": "Travis",
    "houston": "Harris",
    "dallas": "Dallas",
    "san antonio": "Bexar",
    "fort worth": "Tarrant",
    "arlington": "Tarrant",
    "plano": "Collin",
    "el paso": "El Paso",
}


class Broker(BaseModel):
    broker_id: str
    name: str
    source: str  # "trec_<date>" for a real license record, "fictional" otherwise
    license_state: State
    license_number: str
    license_type: LicenseType
    license_status: str  # verbatim from the source; never edited
    license_expiration: date | None  # None means unknown, which the matcher treats as not licensed
    county: str | None
    markets: list[str]  # cities served; empty for TREC rows (TREC doesn't say)
    property_types: list[PropertyType]
    transaction_types: list[TransactionType]
    min_deal_usd: int | None
    max_deal_usd: int | None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v

    @field_validator("markets", "property_types", "transaction_types", mode="before")
    @classmethod
    def _split_list(cls, v: object) -> object:
        return [x.strip() for x in v.split(";") if x.strip()] if isinstance(v, str) else (v or [])

    @property
    def is_fictional(self) -> bool:
        return self.source == "fictional"


def _is_blank(row: dict[str | None, str | list[str] | None]) -> bool:
    """A row of only commas/whitespace (e.g. a trailing line from a spreadsheet) carries no broker."""
    return all(v is None or (isinstance(v, str) and not v.strip()) for v in row.values())


def load_brokers(path: Path = BROKERS_CSV) -> list[Broker]:
    """Load every broker row; a bad row fails the whole load, naming its line and broker_id.

    utf-8-sig, because a spreadsheet-saved CSV starts with a BOM that would otherwise turn
    the first header into '\ufeffbroker_id'. Blank rows are skipped rather than failing.
    A bad row still raises: silently dropping a broker would hide a data error.
    """
    brokers: list[Broker] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if _is_blank(row):
                continue
            try:
                brokers.append(Broker.model_validate(row))
            except ValueError as e:  # pydantic.ValidationError subclasses ValueError
                raise ValueError(
                    f"{path}: bad broker row at CSV line {reader.line_num} (broker_id={row.get('broker_id')!r}): {e}"
                ) from e
    return brokers
