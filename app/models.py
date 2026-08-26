from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Integer, String, UniqueConstraint

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CanonicalEvent(Base):
    """The merged, cross-rail view of a single transaction.

    Uniqueness is (tenant_bank_id, rail_type, transaction_id) -- never
    transaction_id alone, since the same transaction_id can recur across
    unrelated tenant banks.
    """

    __tablename__ = "canonical_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_bank_id", "rail_type", "transaction_id",
            name="uq_canonical_event_key",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    tenant_bank_id = Column(String, nullable=False, index=True)
    rail_type = Column(String, nullable=False, index=True)
    transaction_id = Column(String, nullable=False, index=True)

    payer_name = Column(String, nullable=True)
    payer_account_ref = Column(String, nullable=True)
    payee_name = Column(String, nullable=True)
    payee_account_ref = Column(String, nullable=True)

    amount = Column(Float, nullable=True)
    currency = Column(String, nullable=True)
    fees = Column(Float, nullable=True)

    # When the transaction actually happened, per the rail's own
    # created/received timestamp (e.g. cheque.issue_date, payment.created_at,
    # message.received_timestamp) -- distinct from first_seen_at/
    # last_updated_at below, which are OUR ingestion-time bookkeeping and
    # say nothing about real chronology. String/ISO 8601 (matching
    # expected_settlement_at's convention) rather than a true DateTime
    # column, so ingestion never has to worry about per-rail
    # timezone/format quirks; consumers (e.g. Track A's snapshotting)
    # parse it with datetime.fromisoformat() when they need to order or
    # window by it.
    transaction_occurred_at = Column(String, nullable=True, index=True)

    status = Column(String, nullable=True)
    risk_flags = Column(JSON, nullable=True)  # whichever of sanctions/aml/fraud/cvv/avs the rail provides

    # Raw source identifiers vs. resolved identifiers. source_* is set by
    # the Aligner (Step 2) at ingestion time; merchant_id/individual_id are
    # set later by Step 4's resolve_parties(), which looks up (or creates)
    # a row in merchants/individuals keyed on (source_*_id, tenant_bank_id)
    # and writes the canonical id back here. Kept as separate fields so
    # resolution logic can change (e.g. direct lookup -> fuzzy matching)
    # without touching this schema.
    source_merchant_id = Column(String, nullable=True)
    merchant_id = Column(String, nullable=True, index=True)  # FK -> merchants.merchant_id
    source_individual_id = Column(String, nullable=True)
    individual_id = Column(String, nullable=True, index=True)  # FK -> individuals.individual_id

    processor_name = Column(String, nullable=True)  # CARD rail only (Airwallex/Worldpay); null for all other rails
    onboarded_by = Column(String, nullable=True)

    # The counterparty-side bank name/identifier as given by the source
    # (e.g. a cheque's issuing bank, a wire's debtor-agent bank, a card's
    # merchant settlement bank). Distinct from tenant_bank_id, which is the
    # bank we ingested the file from -- this can name a different bank
    # entirely and its meaning shifts per rail, so it's just retained as-is.
    counterparty_bank_name = Column(String, nullable=True)

    # --- Anomaly-detection fields (Huntington "anomaly-ready" schema) ---
    # Each maps to one of that schema's six reusable building blocks. The
    # "headline" flag/status/join-key fields a detection engine would
    # filter or join on directly are promoted to their own column; the
    # rest of each block's supporting detail (counts, scores, timestamps,
    # references) is merged into that block's *_details JSON column via
    # JSON_MERGE, the same pattern risk_flags already uses -- no new
    # ingestion logic needed for either kind.
    #
    # fraud_risk_indicators.new_payee_risk: ACH/Wires/FedNow/Cheques
    # source is_new_payee/new_payee_risk_flag; Cards source their analog
    # new_merchant_risk.is_new_merchant. Same underlying concept -- "first
    # time paying this counterparty" -- so both map to one canonical flag.
    new_payee_risk_flag = Column(Boolean, nullable=True)
    # funnel_account_detection: not applicable to Cards (a card's
    # counterparty is a merchant, not a peer beneficiary) -- simply never
    # mapped for that rail, same as processor_name for non-CARD rails.
    funnel_account_flag = Column(Boolean, nullable=True)
    velocity_threshold_breached = Column(Boolean, nullable=True)
    structuring_flag = Column(Boolean, nullable=True)
    fraud_risk_details = Column(JSON, nullable=True)

    # network_response_control (real-time/switched rails; Cheques' analog
    # is clearing_network_response)
    network_timeout_flag = Column(Boolean, nullable=True)
    network_response_details = Column(JSON, nullable=True)

    # file_batch_tracking (batched rails: ACH, Cards, Cheques' cash
    # letter). batch_id covers whichever rail-specific identifier applies
    # (ach_batch.batch_id, cash_letter_id, settlement_batch_id) -- the
    # missing ingredient for detecting a batch that never reaches
    # settlement.
    batch_id = Column(String, nullable=True, index=True)
    file_reached_settlement = Column(Boolean, nullable=True)
    expected_settlement_at = Column(String, nullable=True)  # ISO 8601; ready-made SLA/timeout reference
    batch_tracking_details = Column(JSON, nullable=True)

    # retry_control (all rails). idempotency_key/original_transaction_id
    # are the join keys that let a genuine retry-duplicate be linked back
    # to its original attempt instead of showing up as two unrelated rows.
    is_retry = Column(Boolean, nullable=True)
    original_transaction_id = Column(String, nullable=True, index=True)
    idempotency_key = Column(String, nullable=True, index=True)
    duplicate_check_status = Column(String, nullable=True)
    retry_details = Column(JSON, nullable=True)

    # format_validation_details (all rails) -- structured rejection
    # code/reason/field errors, replacing a flat PASSED/FAILED read.
    format_validation_status = Column(String, nullable=True)
    format_validation_errors = Column(JSON, nullable=True)

    # reconciliation (all rails, post-settlement): network-settled amount
    # vs. ledger-posted amount side by side, so a break is a direct field
    # comparison rather than a cross-system join.
    reconciliation_status = Column(String, nullable=True)
    reconciliation_variance_amount = Column(Float, nullable=True)
    reconciliation_details = Column(JSON, nullable=True)

    snapshot_pre = Column(JSON, nullable=True)
    snapshot_post = Column(JSON, nullable=True)

    first_seen_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class SourceColumnMapping(Base):
    """Config, not code: how a tenant/rail's raw columns become canonical fields.

    A new bank, a renamed source column, or a new canonical field is handled
    by inserting/editing rows here -- never by branching on tenant_bank_id or
    rail_type in the ingestion code.

    condition_column/condition_value are optional: when set, this mapping
    row only applies to a given raw row if
    raw_row[condition_column] == condition_value. This lets one source
    column route to different canonical fields depending on another
    column's value (e.g. a shared "entity_id" column that should become
    source_merchant_id when entity_type == "MERCHANT" and
    source_individual_id when entity_type == "INDIVIDUAL"), without any
    per-field/per-rail conditional logic in the ingestion code itself --
    the condition is still just config.
    """

    __tablename__ = "source_column_mappings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_bank_id", "rail_type", "source_column_name",
            "canonical_field_name", "condition_value",
            name="uq_source_column_mapping_key",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_bank_id = Column(String, nullable=False, index=True)
    rail_type = Column(String, nullable=False, index=True)
    source_column_name = Column(String, nullable=False)
    canonical_field_name = Column(String, nullable=False)
    transform_type = Column(String, nullable=False, default="DIRECT")
    condition_column = Column(String, nullable=True)
    condition_value = Column(String, nullable=True)


