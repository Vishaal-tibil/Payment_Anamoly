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
# ministral-8b-latest, not mistral-large-latest: measured against this
# project's real keys, mistral-large returns "403 tier_not_allowed" on every
# call (a genuine plan restriction -- three separate keys, same error), and
# mistral-small/medium stay 429-limited even with backoff. ministral-8b is
# reachable, and measured 6/6 on the code-enforced grounding check at ~6s
# per call versus ~14s. Override MISTRAL_MODEL on a paid plan to use large.
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "ministral-8b-latest")


def _load_mistral_keys() -> list[str]:
    """Ordered list of Mistral API keys, first one preferred.

    Multiple keys exist for quota failover, not load balancing: the shared
    tier exhausts, and a second key lets narration keep working instead of
    going dark mid-demo. app/agent/narration.py walks this list in order
    and only advances when a key is genuinely spent or rejected.

    Two supported shapes, so neither a .env nor a container env-var list
    is awkward:
      MISTRAL_API_KEY=k1
      MISTRAL_API_KEY_2=k2          (also _3, _4, ... contiguous)
    or a single comma-separated variable:
      MISTRAL_API_KEYS=k1,k2,k3

    Order is deterministic: MISTRAL_API_KEYS first if set, else
    MISTRAL_API_KEY then the numbered ones ascending. Duplicates are
    dropped (a key repeated across variables would otherwise burn two
    failover attempts on the same exhausted quota) and order is preserved.
    """
    raw = os.getenv("MISTRAL_API_KEYS", "").strip()
    if raw:
        candidates = [k.strip() for k in raw.split(",")]
    else:
        candidates = [os.getenv("MISTRAL_API_KEY", "")]
        index = 2
        while True:
            value = os.getenv(f"MISTRAL_API_KEY_{index}")
            if value is None:
                break  # contiguous only -- a gap ends the list, not a silent skip
            candidates.append(value)
            index += 1

    seen: set[str] = set()
    keys: list[str] = []
    for key in (k.strip() for k in candidates):
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


MISTRAL_API_KEYS = _load_mistral_keys()

# Backwards compatibility: existing code and tests read MISTRAL_API_KEY as
# "is narration configured at all". None when no key is set anywhere.
MISTRAL_API_KEY = MISTRAL_API_KEYS[0] if MISTRAL_API_KEYS else None

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
