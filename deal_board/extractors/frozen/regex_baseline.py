"""Keyword and regex baseline: the floor that the Claude extractor has to beat.

Each field uses one simple rule. Where a rule is knowingly naive (first state wins, the largest
dollar amount wins), that is the point: the eval shows where simple rules break.
"""
import re
from typing import get_args

from schema import DealRequirement, State

CODES = set(get_args(State))
NAMES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
# Uppercase codes only, so "in" or "me" in normal text don't match. "Kansas City" is a city, not a state.
STATE_RE = re.compile(r"\b(" + "|".join(sorted(NAMES, key=len, reverse=True)) + r")\b(?! City)|\b([A-Z]{2})\b")

CAP = r"[A-Z][a-z]+(?: [A-Z][a-z]+)*"  # capitalized phrase, e.g. "Las Vegas"
STATE_ALT = "|".join([*NAMES, *CODES])
CITY_BEFORE_STATE = re.compile(rf"\b({CAP}),?\s+(?:{STATE_ALT})\b")  # "Austin, TX", "Boise, Idaho"
CITY_AFTER_PREP = re.compile(rf"\b(?:in|near|outside)\s+(?:downtown\s+)?({CAP})")  # "in Chicago"

NOT_DEAL = re.compile(r"\b(hiring|just closed|CE hours|lender\b(?!-)|market update|template)", re.I)

# Checked in order; the first match wins. Specific types come before generic ones.
PROPERTY_RULES = [
    ("mixed_use", r"mixed[- ]use"),
    ("industrial", r"warehouse|industrial|flex"),
    ("office", r"office"),
    ("retail", r"retail|strip center|NNN"),
    ("multifamily", r"duplex|fourplex|\d-unit|\d-flat|multifamily|apartments?"),
    ("condo_townhome", r"condo|townhome|townhouse"),
    ("land", r"\bland\b|acres"),
    ("single_family", r"\bSFR\b|single[- ]family|ranch|\bhomes?\b|house"),
]
TRANSACTION_RULES = [
    ("1031_exchange", r"\b1031\b"),
    ("lease", r"lease|tenant rep"),
    ("sell", r"\bsell|seller|listing|\blist\b|asking"),
    ("buy", r"\bbuy|buyer|purchase|budget|pre-approved"),
]
MONEY = re.compile(
    r"\$\s?(\d+(?:\.\d+)?)\s*(k|m|million)?(?:\s*-\s*\$?(\d+(?:\.\d+)?)\s*(k|m|million)?)?(?!\s*/\s*sf)",
    re.I,
)
DURATION = re.compile(r"(\d+)[\s-]*(day|week|month)s?\b", re.I)
UNIT_DAYS = {"day": 1, "week": 7, "month": 30}


def _first(rules, text):
    return next((label for label, pat in rules if re.search(pat, text, re.I)), None)


def _state(text):
    for m in STATE_RE.finditer(text):
        if m.group(1):
            return NAMES[m.group(1)]
        if m.group(2) in CODES:
            return m.group(2)
    return None


def _city(text):
    for pattern in (CITY_BEFORE_STATE, CITY_AFTER_PREP):
        for m in pattern.finditer(text):
            if m.group(1) not in NAMES:  # "in Florida" is a state, not a city
                return m.group(1)
    return None


def _scale(num, suffix):
    mult = {"k": 1_000, "m": 1_000_000, "million": 1_000_000}.get((suffix or "").lower(), 1)
    return int(float(num) * mult)


def _deal_value(text):
    amounts = []
    for lo, lo_sfx, hi, hi_sfx in MONEY.findall(text):
        # In "$300-400k" the suffix after the upper bound applies to both numbers.
        amounts.append(_scale(hi, hi_sfx) if hi else _scale(lo, lo_sfx))
    return max(amounts, default=None)  # naive: the biggest number is usually the price


def _timeline(text):
    if re.search(r"\bASAP\b|immediately", text, re.I):
        return "within_30_days"
    m = DURATION.search(text)
    if not m:
        return None
    days = int(m.group(1)) * UNIT_DAYS[m.group(2).lower()]
    return "within_30_days" if days <= 30 else "31_to_90_days" if days <= 90 else "over_90_days"


class RegexExtractor:
    name = "regex"

    def extract(self, text: str) -> DealRequirement:
        if NOT_DEAL.search(text):
            return DealRequirement(is_deal_request=False)
        return DealRequirement(
            is_deal_request=True,
            state=_state(text),
            city=_city(text),
            property_type=_first(PROPERTY_RULES, text),
            transaction_type=_first(TRANSACTION_RULES, text),
            deal_value_usd=_deal_value(text),
            timeline=_timeline(text),
        )
