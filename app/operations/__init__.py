"""Step 6b: Operational Issues engine.

Separate from app/anomaly/ (Step 6a, Fraud/Anomaly Detection) on purpose:
this engine asks "did the payment pipeline itself work correctly", not
"is this behavior unusual". That means the fraud engine's hard rule is
reversed here -- network_timeout_flag, file_reached_settlement,
duplicate_check_status, and format_validation_status are NOT pre-computed
fraud verdicts being reverse-engineered; they're plain operational facts,
and reading them directly is the entire point of this engine. Don't
import app/anomaly's exclusion list into this package by habit.

All four issue types are implemented: Batch Never Settles (rules.py,
deterministic), Network/Processor Timeout (drift.py, rolling z-score),
Duplicate Payment (duplicate_payment.py, deterministic exact-key join),
Formatting Rejection (format_rejection.py, deterministic listing +
rolling z-score for the spike half).
"""
