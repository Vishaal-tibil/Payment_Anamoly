from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .models import CanonicalEvent

# Every canonical_events column except the composite key and the
# system-managed/snapshot columns can be set from mapped_fields.
_MUTABLE_FIELDS = {c.name for c in CanonicalEvent.__table__.columns} - {
    "id", "tenant_bank_id", "rail_type", "transaction_id",
    "first_seen_at", "last_updated_at", "snapshot_pre", "snapshot_post",
}


def _is_populated(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value.strip() == "":
        return False
    if isinstance(value, dict) and len(value) == 0:
        return False
    return True


def _merge_field(existing_value: Any, incoming_value: Any) -> Any:
    """How one field's existing value combines with an incoming one.

    Dict-valued fields (e.g. risk_flags, built via JSON_MERGE) merge key by
    key rather than replacing the whole value -- otherwise a PRE file's
    risk flags would be wiped out wholesale the moment a POST file
    contributes its own, different, risk-flag keys for the same
    transaction. Every other field type keeps simple replacement.
    """
    if isinstance(incoming_value, dict) and isinstance(existing_value, dict):
        merged = dict(existing_value)
        merged.update(incoming_value)
        return merged
    return incoming_value


def upsert_canonical_event(
    db: Session,
    tenant_bank_id: str,
    rail_type: str,
    transaction_id: str,
    mapped_fields: dict[str, Any],
    raw_row: dict[str, Any],
    is_pre_settlement: bool,
) -> CanonicalEvent:
    """Creates or merges a canonical_events row for (tenant_bank_id, rail_type, transaction_id).

    Merge rule: a field is only overwritten when the incoming value is
    non-null/non-empty, so a later arrival can never null out a field an
    earlier arrival already populated. The same rule applies regardless of
    whether PRE or POST arrives first, so out-of-order arrival is safe.
    Re-running the same payload is idempotent: values are re-set to the
    same thing and an already-populated snapshot slot is left untouched.

    Flushes (not commits) the change -- the caller controls transaction
    boundaries, since a caller processing many rows from one file wants to
    batch commits rather than fsync once per row.
    """
    existing = (
        db.query(CanonicalEvent)
        .filter_by(tenant_bank_id=tenant_bank_id, rail_type=rail_type, transaction_id=transaction_id)
        .one_or_none()
    )
    now = datetime.now(timezone.utc)

    if existing is None:
        event = CanonicalEvent(
            tenant_bank_id=tenant_bank_id,
            rail_type=rail_type,
            transaction_id=transaction_id,
            first_seen_at=now,
            last_updated_at=now,
        )
        for field, value in mapped_fields.items():
            if field in _MUTABLE_FIELDS and _is_populated(value):
                setattr(event, field, value)
        if is_pre_settlement:
            event.snapshot_pre = raw_row
        else:
            event.snapshot_post = raw_row
        db.add(event)
        db.flush()
        return event

    for field, value in mapped_fields.items():
        if field in _MUTABLE_FIELDS and _is_populated(value):
            setattr(existing, field, _merge_field(getattr(existing, field), value))

    if is_pre_settlement:
        if existing.snapshot_pre is None:
            existing.snapshot_pre = raw_row
    else:
        if existing.snapshot_post is None:
            existing.snapshot_post = raw_row

    existing.last_updated_at = now
    db.flush()
    return existing
