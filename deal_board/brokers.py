"""Broker records as typed objects, loaded from data/brokers.csv.

This module only loads data. The compliance filter and scoring live in matcher.py.
"""

import csv
from datetime import date
from pathlib import Path

from pydantic import BaseModel, field_validator

from deal_board.paths import DATA_DIR
from deal_board.schema import PropertyType, State, TransactionType

BROKERS_CSV = DATA_DIR / "brokers.csv"

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
    license_type: str  # "Broker Individual" or "Sales Agent"
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


def load_brokers(path: Path = BROKERS_CSV) -> list[Broker]:
    with open(path, newline="") as f:
        return [Broker.model_validate(row) for row in csv.DictReader(f)]
