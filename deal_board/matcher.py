"""Match a deal requirement to brokers who may legally take it, then rank by fit.

Licensing is a hard gate, not a score: a broker who can't be verified as licensed
in the deal's state is never returned, however good the fit. Ranking only orders
the brokers who pass the gate, and every point comes with a plain-English reason.
"""

import re
from datetime import date

from pydantic import BaseModel

from deal_board.brokers import TX_CITY_TO_COUNTY, Broker
from deal_board.cities import market_key
from deal_board.schema import DealRequirement


class Match(BaseModel):
    broker: Broker
    score: int
    reasons: list[str]  # one line per point source, e.g. "+3 specializes in retail"; 0-point notes allowed


class MatchResult(BaseModel):
    matches: list[Match]  # ranked, at most `limit`
    excluded_for_licensing: int  # brokers rejected by is_licensed for this deal


# A scoring rule's contribution: points plus the sentence that justifies them.
Point = tuple[int, str]

SALES_AGENT_NOTE = "Referral fee must be paid through sponsoring broker"

# The county map keyed the same way as the deal's city, so "Ft Worth" finds Tarrant.
_TX_COUNTY_BY_KEY = {market_key(city, "TX"): county for city, county in TX_CITY_TO_COUNTY.items()}


# A real license record's status is only as current as its download. Past this many days after the
# snapshot, a suspension or revocation since then could be missed, so the gate stops vouching for
# the row. 90, not 30: TREC status changes slowly, and the red-team suite pins an as_of of
# 2026-11-30 against the 2026-10-20 snapshot. Re-download the data to restore the rows.
MAX_SNAPSHOT_AGE_DAYS = 90

_TREC_PREFIX = "trec_"
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _snapshot_is_fresh(broker: Broker, as_of: date) -> bool:
    """True unless the row is a 'trec_<YYYY-MM-DD>' record too old, or too garbled, to trust.

    Fictional rows carry no snapshot and are unaffected. An unparseable trec_ date fails
    closed like any other unknown.
    """
    if not broker.source.startswith(_TREC_PREFIX):
        return True
    stamp = broker.source.removeprefix(_TREC_PREFIX)
    if not _ISO_DATE.fullmatch(stamp):
        return False
    try:
        snapshot = date.fromisoformat(stamp)
    except ValueError:  # e.g. 2026-02-30
        return False
    return (as_of - snapshot).days <= MAX_SNAPSHOT_AGE_DAYS


def is_licensed(broker: Broker, state: str | None, as_of: date) -> bool:
    """Fail closed: any unknown (no state, no expiration) means we can't vouch for the broker.

    Status must be exactly "Active" because TREC uses compound statuses such as
    "Probation - Active" that carry restrictions we don't want to route referrals into.
    A real (trec_) record also needs a snapshot no older than MAX_SNAPSHOT_AGE_DAYS at
    `as_of`, since its status is frozen at download time while `as_of` moves on.
    `as_of` is passed in so results are reproducible and never depend on the clock.
    """
    return (
        _snapshot_is_fresh(broker, as_of)
        and state is not None
        and broker.license_state == state
        and broker.license_status == "Active"
        and broker.license_expiration is not None
        and broker.license_expiration >= as_of
    )


def _property_point(req: DealRequirement, broker: Broker) -> Point | None:
    if req.property_type is not None and req.property_type in broker.property_types:
        return 3, f"+3 specializes in {req.property_type.value}"
    return None


def _transaction_point(req: DealRequirement, broker: Broker) -> Point | None:
    if req.transaction_type is not None and req.transaction_type in broker.transaction_types:
        return 2, f"+2 handles {req.transaction_type.value} deals"
    return None


def _market_point(req: DealRequirement, broker: Broker) -> Point | None:
    """A listed market wins; otherwise fall back to county, since TREC rows only record a county.

    The county map is Texas-only, so it is consulted only for TX deals, and a city
    missing from the map must not "match" a broker whose county is also unknown.
    Both sides go through cities.market_key, so "Austin, TX", "Ft. Lauderdale" or a
    non-breaking space still match, and any city the eval scores correct can earn the
    point. A market written "City, ST" counts only when ST is the deal's state, so a NY
    broker listing "Hoboken, NJ" gets no local-fit point on a deal routed to NY.
    """
    if req.city is None:
        return None
    city = market_key(req.city, req.state)
    if city in {market_key(m, req.state) for m in broker.markets}:
        return 2, f"+2 serves {req.city}"
    county = _TX_COUNTY_BY_KEY.get(city) if req.state == "TX" else None
    if county is not None and county == broker.county:
        return 2, f"+2 based in {county} County, which covers {req.city}"
    return None


def _deal_size_point(req: DealRequirement, broker: Broker) -> Point | None:
    """Only a fully known range counts; an open-ended range says nothing about fit."""
    lo, hi, value = broker.min_deal_usd, broker.max_deal_usd, req.deal_value_usd
    if value is None or lo is None or hi is None or not lo <= value <= hi:
        return None
    return 1, f"+1 deal size ${value:,} is within their ${lo:,}\u2013${hi:,} range"


RULES = (_property_point, _transaction_point, _market_point, _deal_size_point)


def _score(req: DealRequirement, broker: Broker) -> tuple[Match, int]:
    """Return the match and how many rules earned points (the first tie-breaker)."""
    points = [p for rule in RULES if (p := rule(req, broker)) is not None]
    reasons = [text for _, text in points]
    if broker.license_type == "Sales Agent":
        reasons.append(SALES_AGENT_NOTE)
    return Match(broker=broker, score=sum(n for n, _ in points), reasons=reasons), len(points)


def match(req: DealRequirement, brokers: list[Broker], as_of: date, limit: int = 5) -> MatchResult:
    """Gate on licensing first, then rank; licensed brokers with no fit still appear, last.

    A non-deal post isn't checked against anyone, so nobody counts as excluded. A deal
    with no state can't be checked either, so every broker is excluded rather than guessed.
    Ties break on how many rules matched, then broker_id, so output is deterministic.
    """
    if not req.is_deal_request:
        return MatchResult(matches=[], excluded_for_licensing=0)
    licensed = [b for b in brokers if is_licensed(b, req.state, as_of)]
    scored = sorted(
        (_score(req, b) for b in licensed),
        key=lambda pair: (-pair[0].score, -pair[1], pair[0].broker.broker_id),
    )
    return MatchResult(
        matches=[m for m, _ in scored[: max(limit, 0)]],
        excluded_for_licensing=len(brokers) - len(licensed),
    )
