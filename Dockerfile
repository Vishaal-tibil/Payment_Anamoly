# Merchant Payment Intelligence Platform -- backend API image.
#
# Target: a single EC2 instance running this container behind nginx or an
# ALB. Sized for the pilot: SQLite + one uvicorn process, which is what
# this workload actually is (batch compute runs plus read-mostly dashboard
# traffic). See README's Deployment section for the EC2 steps and for what
# has to change before this is a multi-instance service.
#
# Two stages so the ~500MB of build tooling scipy/scikit-learn may need is
# never shipped: stage 1 resolves wheels into a virtualenv, stage 2 copies
# only that venv plus the application.

# ---------- Stage 1: build the virtualenv ----------
FROM python:3.11-slim AS builder

# build-essential/gfortran are only needed if pip has to compile numpy or
# scipy from source (no manylinux wheel for the resolved version). They
# stay in this stage and never reach the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gfortran \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt .

# Drop the test-only dependencies: pytest/httpx are in requirements.txt so
# a local clone gets everything from one file, but they have no business in
# a production image.
RUN grep -viE '^(pytest|httpx)' requirements.txt > requirements.runtime.txt \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.runtime.txt


# ---------- Stage 2: runtime ----------
FROM python:3.11-slim AS runtime

# libgomp1 is the OpenMP runtime scikit-learn links against (Isolation
# Forest and HDBSCAN both use it). It is NOT in python:*-slim, and its
# absence fails at import time, not build time -- so a missing libgomp1
# only surfaces when someone first triggers a compute run.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root: the API only ever needs to read its own code and read/write the
# database volume.
RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser scripts/ ./scripts/
COPY --chown=appuser:appuser sample_data/ ./sample_data/

# The database lives on a mounted volume, NOT in the image: it holds real
# ingested transactions, analyst reviews and cached AI narratives, all of
# which must survive a redeploy. DB_PATH points here (app/config.py); the
# directory is created and owned up front so the non-root user can write
# even when Docker creates an empty volume on first run.
RUN mkdir -p /data && chown appuser:appuser /data
ENV DATA_DIR=/data \
    DB_PATH=/data/payments.db
VOLUME ["/data"]

USER appuser
EXPOSE 8000

# Liveness only -- deliberately does no database work, so a database
# problem surfaces as a real error instead of silently restart-looping.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

# 0.0.0.0, not 127.0.0.1: the latter would only be reachable from inside
# the container. One worker on purpose -- SQLite in WAL mode handles
# concurrent readers well but multiple worker processes writing the same
# file is how that breaks; scale by moving to Postgres first (see README).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
