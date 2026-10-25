"""One interface for every extractor, so eval.py and the API can swap them freely."""

from typing import Protocol

from deal_board.schema import DealRequirement, JSONObject


class ExtractionError(Exception):
    """The extractor couldn't produce a valid DealRequirement (refusal, no tool call, bad output)."""


class Extractor(Protocol):
    name: str

    def extract(self, text: str) -> DealRequirement: ...


def parse_requirement(raw: JSONObject) -> DealRequirement:
    """Validate a raw extractor output. Raises ExtractionError instead of repairing bad values.

    The schema normalises only harmless formatting: an ASCII state's case and surrounding
    whitespace (' tx ' -> 'TX'), whitespace around city and notes, and a blank state, city or
    notes -> None. Anything else that doesn't fit raises, e.g. state 48, ['TX'], b'TX',
    a non-ASCII 'ıl', 'Texas' or 'ZZ', or a non-string city.
    """
    try:
        return DealRequirement.model_validate(raw)
    except ValueError as e:  # pydantic.ValidationError subclasses ValueError
        raise ExtractionError(f"invalid output: {e}") from e
