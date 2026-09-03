"""Environment/config loading.

Every value here has a default that preserves the local-dev behaviour
this project was built with, so nothing changes for someone who just
clones and runs. The overrides exist for containerised/hosted runs
(see Dockerfile and README's Deployment section), where the database
lives on a mounted volume and the frontend is served from a real
domain rather than localhost.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# --- Step 7: LLM Agent Layer (Mistral) --------------------------------------
# Never baked into the image -- supplied at runtime (EC2 user-data, systemd
# EnvironmentFile, or `docker run --env-file`). The agent raises a clear
# error if it's missing rather than silently degrading, so a deployment
# without it still serves every non-narration endpoint.
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY") or None
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-large-latest")

# --- Database ---------------------------------------------------------------
# Relative "data/payments.db" by default (the committed demo database, so a
# fresh clone works immediately). In Docker this points at a mounted volume
# so the database survives container replacement -- a container-local path
# would silently discard every ingested transaction, review and narrative
# on the next deploy.
DATA_DIR = os.getenv("DATA_DIR", "data")
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "payments.db"))

# --- CORS -------------------------------------------------------------------
# Default stays the local-dev Vite regex (localhost on any port, since Vite
# auto-increments past 5173). A hosted frontend is a different origin, so
# CORS_ALLOW_ORIGINS takes a comma-separated allowlist of exact origins --
# an explicit list, not a wildcard, because credentials are allowed.
_DEV_ORIGIN_REGEX = r"http://(localhost|127\.0\.0\.1):\d+"

_raw_origins = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
CORS_ALLOW_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]
# Only fall back to the permissive dev regex when no explicit allowlist is
# configured -- a hosted deployment sets CORS_ALLOW_ORIGINS and gets exactly
# the origins it names, never localhost too.
CORS_ALLOW_ORIGIN_REGEX = None if CORS_ALLOW_ORIGINS else _DEV_ORIGIN_REGEX
