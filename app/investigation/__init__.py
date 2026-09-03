"""Investigation Cases -- Analyst-facing case clustering.

Groups existing OperationalIssue/ReconciliationBreak/EntitySnapshot rows
into "cases" for the Analyst persona's Investigation Queue. Purely a
display/organization layer over signals the three detection engines
already computed -- detects nothing new, same "reshape, don't recompute"
rule app/dashboard.py follows.

Case validation (Valid/Invalid) propagates a real AnalystReview
(CONFIRMED/DISMISSED) to every one of the case's contributing alerts --
see app/routers/analyst.py's validate_investigation_case(). This is the
real feedback loop Detection Performance's confirmation_rate/
false_positive_rate (app/review/) reads; InvestigationCase.validation_
status itself stays a display-only summary of what those real reviews
already say, never a second source of truth.
"""
