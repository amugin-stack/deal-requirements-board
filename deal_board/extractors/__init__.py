"""One interface for every extractor, so eval.py and the API can swap them freely."""
from typing import Protocol

from deal_board.schema import DealRequirement


class ExtractionError(Exception):
    """The extractor couldn't produce a valid DealRequirement (refusal, no tool call, bad output)."""


class Extractor(Protocol):
    name: str

    def extract(self, text: str) -> DealRequirement: ...


def parse_requirement(raw: dict) -> DealRequirement:
    """Validate a raw extractor output. Raises ExtractionError instead of repairing bad values."""
    try:
        return DealRequirement.model_validate(raw)
    except ValueError as e:  # pydantic.ValidationError subclasses ValueError
        raise ExtractionError(f"invalid output: {e}") from e
