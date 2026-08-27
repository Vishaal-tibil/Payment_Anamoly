"""Step 6b: Operational Issues engine.

Separate from app/anomaly/ (Step 6a, Fraud/Anomaly Detection) on purpose:
the knowledge doc's Section 10 assigns each Operational category a
deterministic/rule-based or time-series check, not an entity behavioral
baseline scored by Isolation Forest/HDBSCAN -- so these modules operate
directly on canonical_events, not through EntitySnapshot.
"""
