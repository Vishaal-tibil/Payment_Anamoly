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
from .config import CORS_ALLOW_ORIGIN_REGEX, CORS_ALLOW_ORIGINS
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

# Local dev defaults to a localhost regex (Vite auto-increments past 5173
# whenever that port's taken). A hosted deployment sets CORS_ALLOW_ORIGINS
# to an explicit comma-separated allowlist -- e.g. the Firebase Hosting
# domain -- and then localhost is NOT included. See app/config.py.
#
# Still no auth (pilot system): CORS restricts which browser origins may
# call this API, it does not authenticate anyone. Put this behind a real
# auth layer before exposing non-demo data.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_origin_regex=CORS_ALLOW_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz", tags=["Ops"])
async def healthz():
    """Liveness probe for the container/load balancer. Deliberately does
    no database work: it answers "is this process serving?", which is what
    a restart policy should act on. A database problem should surface as a
    real 500 from a real endpoint, not silently cycle the container.
    """
    return {"status": "ok"}

app.include_router(analyst_router, tags=["Analyst"])
app.include_router(head_of_operations_router, tags=["Head of Operations"])
app.include_router(pipeline_router, tags=["Pipeline"])
