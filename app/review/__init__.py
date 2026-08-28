"""Analyst review workflow.

Every finding this platform surfaces -- a fraud/anomaly signal, an
operational issue, a reconciliation break -- is a detected *claim*, not
a verdict. This module tracks whether a human analyst has actually
looked at each one: PENDING until reviewed, then CONFIRMED (a real
issue) or DISMISSED (false positive / not actionable).

This is what makes the "analyst view" vs "senior view" distinction real
rather than cosmetic: the analyst view is where PENDING claims get
worked; the senior view reads the review counts this module produces to
show *how much of what's been detected has actually been verified by a
person*, not just the raw detection counts.

Same composite-key, upsert-by-key shape as AgentNarrative (app/agent/models.py)
-- one row per (signal_type, tenant_bank_id, reference_id), since a review
is fundamentally the same kind of "one fact about one detected row" as a
narrative is.
"""
