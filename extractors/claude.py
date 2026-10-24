"""Claude extractor: one forced tool call whose input_schema is the Pydantic schema.

Why a forced tool call instead of asking for JSON in text: the model must answer by filling the
schema's fields, so there's no prose to parse. Pydantic then validates the result. If it fails
(e.g. state "Oregon" instead of "OR"), we raise rather than repair, so the eval counts it.
"""
import hashlib
import json
import os

import anthropic

from extractors import ExtractionError
from schema import DealRequirement, ExtractedFields

# A cheap model. Note: Haiku 5.5 accepts a forced tool_choice, but Opus 5.5 and Sonnet 5.5
# reject it with a 400. Switching to those would need tool_choice "auto" plus a prompt instruction.
DEFAULT_MODEL = "claude-haiku-5-5"

SYSTEM_PROMPT = """You extract deal requirements from posts on a real estate broker referral board.

Fill a field only when the post states it. Use null when the post doesn't say, is ambiguous, or \
would need a guess. A null is always better than a guess: a wrong state sends the deal to brokers \
who aren't licensed there. Follow each field's description exactly.

Broker shorthand: SFR = single-family residence; NNN = triple-net lease; BOM = back on market; \
BOV = broker opinion of value; NOI = net operating income; cap = cap rate; \
1031 = tax-deferred exchange; 2-flat = two-unit building; co-op agent = cooperating agent."""

TOOL = {
    "name": "record_deal_requirement",
    "description": "Record the structured requirement extracted from one post.",
    "input_schema": ExtractedFields.model_json_schema(),
}

# Changes whenever the prompt or schema changes, so saved eval results say which version ran.
PROMPT_VERSION = hashlib.sha256(
    (SYSTEM_PROMPT + json.dumps(TOOL, sort_keys=True)).encode()
).hexdigest()[:8]


class ClaudeExtractor:
    name = "claude"

    def __init__(self, client=None, model: str | None = None):
        # A client can be passed in, so tests can use a fake one and run offline.
        self.client = client or anthropic.Anthropic()
        self.model = model or os.environ.get("DEAL_MODEL", DEFAULT_MODEL)

    def extract(self, text: str) -> DealRequirement:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[TOOL],
            tool_choice={"type": "tool", "name": TOOL["name"]},
            messages=[{"role": "user", "content": f"<post>\n{text}\n</post>"}],
        )
        if response.stop_reason == "refusal":
            raise ExtractionError("model refused")
        call = next((b for b in response.content if b.type == "tool_use"), None)
        if call is None:
            raise ExtractionError(f"no tool call (stop_reason={response.stop_reason})")
        try:
            return DealRequirement.model_validate(call.input)
        except ValueError as e:  # pydantic.ValidationError subclasses ValueError
            raise ExtractionError(f"invalid output: {e}") from e