class Merchant(Base):
    """Canonical merchant registry, written by Step 4 resolution.

    In this pilot, resolution is a direct lookup keyed on
    (source_merchant_id, tenant_bank_id) -- deliberately scoped per tenant
    since the same source_merchant_id can recur across unrelated tenants
    (same reason canonical_events isn't keyed on transaction_id alone).
    """

    __tablename__ = "merchants"
    __table_args__ = (
        UniqueConstraint(
            "source_merchant_id", "tenant_bank_id",
            name="uq_merchant_source_per_tenant",
        ),
    )

    merchant_id = Column(String, primary_key=True)  # "MER-XXXXXXXX"
    source_merchant_id = Column(String, nullable=False, index=True)
    tenant_bank_id = Column(String, nullable=False, index=True)

    legal_name = Column(String, nullable=True)
    trade_name = Column(String, nullable=True)
    merchant_location = Column(JSON, nullable=True)
    merchant_account = Column(JSON, nullable=True)
    processor_name = Column(String, nullable=True)
    onboarded_by = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class Individual(Base):
    """Canonical individual (account holder) registry, written by Step 4 resolution."""

    __tablename__ = "individuals"
    __table_args__ = (
        UniqueConstraint(
            "source_individual_id", "tenant_bank_id",
            name="uq_individual_source_per_tenant",
        ),
    )

    individual_id = Column(String, primary_key=True)  # "IND-XXXXXXXX"
    source_individual_id = Column(String, nullable=False, index=True)
    tenant_bank_id = Column(String, nullable=False, index=True)

    full_name = Column(String, nullable=True)
    account_ref = Column(String, nullable=True)
    account_type = Column(String, nullable=True)
    onboarded_by = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class PartyFeatures(Base):
    """Step 5: per-party aggregates over canonical_events, computed by
    compute_features() (feature_store.py). Fully derived data -- recomputed
    and replaced wholesale on each run, never merged field-by-field like
    canonical_events.

    One row per resolved party (a merchant OR an individual). party_id is
    treated as globally unique the same way merchants.merchant_id /
    individuals.individual_id already are (random MER-/IND- ids, not
    reused across tenants), so a single table serves both instead of two
    parallel ones -- party_type just says which registry it summarizes.
    """

    __tablename__ = "party_features"

    party_id = Column(String, primary_key=True)  # merchant_id or individual_id
    party_type = Column(String, nullable=False)  # "MERCHANT" or "INDIVIDUAL"
    tenant_bank_id = Column(String, nullable=False, index=True)

    transaction_count = Column(Integer, nullable=False, default=0)
    total_amount = Column(Float, nullable=True)
    avg_amount = Column(Float, nullable=True)
    rails_active = Column(JSON, nullable=True)
    distinct_counterparties = Column(Integer, nullable=True)

    # Each *_rate is (flagged count) / (count of transactions where that
    # signal was actually evaluated) -- never divides by transactions
    # where the rail doesn't produce that field at all, and null (not 0)
    # when the signal was never evaluated for this party, so "never
    # flagged" and "never applicable" aren't conflated.
    new_payee_risk_rate = Column(Float, nullable=True)
    funnel_account_rate = Column(Float, nullable=True)
    velocity_breach_rate = Column(Float, nullable=True)
    structuring_rate = Column(Float, nullable=True)
    network_timeout_rate = Column(Float, nullable=True)
    is_retry_rate = Column(Float, nullable=True)
    format_reject_rate = Column(Float, nullable=True)
    reconciliation_break_rate = Column(Float, nullable=True)

    first_seen_at = Column(DateTime(timezone=True), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    computed_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class IngestionLog(Base):
    __tablename__ = "ingestion_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_name = Column(String, nullable=False)
    tenant_bank_id = Column(String, nullable=False)
    rail_type = Column(String, nullable=False)
    settlement_stage = Column(String, nullable=False)
    row_count = Column(Integer, nullable=False, default=0)
    rows_mapped = Column(Integer, nullable=False, default=0)
    rows_failed = Column(Integer, nullable=False, default=0)
    ingested_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    errors = Column(JSON, nullable=True, default=list)
