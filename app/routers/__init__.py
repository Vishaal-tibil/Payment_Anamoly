"""API routers, one component per persona/purpose -- see main.py for how
these three get mounted onto the app.

- analyst.py: Investigation Queue + Insights (Overview/Anomalies/Payment
  Rails/Detection Performance) -- everything the Analyst persona's UI reads.
- head_of_operations.py: the executive rollup (single Payment Health
  score, review completion) + the analyst review workflow those rollups
  are computed from.
- pipeline.py: ingestion, resolution, and every engine's *_compute
  trigger -- not consumed directly by either persona's UI, these produce
  the data the other two routers read.
"""
