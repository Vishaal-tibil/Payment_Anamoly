"""Seeds source_column_mappings for the Meridian Trust Bank pilot dataset
(Raw_data/split/meridian_trust_bank_<rail>_<pre|post>.csv).

This is the SECOND generation of this tenant's data. The first generation
used simple flat column names shared across all 5 rails (entity_name,
amount, issuer_bank, ...). This generation flattens the full nested
anomaly-ready schema using dot-notation paths (cheque.micr.routing_number,
fraud_risk_indicators.new_payee_risk.is_new_payee, ...), and -- critically
-- each rail now has its OWN distinct set of column paths, mirroring the
rail's own nested JSON shape. There is no shared column layout across
rails any more, so unlike every mapping config before this one, each rail
gets its own mapping list below rather than one list reused for all 5.

Because the column names changed wholesale, this script REPLACES (deletes,
then re-inserts) this tenant's mapping rows rather than only adding when
empty -- the old rows are dead config against the new files, not a subset
of it.

Usage: .venv/Scripts/python.exe scripts/seed_meridian_mappings.py
"""
from __future__ import annotations

from app.database import Base, SessionLocal, engine
from app.models import SourceColumnMapping

TENANT_BANK_ID = "MERIDIAN_TRUST_BANK"

# (source_column_name, canonical_field_name, transform_type, condition_column, condition_value)
Mapping = tuple[str, str, str, str | None, str | None]

# Identical across all 5 rails: the entity_type-conditional identity
# routing, and the retry_control / reconciliation / velocity / structuring
# blocks, whose field names don't vary by rail (only funnel/new_payee do,
# handled per-rail below since Cards lack funnel and use new_merchant_risk
# instead of new_payee_risk).
_COMMON: list[Mapping] = [
    ("transaction_id", "transaction_id", "DIRECT", None, None),
    ("entity_id", "source_merchant_id", "DIRECT", "entity_type", "MERCHANT"),
    ("entity_id", "source_individual_id", "DIRECT", "entity_type", "INDIVIDUAL"),

    ("retry_control.is_retry", "is_retry", "DIRECT", None, None),
    ("retry_control.original_transaction_id", "original_transaction_id", "DIRECT", None, None),
    ("retry_control.idempotency_key", "idempotency_key", "DIRECT", None, None),
    ("retry_control.duplicate_check_status", "duplicate_check_status", "DIRECT", None, None),
    ("retry_control.retry_count", "retry_details", "JSON_MERGE", None, None),
    ("retry_control.retry_reason", "retry_details", "JSON_MERGE", None, None),

    ("reconciliation.reconciliation_status", "reconciliation_status", "DIRECT", None, None),
    ("reconciliation.variance_amount", "reconciliation_variance_amount", "DIRECT", None, None),
    ("reconciliation.reconciliation_timestamp", "reconciliation_details", "JSON_MERGE", None, None),
    ("reconciliation.amount_match", "reconciliation_details", "JSON_MERGE", None, None),
    ("reconciliation.ledger_transaction_id", "reconciliation_details", "JSON_MERGE", None, None),
    ("reconciliation.network_settlement_amount", "reconciliation_details", "JSON_MERGE", None, None),
    ("reconciliation.ledger_posted_amount", "reconciliation_details", "JSON_MERGE", None, None),
    ("reconciliation.network_settlement_reference", "reconciliation_details", "JSON_MERGE", None, None),

    ("fraud_risk_indicators.velocity_checks.velocity_threshold_breached", "velocity_threshold_breached", "DIRECT", None, None),
    ("fraud_risk_indicators.velocity_checks.transaction_count_1h", "fraud_risk_details", "JSON_MERGE", None, None),
    ("fraud_risk_indicators.velocity_checks.transaction_count_24h", "fraud_risk_details", "JSON_MERGE", None, None),
    ("fraud_risk_indicators.velocity_checks.cumulative_amount_24h", "fraud_risk_details", "JSON_MERGE", None, None),
    ("fraud_risk_indicators.velocity_checks.velocity_baseline_avg_24h", "fraud_risk_details", "JSON_MERGE", None, None),
    ("fraud_risk_indicators.velocity_checks.velocity_score", "fraud_risk_details", "JSON_MERGE", None, None),

    ("fraud_risk_indicators.structuring_detection.structuring_flag", "structuring_flag", "DIRECT", None, None),
    ("fraud_risk_indicators.structuring_detection.reporting_threshold_amount", "fraud_risk_details", "JSON_MERGE", None, None),
    ("fraud_risk_indicators.structuring_detection.amount_to_threshold_ratio", "fraud_risk_details", "JSON_MERGE", None, None),
    ("fraud_risk_indicators.structuring_detection.related_transactions_24h", "fraud_risk_details", "JSON_MERGE", None, None),

    ("file_batch_tracking.expected_settlement_timestamp", "expected_settlement_at", "DIRECT", None, None),
    ("file_batch_tracking.file_reached_settlement", "file_reached_settlement", "DIRECT", None, None),
    ("file_batch_tracking.file_ack_status", "batch_tracking_details", "JSON_MERGE", None, None),
    ("file_batch_tracking.actual_settlement_timestamp", "batch_tracking_details", "JSON_MERGE", None, None),
    ("file_batch_tracking.settlement_lag_minutes", "batch_tracking_details", "JSON_MERGE", None, None),
    ("file_batch_tracking.batch_status", "batch_tracking_details", "JSON_MERGE", None, None),
]

