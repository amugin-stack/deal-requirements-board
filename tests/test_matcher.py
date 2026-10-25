"""Behavior of matcher.match: scoring, ranking, and the licensing gate, from the contract only.

These tests exist so the matcher can be checked against its written contract without
reading its code: every point must be earned by a rule and explained by a reason, the
ranking must be deterministic, and only licensed brokers may come back. Small hand-built
brokers isolate each rule; the trap rows in data/brokers.csv check the real data.
"""

import re
from datetime import date, timedelta

import pytest

from deal_board.brokers import TX_CITY_TO_COUNTY, Broker, load_brokers
from deal_board.matcher import Match, MatchResult, match
from deal_board.schema import DealRequirement, PropertyType, TransactionType

AS_OF = date(2026, 10, 20)
FAR_FUTURE = date(2030, 1, 1)
SALES_AGENT_NOTE = "Referral fee must be paid through sponsoring broker"
POINT_RE = re.compile(r"^\+(\d+)")

ALL_BROKERS = load_brokers()
BY_ID = {b.broker_id: b for b in ALL_BROKERS}


# ---------- helpers ----------


def make_broker(broker_id: str = "b-001", **overrides: object) -> Broker:
    """A licensed broker with an empty profile; overrides add the fields a test needs."""
    fields: dict[str, object] = {
        "broker_id": broker_id,
        "name": f"Fictional Broker {broker_id}",
        "source": "fictional",
        "license_state": "TX",
        "license_number": f"FICT-{broker_id}",
        "license_type": "Broker Individual",
        "license_status": "Active",
        "license_expiration": FAR_FUTURE,
        "county": None,
        "markets": [],
        "property_types": [],
        "transaction_types": [],
        "min_deal_usd": None,
        "max_deal_usd": None,
    }
    fields.update(overrides)
    return Broker.model_validate(fields)


def make_req(**fields: object) -> DealRequirement:
    return DealRequirement.model_validate({"is_deal_request": True, **fields})


def point_total(reasons: list[str]) -> int:
    return sum(int(m.group(1)) for r in reasons if (m := POINT_RE.match(r)))


def point_amounts(reasons: list[str]) -> list[int]:
    return sorted(int(m.group(1)) for r in reasons if (m := POINT_RE.match(r)))


def only_match(req: DealRequirement, broker: Broker) -> Match:
    result = match(req, [broker], AS_OF)
    assert len(result.matches) == 1, result
    assert result.matches[0].broker.broker_id == broker.broker_id
    return result.matches[0]


def assert_reasons_explain_score(m: Match) -> None:
    assert point_total(m.reasons) == m.score, (m.score, m.reasons)


# ---------- scoring per rule ----------


def test_property_type_match_scores_3() -> None:
    b = make_broker(property_types=[PropertyType.retail, PropertyType.office])
    m = only_match(make_req(state="TX", property_type="retail"), b)
    assert m.score == 3
    assert point_amounts(m.reasons) == [3]


def test_property_type_mismatch_scores_0() -> None:
    b = make_broker(property_types=[PropertyType.office])
    m = only_match(make_req(state="TX", property_type="retail"), b)
    assert m.score == 0
    assert point_amounts(m.reasons) == []


def test_transaction_type_match_scores_2() -> None:
    b = make_broker(transaction_types=[TransactionType.exchange_1031])
    m = only_match(make_req(state="TX", transaction_type="1031_exchange"), b)
    assert m.score == 2
    assert point_amounts(m.reasons) == [2]


def test_market_match_by_city_scores_2() -> None:
    b = make_broker(markets=["Austin"])
    m = only_match(make_req(state="TX", city="Austin"), b)
    assert m.score == 2
    assert point_amounts(m.reasons) == [2]


@pytest.mark.parametrize("city", ["austin", "AUSTIN", "aUsTiN"])
def test_market_match_is_case_insensitive(city: str) -> None:
    b = make_broker(markets=["Austin"])
    m = only_match(make_req(state="TX", city=city), b)
    assert m.score == 2


def test_market_match_is_case_insensitive_on_broker_side() -> None:
    b = make_broker(license_state="FL", markets=["TAMPA"])
    m = only_match(make_req(state="FL", city="Tampa"), b)
    assert m.score == 2


