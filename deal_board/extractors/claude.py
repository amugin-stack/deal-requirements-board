"""Claude extractor: one forced tool call whose input_schema is the Pydantic schema.

Why a forced tool call instead of asking for JSON in text: the model must answer by filling the
schema's fields, so there's no prose to parse. Pydantic then validates the result. If it fails
(e.g. state "Oregon" instead of "OR"), we raise rather than repair, so the eval counts it.
"""
import copy
import hashlib
import json
import os
from dataclasses import dataclass

import anthropic

from deal_board.extractors import ExtractionError, parse_requirement
from deal_board.schema import DealRequirement, ExtractedFields

# A cheap model. Note: Haiku 5.5 accepts a forced tool_choice, but Opus 5.5 and Sonnet 5.5
# reject it with a 400. Switching to those would need tool_choice "auto" plus a prompt instruction.
DEFAULT_MODEL = "claude-haiku-5-5"

SYSTEM_PROMPT = """You extract deal requirements from posts on a real estate broker referral board.

Fill a field only when the post states it. Use null when the post doesn't say, is ambiguous, or \
would need a guess. A null is always better than a guess: a wrong state sends the deal to brokers \
who aren't licensed there. Follow each field's description exactly.

When is_deal_request is true, city and state are judged separately: if the post names one city, \
put that city name as written in `city` even when its state is unclear (the same city name can \
exist in several states); the unclear state makes only `state` null. When is_deal_request is \
false, leave every other field null, even if the post mentions a place.

A request for a real estate professional to handle specific properties is a deal request even \
when it isn't a purchase, sale, or lease (e.g. a property manager for a client's rentals): set \
is_deal_request true, transaction_type null, and fill location and property type as usual. \
Requests for lenders, attorneys, or other non-brokerage services are still not deal requests.

Broker shorthand: SFR = single-family residence; NNN = triple-net lease; BOM = back on market; \
BOV = broker opinion of value; NOI = net operating income; cap = cap rate; \
1031 = tax-deferred exchange; 2-flat = two-unit building; co-op agent = cooperating agent."""

def _strict_schema(schema: dict) -> dict:
    """Adapt the Pydantic schema for tool `strict: true` (API-enforced schema, enums included).

    Strict mode needs `additionalProperties: false` on objects and every property listed in
    `required` (optional fields are already nullable via anyOf). Defaults and numeric bounds are
    dropped because strict mode doesn't use them; Pydantic still validates them afterwards.
    """
    schema = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("minimum", None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


TOOL = {
    "name": "record_deal_requirement",
    "description": "Record the structured requirement extracted from one post.",
    "input_schema": _strict_schema(ExtractedFields.model_json_schema()),
    "strict": True,
}

# Changes whenever the prompt or schema changes, so saved eval results say which version ran.
PROMPT_VERSION = hashlib.sha256(
    (SYSTEM_PROMPT + json.dumps(TOOL, sort_keys=True)).encode()
).hexdigest()[:8]


@dataclass(frozen=True)
class Usage:
    """Billed tokens for one API call, so eval.py can report what a run cost."""

    input_tokens: int
    output_tokens: int


class ClaudeExtractor:
    name = "claude"

    def __init__(self, client=None, model: str | None = None):
        # A client can be passed in, so tests can use a fake one and run offline.
        self.client = client or anthropic.Anthropic()
        self.model = model or os.environ.get("DEAL_MODEL", DEFAULT_MODEL)
        # Usage of the most recent extract_raw call; None before any call or when the response
        # carries no usage (fake clients). Reset per call, so a stale value is never reported.
        self.last_usage: Usage | None = None

    def extract(self, text: str) -> DealRequirement:
        return parse_requirement(self.extract_raw(text))

    def extract_raw(self, text: str) -> dict:
        """The tool call's input exactly as Claude returned it, before validation.

        eval.py saves this so results can be re-scored later without calling the API again.
        """
        self.last_usage = None
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[TOOL],
            tool_choice={"type": "tool", "name": TOOL["name"]},
            messages=[{"role": "user", "content": f"<post>\n{text}\n</post>"}],
        )
        # Recorded before the refusal/no-tool-call checks: those calls are billed too.
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = Usage(usage.input_tokens, usage.output_tokens)
        if response.stop_reason == "refusal":
            raise ExtractionError("model refused")
        call = next((b for b in response.content if b.type == "tool_use"), None)
        if call is None:
            raise ExtractionError(f"no tool call (stop_reason={response.stop_reason})")
        return call.input
