from __future__ import annotations

from sqlalchemy.orm import Session

from .models import SourceColumnMapping

# Sample source_column_mappings for the KEYBANK sample data in
# sample_data/. This is config, not ingestion logic -- a new bank, a
# renamed column, or a new field is handled by adding rows here (or via
# the source_column_mappings table directly), never by editing ingestion.py.
TENANT_BANK_ID = "KEYBANK"

# (rail_type, source_column_name, canonical_field_name, transform_type)
_SAMPLE_MAPPINGS: list[tuple[str, str, str, str]] = [
    # --- WIRE (keybank_wire_pre.csv / keybank_wire_post.csv) ---
    ("WIRE", "wire_ref", "transaction_id", "DIRECT"),
    ("WIRE", "debtor_name", "payer_name", "RENAME"),
    ("WIRE", "debtor_account_number", "payer_account_ref", "LAST4_MASK"),
    ("WIRE", "creditor_name", "payee_name", "RENAME"),
    ("WIRE", "creditor_account_number", "payee_account_ref", "LAST4_MASK"),
    ("WIRE", "instructed_amount", "amount", "RENAME"),   # PRE file's column for amount
    ("WIRE", "settled_amount", "amount", "RENAME"),      # POST file's column for amount
    ("WIRE", "currency", "currency", "DIRECT"),
    ("WIRE", "wire_fee", "fees", "RENAME"),
    ("WIRE", "wire_status", "status", "NORMALIZE_ENUM"),
    ("WIRE", "ofac_screen_result", "risk_flags", "JSON_MERGE"),
    ("WIRE", "aml_screen_result", "risk_flags", "JSON_MERGE"),
    ("WIRE", "business_id", "source_merchant_id", "RENAME"),
    ("WIRE", "relationship_manager", "onboarded_by", "RENAME"),

    # --- CARD (keybank_card_pre.csv / keybank_card_post.csv) ---
    # legal_name maps to payer_name, not payee_name: the resolved
    # merchant here IS the entity (source_merchant_id comes from the same
    # "merchant_id" column), so its name belongs in payer_name under the
    # entity->payer_name convention every tenant's config follows -- Step
    # 4's resolver reads payer_name only, with no payee_name fallback.
    ("CARD", "transaction_id", "transaction_id", "DIRECT"),
    ("CARD", "merchant_id", "source_merchant_id", "DIRECT"),
    ("CARD", "legal_name", "payer_name", "DIRECT"),
    ("CARD", "amount", "amount", "DIRECT"),
    ("CARD", "currency", "currency", "DIRECT"),
    ("CARD", "status", "status", "DIRECT"),
    ("CARD", "processor", "processor_name", "DIRECT"),
    ("CARD", "onboarded_by", "onboarded_by", "DIRECT"),
]


def seed_sample_mappings_if_empty(db: Session) -> None:
    if db.query(SourceColumnMapping).first() is not None:
        return
    for rail_type, source_column_name, canonical_field_name, transform_type in _SAMPLE_MAPPINGS:
        db.add(SourceColumnMapping(
            tenant_bank_id=TENANT_BANK_ID,
            rail_type=rail_type,
            source_column_name=source_column_name,
            canonical_field_name=canonical_field_name,
            transform_type=transform_type,
        ))
    db.commit()
