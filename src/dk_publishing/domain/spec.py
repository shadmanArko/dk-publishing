"""The publishing contract's building blocks: what a producer must make, as data.

The contract (`application/use_cases/publishing_spec.py`) is assembled from the same guides,
settings and constants the engine enforces, so it never holds a second copy of a rule. Here: the
facts no code can derive (how a live post reaches the platform, its quotas) and the JSON Schema for
one variant, so a client can check a post offline before it ever calls the API. The API's own check
stays the authority: the schema is a pre-check (a few rules, like counting a link as 23 characters,
only the platform's validation knows).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from dk_publishing.domain.guide import FieldGuide, FormatGuide

SPEC_FORMAT = "publishing-spec/v1"
JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"

_KIND = {"text": "string", "choice": "string", "bool": "boolean", "number": "number"}


@dataclass(frozen=True, slots=True)
class PlatformFacts:
    """What a platform's code cannot tell about itself: the route a live post takes, the API
    version it is pinned to, and the quotas the platform applies."""

    route_live: str
    quotas: tuple[str, ...] = ()
    api_version: str | None = None


def field_schema(field: FieldGuide) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": _KIND.get(field.kind, "string")}
    if field.choices:
        schema["enum"] = list(field.choices)
    if field.max_length is not None and field.kind == "text":
        schema["maxLength"] = field.max_length
    if field.required and field.kind == "text":
        schema["pattern"] = r"\S"  # required text must not be only spaces (the adapters refuse it)
    if field.note:
        schema["description"] = field.note
    return schema


def variant_schema(platform: str, fmt: FormatGuide, accounts: Sequence[str]) -> dict[str, Any]:
    """JSON Schema (2020-12) for one variant of this platform and format, as the API takes it."""
    account: dict[str, Any] = {"type": "string"}
    if accounts:
        account["enum"] = list(accounts)
    properties: dict[str, Any] = {
        "platform": {"const": platform},
        "format": {"const": fmt.name},
        "slot": {
            "type": "string",
            "format": "date-time",
            "description": "ISO 8601 with a UTC offset, e.g. 2026-10-12T19:30:00+02:00.",
        },
        "account": account,
        "media": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": fmt.media.min,
            "maxItems": fmt.media.max,
        },
    }
    required = ["platform", "format", "slot"]
    if fmt.caption is not None:
        properties["caption"] = field_schema(fmt.caption)
        if fmt.caption.required:
            required.append("caption")
    if fmt.media.min > 0:
        required.append("media")
    options: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {f.name: field_schema(f) for f in fmt.fields},
    }
    must = [f.name for f in fmt.fields if f.required]
    if must:
        options["required"] = must
        required.append("options")
    properties["options"] = options
    return {
        "$schema": JSON_SCHEMA,
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }
