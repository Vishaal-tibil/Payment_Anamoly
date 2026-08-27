from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Integer, String

from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EntitySnapshot(Base):
    """Track A output: behavioral feature snapshots for the unsupervised
    anomaly-detection engine (Isolation Forest / HDBSCAN / time-series).

    NOT the same thing as party_features (Step 5). party_features rolls up
    the source's own pre-computed risk flags for dashboards/other engines;
    this table is built exclusively from raw canonical_events facts
    (amount, transaction_occurred_at, payee_name, is_retry,
    network_response_details, format_validation_status) so it never leaks
    the answer this engine is trying to (re)discover. See features.py's
    module docstring for the exact exclusion list and why.

    Merchants get one row per (party, week) they were active in -- they
    have enough history (19-45 txns) for weekly windows to mean something.
    Individuals get exactly one row, a to-date snapshot -- with a median
    of 2 transactions each, windowing would be almost entirely empty rows.
    window_type says which kind a given row is.
    """

    __tablename__ = "anomaly_entity_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)

    party_id = Column(String, nullable=False, index=True)  # merchant_id or individual_id
    party_type = Column(String, nullable=False)  # "MERCHANT" or "INDIVIDUAL"
    tenant_bank_id = Column(String, nullable=False, index=True)
    segment = Column(String, nullable=False, index=True)  # cohort key a model is trained per; == party_type today

    window_type = Column(String, nullable=False)  # "WEEKLY" or "TO_DATE"
    window_start = Column(DateTime(timezone=True), nullable=True)  # null for TO_DATE
    window_end = Column(DateTime(timezone=True), nullable=False)

    transaction_count = Column(Integer, nullable=False, default=0)
    amount_total = Column(Float, nullable=True)
    amount_avg = Column(Float, nullable=True)
    amount_median = Column(Float, nullable=True)
    amount_std = Column(Float, nullable=True)

    unique_counterparties = Column(Integer, nullable=True)
    new_counterparty_ratio = Column(Float, nullable=True)
    retry_ratio = Column(Float, nullable=True)

    avg_response_time_ms = Column(Float, nullable=True)
    timeout_ratio = Column(Float, nullable=True)  # response_time_ms > expected_response_sla_ms, computed by us
    format_reject_ratio = Column(Float, nullable=True)

    rails_used = Column(JSON, nullable=True)
    account_age_days = Column(Float, nullable=True)  # days since this party's first-ever transaction

    # Chronological train/test split (Section 6). Only meaningful for
    # WEEKLY (merchant) rows -- a single TO_DATE row per individual has
    # nothing to validate against, so those are always "train".
    split = Column(String, nullable=False, default="train")

    computed_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    # --- Output contract: each track writes ONLY its own column(s) here,
    # keyed by this row's id -- never a separate table/format. Whoever
    # does final aggregation (Section 8) reads all four off the same row.
    isolation_forest_score = Column(Float, nullable=True)  # Track B
    cluster_id = Column(Integer, nullable=True)  # Track D
    cluster_changed = Column(Boolean, nullable=True)  # Track D: did this entity's cluster shift vs. its prior snapshot
    timeseries_drift_score = Column(Float, nullable=True)  # Track C

    final_anomaly_score = Column(Float, nullable=True)  # 0-100, aggregation step only
    anomaly_band = Column(String, nullable=True)  # Normal / Low-Medium / High / Critical, aggregation step only


class BeneficiarySnapshot(Base):
    """Funnel-account detection input. Deliberately a SEPARATE table from
    EntitySnapshot, not a variant of it -- EntitySnapshot is grouped by
    SENDER (one row per merchant/individual) and answers "how many
    different people did this entity pay" (unique_counterparties).
    Funnel detection needs the opposite question: "how many different
    senders paid THIS beneficiary, and how many of them are new."
    EntitySnapshot structurally cannot answer that no matter what's built
    on top of it (Isolation Forest, clustering) -- it requires grouping
    raw canonical_events by counterparty instead of by resolved party,
    which is what this table does.

    Same leakage rule as EntitySnapshot: every value here is computed
    fresh from raw canonical_events facts (payee identity, payer identity,
    amount, transaction_occurred_at), never from the source's own
    funnel_account_flag / distinct_originating_accounts_24h/7d in
    fraud_risk_details. See beneficiary_features.py's module docstring.
    """

    __tablename__ = "anomaly_beneficiary_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # payee_account_ref when available (a real account reference, stable
    # across rails); falls back to payee_name (bare string) only when no
    # account ref was captured for that transaction -- see
    # beneficiary_features.py's _beneficiary_key().
    beneficiary_key = Column(String, nullable=False, index=True)
    beneficiary_name = Column(String, nullable=True)  # display only: last-seen payee_name for this key
    tenant_bank_id = Column(String, nullable=False, index=True)

    window_start = Column(DateTime(timezone=True), nullable=False)
    window_end = Column(DateTime(timezone=True), nullable=False)

    transaction_count = Column(Integer, nullable=False, default=0)
    amount_total = Column(Float, nullable=True)

    distinct_senders = Column(Integer, nullable=False, default=0)
    # Senders paying THIS beneficiary for the first time ever (not "first
    # time this week") as of this window -- computed the same
    # first-sighting-tracking way as EntitySnapshot's new_counterparty_ratio,
    # just from the beneficiary's side of the relationship instead of the
    # sender's.
    distinct_new_senders = Column(Integer, nullable=False, default=0)
    new_sender_ratio = Column(Float, nullable=True)
    sender_party_types = Column(JSON, nullable=True)  # e.g. ["MERCHANT", "INDIVIDUAL"] -- who's paying them

    computed_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    # Output: Track C only for now (see timeseries.py's score_funnel_drift).
    # Isolation Forest/clustering don't currently run against this table --
    # would mean a third segment-model, deliberately out of scope for v1
    # per the funnel gap discussion (a rule/drift-based signal here is a
    # better fit than ML at this data volume anyway).
    funnel_drift_score = Column(Float, nullable=True)
