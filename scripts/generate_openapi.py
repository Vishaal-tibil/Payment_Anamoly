"""Generates docs/openapi.json (+ docs/openapi.yaml) for handoff to the
frontend dev.

Why not just dump app.openapi() as-is: none of the endpoints in main.py
declare a response_model (they return plain dicts from resolve_parties(),
compute_features(), the _*_summary() helpers, etc.), so FastAPI's raw
schema would leave every response as an untyped blob. That's useless to
someone building UI/types against it. This script takes FastAPI's
auto-generated spec (accurate for paths/params/request bodies, since
those DO come from real Pydantic models) and enriches it with response
schemas and realistic examples hand-derived from the actual return
shapes in main.py / resolution.py / feature_store.py / anomaly/*.py.

Re-run this after any endpoint/response-shape change and re-share the
regenerated file; it is not wired to a test or CI, just a one-off/
periodic doc generator.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from app.main import app

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

# --- Reusable field-level schemas ------------------------------------------

_ERRORS_ARRAY = {
    "type": "array",
    "description": "Per-row/per-party processing errors from this run. Empty on a fully clean run.",
    "items": {
        "type": "object",
        "additionalProperties": True,
        "example": {"type": "party_error", "party_id": "MER-0B69E2FD", "error": "description of what failed"},
    },
}

_NULLABLE_STRING = {"type": "string", "nullable": True}
_NULLABLE_NUMBER = {"type": "number", "nullable": True}
_NULLABLE_INT = {"type": "integer", "nullable": True}
_NULLABLE_BOOL = {"type": "boolean", "nullable": True}
_NULLABLE_DATETIME = {"type": "string", "format": "date-time", "nullable": True}
_DATETIME = {"type": "string", "format": "date-time"}
_NULLABLE_OBJECT = {"type": "object", "nullable": True, "additionalProperties": True}
_NULLABLE_STRING_ARRAY = {"type": "array", "nullable": True, "items": {"type": "string"}}

# --- Component schemas -------------------------------------------------

SCHEMAS: dict[str, dict] = {
    "HTTPError": {
        "type": "object",
        "properties": {"detail": {"type": "string"}},
        "required": ["detail"],
    },
    "IngestFileResult": {
        "type": "object",
        "description": "Returned by POST /ingest/file.",
        "properties": {
            "ingestion_log_id": {"type": "integer"},
            "file_name": {"type": "string"},
            "tenant_bank_id": {"type": "string"},
            "rail_type": {"type": "string"},
            "settlement_stage": {"type": "string", "enum": ["PRE", "POST"]},
            "row_count": {"type": "integer"},
            "rows_mapped": {"type": "integer"},
            "rows_failed": {"type": "integer"},
            "errors": _ERRORS_ARRAY,
        },
        "required": ["ingestion_log_id", "file_name", "tenant_bank_id", "rail_type", "settlement_stage", "row_count", "rows_mapped", "rows_failed", "errors"],
    },
    "ResolvePartiesResult": {
        "type": "object",
        "description": "Returned by POST /resolve/parties.",
        "properties": {
            "resolved_merchants": {"type": "integer"},
            "resolved_individuals": {"type": "integer"},
            "created_new_merchants": {"type": "integer"},
            "created_new_individuals": {"type": "integer"},
            "skipped_already_resolved": {"type": "integer"},
            "errors": _ERRORS_ARRAY,
        },
        "required": ["resolved_merchants", "resolved_individuals", "created_new_merchants", "created_new_individuals", "skipped_already_resolved", "errors"],
    },
    "MerchantSummary": {
        "type": "object",
        "properties": {
            "merchant_id": {"type": "string", "example": "MER-0B69E2FD"},
            "source_merchant_id": {"type": "string"},
            "tenant_bank_id": {"type": "string"},
            "legal_name": _NULLABLE_STRING,
            "processor_name": _NULLABLE_STRING,
            "onboarded_by": _NULLABLE_STRING,
            "transaction_count": {"type": "integer"},
        },
        "required": ["merchant_id", "source_merchant_id", "tenant_bank_id", "legal_name", "processor_name", "onboarded_by", "transaction_count"],
    },
    "MerchantList": {
        "type": "object",
        "properties": {
            "total": {"type": "integer"},
            "merchants": {"type": "array", "items": {"$ref": "#/components/schemas/MerchantSummary"}},
        },
        "required": ["total", "merchants"],
    },
    "MerchantDetail": {
        "type": "object",
        "properties": {
            "merchant_id": {"type": "string", "example": "MER-0B69E2FD"},
            "source_merchant_id": {"type": "string"},
            "tenant_bank_id": {"type": "string"},
            "legal_name": _NULLABLE_STRING,
            "trade_name": _NULLABLE_STRING,
            "merchant_location": _NULLABLE_OBJECT,
            "merchant_account": _NULLABLE_OBJECT,
            "processor_name": _NULLABLE_STRING,
            "onboarded_by": _NULLABLE_STRING,
            "created_at": _DATETIME,
            "transaction_count": {"type": "integer"},
            "rails_active": {"type": "array", "items": {"type": "string"}, "example": ["ACH", "WIRE"]},
        },
        "required": ["merchant_id", "source_merchant_id", "tenant_bank_id", "legal_name", "trade_name", "merchant_location", "merchant_account", "processor_name", "onboarded_by", "created_at", "transaction_count", "rails_active"],
    },
    "IndividualSummary": {
        "type": "object",
        "properties": {
            "individual_id": {"type": "string", "example": "IND-B1F9C818"},
            "source_individual_id": {"type": "string"},
            "tenant_bank_id": {"type": "string"},
            "full_name": _NULLABLE_STRING,
            "account_type": _NULLABLE_STRING,
            "onboarded_by": _NULLABLE_STRING,
            "transaction_count": {"type": "integer"},
        },
        "required": ["individual_id", "source_individual_id", "tenant_bank_id", "full_name", "account_type", "onboarded_by", "transaction_count"],
    },
    "IndividualList": {
        "type": "object",
        "properties": {
            "total": {"type": "integer"},
            "individuals": {"type": "array", "items": {"$ref": "#/components/schemas/IndividualSummary"}},
        },
        "required": ["total", "individuals"],
    },
    "IndividualDetail": {
        "type": "object",
        "properties": {
            "individual_id": {"type": "string", "example": "IND-B1F9C818"},
            "source_individual_id": {"type": "string"},
            "tenant_bank_id": {"type": "string"},
            "full_name": _NULLABLE_STRING,
            "account_ref": _NULLABLE_STRING,
            "account_type": _NULLABLE_STRING,
            "onboarded_by": _NULLABLE_STRING,
            "created_at": _DATETIME,
            "transaction_count": {"type": "integer"},
            "rails_active": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["individual_id", "source_individual_id", "tenant_bank_id", "full_name", "account_ref", "account_type", "onboarded_by", "created_at", "transaction_count", "rails_active"],
    },
    "ComputeFeaturesResult": {
        "type": "object",
        "description": "Returned by POST /features/compute.",
        "properties": {
            "parties_computed": {"type": "integer"},
            "merchants_computed": {"type": "integer"},
            "individuals_computed": {"type": "integer"},
            "errors": _ERRORS_ARRAY,
        },
        "required": ["parties_computed", "merchants_computed", "individuals_computed", "errors"],
    },
    "PartyFeatures": {
        "type": "object",
        "description": (
            "Step 5 dashboard/reporting rollup. NOT safe to use as anomaly-model "
            "input -- several *_rate fields are rollups of the source's own "
            "pre-computed risk flags, which the anomaly-detection engine "
            "(see the /anomaly/* endpoints) exists to independently (re)derive."
        ),
        "properties": {
            "party_id": {"type": "string"},
            "party_type": {"type": "string", "enum": ["MERCHANT", "INDIVIDUAL"]},
            "tenant_bank_id": {"type": "string"},
            "transaction_count": {"type": "integer"},
            "total_amount": _NULLABLE_NUMBER,
            "avg_amount": _NULLABLE_NUMBER,
            "rails_active": _NULLABLE_STRING_ARRAY,
            "distinct_counterparties": _NULLABLE_INT,
            "new_payee_risk_rate": _NULLABLE_NUMBER,
            "funnel_account_rate": _NULLABLE_NUMBER,
            "velocity_breach_rate": _NULLABLE_NUMBER,
            "structuring_rate": _NULLABLE_NUMBER,
            "network_timeout_rate": _NULLABLE_NUMBER,
            "is_retry_rate": _NULLABLE_NUMBER,
            "format_reject_rate": _NULLABLE_NUMBER,
            "reconciliation_break_rate": _NULLABLE_NUMBER,
            "first_seen_at": _NULLABLE_DATETIME,
            "last_seen_at": _NULLABLE_DATETIME,
            "computed_at": _DATETIME,
        },
        "required": ["party_id", "party_type", "tenant_bank_id", "transaction_count", "computed_at"],
    },
    "PartyFeaturesList": {
        "type": "object",
        "properties": {
            "total": {"type": "integer"},
            "features": {"type": "array", "items": {"$ref": "#/components/schemas/PartyFeatures"}},
        },
        "required": ["total", "features"],
    },
    "ComputeSnapshotsResult": {
        "type": "object",
        "description": "Returned by POST /anomaly/snapshots/compute.",
        "properties": {
            "parties_processed": {"type": "integer"},
            "merchant_snapshots_created": {"type": "integer"},
            "individual_snapshots_created": {"type": "integer"},
            "skipped_no_timestamp": {"type": "integer"},
            "errors": _ERRORS_ARRAY,
        },
        "required": ["parties_processed", "merchant_snapshots_created", "individual_snapshots_created", "skipped_no_timestamp", "errors"],
    },
    "EntitySnapshot": {
        "type": "object",
        "description": (
            "Track A behavioral snapshot -- one row per (merchant, week) or one "
            "TO_DATE row per individual. isolation_forest_score/cluster_id/"
            "cluster_changed/timeseries_drift_score/final_anomaly_score/"
            "anomaly_band are null until the corresponding track has scored this "
            "row (Track B, D, C, and the final aggregation step, respectively)."
        ),
        "properties": {
            "id": {"type": "integer"},
            "party_id": {"type": "string"},
            "party_type": {"type": "string", "enum": ["MERCHANT", "INDIVIDUAL"]},
            "tenant_bank_id": {"type": "string"},
            "segment": {"type": "string"},
            "window_type": {"type": "string", "enum": ["WEEKLY", "TO_DATE"]},
            "window_start": _NULLABLE_DATETIME,
            "window_end": _DATETIME,
            "transaction_count": {"type": "integer"},
            "amount_total": _NULLABLE_NUMBER,
            "amount_avg": _NULLABLE_NUMBER,
            "amount_median": _NULLABLE_NUMBER,
            "amount_std": _NULLABLE_NUMBER,
            "unique_counterparties": _NULLABLE_INT,
            "new_counterparty_ratio": _NULLABLE_NUMBER,
            "retry_ratio": _NULLABLE_NUMBER,
            "avg_response_time_ms": _NULLABLE_NUMBER,
            "timeout_ratio": _NULLABLE_NUMBER,
            "format_reject_ratio": _NULLABLE_NUMBER,
            "rails_used": _NULLABLE_STRING_ARRAY,
            "account_age_days": _NULLABLE_NUMBER,
            "split": {"type": "string", "enum": ["train", "test"]},
            "computed_at": _DATETIME,
            "isolation_forest_score": {**_NULLABLE_NUMBER, "description": "0-100, Track B"},
            "cluster_id": {**_NULLABLE_INT, "description": "Track D"},
            "cluster_changed": {**_NULLABLE_BOOL, "description": "Track D"},
            "timeseries_drift_score": {**_NULLABLE_NUMBER, "description": "0-100, Track C"},
            "final_anomaly_score": {**_NULLABLE_NUMBER, "description": "0-100, aggregation step"},
            "anomaly_band": {
                "type": "string", "nullable": True,
                "enum": ["Normal", "Low-Medium", "High", "Critical", None],
                "description": "Aggregation step",
            },
        },
        "required": ["id", "party_id", "party_type", "tenant_bank_id", "segment", "window_type", "window_end", "transaction_count", "split", "computed_at"],
    },
    "EntitySnapshotList": {
        "type": "object",
        "properties": {
            "total": {"type": "integer"},
            "snapshots": {"type": "array", "items": {"$ref": "#/components/schemas/EntitySnapshot"}},
        },
        "required": ["total", "snapshots"],
    },
    "ScoreDriftResult": {
        "type": "object",
        "description": "Returned by POST /anomaly/timeseries/compute.",
        "properties": {
            "scored": {"type": "integer"},
            "skipped_insufficient_history": {"type": "integer"},
            "parties_processed": {"type": "integer"},
            "errors": _ERRORS_ARRAY,
        },
        "required": ["scored", "skipped_insufficient_history", "parties_processed", "errors"],
    },
    "ComputeBeneficiarySnapshotsResult": {
        "type": "object",
        "description": "Returned by POST /anomaly/beneficiary-snapshots/compute.",
        "properties": {
            "beneficiaries_processed": {"type": "integer"},
            "snapshots_created": {"type": "integer"},
            "skipped_no_beneficiary_key": {"type": "integer"},
            "errors": _ERRORS_ARRAY,
        },
        "required": ["beneficiaries_processed", "snapshots_created", "skipped_no_beneficiary_key", "errors"],
    },
    "ScoreFunnelDriftResult": {
        "type": "object",
        "description": "Returned by POST /anomaly/funnel/compute.",
        "properties": {
            "scored": {"type": "integer"},
            "skipped_insufficient_history": {"type": "integer"},
            "beneficiaries_processed": {"type": "integer"},
            "errors": _ERRORS_ARRAY,
        },
        "required": ["scored", "skipped_insufficient_history", "beneficiaries_processed", "errors"],
    },
    "BeneficiarySnapshot": {
        "type": "object",
        "description": (
            "Funnel Account detection input/output -- one row per (beneficiary, "
            "week). Grouped by RECEIVER, not sender (see EntitySnapshot). "
            "funnel_drift_score is null until Track C's /anomaly/funnel/compute "
            "has scored this row."
        ),
        "properties": {
            "id": {"type": "integer"},
            "beneficiary_key": {"type": "string", "description": "payee_account_ref, or payee_name if no account ref was captured"},
            "beneficiary_name": _NULLABLE_STRING,
            "tenant_bank_id": {"type": "string"},
            "window_start": _DATETIME,
            "window_end": _DATETIME,
            "transaction_count": {"type": "integer"},
            "amount_total": _NULLABLE_NUMBER,
            "distinct_senders": {"type": "integer"},
            "distinct_new_senders": {"type": "integer"},
            "new_sender_ratio": _NULLABLE_NUMBER,
            "sender_party_types": {**_NULLABLE_STRING_ARRAY, "example": ["MERCHANT", "INDIVIDUAL"]},
            "funnel_drift_score": {**_NULLABLE_NUMBER, "description": "0-100, Track C"},
            "computed_at": _DATETIME,
        },
        "required": ["id", "beneficiary_key", "tenant_bank_id", "window_start", "window_end", "transaction_count", "distinct_senders", "distinct_new_senders", "computed_at"],
    },
    "BeneficiarySnapshotList": {
        "type": "object",
        "properties": {
            "total": {"type": "integer"},
            "snapshots": {"type": "array", "items": {"$ref": "#/components/schemas/BeneficiarySnapshot"}},
        },
        "required": ["total", "snapshots"],
    },
    "IsolationForestSegmentResult": {
        "type": "object",
        "properties": {
            "segment": {"type": "string", "enum": ["MERCHANT", "INDIVIDUAL"]},
            "tier_counts": {
                "type": "object",
                "description": "Count of parties in this segment at each observation-volume tier.",
                "properties": {
                    "SEGMENT_BASELINE": {"type": "integer"},
                    "ENTITY_SPECIFIC": {"type": "integer"},
                    "FULL_MODEL": {"type": "integer"},
                },
            },
            "parties": {"type": "integer", "nullable": True},
            "train_rows": {"type": "integer", "nullable": True},
            "rows_scored": {"type": "integer"},
            "score_min": {"type": "number", "nullable": True},
            "score_median": {"type": "number", "nullable": True},
            "score_max": {"type": "number", "nullable": True},
        },
        "required": ["segment", "rows_scored"],
    },
    "IsolationForestTrainResult": {
        "type": "object",
        "description": "Returned by POST /anomaly/isolation-forest/train. Keyed by segment.",
        "properties": {
            "MERCHANT": {"$ref": "#/components/schemas/IsolationForestSegmentResult"},
            "INDIVIDUAL": {"$ref": "#/components/schemas/IsolationForestSegmentResult"},
        },
        "required": ["MERCHANT", "INDIVIDUAL"],
    },
    "ClusterSegmentResult": {
        "type": "object",
        "properties": {
            "rows_clustered": {"type": "integer"},
            "distinct_parties": {"type": "integer"},
            "cluster_sizes": {
                "type": "object",
                "description": "Cluster id (as a string key; -1 is HDBSCAN's \"noise\"/unclustered label, not an error) -> row count.",
                "additionalProperties": {"type": "integer"},
                "example": {"-1": 63, "0": 14, "1": 6},
            },
            "n_clusters": {"type": "integer", "description": "Count of real clusters found, excluding noise (-1)."},
            "noise_count": {"type": "integer"},
        },
        "required": ["rows_clustered", "distinct_parties", "cluster_sizes", "n_clusters", "noise_count"],
    },
    "ClusterAndScoreResult": {
        "type": "object",
        "description": "Returned by POST /anomaly/clustering/compute. Keyed by segment.",
        "properties": {
            "segments": {
                "type": "object",
                "properties": {
                    "MERCHANT": {"$ref": "#/components/schemas/ClusterSegmentResult"},
                    "INDIVIDUAL": {"$ref": "#/components/schemas/ClusterSegmentResult"},
                },
            },
            "errors": _ERRORS_ARRAY,
        },
        "required": ["segments", "errors"],
    },
    "ComputeFinalScoreResult": {
        "type": "object",
        "description": (
            "Returned by POST /anomaly/final-score/compute. Requires "
            "isolation_forest_score already populated on every targeted row "
            "(run POST /anomaly/isolation-forest/train first) -- 400s otherwise. "
            "cluster_changed/timeseries_drift_score do NOT need to be populated "
            "first; null there contributes 0 to that row's weighted score rather "
            "than blocking the run."
        ),
        "properties": {
            "rows_scored": {"type": "integer"},
            "band_counts": {
                "type": "object",
                "description": "anomaly_band -> row count, for whichever bands actually occurred.",
                "properties": {
                    "Normal": {"type": "integer"},
                    "Low-Medium": {"type": "integer"},
                    "High": {"type": "integer"},
                    "Critical": {"type": "integer"},
                },
                "example": {"Normal": 98, "Low-Medium": 68, "High": 6, "Critical": 2},
            },
        },
        "required": ["rows_scored", "band_counts"],
    },
    "DetectDuplicatePaymentsResult": {
        "type": "object",
        "description": "Returned by POST /operations/duplicate-payments/compute.",
        "properties": {
            "groups_checked": {"type": "integer", "description": "Idempotency-key/retry-link groups with more than one transaction."},
            "duplicate_payments_flagged": {"type": "integer", "description": "Of those groups, how many had 2+ transactions reach SETTLED."},
        },
        "required": ["groups_checked", "duplicate_payments_flagged"],
    },
    "ListFormatRejectionsResult": {
        "type": "object",
        "description": "Returned by POST /operations/format-rejections/compute.",
        "properties": {"rejections_listed": {"type": "integer"}},
        "required": ["rejections_listed"],
    },
    "ScoreFormatRejectionDriftResult": {
        "type": "object",
        "description": "Returned by POST /operations/format-rejections/spikes/compute.",
        "properties": {
            "weeks_scored": {"type": "integer", "description": "Merchant-weeks with enough prior history to score at all."},
            "spikes_flagged": {"type": "integer", "description": "Of those, how many scored >= 60 (meaningfully elevated, not just scored)."},
        },
        "required": ["weeks_scored", "spikes_flagged"],
    },
    "DetectUnsettledBatchesResult": {
        "type": "object",
        "description": "Returned by POST /operations/batches/compute.",
        "properties": {
            "batches_checked": {"type": "integer"},
            "batches_flagged": {"type": "integer", "description": "Past expected_settlement_at with at least one unsettled transaction."},
        },
        "required": ["batches_checked", "batches_flagged"],
    },
    "DetectTimeoutSpikesResult": {
        "type": "object",
        "description": "Returned by POST /operations/timeout/compute.",
        "properties": {
            "weeks_checked": {"type": "integer", "description": "Merchant-weeks with enough prior history to z-score at all."},
            "weeks_flagged": {"type": "integer", "description": "Of those, how many had |z-score| >= 2.0."},
        },
        "required": ["weeks_checked", "weeks_flagged"],
    },
    "OperationalIssue": {
        "type": "object",
        "description": (
            "One detected operational issue instance -- all four issue types "
            "are live: DUPLICATE_PAYMENT, FORMAT_REJECTION, "
            "FORMAT_REJECTION_SPIKE, BATCH_NOT_SETTLED (deterministic, "
            "severity_score null) and NETWORK_TIMEOUT_SPIKE (0-100, z-score "
            "based)."
        ),
        "properties": {
            "id": {"type": "integer"},
            "issue_type": {"type": "string", "enum": ["DUPLICATE_PAYMENT", "FORMAT_REJECTION", "FORMAT_REJECTION_SPIKE", "NETWORK_TIMEOUT_SPIKE", "BATCH_NOT_SETTLED"]},
            "tenant_bank_id": {"type": "string"},
            "reference_type": {"type": "string", "enum": ["TRANSACTION", "BATCH", "PARTY"]},
            "reference_id": {"type": "string", "description": "transaction_id, batch_id, or party_id depending on reference_type."},
            "window_start": _NULLABLE_DATETIME,
            "window_end": _NULLABLE_DATETIME,
            "severity_score": _NULLABLE_NUMBER,
            "details": _NULLABLE_OBJECT,
            "detected_at": _DATETIME,
        },
        "required": ["id", "issue_type", "tenant_bank_id", "reference_type", "reference_id", "detected_at"],
    },
    "OperationalIssueList": {
        "type": "object",
        "properties": {
            "total": {"type": "integer"},
            "issues": {"type": "array", "items": {"$ref": "#/components/schemas/OperationalIssue"}},
        },
        "required": ["total", "issues"],
    },
    "DetectReconciliationBreaksResult": {
        "type": "object",
        "description": "Returned by POST /reconciliation/breaks/compute.",
        "properties": {
            "transactions_checked": {"type": "integer", "description": "Transactions with a non-null reconciliation_status."},
            "confirmed_breaks": {"type": "integer", "description": "reconciliation_status == \"BREAK\" (source-flagged)."},
            "provisional_variances": {"type": "integer", "description": "Not yet BREAK, but reconciliation_variance_amount is already nonzero -- an early-warning signal."},
        },
        "required": ["transactions_checked", "confirmed_breaks", "provisional_variances"],
    },
    "ReconciliationBreak": {
        "type": "object",
        "description": (
            "One detected reconciliation problem. detection_type is "
            "CONFIRMED_BREAK (source already called it a break -- not every "
            "one carries a nonzero variance_amount, some are flagged for "
            "other reasons) or PROVISIONAL_VARIANCE (source hasn't called it "
            "a break yet, but the variance is already nonzero)."
        ),
        "properties": {
            "id": {"type": "integer"},
            "tenant_bank_id": {"type": "string"},
            "transaction_id": {"type": "string"},
            "rail_type": {"type": "string"},
            "detection_type": {"type": "string", "enum": ["CONFIRMED_BREAK", "PROVISIONAL_VARIANCE"]},
            "source_reconciliation_status": _NULLABLE_STRING,
            "variance_amount": _NULLABLE_NUMBER,
            "amount": _NULLABLE_NUMBER,
            "details": _NULLABLE_OBJECT,
            "detected_at": _DATETIME,
        },
        "required": ["id", "tenant_bank_id", "transaction_id", "rail_type", "detection_type", "detected_at"],
    },
    "ReconciliationBreakList": {
        "type": "object",
        "properties": {
            "total": {"type": "integer"},
            "breaks": {"type": "array", "items": {"$ref": "#/components/schemas/ReconciliationBreak"}},
        },
        "required": ["total", "breaks"],
    },
}

# --- Per-path response overrides ----------------------------------------
# (method, path) -> {status_code: {"schema": "<ComponentName>", "description": str}}

RESPONSES: dict[tuple[str, str], dict[str, dict]] = {
    ("post", "/ingest/file"): {
        "200": {"schema": "IngestFileResult", "description": "File ingested (even if some rows failed -- check rows_failed/errors)."},
        "400": {"schema": "HTTPError", "description": "Wrong file extension (must be .csv, .xlsx, or .xls)."},
        "422": {"description": "Missing/invalid form fields."},
    },
    ("post", "/resolve/parties"): {"200": {"schema": "ResolvePartiesResult", "description": "Resolution run completed."}},
    ("get", "/merchants"): {"200": {"schema": "MerchantList", "description": "Page of merchants for this tenant."}},
    ("get", "/merchants/{merchant_id}"): {
        "200": {"schema": "MerchantDetail", "description": "Merchant found."},
        "404": {"schema": "HTTPError", "description": "No merchant with this merchant_id."},
    },
    ("get", "/individuals"): {"200": {"schema": "IndividualList", "description": "Page of individuals for this tenant."}},
    ("get", "/individuals/{individual_id}"): {
        "200": {"schema": "IndividualDetail", "description": "Individual found."},
        "404": {"schema": "HTTPError", "description": "No individual with this individual_id."},
    },
    ("post", "/features/compute"): {"200": {"schema": "ComputeFeaturesResult", "description": "Feature computation run completed."}},
    ("get", "/features"): {"200": {"schema": "PartyFeaturesList", "description": "Page of party feature rollups."}},
    ("get", "/features/{party_id}"): {
        "200": {"schema": "PartyFeatures", "description": "Features found."},
        "404": {"schema": "HTTPError", "description": "No features computed yet for this party_id -- run POST /features/compute first."},
    },
    ("post", "/anomaly/snapshots/compute"): {"200": {"schema": "ComputeSnapshotsResult", "description": "Snapshot computation run completed."}},
    ("get", "/anomaly/snapshots"): {"200": {"schema": "EntitySnapshotList", "description": "Page of behavioral snapshots."}},
    ("post", "/anomaly/timeseries/compute"): {"200": {"schema": "ScoreDriftResult", "description": "Time-series drift scoring run completed."}},
    ("post", "/anomaly/beneficiary-snapshots/compute"): {"200": {"schema": "ComputeBeneficiarySnapshotsResult", "description": "Beneficiary snapshot computation run completed."}},
    ("post", "/anomaly/funnel/compute"): {"200": {"schema": "ScoreFunnelDriftResult", "description": "Funnel drift scoring run completed."}},
    ("get", "/anomaly/beneficiary-snapshots"): {"200": {"schema": "BeneficiarySnapshotList", "description": "Page of beneficiary snapshots."}},
    ("post", "/anomaly/isolation-forest/train"): {"200": {"schema": "IsolationForestTrainResult", "description": "Training run completed for both segments."}},
    ("post", "/anomaly/clustering/compute"): {"200": {"schema": "ClusterAndScoreResult", "description": "Clustering run completed for both segments."}},
    ("post", "/anomaly/final-score/compute"): {
        "200": {"schema": "ComputeFinalScoreResult", "description": "Final aggregation run completed."},
        "400": {"schema": "HTTPError", "description": "One or more targeted rows are missing isolation_forest_score -- run POST /anomaly/isolation-forest/train first."},
    },
    ("post", "/operations/duplicate-payments/compute"): {"200": {"schema": "DetectDuplicatePaymentsResult", "description": "Duplicate-payment detection run completed."}},
    ("post", "/operations/format-rejections/compute"): {"200": {"schema": "ListFormatRejectionsResult", "description": "Format-rejection listing run completed."}},
    ("post", "/operations/format-rejections/spikes/compute"): {"200": {"schema": "ScoreFormatRejectionDriftResult", "description": "Format-rejection rate drift scoring run completed."}},
    ("post", "/operations/batches/compute"): {"200": {"schema": "DetectUnsettledBatchesResult", "description": "Unsettled-batch detection run completed."}},
    ("post", "/operations/timeout/compute"): {"200": {"schema": "DetectTimeoutSpikesResult", "description": "Timeout-spike detection run completed."}},
    ("get", "/operations/issues"): {"200": {"schema": "OperationalIssueList", "description": "Page of detected operational issues, most recent first."}},
    ("post", "/reconciliation/breaks/compute"): {"200": {"schema": "DetectReconciliationBreaksResult", "description": "Reconciliation break detection run completed."}},
    ("get", "/reconciliation/breaks"): {"200": {"schema": "ReconciliationBreakList", "description": "Page of detected reconciliation breaks, most recent first."}},
}

TAGS: dict[str, list[str]] = {
    "Ingestion (Steps 1-3)": ["/ingest/file"],
    "Identity Resolution (Step 4)": ["/resolve/parties", "/merchants", "/merchants/{merchant_id}", "/individuals", "/individuals/{individual_id}"],
    "Feature Store (Step 5, dashboard only)": ["/features/compute", "/features", "/features/{party_id}"],
    "Anomaly Detection - Behavioral Snapshots (Track A)": ["/anomaly/snapshots/compute", "/anomaly/snapshots"],
    "Anomaly Detection - Isolation Forest (Track B)": ["/anomaly/isolation-forest/train"],
    "Anomaly Detection - Time-Series Drift (Track C)": ["/anomaly/timeseries/compute"],
    "Anomaly Detection - Funnel Account (Track A input + Track C)": ["/anomaly/beneficiary-snapshots/compute", "/anomaly/funnel/compute", "/anomaly/beneficiary-snapshots"],
    "Anomaly Detection - HDBSCAN Clustering (Track D)": ["/anomaly/clustering/compute"],
    "Anomaly Detection - Final Aggregation (Section 8)": ["/anomaly/final-score/compute"],
    "Operational Issues - Duplicate Payment (Step 6b)": ["/operations/duplicate-payments/compute"],
    "Operational Issues - Formatting Rejection (Step 6b)": ["/operations/format-rejections/compute", "/operations/format-rejections/spikes/compute"],
    "Operational Issues - Batch Never Settles (Step 6b)": ["/operations/batches/compute"],
    "Operational Issues - Network/Processor Timeout (Step 6b)": ["/operations/timeout/compute"],
    "Operational Issues - Issue Feed (Step 6b)": ["/operations/issues"],
    "Reconciliation (Step 6c)": ["/reconciliation/breaks/compute", "/reconciliation/breaks"],
}


def _apply_tags(spec: dict) -> None:
    path_to_tag = {path: tag for tag, paths in TAGS.items() for path in paths}
    for path, methods in spec["paths"].items():
        tag = path_to_tag.get(path)
        if not tag:
            continue
        for op in methods.values():
            if isinstance(op, dict):
                op["tags"] = [tag]
    spec["tags"] = [{"name": tag} for tag in TAGS]


def _apply_responses(spec: dict) -> None:
    for (method, path), status_map in RESPONSES.items():
        op = spec["paths"].get(path, {}).get(method)
        if op is None:
            raise KeyError(f"{method.upper()} {path} not found in generated spec -- did a route change?")
        for status_code, cfg in status_map.items():
            entry = {"description": cfg.get("description", "")}
            if "schema" in cfg:
                entry["content"] = {"application/json": {"schema": {"$ref": f"#/components/schemas/{cfg['schema']}"}}}
            op.setdefault("responses", {})[status_code] = entry


def build_spec() -> dict:
    spec = app.openapi()
    spec["info"]["description"] = (
        "Merchant Payment Intelligence Platform -- backend API for the frontend team.\n\n"
        "Covers Steps 1-5 (ingestion, identity resolution, feature store), the "
        "complete Fraud/Anomaly Detection engine (Step 6a): Track A (behavioral "
        "snapshots), Track B (Isolation Forest), Track C (time-series drift, "
        "including Funnel Account detection), Track D (HDBSCAN clustering), and "
        "the Section 8 final aggregation that combines all three model tracks "
        "into one final_anomaly_score/anomaly_band per entity -- plus the "
        "complete Operational Issues engine (Step 6b, all four detectors: "
        "Duplicate Payment, Formatting Rejection, Batch Never Settles, "
        "Network/Processor Timeout) and the Reconciliation engine (Step 6c).\n\n"
        "All `/anomaly/*/compute` and `/resolve/parties` and `/features/compute` "
        "endpoints are POST, take an optional `{\"tenant_bank_id\": null}` body "
        "(omit or null = all tenants), and return a run-summary object, not the "
        "computed rows themselves -- fetch those via the matching GET endpoint. "
        "Run them in dependency order for a fresh tenant: snapshots/compute -> "
        "isolation-forest/train + timeseries/compute + clustering/compute (any "
        "order) -> final-score/compute."
    )
    spec["info"]["contact"] = {"name": "Vishaal", "email": "vishaal.g@tibilsolutions.com"}
    spec["servers"] = [{"url": "http://localhost:8000", "description": "Local dev (uvicorn app.main:app --reload)"}]
    spec.setdefault("components", {}).setdefault("schemas", {}).update(SCHEMAS)
    _apply_responses(spec)
    _apply_tags(spec)
    return spec


def main() -> None:
    spec = build_spec()
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "openapi.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    (DOCS_DIR / "openapi.yaml").write_text(yaml.dump(spec, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"wrote {DOCS_DIR / 'openapi.json'} and {DOCS_DIR / 'openapi.yaml'}")
    print(f"{len(spec['paths'])} paths, {len(spec['components']['schemas'])} component schemas")


if __name__ == "__main__":
    main()
