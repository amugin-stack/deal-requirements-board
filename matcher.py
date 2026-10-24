"""Match a deal requirement to brokers who may legally take it, then rank by fit.

Licensing is a hard gate, not a score: a broker who can't be verified as licensed
in the deal's state is never returned, however good the fit. Ranking only orders
the brokers who pass the gate, and every point comes with a plain-English reason.
"""
from datetime import date

from pydantic import BaseModel

from brokers import Broker
from schema import DealRequirement


class Match(BaseModel):
    broker: Broker
    score: int
    reasons: list[str]  # one line per point source, e.g. "+3 specializes in retail"; 0-point notes allowed


class MatchResult(BaseModel):
    matches: list[Match]  # ranked, at most `limit`
    excluded_for_licensing: int  # brokers rejected by is_licensed for this deal


def is_licensed(broker: Broker, state: str | None, as_of: date) -> bool:
    raise NotImplementedError


def match(req: DealRequirement, brokers: list[Broker], as_of: date, limit: int = 5) -> MatchResult:
    raise NotImplementedError
