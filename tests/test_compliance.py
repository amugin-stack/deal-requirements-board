"""P8 compliance gate: no deal post may ever surface a broker who can't legally take it.

"Stay Compliant. Close Anywhere." only holds if the licensing filter is airtight. This
file runs every labeled post (dev and holdout) against the real broker pool plus
deliberately broken copies of every broker (bad status, expired yesterday, unknown
expiration, wrong state) and checks each returned broker with an oracle written here,
independently of matcher.is_licensed. One parametrized case per post, so a failure
names the post that leaked.
"""
import csv
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from deal_board.brokers import BROKERS_CSV, Broker, load_brokers
from deal_board.matcher import is_licensed, match
from deal_board.schema import DealRequirement

AS_OF = date(2026, 10, 20)
DATA = Path(__file__).parent.parent / "data"
POST_FILES = [DATA / "posts.jsonl", DATA / "holdout.jsonl"]


# ---------- the oracle: the licensing rule, restated ----------

def oracle_licensed(b: Broker, state: str | None, as_of: date) -> bool:
    if state is None:
        return False
    if b.license_state != state:
        return False
    if b.license_status != "Active":
        return False
    if b.license_expiration is None:
        return False
    return b.license_expiration >= as_of


# ---------- data ----------

def load_posts() -> list[tuple[str, DealRequirement]]:
    posts = []
    for path in POST_FILES:
        with open(path) as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    posts.append((row["id"], DealRequirement.model_validate(row["label"])))
    return posts


def non_active_statuses() -> list[str]:
    with open(BROKERS_CSV, newline="") as f:
        statuses = {row["license_status"] for row in csv.DictReader(f)}
    return sorted(statuses - {"Active"})


def other_state(state: str) -> str:
    return "OK" if state != "OK" else "NM"


def make_variants(b: Broker) -> list[Broker]:
    """Copies of b that must never be returned for any deal."""
    variants = [
        b.model_copy(update={"broker_id": f"{b.broker_id}~status-{i}", "license_status": s})
        for i, s in enumerate(STATUSES)
    ]
    variants.append(b.model_copy(update={
        "broker_id": f"{b.broker_id}~expired-yesterday",
        "license_expiration": AS_OF - timedelta(days=1),
    }))
    variants.append(b.model_copy(update={
        "broker_id": f"{b.broker_id}~expiration-unknown",
        "license_expiration": None,
    }))
    variants.append(b.model_copy(update={
        "broker_id": f"{b.broker_id}~wrong-state",
        "license_state": other_state(b.license_state),
    }))
    return variants


POSTS = load_posts()
BASE = load_brokers()
STATUSES = non_active_statuses()
VARIANTS = [v for b in BASE for v in make_variants(b)]
VARIANT_IDS = {v.broker_id for v in VARIANTS}
POOL = BASE + VARIANTS


def test_fixture_sanity():
    assert len(POSTS) == 50
    assert len({pid for pid, _ in POSTS}) == len(POSTS)
    assert len({b.broker_id for b in POOL}) == len(POOL)
    # The variants must actually exercise the statuses seen in the real data.
    assert {"Inactive", "Suspended", "Probation - Active", "Expired less than 6 months"} <= set(STATUSES)
    # Every variant is unlicensed in its original broker's state, and no post names the
    # state the wrong-state variants were moved to, so no variant may ever be returned.
    original_state = {b.broker_id: b.license_state for b in BASE}
    assert not any(
        oracle_licensed(v, original_state[v.broker_id.split("~")[0]], AS_OF) for v in VARIANTS
    )
    moved_to = {v.license_state for v in VARIANTS if v.broker_id.endswith("~wrong-state")}
    assert not moved_to & {r.state for _, r in POSTS}


# ---------- the gate, per post ----------

