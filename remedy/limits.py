"""Shared limits for untrusted cases and generated output."""
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

MAX_STRING = 500
MAX_STRING_BUDGET = 8192
MAX_NODES = 512
MAX_DEPTH = 8
MAX_ITEMS = 8
MAX_CATEGORIES = 8
MAX_ATTRIBUTES = 8
MAX_EVENTS = 16
MAX_ARTEFACT_BYTES = 24576
MAX_RESPONSE_BYTES = 131072


def validate_envelope(value):
    """Bound the entire input, including ignored fields, before regex work."""
    pending = [(value, 0)]
    nodes = 0
    budget = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("case is too complex")
        if isinstance(current, str):
            budget += len(current)
            if len(current) > MAX_STRING or budget > MAX_STRING_BUDGET:
                raise ValueError("case string budget exceeded")
        elif isinstance(current, Mapping):
            if len(current) > 32:
                raise ValueError("too many object members")
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ValueError("object keys must be strings")
                pending.append((key, depth + 1))
                pending.append((child, depth + 1))
        elif isinstance(current, list):
            if len(current) > MAX_EVENTS:
                raise ValueError("too many list entries")
            pending.extend((child, depth + 1) for child in current)
        elif isinstance(current, int):
            if current.bit_length() > 64:
                raise ValueError("integer is too large")
        elif current is not None and not isinstance(current, (float, Decimal, date, datetime)):
            raise ValueError("unsupported case value")


def limited(value, maximum, name):
    if len(value) > maximum:
        raise ValueError(name + " has too many entries")
    return value