@pytest.mark.parametrize("city,county", sorted(TX_CITY_TO_COUNTY.items()))
def test_tx_county_fallback_scores_2(city: str, county: str) -> None:
    b = make_broker(county=county)  # no markets listed, like a TREC row
    m = only_match(make_req(state="TX", city=city.title()), b)
    assert m.score == 2
    assert point_amounts(m.reasons) == [2]


def test_tx_county_fallback_wrong_county_scores_0() -> None:
    b = make_broker(county="Harris")
    m = only_match(make_req(state="TX", city="Austin"), b)
    assert m.score == 0


def test_tx_county_fallback_unknown_city_scores_0() -> None:
    b = make_broker(county="Travis")
    m = only_match(make_req(state="TX", city="Waco"), b)
    assert m.score == 0


def test_tx_unknown_city_does_not_match_broker_with_unknown_county() -> None:
    # Regression: an unmapped city and a blank county are both "unknown", not equal.
    # Without this, every TREC row with no county would earn +2 for any unmapped TX city.
    b = make_broker(county=None)
    m = only_match(make_req(state="TX", city="Waco"), b)
    assert m.score == 0 and m.reasons == []


def test_market_match_is_exact_not_substring() -> None:
    b = make_broker(license_state="IL", markets=["Arlington Heights"])
    m = only_match(make_req(state="IL", city="Arlington"), b)
    assert m.score == 0


def test_market_and_county_together_award_market_points_once() -> None:
    b = make_broker(markets=["Austin"], county="Travis")
    m = only_match(make_req(state="TX", city="Austin"), b)
    assert m.score == 2
    assert_reasons_explain_score(m)


def test_county_fallback_does_not_apply_outside_tx() -> None:
    # Arlington, VA is not Arlington, TX: a VA broker whose county field happens
    # to read "Tarrant" must not earn the market point through the TX lookup.
    b = make_broker(license_state="VA", county="Tarrant")
    m = only_match(make_req(state="VA", city="Arlington"), b)
    assert m.score == 0
    assert point_amounts(m.reasons) == []


@pytest.mark.parametrize("value", [500_000, 750_000, 1_000_000])
def test_deal_value_inside_inclusive_bounds_scores_1(value: int) -> None:
    b = make_broker(min_deal_usd=500_000, max_deal_usd=1_000_000)
    m = only_match(make_req(state="TX", deal_value_usd=value), b)
    assert m.score == 1
    assert point_amounts(m.reasons) == [1]


@pytest.mark.parametrize("value", [499_999, 1_000_001])
def test_deal_value_outside_bounds_scores_0(value: int) -> None:
    b = make_broker(min_deal_usd=500_000, max_deal_usd=1_000_000)
    m = only_match(make_req(state="TX", deal_value_usd=value), b)
    assert m.score == 0


@pytest.mark.parametrize("lo,hi", [(None, 1_000_000), (500_000, None), (None, None)])
def test_deal_value_with_unknown_bound_scores_0(lo: int | None, hi: int | None) -> None:
    b = make_broker(min_deal_usd=lo, max_deal_usd=hi)
    m = only_match(make_req(state="TX", deal_value_usd=750_000), b)
    assert m.score == 0


def test_full_fit_scores_8_with_one_reason_per_rule() -> None:
    b = make_broker(
        markets=["Austin"],
        property_types=[PropertyType.single_family],
        transaction_types=[TransactionType.buy],
        min_deal_usd=100_000,
        max_deal_usd=1_000_000,
    )
    req = make_req(
        state="TX",
        city="Austin",
        property_type="single_family",
        transaction_type="buy",
        deal_value_usd=650_000,
    )
    m = only_match(req, b)
    assert m.score == 8
    assert point_amounts(m.reasons) == [1, 2, 2, 3]
    assert_reasons_explain_score(m)


def test_fields_absent_in_requirement_give_no_points() -> None:
    b = make_broker(
        markets=["Austin"],
        county="Travis",
        property_types=[PropertyType.single_family],
        transaction_types=[TransactionType.buy],
        min_deal_usd=0,
        max_deal_usd=10_000_000,
    )
    m = only_match(make_req(state="TX"), b)  # only the state is known
    assert m.score == 0
    assert point_amounts(m.reasons) == []


