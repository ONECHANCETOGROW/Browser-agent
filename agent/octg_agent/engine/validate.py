"""Tiny JSON-Schema-subset validator (type, enum, minimum, maximum, minLength, maxLength, items, maxItems, required, additionalProperties)."""
from __future__ import annotations

from typing import Any

_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def validate(schema: dict, value: Any, path: str = "parameters") -> list[str]:
    errors: list[str] = []
    t = schema.get("type")
    types = t if isinstance(t, list) else ([t] if t else [])
    if types and not any(_TYPES.get(x, lambda _v: False)(value) for x in types):
        return [f"{path}: expected {'/'.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        errors.append(f"{path}: must not be empty" if schema["minLength"] <= 1 else f"{path}: must be at least {schema['minLength']} characters")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        errors.append(f"{path}: must be at most {schema['maxLength']} characters")
    if isinstance(value, list) and "maxItems" in schema and len(value) > schema["maxItems"]:
        errors.append(f"{path}: at most {schema['maxItems']} items")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += validate(schema["items"], item, f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}.{req}: required")
        for k, v in value.items():
            if k in props:
                errors += validate(props[k], v, f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{k}: unknown parameter")
    return errors
