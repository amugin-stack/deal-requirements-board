"""Tests aimed at matcher bugs the earlier suite let through (licensing gate, reasons, tie-breaks).

All brokers are built here and are fictional; nothing reads data/.
"""

from datetime import date
from typing import Any

import pytest

from deal_board.brokers import Broker
from deal_board.matcher import is_licensed, match
from deal_board.schema import DealRequirement, PropertyType, TransactionType

AS_OF = date(2026, 10, 20)
FAR_FUTURE = date(2030, 12, 31)


def make_broker(broker_id: str, **overrides: object) -> Broker:
    """An Active, unexpired, fictional TX broker with an empty profile."""
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


@pytest.mark.parametrize("state", ["", "T", "X", "TXX", "tx", " TX"])
def test_is_licensed_rejects_partial_or_empty_state(state: str) -> None:
    """Guards against a substring state check (`state in license_state`).

    With that bug '' / 'T' / 'X' would pass the gate for a TX broker, routing a deal
    to a broker not licensed in its state. The gate must demand an exact match.
    """
    broker = make_broker("fx-gate-35")
    assert is_licensed(broker, "TX", AS_OF) is True
    assert is_licensed(broker, state, AS_OF) is False


@pytest.mark.parametrize("state", ["", "T", "X", "TXX"])
def test_match_excludes_tx_broker_for_partial_state(state: str) -> None:
    """Guards the same substring bug end to end: match() must exclude the TX broker.

    model_construct skips validation, standing in for any path that hands the matcher a
    requirement whose state isn't a clean two-letter code.
    """
    fields: dict[str, Any] = {
        "is_deal_request": True,
        "state": state,  # deliberately not a valid State: model_construct skips validation
        "city": None,
        "property_type": PropertyType.retail,
        "transaction_type": TransactionType.buy,
        "deal_value_usd": None,
        "timeline": None,
        "notes": None,
    }
    req = DealRequirement.model_construct(**fields)
    broker = make_broker("fx-gate-35", property_types=["retail"], transaction_types=["buy"])
    result = match(req, [broker], AS_OF)
    assert result.matches == []
    assert result.excluded_for_licensing == 1


def test_transaction_reason_names_the_transaction_type_lease() -> None:
    """Guards against the reason dropping the type ('+2 handles deals').

    The reason is the user-facing explanation; without the type it no longer says what matched.
    """
    broker = make_broker("fx-match-28", transaction_types=["lease"])
    req = DealRequirement.model_validate({"is_deal_request": True, "state": "TX", "transaction_type": "lease"})
    result = match(req, [broker], AS_OF)
    assert [m.reasons for m in result.matches] == [["+2 handles lease deals"]]


@pytest.mark.parametrize("ttype", list(TransactionType))
def test_transaction_reason_names_the_transaction_type(ttype: TransactionType) -> None:
    """Guards the same reason-text bug for every TransactionType, asserting the exact string."""
    broker = make_broker("fx-match-28", transaction_types=[ttype.value])
    req = DealRequirement.model_validate({"is_deal_request": True, "state": "TX", "transaction_type": ttype.value})
    result = match(req, [broker], AS_OF)
    assert len(result.matches) == 1
    assert result.matches[0].score == 2
    assert result.matches[0].reasons == [f"+2 handles {ttype.value} deals"]


@pytest.mark.parametrize("reverse", [False, True])
def test_tie_break_uses_broker_id_not_name(reverse: bool) -> None:
    """Guards against the final tie-break using broker name instead of broker_id.

    Names sort opposite to ids here ('Zeta' has the lower id), so a name tie-break would
    return ['fx-b-2', 'fx-b-1']. broker_id is unique, so it is the only deterministic key.
    Both input orders are checked so input order can't mask the bug.
    """
    zeta = make_broker("fx-b-1", name="Fictional Broker Zeta", property_types=["retail"])
    alpha = make_broker("fx-b-2", name="Fictional Broker Alpha", property_types=["retail"])
    brokers = [alpha, zeta] if reverse else [zeta, alpha]
    req = DealRequirement.model_validate({"is_deal_request": True, "state": "TX", "property_type": "retail"})
    result = match(req, brokers, AS_OF)
    assert [m.score for m in result.matches] == [3, 3]
    assert [m.broker.broker_id for m in result.matches] == ["fx-b-1", "fx-b-2"]
