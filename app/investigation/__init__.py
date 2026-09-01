"""Investigation Cases -- Analyst-facing case clustering.

Groups existing OperationalIssue/ReconciliationBreak/EntitySnapshot rows
into "cases" for the Analyst persona's Investigation Queue. Purely a
display/organization layer over signals the three detection engines
already computed -- detects nothing new, same "reshape, don't recompute"
rule app/dashboard.py follows.

Case validation (Valid/Invalid) is deliberately NOT wired into the
analyst review workflow (app/review/) or any Detection Performance
metric -- it's a display-only field on InvestigationCase itself. No
feedback loop yet.
"""
