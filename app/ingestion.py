from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .canonical_store import upsert_canonical_event
from .models import CanonicalEvent, IngestionLog, SourceColumnMapping
from .transforms import apply_transform, coerce_to_column_type

# upsert_canonical_event only flushes; process_file commits once per batch
# instead of once per row, since fsync-per-row doesn't scale to
# multi-thousand-row files (a single canonical_events row is a cheap write,
# but committing 5,000+ of them one at a time is not).
_COMMIT_BATCH_SIZE = 500


def _read_dataframe(filename: str, content: bytes) -> pd.DataFrame:
    lower = filename.lower()
    buffer = io.BytesIO(content)
    if lower.endswith(".xlsx") or lower.endswith(".xls"):
        df = pd.read_excel(buffer, dtype=str)
    else:
        df = pd.read_csv(buffer, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.where(pd.notna(df), None)
    return df


def _load_mappings(db: Session, tenant_bank_id: str, rail_type: str) -> list[SourceColumnMapping]:
    return (
        db.query(SourceColumnMapping)
        .filter_by(tenant_bank_id=tenant_bank_id, rail_type=rail_type)
        .all()
    )


def _apply_mappings(mappings: list[SourceColumnMapping], raw_row: dict[str, Any]) -> dict[str, Any]:
    """Turns one raw row into canonical field values.

    Fully generic: every mapping row is applied the same way regardless of
    which field or rail it targets. A canonical field with no mapping row
    for this (tenant_bank_id, rail_type) simply never appears in the
    result, which is what leaves it null on the stored event -- that's the
    mapping config's job, not a conditional in this loop. A mapping row
    with condition_column/condition_value set is likewise just skipped
    when the row's value doesn't match -- the branching lives in config
    (see SourceColumnMapping), not in this function.
    """
    mapped: dict[str, Any] = {}
    for mapping in mappings:
        if mapping.condition_column and raw_row.get(mapping.condition_column) != mapping.condition_value:
            continue
        if mapping.source_column_name not in raw_row:
            continue
        raw_value = raw_row[mapping.source_column_name]
        transformed = apply_transform(mapping.transform_type, raw_value)
        coerced = coerce_to_column_type(CanonicalEvent, mapping.canonical_field_name, transformed)

        if mapping.transform_type == "JSON_MERGE":
            bucket = mapped.setdefault(mapping.canonical_field_name, {})
            if coerced is not None:
                bucket[mapping.source_column_name] = coerced
        else:
            mapped[mapping.canonical_field_name] = coerced

    return mapped


def process_file(
    db: Session,
    tenant_bank_id: str,
    rail_type: str,
    settlement_stage: str,
    filename: str,
    content: bytes,
) -> IngestionLog:
    df = _read_dataframe(filename, content)
    mappings = _load_mappings(db, tenant_bank_id, rail_type)
    mapped_columns = {m.source_column_name for m in mappings}
    unmapped_columns = [c for c in df.columns if c not in mapped_columns]

    errors: list[dict[str, Any]] = []
    if unmapped_columns:
        errors.append({
            "type": "unmapped_columns",
            "columns": unmapped_columns,
            "detail": "No source_column_mappings entry for these columns; the raw values are still retained in the row snapshot.",
        })

    is_pre_settlement = settlement_stage == "PRE"
    rows_mapped = 0
    rows_failed = 0

    for idx, row in df.iterrows():
        raw_row = row.to_dict()
        try:
            mapped_fields = _apply_mappings(mappings, raw_row)
            transaction_id = mapped_fields.get("transaction_id")
            if not transaction_id:
                raise ValueError("no transaction_id produced by mapping config for this row")

            upsert_canonical_event(
                db,
                tenant_bank_id=tenant_bank_id,
                rail_type=rail_type,
                transaction_id=transaction_id,
                mapped_fields=mapped_fields,
                raw_row=raw_row,
                is_pre_settlement=is_pre_settlement,
            )
            rows_mapped += 1
            if rows_mapped % _COMMIT_BATCH_SIZE == 0:
                db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            rows_failed += 1
            errors.append({"type": "row_error", "row_index": int(idx), "error": str(exc)})
        except Exception as exc:
            rows_failed += 1
            errors.append({"type": "row_error", "row_index": int(idx), "error": str(exc)})

    log = IngestionLog(
        file_name=filename,
        tenant_bank_id=tenant_bank_id,
        rail_type=rail_type,
        settlement_stage=settlement_stage,
        row_count=len(df),
        rows_mapped=rows_mapped,
        rows_failed=rows_failed,
        ingested_at=datetime.now(timezone.utc),
        errors=errors,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log
