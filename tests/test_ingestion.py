from __future__ import annotations

import io

import pandas as pd

from app.ingestion import process_file
from app.models import CanonicalEvent, IngestionLog, SourceColumnMapping


def _seed_mappings(db):
    rows = [
        ("wire_ref", "transaction_id", "DIRECT"),
        ("debtor_name", "payer_name", "RENAME"),
        ("creditor_name", "payee_name", "RENAME"),
        ("amount_usd", "amount", "RENAME"),
        ("currency", "currency", "DIRECT"),
        ("biz_id", "source_merchant_id", "RENAME"),
    ]
    for source, canonical, transform in rows:
        db.add(SourceColumnMapping(
            tenant_bank_id="KEYBANK", rail_type="WIRE",
            source_column_name=source, canonical_field_name=canonical,
            transform_type=transform,
        ))
    db.commit()


def _csv_bytes(rows: list[dict]) -> bytes:
    df = pd.DataFrame(rows)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8")


def test_generic_mapping_loop_populates_and_leaves_gaps_null(db_session):
    _seed_mappings(db_session)
    content = _csv_bytes([{
        "wire_ref": "WIRE-100",
        "debtor_name": "Eve",
        "creditor_name": "Acme Co",
        "amount_usd": "999.99",
        "currency": "USD",
        "biz_id": "MERCH-1",
        "not_in_config": "surprise",
    }])

    log = process_file(
        db_session, tenant_bank_id="KEYBANK", rail_type="WIRE",
        settlement_stage="PRE", filename="test.csv", content=content,
    )

    assert isinstance(log, IngestionLog)
    assert log.row_count == 1
    assert log.rows_mapped == 1
    assert log.rows_failed == 0
    assert any(
        e.get("type") == "unmapped_columns" and "not_in_config" in e.get("columns", [])
        for e in log.errors
    )

    event = db_session.query(CanonicalEvent).filter_by(transaction_id="WIRE-100").one()
    assert event.payer_name == "Eve"
    assert event.payee_name == "Acme Co"
    assert event.amount == 999.99
    assert event.currency == "USD"

    # source_merchant_id is set by the Aligner; merchant_id is left for
    # Step 4's resolve_parties() to populate -- ingestion alone never sets it.
    assert event.source_merchant_id == "MERCH-1"
    assert event.merchant_id is None
    assert event.source_individual_id is None
    assert event.individual_id is None

    # No mapping row targets processor_name for this tenant/rail -- the
    # generic loop simply never sets it, no conditional required.
    assert event.processor_name is None

    # Raw row retained in full, including the unmapped column.
    assert event.snapshot_pre["not_in_config"] == "surprise"
    assert event.snapshot_post is None


def test_unmapped_column_logged_but_does_not_crash(db_session):
    _seed_mappings(db_session)
    content = _csv_bytes([{
        "wire_ref": "WIRE-101",
        "debtor_name": "Frank",
        "creditor_name": "Beta Inc",
        "amount_usd": "10.00",
        "currency": "USD",
        "biz_id": "MERCH-2",
        "mystery_column_a": "x",
        "mystery_column_b": "y",
    }])

    log = process_file(
        db_session, tenant_bank_id="KEYBANK", rail_type="WIRE",
        settlement_stage="PRE", filename="test2.csv", content=content,
    )

    assert log.rows_failed == 0
    unmapped_entries = [e for e in log.errors if e.get("type") == "unmapped_columns"]
    assert len(unmapped_entries) == 1
    assert set(unmapped_entries[0]["columns"]) == {"mystery_column_a", "mystery_column_b"}


def test_boolean_and_json_detail_fields_map_correctly(db_session):
    # Anomaly-detection fields: boolean flags plus a *_details JSON bucket,
    # same JSON_MERGE mechanism as risk_flags.
    rows = [
        ("wire_ref", "transaction_id", "DIRECT", None, None),
        ("is_new_payee", "new_payee_risk_flag", "DIRECT", None, None),
        ("funnel_flag", "funnel_account_flag", "DIRECT", None, None),
        ("ach_batch_id", "batch_id", "DIRECT", None, None),
        ("payee_relationship_age_days", "fraud_risk_details", "JSON_MERGE", None, None),
        ("velocity_score", "fraud_risk_details", "JSON_MERGE", None, None),
    ]
    for source, canonical, transform, cond_col, cond_val in rows:
        db_session.add(SourceColumnMapping(
            tenant_bank_id="KEYBANK", rail_type="WIRE",
            source_column_name=source, canonical_field_name=canonical,
            transform_type=transform, condition_column=cond_col, condition_value=cond_val,
        ))
    db_session.commit()

    content = _csv_bytes([{
        "wire_ref": "WIRE-200",
        "is_new_payee": "true",
        "funnel_flag": "False",
        "ach_batch_id": "BATCH-42",
        "payee_relationship_age_days": "0",
        "velocity_score": "17",
    }])

    log = process_file(
        db_session, tenant_bank_id="KEYBANK", rail_type="WIRE",
        settlement_stage="PRE", filename="test4.csv", content=content,
    )

    assert log.rows_failed == 0
    event = db_session.query(CanonicalEvent).filter_by(transaction_id="WIRE-200").one()
    assert event.new_payee_risk_flag is True
    assert event.funnel_account_flag is False
    assert event.batch_id == "BATCH-42"
    assert event.fraud_risk_details == {"payee_relationship_age_days": "0", "velocity_score": "17"}


def test_unparseable_boolean_is_logged_not_fatal(db_session):
    db_session.add(SourceColumnMapping(
        tenant_bank_id="KEYBANK", rail_type="WIRE",
        source_column_name="wire_ref", canonical_field_name="transaction_id",
        transform_type="DIRECT",
    ))
    db_session.add(SourceColumnMapping(
        tenant_bank_id="KEYBANK", rail_type="WIRE",
        source_column_name="is_new_payee", canonical_field_name="new_payee_risk_flag",
        transform_type="DIRECT",
    ))
    db_session.commit()

    content = _csv_bytes([{"wire_ref": "WIRE-201", "is_new_payee": "maybe"}])
    log = process_file(
        db_session, tenant_bank_id="KEYBANK", rail_type="WIRE",
        settlement_stage="PRE", filename="test5.csv", content=content,
    )

    assert log.rows_mapped == 0
    assert log.rows_failed == 1
    assert any(e.get("type") == "row_error" for e in log.errors)
    assert db_session.query(CanonicalEvent).filter_by(transaction_id="WIRE-201").count() == 0


def test_row_missing_transaction_id_is_logged_not_fatal(db_session):
    # No mapping targets "transaction_id" at all for this tenant/rail.
    db_session.add(SourceColumnMapping(
        tenant_bank_id="MTBANK", rail_type="WIRE",
        source_column_name="payer", canonical_field_name="payer_name",
        transform_type="RENAME",
    ))
    db_session.commit()

    content = _csv_bytes([{"payer": "Gina"}])
    log = process_file(
        db_session, tenant_bank_id="MTBANK", rail_type="WIRE",
        settlement_stage="PRE", filename="test3.csv", content=content,
    )

    assert log.row_count == 1
    assert log.rows_mapped == 0
    assert log.rows_failed == 1
    assert any(e.get("type") == "row_error" for e in log.errors)
    assert db_session.query(CanonicalEvent).count() == 0
