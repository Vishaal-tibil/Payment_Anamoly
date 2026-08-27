"""Environment/config loading. Currently just Step 7's Mistral credentials --
no LLM agent code is built yet (that's the next piece, once a real API key
is available); this only makes MISTRAL_API_KEY/MISTRAL_MODEL readable from
.env so the agent module can pick them up the moment it's written.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY") or None
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-large-latest")