@pytest.mark.parametrize("req", [r for _, r in POSTS], ids=[pid for pid, _ in POSTS])
def test_post_returns_only_licensed_brokers(req: DealRequirement):
    pool_before = [b.model_dump() for b in POOL]
    result = match(req, POOL, AS_OF, limit=len(POOL) + 10)

    for m in result.matches:
        assert oracle_licensed(m.broker, req.state, AS_OF), m.broker.broker_id
        assert m.broker.broker_id not in VARIANT_IDS, m.broker.broker_id

    returned = [m.broker.broker_id for m in result.matches]
    assert len(returned) == len(set(returned)), "a broker was returned twice"

    if not req.is_deal_request:
        assert result.matches == [] and result.excluded_for_licensing == 0
    elif req.state is None:
        assert result.matches == [] and result.excluded_for_licensing == len(POOL)
    else:
        expected = {b.broker_id for b in POOL if oracle_licensed(b, req.state, AS_OF)}
        assert set(returned) == expected  # with a big limit, every licensed broker comes back
        assert result.excluded_for_licensing + len(result.matches) == len(POOL)

    assert [b.model_dump() for b in POOL] == pool_before


def test_duplicate_broker_id_returns_only_the_licensed_record():
    # Regression: the gate must judge each record, never look brokers up by id. If an
    # unlicensed record shares an id with a licensed one, only the licensed object comes back.
    licensed = next(b for b in BASE if b.broker_id == "fx-01")
    revoked = licensed.model_copy(update={"license_status": "Revoked"})
    for pool in ([licensed, revoked], [revoked, licensed]):
        result = match(DealRequirement(is_deal_request=True, state="FL"), pool, AS_OF)
        assert [m.broker for m in result.matches] == [licensed]
        assert result.matches[0].broker is licensed
        assert result.excluded_for_licensing == 1


# ---------- is_licensed against the oracle ----------

def _base() -> Broker:
    return next(b for b in BASE if b.broker_id == "fx-01")  # FL, Active, expires 2028


@pytest.mark.parametrize("b", POOL[:: max(1, len(POOL) // 60)], ids=lambda b: b.broker_id)
@pytest.mark.parametrize("state", ["TX", "FL", "OK", None])
def test_is_licensed_agrees_with_oracle(b: Broker, state: str | None):
    assert is_licensed(b, state, AS_OF) == oracle_licensed(b, state, AS_OF)


@pytest.mark.parametrize(
    "expiration,expected",
    [
        (AS_OF, True),                       # expiring today is still licensed
        (AS_OF + timedelta(days=1), True),   # tomorrow
        (AS_OF - timedelta(days=1), False),  # yesterday
        (None, False),                       # unknown
    ],
    ids=["today", "tomorrow", "yesterday", "unknown"],
)
def test_expiration_boundary(expiration: date | None, expected: bool):
    b = _base().model_copy(update={"license_expiration": expiration})
    assert is_licensed(b, "FL", AS_OF) is expected
    returned = match(DealRequirement(is_deal_request=True, state="FL"), [b], AS_OF).matches
    assert bool(returned) is expected


def test_as_of_is_respected_not_the_clock():
    b = _base().model_copy(update={"license_expiration": date(2020, 1, 1)})
    assert is_licensed(b, "FL", date(2019, 12, 31)) is True
    assert is_licensed(b, "FL", date(2020, 1, 2)) is False


def test_state_none_is_not_licensed():
    assert is_licensed(_base(), None, AS_OF) is False


def test_state_must_match_exactly():
    assert is_licensed(_base(), "FL", AS_OF) is True
    assert is_licensed(_base(), "GA", AS_OF) is False
    assert is_licensed(_base(), "fl", AS_OF) is False  # a different string, so not a match


@pytest.mark.parametrize("status", STATUSES)
def test_every_non_active_status_is_not_licensed(status: str):
    b = _base().model_copy(update={"license_status": status})
    assert is_licensed(b, "FL", AS_OF) is False


@pytest.mark.parametrize("status", ["active", "ACTIVE", " Active", "Active "])
def test_status_must_be_exactly_active(status: str):
    b = _base().model_copy(update={"license_status": status})
    assert is_licensed(b, "FL", AS_OF) is False