# new_payee_risk + funnel_account_detection: every rail except Card.
_NEW_PAYEE_AND_FUNNEL: list[Mapping] = [
    ("fraud_risk_indicators.new_payee_risk.new_payee_risk_flag", "new_payee_risk_flag", "DIRECT", None, None),
    ("fraud_risk_indicators.new_payee_risk.is_new_payee", "fraud_risk_details", "JSON_MERGE", None, None),
    ("fraud_risk_indicators.new_payee_risk.payee_account_first_seen_date", "fraud_risk_details", "JSON_MERGE", None, None),
    ("fraud_risk_indicators.new_payee_risk.prior_transaction_count_with_payee", "fraud_risk_details", "JSON_MERGE", None, None),
    ("fraud_risk_indicators.new_payee_risk.payee_relationship_age_days", "fraud_risk_details", "JSON_MERGE", None, None),

    ("fraud_risk_indicators.funnel_account_detection.funnel_account_flag", "funnel_account_flag", "DIRECT", None, None),
    ("fraud_risk_indicators.funnel_account_detection.beneficiary_account_number", "fraud_risk_details", "JSON_MERGE", None, None),
    ("fraud_risk_indicators.funnel_account_detection.distinct_originating_accounts_24h", "fraud_risk_details", "JSON_MERGE", None, None),
    ("fraud_risk_indicators.funnel_account_detection.distinct_originating_accounts_7d", "fraud_risk_details", "JSON_MERGE", None, None),
    ("fraud_risk_indicators.funnel_account_detection.related_transaction_ids", "fraud_risk_details", "JSON_MERGE", None, None),

    ("fraud_risk_indicators.structuring_detection.aggregate_amount_24h_same_originator", "fraud_risk_details", "JSON_MERGE", None, None),
]

# network_response_control: every rail except Cheque (which calls its
# analog clearing_network_response, handled in the Cheque list directly).
_NETWORK_RESPONSE_STANDARD: list[Mapping] = [
    ("network_response_control.network_timeout_flag", "network_timeout_flag", "DIRECT", None, None),
    ("network_response_control.request_timestamp", "network_response_details", "JSON_MERGE", None, None),
    ("network_response_control.response_received", "network_response_details", "JSON_MERGE", None, None),
    ("network_response_control.response_timestamp", "network_response_details", "JSON_MERGE", None, None),
    ("network_response_control.response_time_ms", "network_response_details", "JSON_MERGE", None, None),
    ("network_response_control.expected_response_sla_ms", "network_response_details", "JSON_MERGE", None, None),
    ("network_response_control.response_code", "network_response_details", "JSON_MERGE", None, None),
]

