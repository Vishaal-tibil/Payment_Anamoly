"""Step 6d: Payment Health -- the culmination engine.

Unlike Tracks A-D (fraud/anomaly) or Operational Issues or Reconciliation,
this engine detects nothing new. It reads the three engines' already-computed,
already-real output (EntitySnapshot.anomaly_band, OperationalIssue rows,
ReconciliationBreak rows) plus raw settlement facts from CanonicalEvent, and
combines them into one bank-wide 0-100 health score -- the single number a
senior stakeholder would ask for first: "how healthy is this bank's payment
operation, overall?"

Deterministic, not ML -- there's nothing to learn here, just a documented
weighted rollup of facts that are already real. See scoring.py's module
docstring for the exact formula and why each weight is what it is.
"""
