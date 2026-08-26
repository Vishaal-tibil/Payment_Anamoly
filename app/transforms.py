from __future__ import annotations

import math
from typing import Any

from sqlalchemy import JSON as SA_JSON
from sqlalchemy import Boolean, Float

# Small synonym table so common status vocabularies across tenants/rails
# collapse to one canonical spelling. Applies uniformly regardless of which
# field or rail it's used on -- the dispatch is on transform_type, not on
# field/rail identity.
_STATUS_SYNONYMS = {
    "SETTLED": "SETTLED", "COMPLETE": "SETTLED", "COMPLETED": "SETTLED",
    "POSTED": "SETTLED", "SUCCESS": "SETTLED", "SUCCESSFUL": "SETTLED",
    "PENDING": "PENDING", "PROCESSING": "PENDING", "INITIATED": "PENDING", "IN_PROGRESS": "PENDING",
    "FAILED": "FAILED", "REJECTED": "FAILED", "DECLINED": "FAILED", "RETURNED": "FAILED", "CANCELLED": "FAILED",
}

_TRUE_STRINGS = {"true", "1", "yes", "y", "t"}
_FALSE_STRINGS = {"false", "0", "no", "n", "f"}


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def normalize_enum(value: Any) -> str:
    key = str(value).strip().upper().replace(" ", "_")
    return _STATUS_SYNONYMS.get(key, key)


def last4_mask(value: Any) -> str:
    digits = str(value).strip()
    tail = digits[-4:] if len(digits) >= 4 else digits
    return f"****{tail}"


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    key = str(value).strip().lower()
    if key in _TRUE_STRINGS:
        return True
    if key in _FALSE_STRINGS:
        return False
    raise ValueError(f"Cannot coerce {value!r} to a boolean")


def apply_transform(transform_type: str, value: Any) -> Any:
    """Dispatches purely on transform_type -- never on field name or rail."""
    if _is_missing(value):
        return None
    if transform_type in ("DIRECT", "RENAME", "JSON_MERGE"):
        return value
    if transform_type == "NORMALIZE_ENUM":
        return normalize_enum(value)
    if transform_type == "LAST4_MASK":
        return last4_mask(value)
    raise ValueError(f"Unknown transform_type: {transform_type!r}")


def coerce_to_column_type(model: Any, field_name: str, value: Any) -> Any:
    """Coerces a value to match the canonical_events column it targets.

    Reads the target type off the SQLAlchemy model itself, so numeric
    fields (amount, fees, ...) are cast correctly no matter which rail or
    tenant mapping produced them -- no hardcoded field-name list.
    """
    if value is None:
        return None
    column = model.__table__.columns.get(field_name)
    if column is None:
        return value
    col_type = column.type
    if isinstance(col_type, Boolean):
        return _to_bool(value)
    if isinstance(col_type, Float):
        return float(value)
    if isinstance(col_type, SA_JSON):
        return value
    return str(value)
