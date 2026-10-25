"""Claude extractor: one forced tool call whose input_schema is the Pydantic schema.

Why a forced tool call instead of asking for JSON in text: the model must answer by filling the
schema's fields, so there's no prose to parse. Bad output raises ExtractionError rather than being
repaired, so the eval counts it and nothing half-formed is routed:
- extract_raw accepts only a response that stopped on our tool call (stop_reason "tool_use" and a
  tool_use block named TOOL["name"]); a max_tokens cut-off, a refusal, a pause or another tool raise.
- check_claude_raw requires exactly ExtractedFields' keys with strict JSON types, because Pydantic's
  lax mode would otherwise fill omitted keys with None, turn "yes" into True and "450000" into
  450000, and drop unknown keys. extract and eval's (re)scoring of Claude raws both apply it.
- Pydantic then validates the values (e.g. state "Oregon" instead of "OR" fails).
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeGuard

import anthropic
from anthropic.types import MessageParam, ToolChoiceToolParam, ToolParam

from deal_board.config import DEFAULT_MODEL, load_settings
from deal_board.extractors import ExtractionError, parse_requirement
from deal_board.schema import DealRequirement, ExtractedFields

# DEFAULT_MODEL lives in deal_board.config (with the note on why Haiku); re-exported here for
# anything that imported it from this module.
__all__ = [
    "DEFAULT_MODEL",
    "INPUT_SCHEMA",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "TOOL",
    "ClaudeExtractor",
    "Usage",
    "check_claude_raw",
]

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


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt the Pydantic schema for tool `strict: true` (API-enforced schema, enums included).

    Strict mode needs `additionalProperties: false` on objects and every property listed in
    `required` (optional fields are already nullable via anyOf). Defaults and numeric bounds are
    dropped because strict mode doesn't use them; Pydantic still validates them afterwards.
    """
    schema = copy.deepcopy(schema)

    def walk(node: object) -> None:
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


# The tool's input schema as plain JSON, kept by name so tests can inspect it (ToolParam types
# input_schema as a narrow TypedDict that doesn't list strict mode's extra keys).
INPUT_SCHEMA: dict[str, Any] = _strict_schema(ExtractedFields.model_json_schema())

TOOL: ToolParam = {
    "name": "record_deal_requirement",
    "description": "Record the structured requirement extracted from one post.",
    "input_schema": INPUT_SCHEMA,
    "strict": True,
}

# Changes whenever the prompt or schema changes, so saved eval results say which version ran.
PROMPT_VERSION = hashlib.sha256((SYSTEM_PROMPT + json.dumps(TOOL, sort_keys=True)).encode()).hexdigest()[:8]


@dataclass(frozen=True)
class Usage:
    """Billed tokens for one API call, so eval.py can report what a run cost."""

    input_tokens: int
    output_tokens: int


# --- The slice of the Anthropic client this module uses -----------------------------------------
# Structural types, so `anthropic.Anthropic` and the duck-typed fakes in the tests both fit.


class _ContentBlock(Protocol):
    @property
    def type(self) -> str: ...


class _ToolUseBlock(_ContentBlock, Protocol):
    @property
    def name(self) -> str: ...

    @property
    def input(self) -> dict[str, object]: ...


class _Response(Protocol):
    @property
    def stop_reason(self) -> str | None: ...

    @property
    def content(self) -> Sequence[_ContentBlock]: ...


class _Messages(Protocol):
    # Exactly the arguments extract_raw passes, typed with the SDK's own param types, so mypy
    # checks the call against anthropic's real (overloaded) create and the fakes still fit.
    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        tools: list[ToolParam],
        tool_choice: ToolChoiceToolParam,
        messages: list[MessageParam],
    ) -> _Response: ...


class MessagesClient(Protocol):
    """What ClaudeExtractor needs from a client: `client.messages.create(...)`."""

    @property
    def messages(self) -> _Messages: ...


def _is_tool_use(block: _ContentBlock) -> TypeGuard[_ToolUseBlock]:
    return block.type == "tool_use"


