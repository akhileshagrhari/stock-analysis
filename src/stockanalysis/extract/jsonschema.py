"""Pydantic model -> a JSON schema the structured-outputs API will accept.

`client.messages.parse()` does this conversion internally, but the Batch API has
no `parse()` — batch requests carry a raw `output_config.format`. Since the
backfill runs through batches (50% cheaper, and latency is irrelevant when you
are filling five years of history), we need the conversion ourselves.

The API's schema subset is narrower than what Pydantic emits:

  * every object needs ``additionalProperties: false`` and must list *all* of
    its properties in ``required`` — optionality is expressed by allowing null,
    not by omitting the key
  * numeric and string constraints (``minimum``, ``maxLength``, ``pattern``, …)
    are rejected outright
  * only a fixed set of string ``format`` values is recognised

Getting this wrong produces a 400 on a batch of several hundred requests, after
you have already paid to assemble it — hence the unit tests.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Keywords the structured-outputs subset does not accept. Silently dropped
# rather than raising: they are assertions we re-implement in validate.py, so
# losing them here costs nothing.
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "contains",
        "patternProperties",
        "propertyNames",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
)

_SUPPORTED_FORMATS = frozenset(
    {
        "date-time",
        "time",
        "date",
        "duration",
        "email",
        "hostname",
        "uri",
        "ipv4",
        "ipv6",
        "uuid",
    }
)

# Subschemas live under these keys and must be walked too.
_SUBSCHEMA_KEYS = ("anyOf", "allOf", "oneOf", "prefixItems")


def to_api_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for `model`, conformed to the structured-outputs subset."""
    return _conform(model.model_json_schema())


def _conform(node: Any) -> Any:
    if isinstance(node, list):
        return [_conform(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED_KEYWORDS:
            continue
        if key == "format" and value not in _SUPPORTED_FORMATS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _conform(sub) for name, sub in value.items()}
        elif key in ("$defs", "definitions") and isinstance(value, dict):
            out[key] = {name: _conform(sub) for name, sub in value.items()}
        elif key in _SUBSCHEMA_KEYS or key in ("items", "additionalProperties"):
            out[key] = _conform(value)
        else:
            out[key] = _conform(value)

    if "properties" in out:
        out["additionalProperties"] = False
        # Strict mode requires every property to be required. Our fields are all
        # nullable, so this costs no expressiveness — the model says "null"
        # instead of omitting the key, which is easier to detect downstream.
        out["required"] = list(out["properties"].keys())

    return out