def test_every_point_reason_starts_with_its_amount() -> None:
    b = make_broker(
        markets=["Austin"],
        property_types=[PropertyType.single_family],
        transaction_types=[TransactionType.buy],
        min_deal_usd=100_000,
        max_deal_usd=1_000_000,
    )
    req = make_req(
        state="TX",
        city="Austin",
        property_type="single_family",
        transaction_type="buy",
        deal_value_usd=650_000,
    )
    m = only_match(req, b)
    allowed = {"+1", "+2", "+3"}
    for r in m.reasons:
        mm = POINT_RE.match(r)
        if mm:
            assert mm.group(0) in allowed, r


# ---------- sales agents ----------


def test_sales_agent_gets_exact_referral_note_worth_0_points() -> None:
    b = make_broker(license_type="Sales Agent", property_types=[PropertyType.retail])
    m = only_match(make_req(state="TX", property_type="retail"), b)
    assert SALES_AGENT_NOTE in m.reasons
    assert m.score == 3  # the note adds nothing
    assert_reasons_explain_score(m)


def test_broker_individual_gets_no_referral_note() -> None:
    b = make_broker(property_types=[PropertyType.retail])
    m = only_match(make_req(state="TX", property_type="retail"), b)
    assert SALES_AGENT_NOTE not in m.reasons


def test_sales_agent_with_score_0_still_returned_with_note() -> None:
    b = make_broker(license_type="Sales Agent")
    m = only_match(make_req(state="TX"), b)
    assert m.score == 0
    assert m.reasons.count(SALES_AGENT_NOTE) == 1


def test_fx19_sales_agent_in_real_data_gets_note() -> None:
    req = make_req(
        state="CO", city="Denver", property_type="single_family", transaction_type="sell", deal_value_usd=715_000
    )
    result = match(req, ALL_BROKERS, AS_OF, limit=50)
    fx19 = next(m for m in result.matches if m.broker.broker_id == "fx-19")
    assert SALES_AGENT_NOTE in fx19.reasons
    assert fx19.score == 8
    assert_reasons_explain_score(fx19)


# ---------- ordering, ties, limit ----------


def test_orders_by_score_desc() -> None:
    low = make_broker("b-1", transaction_types=[TransactionType.buy])  # 2
    high = make_broker("b-2", property_types=[PropertyType.retail])  # 3
    top = make_broker("b-3", property_types=[PropertyType.retail], transaction_types=[TransactionType.buy])  # 5
    req = make_req(state="TX", property_type="retail", transaction_type="buy")
    result = match(req, [low, high, top], AS_OF)
    assert [m.broker.broker_id for m in result.matches] == ["b-3", "b-2", "b-1"]
    assert [m.score for m in result.matches] == [5, 3, 2]


def test_equal_score_breaks_tie_by_reason_count_desc() -> None:
    # Both score 3: one via property type (+3), the other via transaction (+2) and value (+1).
    one_reason = make_broker("b-1", property_types=[PropertyType.retail])
    two_reasons = make_broker("b-2", transaction_types=[TransactionType.buy], min_deal_usd=0, max_deal_usd=1_000_000)
    req = make_req(state="TX", property_type="retail", transaction_type="buy", deal_value_usd=500_000)
    result = match(req, [one_reason, two_reasons], AS_OF)
    assert [m.score for m in result.matches] == [3, 3]
    assert [m.broker.broker_id for m in result.matches] == ["b-2", "b-1"]


def test_reason_count_tie_break_ignores_sales_agent_note() -> None:
    # Regression: the tie-break counts point-earning reasons only. Counting the 0-point
    # referral note would rank a sales agent above an equally scored broker.
    agent = make_broker("a-agent", license_type="Sales Agent", property_types=[PropertyType.retail])
    broker = make_broker("z-broker", transaction_types=[TransactionType.buy], min_deal_usd=0, max_deal_usd=1_000_000)
    req = make_req(state="TX", property_type="retail", transaction_type="buy", deal_value_usd=500_000)
    result = match(req, [agent, broker], AS_OF)
    assert [m.score for m in result.matches] == [3, 3]
    assert [m.broker.broker_id for m in result.matches] == ["z-broker", "a-agent"]


def test_equal_score_and_reasons_breaks_tie_by_broker_id_asc() -> None:
    brokers = [make_broker(bid, property_types=[PropertyType.retail]) for bid in ["b-3", "b-1", "b-2"]]
    result = match(make_req(state="TX", property_type="retail"), brokers, AS_OF)
    assert [m.broker.broker_id for m in result.matches] == ["b-1", "b-2", "b-3"]


