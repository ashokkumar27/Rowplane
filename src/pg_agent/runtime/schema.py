"""Small JSON-schema subset used by the harness control plane."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pg_agent.runtime.errors import ToolValidationError


def validate_json_schema_subset(
    schema: Mapping[str, Any] | None,
    value: Any,
    *,
    subject: str = "value",
) -> None:
    """Validate a conservative JSON-schema subset without extra dependencies."""

    if not schema:
        return
    if not isinstance(schema, Mapping):
        raise ToolValidationError(f"{subject} schema must be an object")

    if "const" in schema and value != schema["const"]:
        raise ToolValidationError(f"{subject} must equal {schema['const']!r}")

    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, Sequence) or isinstance(enum, str):
            raise ToolValidationError(f"{subject} schema enum must be an array")
        if value not in enum:
            raise ToolValidationError(f"{subject} must be one of {list(enum)!r}")

    expected_type = schema.get("type")
    if expected_type is not None and not _matches_json_type(value, expected_type):
        raise ToolValidationError(f"{subject} must be of type {expected_type}")

    if _expects_object(schema):
        if not isinstance(value, Mapping):
            raise ToolValidationError(f"{subject} must be an object")
        _validate_object(schema, value, subject=subject)

    if _expects_array(schema):
        if not isinstance(value, list):
            raise ToolValidationError(f"{subject} must be an array")
        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, Mapping):
                raise ToolValidationError(f"{subject}.items schema must be an object")
            for index, item in enumerate(value):
                validate_json_schema_subset(item_schema, item, subject=f"{subject}[{index}]")

    if isinstance(value, str):
        min_length = schema.get("minLength")
        if min_length is not None and len(value) < int(min_length):
            raise ToolValidationError(f"{subject} must be at least {min_length} characters")

    if isinstance(value, int | float) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if minimum is not None and value < float(minimum):
            raise ToolValidationError(f"{subject} must be >= {minimum}")
        maximum = schema.get("maximum")
        if maximum is not None and value > float(maximum):
            raise ToolValidationError(f"{subject} must be <= {maximum}")


def _validate_object(
    schema: Mapping[str, Any],
    value: Mapping[str, Any],
    *,
    subject: str,
) -> None:
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ToolValidationError(f"{subject} schema.required must be a list of strings")
    missing = [key for key in required if key not in value]
    if missing:
        raise ToolValidationError(f"{subject} is missing required keys: {missing}")

    properties = schema.get("properties", {})
    if properties and not isinstance(properties, Mapping):
        raise ToolValidationError(f"{subject} schema.properties must be an object")
    for key, property_schema in properties.items():
        if key not in value:
            continue
        if not isinstance(property_schema, Mapping):
            raise ToolValidationError(f"{subject}.{key} schema must be an object")
        validate_json_schema_subset(property_schema, value[key], subject=f"{subject}.{key}")

    if schema.get("additionalProperties") is False:
        allowed = set(properties)
        extras = sorted(set(value) - allowed)
        if extras:
            raise ToolValidationError(f"{subject} has unexpected keys: {extras}")


def _expects_object(schema: Mapping[str, Any]) -> bool:
    expected_type = schema.get("type")
    return expected_type == "object" or "properties" in schema or "required" in schema


def _expects_array(schema: Mapping[str, Any]) -> bool:
    expected_type = schema.get("type")
    return expected_type == "array" or "items" in schema


def _matches_json_type(value: Any, expected_type: Any) -> bool:
    if isinstance(expected_type, list):
        return any(_matches_json_type(value, item) for item in expected_type)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, Mapping)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "null":
        return value is None
    raise ToolValidationError(f"unsupported JSON schema type: {expected_type}")