def _strict_type_ok(field: str, value: object) -> bool:
    """Whether value has the JSON type the tool schema demands for field, with no coercion.

    bool is checked by identity of type because bool subclasses int: True must not pass as a deal
    value, and 1 must not pass as is_deal_request.
    """
    if field == "is_deal_request":
        return type(value) is bool
    if value is None:
        return True
    if field == "deal_value_usd":
        return type(value) is int
    return type(value) is str


def check_claude_raw(raw: object) -> None:
    """Raise ExtractionError unless raw is exactly the tool's object: ExtractedFields' keys, no more
    and no fewer, each with its strict JSON type (is_deal_request bool; deal_value_usd int or null;
    every other field str or null).

    Why: the API enforces this under strict mode, but parse_requirement validates in lax mode, so
    a truncated call (missing keys), a string "yes" or "450000", a float 4.9e5 or an extra key
    would otherwise score and route as a clean extraction. Values (state codes, enums) are left to
    Pydantic; this only rejects shapes the schema never allows.
    """
    if not isinstance(raw, dict):
        raise ExtractionError(f"invalid output: expected an object, got {type(raw).__name__}")
    expected = set(ExtractedFields.model_fields)
    if missing := sorted(expected - raw.keys()):
        raise ExtractionError(f"invalid output: missing keys {missing}")
    if extra := sorted(str(k) for k in raw.keys() - expected):
        raise ExtractionError(f"invalid output: unexpected keys {extra}")
    if bad := sorted(f for f in expected if not _strict_type_ok(f, raw[f])):
        raise ExtractionError(f"invalid output: wrong JSON type for {bad}")


def _default_client(api_key: str | None) -> anthropic.Anthropic:
    """An Anthropic client using the key from deal_board.config, or a clear error without one.

    Raising here, instead of passing api_key=None, stops the SDK from falling back to its own
    environment lookup (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN) behind config's back.

    open_telemetry=False for the same reason: left unset, the SDK reads ANTHROPIC_OPEN_TELEMETRY_*
    env vars, and one of them (TRACES_CONTENT_MODE=content) puts the full post text into spans.
    """
    if api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; pass a client or set the key to use ClaudeExtractor")
    return anthropic.Anthropic(api_key=api_key, open_telemetry=False)


class ClaudeExtractor:
    name = "claude"

    def __init__(self, client: MessagesClient | None = None, model: str | None = None) -> None:
        # A client can be passed in, so tests can use a fake one and run offline. Settings are
        # read once, here: the key (only when no client is given) and the default model.
        settings = load_settings()
        self.client: MessagesClient = client or _default_client(settings.api_key_value())
        self.model = model or settings.deal_model
        # Usage of the most recent extract_raw call; None before any call or when the response
        # carries no usage (fake clients). Reset per call, so a stale value is never reported.
        self.last_usage: Usage | None = None

    def extract(self, text: str) -> DealRequirement:
        raw = self.extract_raw(text)
        check_claude_raw(raw)
        return parse_requirement(raw)

    def extract_raw(self, text: str) -> dict[str, object]:
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
        # Recorded before the stop-reason/tool-call checks: those calls are billed too.
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = Usage(usage.input_tokens, usage.output_tokens)
        if response.stop_reason == "refusal":
            raise ExtractionError("model refused")
        # Anything but "tool_use" means the call didn't finish on our tool: "max_tokens" can hand
        # back a tool_use block whose input was cut off mid-object, which would validate with nulls.
        if response.stop_reason != "tool_use":
            raise ExtractionError(f"no complete tool call (stop_reason={response.stop_reason})")
        call = next((b for b in response.content if _is_tool_use(b)), None)
        if call is None:
            raise ExtractionError(f"no tool call (stop_reason={response.stop_reason})")
        name = getattr(call, "name", None)  # a block without a name can't be our tool either
        if name != TOOL["name"]:
            raise ExtractionError(f"wrong tool called: {name!r}")
        return call.input
