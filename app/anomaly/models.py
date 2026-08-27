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

    # Structuring proxy (knowledge doc Section 10): fraction of this
    # window's transactions sitting just under the $10,000 CTR reporting
    # threshold. Recomputed fresh from raw amount facts -- not a source
    # pre-built flag -- so it's a legitimate Input contract feature, same
    # rule as new_counterparty_ratio. Added on Track B's branch; Track A
    # (features.py) owns populating it going forward.
    near_threshold_ratio = Column(Float, nullable=True)

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
