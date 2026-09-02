"""FastAPI app assembly -- wiring only. Every endpoint lives in
app/routers/{analyst,head_of_operations,pipeline}.py: analyst.py backs
the Payment Operations Analyst view (Investigation Queue + Insights),
head_of_operations.py backs the Head of Payment Operations view (the
executive rollup + review workflow), pipeline.py is the shared
batch/admin surface (ingestion, resolution, every engine's *_compute
trigger, the merchant/individual directory) neither persona's UI calls
directly.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import models  # noqa: F401  -- registers tables on Base.metadata
from .agent import models as agent_models  # noqa: F401  -- registers agent_narratives
from .anomaly import models as anomaly_models  # noqa: F401  -- registers anomaly_entity_snapshots
from .database import Base, SessionLocal, engine
from .health import models as health_models  # noqa: F401  -- registers payment_health_scores
from .investigation import models as investigation_models  # noqa: F401  -- registers investigation_cases
from .operations import models as operations_models  # noqa: F401  -- registers operational_issues
from .reconciliation import models as reconciliation_models  # noqa: F401  -- registers reconciliation_breaks
from .review import models as review_models  # noqa: F401  -- registers analyst_reviews
from .routers.analyst import router as analyst_router
from .routers.head_of_operations import router as head_of_operations_router
from .routers.pipeline import router as pipeline_router
from .seed import seed_sample_mappings_if_empty


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_sample_mappings_if_empty(db)
    finally:
        db.close()
    yield


app = FastAPI(title="Merchant Payment Intelligence Platform", lifespan=lifespan)

# Local-dev frontend (Vite) origins. Regex (not a fixed port list)
# because Vite auto-increments past 5173 whenever that port's already
# taken -- still scoped to localhost only. Pilot system, no auth yet --
# tighten this to a real allowlist before any non-local deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(analyst_router, tags=["Analyst"])
app.include_router(head_of_operations_router, tags=["Head of Operations"])
app.include_router(pipeline_router, tags=["Pipeline"])