def test_ordering_does_not_depend_on_input_order() -> None:
    brokers = [make_broker(f"b-{i}", property_types=[PropertyType.retail]) for i in range(6)]
    req = make_req(state="TX", property_type="retail")
    a = [m.broker.broker_id for m in match(req, brokers, AS_OF, limit=10).matches]
    b = [m.broker.broker_id for m in match(req, list(reversed(brokers)), AS_OF, limit=10).matches]
    assert a == b


def test_limit_caps_matches() -> None:
    brokers = [make_broker(f"b-{i:02d}") for i in range(10)]
    req = make_req(state="TX")
    assert len(match(req, brokers, AS_OF).matches) == 5  # default
    assert len(match(req, brokers, AS_OF, limit=3).matches) == 3
    assert len(match(req, brokers, AS_OF, limit=50).matches) == 10


def test_limit_keeps_the_best_ranked() -> None:
    weak = [make_broker(f"a-{i}") for i in range(5)]  # score 0, ids sort first
    strong = make_broker("z-1", property_types=[PropertyType.retail])
    result = match(make_req(state="TX", property_type="retail"), [*weak, strong], AS_OF, limit=1)
    assert [m.broker.broker_id for m in result.matches] == ["z-1"]


def test_licensed_score_0_returned_last() -> None:
    zero = make_broker("a-0")
    fit = make_broker("z-9", property_types=[PropertyType.retail])
    result = match(make_req(state="TX", property_type="retail"), [zero, fit], AS_OF)
    assert [m.broker.broker_id for m in result.matches] == ["z-9", "a-0"]
    assert result.matches[-1].score == 0


# ---------- licensing gate and counts ----------


def test_missing_state_returns_nothing_and_excludes_all() -> None:
    brokers = [make_broker(f"b-{i}", markets=["Portland"]) for i in range(4)]
    result = match(make_req(state=None, city="Portland"), brokers, AS_OF)
    assert result.matches == []
    assert result.excluded_for_licensing == 4


def test_non_deal_returns_nothing_and_excludes_none() -> None:
    brokers = [make_broker(f"b-{i}") for i in range(4)]
    result = match(make_req(is_deal_request=False, state="TX"), brokers, AS_OF)
    assert result.matches == []
    assert result.excluded_for_licensing == 0


def test_non_deal_with_no_state_excludes_none() -> None:
    result = match(make_req(is_deal_request=False), [make_broker()], AS_OF)
    assert result == MatchResult(matches=[], excluded_for_licensing=0)


def test_excluded_counts_each_unlicensed_broker() -> None:
    brokers = [
        make_broker("ok-1"),
        make_broker("ok-2"),
        make_broker("x-state", license_state="OK"),
        make_broker("x-inactive", license_status="Inactive"),
        make_broker("x-probation", license_status="Probation - Active"),
        make_broker("x-expired", license_expiration=AS_OF - timedelta(days=1)),
        make_broker("x-unknown", license_expiration=None),
    ]
    result = match(make_req(state="TX"), brokers, AS_OF, limit=50)
    assert sorted(m.broker.broker_id for m in result.matches) == ["ok-1", "ok-2"]
    assert result.excluded_for_licensing == 5


def test_excluded_count_is_not_limited_by_limit() -> None:
    brokers = [make_broker(f"ok-{i}") for i in range(8)] + [
        make_broker(f"x-{i}", license_status="Inactive") for i in range(3)
    ]
    result = match(make_req(state="TX"), brokers, AS_OF, limit=2)
    assert len(result.matches) == 2
    assert result.excluded_for_licensing == 3


def test_empty_pool() -> None:
    result = match(make_req(state="TX"), [], AS_OF)
    assert result == MatchResult(matches=[], excluded_for_licensing=0)


# ---------- trap brokers from data/brokers.csv ----------


def returned_ids(req: DealRequirement) -> set[str]:
    return {m.broker.broker_id for m in match(req, ALL_BROKERS, AS_OF, limit=len(ALL_BROKERS)).matches}


def test_trap_fx03_inactive_perfect_fit_excluded() -> None:
    assert BY_ID["fx-03"].license_status == "Inactive"
    req = make_req(
        state="FL", city="Tampa", property_type="retail", transaction_type="1031_exchange", deal_value_usd=3_500_000
    )
    ids = returned_ids(req)
    assert "fx-03" not in ids
    assert "fx-01" in ids  # the licensed FL broker with the same niche


