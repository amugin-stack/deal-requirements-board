"""Match a deal requirement to brokers who may legally take it, then rank by fit.

Licensing is a hard gate, not a score: a broker who can't be verified as licensed
in the deal's state is never returned, however good the fit. Ranking only orders
the brokers who pass the gate, and every point comes with a plain-English reason.
"""

from datetime import date

from pydantic import BaseModel

from deal_board.brokers import TX_CITY_TO_COUNTY, Broker
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


def is_licensed(broker: Broker, state: str | None, as_of: date) -> bool:
    """Fail closed: any unknown (no state, no expiration) means we can't vouch for the broker.

    Status must be exactly "Active" because TREC uses compound statuses such as
    "Probation - Active" that carry restrictions we don't want to route referrals into.
    `as_of` is passed in so results are reproducible and never depend on the clock.
    """
    return (
        state is not None
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
    """
    if req.city is None:
        return None
    city = req.city.strip().lower()
    if city in {m.strip().lower() for m in broker.markets}:
        return 2, f"+2 serves {req.city}"
    county = TX_CITY_TO_COUNTY.get(city) if req.state == "TX" else None
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
