"""Seeds source_column_mappings for the Huntington-pilot dataset
(Raw_data/split/pilot_bank_<rail>_<pre|post>.csv), across all 5 rails.

The pilot files have no tenant column at all, so every ingest call for
this dataset uses the fixed tenant_bank_id "PILOT_BANK" (per-session
decision -- this dataset has no tenant dimension).

Same mapping rows apply to every rail because the combined dataset uses one
shared column schema (irrelevant columns are simply blank for a given
rail) -- only CARD gets one extra row, for payment_processor_name.

Usage: .venv/Scripts/python.exe scripts/seed_pilot_mappings.py
"""
from __future__ import annotations

from app.database import Base, SessionLocal, engine
from app.models import SourceColumnMapping

TENANT_BANK_ID = "PILOT_BANK"
RAIL_TYPES = ["CHEQUE", "ACH", "WIRE", "FEDNOW", "CARD"]

# (source_column_name, canonical_field_name, transform_type, condition_column, condition_value)
_COMMON_MAPPINGS: list[tuple[str, str, str, str | None, str | None]] = [
    ("transaction_id", "transaction_id", "DIRECT", None, None),

    # entity_id routes to source_merchant_id or source_individual_id
    # depending on entity_type -- the source data gives one shared
    # identifier column for both party types, not two separate ones.
    ("entity_id", "source_merchant_id", "DIRECT", "entity_type", "MERCHANT"),
    ("entity_id", "source_individual_id", "DIRECT", "entity_type", "INDIVIDUAL"),

    # No payment-direction field exists in this dataset. entity_name is the
    # tenant's own tracked party, counterparty_name is the other side --
    # mapped onto payer_name/payee_name as a fixed convention, not a literal
    # "who paid whom" (see project notes).
    ("entity_name", "payer_name", "RENAME", None, None),
    ("counterparty_name", "payee_name", "RENAME", None, None),
    ("account_number_masked", "payer_account_ref", "RENAME", None, None),

    ("amount", "amount", "DIRECT", None, None),
    ("currency", "currency", "DIRECT", None, None),
    ("settlement_status", "status", "NORMALIZE_ENUM", None, None),

    # issuer_bank_name varies per rail for the same entity -- it's the
    # counterparty's bank, not the tenant's.
    ("issuer_bank_name", "counterparty_bank_name", "RENAME", None, None),
    ("onboarded_by", "onboarded_by", "DIRECT", None, None),

    # Validation/risk sub-fields from both files bucket into risk_flags.
    ("validation_status", "risk_flags", "JSON_MERGE", None, None),
    ("fraud_review_status", "risk_flags", "JSON_MERGE", None, None),
    ("sanctions_screening_status", "risk_flags", "JSON_MERGE", None, None),
    ("compliance_status", "risk_flags", "JSON_MERGE", None, None),
    ("validation_status_post", "risk_flags", "JSON_MERGE", None, None),
    ("fraud_review_status_post", "risk_flags", "JSON_MERGE", None, None),
    ("dispute_status", "risk_flags", "JSON_MERGE", None, None),
    ("reject_reason_code", "risk_flags", "JSON_MERGE", None, None),
    ("return_reason_code", "risk_flags", "JSON_MERGE", None, None),
    ("outcome_category", "risk_flags", "JSON_MERGE", None, None),
    ("stall_flag", "risk_flags", "JSON_MERGE", None, None),
    ("sla_breach_flag", "risk_flags", "JSON_MERGE", None, None),
    ("case_alert_id", "risk_flags", "JSON_MERGE", None, None),
    ("chargeback_amount", "risk_flags", "JSON_MERGE", None, None),

    # NOTE: 'amount' above is the field shared identically by both files.
    # settled_amount/posted_amount (POST-only) are deliberately left
    # unmapped: they can diverge from 'amount' (a small delta shows up on
    # some cheque rows) and reconciling that is squarely a future
    # reconciliation-engine concern, not this ingestion layer's. Both
    # values are still fully retained in snapshot_post.
    #
    # NOTE: 'merchant_id' is deliberately left unmapped -- it's just
    # entity_id copied when entity_type == MERCHANT (100% redundant),
    # already covered by the entity_id conditional rows above.
]

_CARD_ONLY_MAPPINGS: list[tuple[str, str, str, str | None, str | None]] = [
    ("payment_processor_name", "processor_name", "RENAME", None, None),
]


def seed_pilot_mappings(db) -> int:
    inserted = 0
    for rail_type in RAIL_TYPES:
        rows = list(_COMMON_MAPPINGS)
        if rail_type == "CARD":
            rows += _CARD_ONLY_MAPPINGS

        for source_column_name, canonical_field_name, transform_type, condition_column, condition_value in rows:
            exists = (
                db.query(SourceColumnMapping)
                .filter_by(
                    tenant_bank_id=TENANT_BANK_ID,
                    rail_type=rail_type,
                    source_column_name=source_column_name,
                    canonical_field_name=canonical_field_name,
                    condition_value=condition_value,
                )
                .first()
            )
            if exists is not None:
                continue
            db.add(SourceColumnMapping(
                tenant_bank_id=TENANT_BANK_ID,
                rail_type=rail_type,
                source_column_name=source_column_name,
                canonical_field_name=canonical_field_name,
                transform_type=transform_type,
                condition_column=condition_column,
                condition_value=condition_value,
            ))
            inserted += 1
    db.commit()
    return inserted


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        inserted = seed_pilot_mappings(db)
        print(f"inserted {inserted} mapping rows for tenant_bank_id={TENANT_BANK_ID!r}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
