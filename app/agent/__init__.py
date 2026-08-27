"""Step 7: LLM Agent Layer (Mistral).

Turns the raw, already-computed signals from the three detection
engines (app/anomaly, app/operations, app/reconciliation) into a short
human-readable narrative + recommended action -- the piece the frontend
placeholder text ("no automated recommendation yet") was waiting on.

This layer never computes a NEW signal and never invents a fact. It
only narrates facts the engines already produced -- see narration.py's
system prompt for the hard rules enforced on every call. If you're
tempted to have it "explain why" something happened beyond what the
input data shows, don't -- that's fabrication, the same failure mode
this whole project has avoided everywhere else.
"""
