"""One interface for every extractor, so eval.py and the API can swap them freely."""
from typing import Protocol

from schema import DealRequirement


class ExtractionError(Exception):
    """The extractor couldn't produce a valid DealRequirement (refusal, no tool call, bad output)."""


class Extractor(Protocol):
    name: str

    def extract(self, text: str) -> DealRequirement: ...