def test_trap_fx05_active_but_expired_excluded() -> None:
    assert BY_ID["fx-05"].license_status == "Active"
    expiration = BY_ID["fx-05"].license_expiration
    assert expiration is not None and expiration < AS_OF
    req = make_req(
        state="AZ", city="Phoenix", property_type="single_family", transaction_type="sell", deal_value_usd=425_000
    )
    ids = returned_ids(req)
    assert "fx-05" not in ids
    assert "fx-04" in ids


def test_trap_fx07_active_unknown_expiration_excluded() -> None:
    assert BY_ID["fx-07"].license_expiration is None
    req = make_req(state="GA", city="Atlanta", property_type="retail", transaction_type="lease")
    ids = returned_ids(req)
    assert "fx-07" not in ids
    assert "fx-06" in ids


def test_trap_fx25_suspended_excluded_and_kansas_city_wrong_state() -> None:
    assert BY_ID["fx-25"].license_status == "Suspended"
    req = make_req(state="KS", city="Kansas City", property_type="multifamily", transaction_type="buy")
    assert returned_ids(req) == set()
    # fx-24 serves Kansas City but is licensed in MO, not KS.
    req_mo = make_req(state="MO", city="Kansas City", property_type="multifamily", transaction_type="buy")
    assert "fx-24" in returned_ids(req_mo)


def test_wrong_state_portland_me_vs_or() -> None:
    req_me = make_req(state="ME", city="Portland", property_type="multifamily", transaction_type="sell")
    req_or = make_req(state="OR", city="Portland", property_type="condo_townhome", transaction_type="buy")
    assert returned_ids(req_me) == {"fx-21"}
    assert returned_ids(req_or) == {"fx-22"}


def test_arlington_va_does_not_return_tarrant_county_tx_brokers() -> None:
    ids = returned_ids(
        make_req(
            state="VA", city="Arlington", property_type="condo_townhome", transaction_type="buy", deal_value_usd=875_000
        )
    )
    assert ids == {"fx-26"}


def test_real_tx_rows_earn_county_point_for_austin() -> None:
    req = make_req(
        state="TX", city="Austin", property_type="single_family", transaction_type="buy", deal_value_usd=650_000
    )
    result = match(req, ALL_BROKERS, AS_OF, limit=len(ALL_BROKERS))
    assert result.matches, "expected licensed TX rows"
    for m in result.matches:
        b = m.broker
        assert b.license_state == "TX"
        expected = 2 if b.county == "Travis" else 0  # TREC rows have no other profile fields
        assert m.score == expected, (b.broker_id, m.reasons)
        assert_reasons_explain_score(m)


def test_all_matches_reasons_sum_to_score_on_real_data() -> None:
    req = make_req(
        state="TX",
        city="Houston",
        property_type="multifamily",
        transaction_type="1031_exchange",
        deal_value_usd=1_500_000,
    )
    for m in match(req, ALL_BROKERS, AS_OF, limit=len(ALL_BROKERS)).matches:
        assert_reasons_explain_score(m)


# ---------- no mutation, no invention ----------


def test_match_does_not_mutate_input_brokers() -> None:
    before = [b.model_dump() for b in ALL_BROKERS]
    req = make_req(
        state="TX", city="Austin", property_type="single_family", transaction_type="buy", deal_value_usd=650_000
    )
    match(req, ALL_BROKERS, AS_OF, limit=len(ALL_BROKERS))
    assert [b.model_dump() for b in ALL_BROKERS] == before


def test_returned_broker_data_equals_input_data() -> None:
    req = make_req(state="TX", city="Dallas")
    for m in match(req, ALL_BROKERS, AS_OF, limit=len(ALL_BROKERS)).matches:
        assert m.broker.model_dump() == BY_ID[m.broker.broker_id].model_dump()


def test_input_list_is_not_reordered_or_resized() -> None:
    brokers = [make_broker(f"b-{i}", property_types=[PropertyType.retail] if i % 2 else []) for i in range(6)]
    ids_before = [b.broker_id for b in brokers]
    match(make_req(state="TX", property_type="retail"), brokers, AS_OF, limit=2)
    assert [b.broker_id for b in brokers] == ids_before