_MAPPINGS_BY_RAIL: dict[str, list[Mapping]] = {
    "CHEQUE": [
        *_COMMON, *_NEW_PAYEE_AND_FUNNEL,
        ("payee.payee_name", "payee_name", "DIRECT", None, None),
        ("payee.depositary_bank.depositor_account_number", "payee_account_ref", "LAST4_MASK", None, None),
        ("cheque.micr.account_number", "payer_account_ref", "LAST4_MASK", None, None),
        ("cheque.amount", "amount", "DIRECT", None, None),
        ("cheque.currency", "currency", "DIRECT", None, None),
        ("cheque.micr.issuer_bank", "counterparty_bank_name", "DIRECT", None, None),
        ("cheque.micr.onboarded_by", "onboarded_by", "DIRECT", None, None),
        ("settlement.status", "status", "NORMALIZE_ENUM", None, None),
        ("file_batch_tracking.cash_letter_id", "batch_id", "DIRECT", None, None),
        ("cheque.issue_date", "transaction_occurred_at", "DIRECT", None, None),

        ("clearing_network_response.network_timeout_flag", "network_timeout_flag", "DIRECT", None, None),
        ("clearing_network_response.request_timestamp", "network_response_details", "JSON_MERGE", None, None),
        ("clearing_network_response.response_received", "network_response_details", "JSON_MERGE", None, None),
        ("clearing_network_response.response_timestamp", "network_response_details", "JSON_MERGE", None, None),
        ("clearing_network_response.response_time_ms", "network_response_details", "JSON_MERGE", None, None),
        ("clearing_network_response.expected_response_sla_ms", "network_response_details", "JSON_MERGE", None, None),
        ("clearing_network_response.response_code", "network_response_details", "JSON_MERGE", None, None),

        ("validation.format_validation_details.format_validation_status", "format_validation_status", "DIRECT", None, None),
        ("validation.format_validation_details.rejection_code", "format_validation_errors", "JSON_MERGE", None, None),
        ("validation.format_validation_details.rejection_reason", "format_validation_errors", "JSON_MERGE", None, None),
        ("validation.format_validation_details.field_level_errors", "format_validation_errors", "JSON_MERGE", None, None),

        ("validation.micr_validation", "risk_flags", "JSON_MERGE", None, None),
        ("validation.amount_validation", "risk_flags", "JSON_MERGE", None, None),
        ("validation.image_quality", "risk_flags", "JSON_MERGE", None, None),
        ("validation.signature_verification", "risk_flags", "JSON_MERGE", None, None),
        ("validation.fraud_review", "risk_flags", "JSON_MERGE", None, None),

        ("presentment.status", "batch_tracking_details", "JSON_MERGE", None, None),
        ("presentment.presentment_id", "batch_tracking_details", "JSON_MERGE", None, None),
        ("presentment.presentment_date", "batch_tracking_details", "JSON_MERGE", None, None),
        ("presentment.clearing_channel", "batch_tracking_details", "JSON_MERGE", None, None),
    ],

    "ACH": [
        *_COMMON, *_NEW_PAYEE_AND_FUNNEL, *_NETWORK_RESPONSE_STANDARD,
        ("originator.name", "payer_name", "DIRECT", None, None),
        ("receiver.name", "payee_name", "DIRECT", None, None),
        ("originator.account_number", "payer_account_ref", "LAST4_MASK", None, None),
        ("receiver.account_number", "payee_account_ref", "LAST4_MASK", None, None),
        ("payment.amount", "amount", "DIRECT", None, None),
        ("payment.currency", "currency", "DIRECT", None, None),
        ("settlement.status", "status", "NORMALIZE_ENUM", None, None),
        ("originator.onboarded_by", "onboarded_by", "DIRECT", None, None),
        ("ach_batch.batch_id", "batch_id", "DIRECT", None, None),
        ("timestamps.received_at", "transaction_occurred_at", "DIRECT", None, None),

        ("validation.format_validation_details.format_validation_status", "format_validation_status", "DIRECT", None, None),
        ("validation.format_validation_details.rejection_code", "format_validation_errors", "JSON_MERGE", None, None),
        ("validation.format_validation_details.rejection_reason", "format_validation_errors", "JSON_MERGE", None, None),
        ("validation.format_validation_details.field_level_errors", "format_validation_errors", "JSON_MERGE", None, None),

        ("validation.routing_validation", "risk_flags", "JSON_MERGE", None, None),
        ("validation.account_validation", "risk_flags", "JSON_MERGE", None, None),
        ("validation.format_validation", "risk_flags", "JSON_MERGE", None, None),
        ("validation.fraud_review", "risk_flags", "JSON_MERGE", None, None),

        ("ach_batch.sec_code", "batch_tracking_details", "JSON_MERGE", None, None),
        ("ach_batch.service_class_code", "batch_tracking_details", "JSON_MERGE", None, None),
        ("ach_batch.batch_number", "batch_tracking_details", "JSON_MERGE", None, None),
        ("ach_file.file_id", "batch_tracking_details", "JSON_MERGE", None, None),
        ("trace.trace_number", "batch_tracking_details", "JSON_MERGE", None, None),
    ],

    "WIRE": [
        *_COMMON, *_NEW_PAYEE_AND_FUNNEL, *_NETWORK_RESPONSE_STANDARD,
        ("debtor.name", "payer_name", "DIRECT", None, None),
        ("creditor.name", "payee_name", "DIRECT", None, None),
        ("debtor.account_number", "payer_account_ref", "LAST4_MASK", None, None),
        ("creditor.account_number", "payee_account_ref", "LAST4_MASK", None, None),
        ("payment.amount", "amount", "DIRECT", None, None),
        ("payment.currency", "currency", "DIRECT", None, None),
        ("settlement.status", "status", "NORMALIZE_ENUM", None, None),
        ("creditor.onboarded_by", "onboarded_by", "DIRECT", None, None),
        # creditor_agent is the counterparty's (creditor's) bank -- not
        # debtor_agent, which is the entity's own bank.
        ("creditor_agent.name", "counterparty_bank_name", "DIRECT", None, None),
        ("audit.received_timestamp", "transaction_occurred_at", "DIRECT", None, None),

        ("format_validation_details.format_validation_status", "format_validation_status", "DIRECT", None, None),
        ("format_validation_details.rejection_code", "format_validation_errors", "JSON_MERGE", None, None),
        ("format_validation_details.rejection_reason", "format_validation_errors", "JSON_MERGE", None, None),
        ("format_validation_details.field_level_errors", "format_validation_errors", "JSON_MERGE", None, None),

        ("processing.account_validation", "risk_flags", "JSON_MERGE", None, None),
        ("processing.sanctions_screening", "risk_flags", "JSON_MERGE", None, None),
        ("processing.aml_screening", "risk_flags", "JSON_MERGE", None, None),
        ("processing.fraud_screening", "risk_flags", "JSON_MERGE", None, None),
        ("processing.compliance_status", "risk_flags", "JSON_MERGE", None, None),
    ],

    "FEDNOW": [
        *_COMMON, *_NEW_PAYEE_AND_FUNNEL, *_NETWORK_RESPONSE_STANDARD,
        # PRE uses flat debtor/creditor; POST restructures under "parties".
        ("debtor.name", "payer_name", "DIRECT", None, None),
        ("parties.debtor.name", "payer_name", "DIRECT", None, None),
        ("creditor.name", "payee_name", "DIRECT", None, None),
        ("parties.creditor.name", "payee_name", "DIRECT", None, None),
        ("debtor.account_number", "payer_account_ref", "LAST4_MASK", None, None),
        ("parties.debtor.account_number", "payer_account_ref", "LAST4_MASK", None, None),
        ("creditor.account_number", "payee_account_ref", "LAST4_MASK", None, None),
        ("parties.creditor.account_number", "payee_account_ref", "LAST4_MASK", None, None),
        ("amount.value", "amount", "DIRECT", None, None),
        ("amount.currency", "currency", "DIRECT", None, None),
        # settlement.status only appears post-settlement; status is null
        # for FedNow PRE rows until POST arrives (same field absent both
        # stages would just stay null -- expected, not a gap to fix).
        ("settlement.status", "status", "NORMALIZE_ENUM", None, None),
        ("creditor.bank.issuer_name", "counterparty_bank_name", "DIRECT", None, None),
        ("parties.creditor.bank_name", "counterparty_bank_name", "DIRECT", None, None),
        ("creditor.bank.onboarded_by", "onboarded_by", "DIRECT", None, None),
        ("parties.creditor.onboarded_by", "onboarded_by", "DIRECT", None, None),
        ("message.received_timestamp", "transaction_occurred_at", "DIRECT", None, None),

        ("validation.format_validation_details.format_validation_status", "format_validation_status", "DIRECT", None, None),
        ("validation.format_validation_details.rejection_code", "format_validation_errors", "JSON_MERGE", None, None),
        ("validation.format_validation_details.rejection_reason", "format_validation_errors", "JSON_MERGE", None, None),
        ("validation.format_validation_details.field_level_errors", "format_validation_errors", "JSON_MERGE", None, None),

        ("risk_compliance.fraud_status", "risk_flags", "JSON_MERGE", None, None),
        ("risk_compliance.sanctions_status", "risk_flags", "JSON_MERGE", None, None),
        ("risk_compliance.aml_status", "risk_flags", "JSON_MERGE", None, None),
        ("risk_compliance.compliance_status", "risk_flags", "JSON_MERGE", None, None),
        ("validation.message_validation", "risk_flags", "JSON_MERGE", None, None),
        ("validation.participant_validation", "risk_flags", "JSON_MERGE", None, None),
        ("validation.account_validation", "risk_flags", "JSON_MERGE", None, None),
        ("validation.duplicate_check", "risk_flags", "JSON_MERGE", None, None),
    ],

    "CARD": [
        *_COMMON, *_NETWORK_RESPONSE_STANDARD,
        # Cards have no funnel-account block (a card's counterparty is a
        # merchant, not a peer beneficiary) and use new_merchant_risk
        # instead of new_payee_risk -- same canonical flag either way.
        ("fraud_risk_indicators.new_merchant_risk.new_merchant_risk_flag", "new_payee_risk_flag", "DIRECT", None, None),
        ("fraud_risk_indicators.new_merchant_risk.is_new_merchant", "fraud_risk_details", "JSON_MERGE", None, None),
        ("fraud_risk_indicators.new_merchant_risk.merchant_first_seen_date", "fraud_risk_details", "JSON_MERGE", None, None),
        ("fraud_risk_indicators.new_merchant_risk.prior_transaction_count_with_merchant", "fraud_risk_details", "JSON_MERGE", None, None),
        ("fraud_risk_indicators.new_merchant_risk.merchant_relationship_age_days", "fraud_risk_details", "JSON_MERGE", None, None),
        ("fraud_risk_indicators.structuring_detection.aggregate_amount_24h_same_card", "fraud_risk_details", "JSON_MERGE", None, None),

        ("merchant.legal_name", "payee_name", "DIRECT", None, None),
        ("transaction.amount", "amount", "DIRECT", None, None),
        ("transaction.currency", "currency", "DIRECT", None, None),
        ("settlement.status", "status", "NORMALIZE_ENUM", None, None),
        ("merchant.payment_processor_name", "processor_name", "DIRECT", None, None),
        ("merchant.merchant_account.onboarded_by", "onboarded_by", "DIRECT", None, None),
        ("merchant.merchant_account.issuer_bank", "counterparty_bank_name", "DIRECT", None, None),
        ("merchant.merchant_account.account_number", "payee_account_ref", "LAST4_MASK", None, None),
        ("file_batch_tracking.settlement_batch_id", "batch_id", "DIRECT", None, None),
        ("settlement.fees.total", "fees", "DIRECT", None, None),
        ("payment.created_at", "transaction_occurred_at", "DIRECT", None, None),

        ("format_validation_details.format_validation_status", "format_validation_status", "DIRECT", None, None),
        ("format_validation_details.rejection_code", "format_validation_errors", "JSON_MERGE", None, None),
        ("format_validation_details.rejection_reason", "format_validation_errors", "JSON_MERGE", None, None),
        ("format_validation_details.field_level_errors", "format_validation_errors", "JSON_MERGE", None, None),

        ("risk.fraud_status", "risk_flags", "JSON_MERGE", None, None),
        ("risk.cvv_result", "risk_flags", "JSON_MERGE", None, None),
        ("risk.avs_result", "risk_flags", "JSON_MERGE", None, None),
        ("dispute.status", "risk_flags", "JSON_MERGE", None, None),
        ("dispute.chargeback_amount", "risk_flags", "JSON_MERGE", None, None),
    ],
}


def reseed_meridian_mappings(db) -> int:
    db.query(SourceColumnMapping).filter_by(tenant_bank_id=TENANT_BANK_ID).delete()
    inserted = 0
    for rail_type, rows in _MAPPINGS_BY_RAIL.items():
        for source_column_name, canonical_field_name, transform_type, condition_column, condition_value in rows:
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
        inserted = reseed_meridian_mappings(db)
        print(f"replaced mapping rows for tenant_bank_id={TENANT_BANK_ID!r}: {inserted} inserted")
    finally:
        db.close()


if __name__ == "__main__":
    main()
