"""Thin Mistral client wrapper.

Verified against the actually-installed mistralai==2.9.4 SDK (its
public entry point is `mistralai.client.Mistral`, not the top-level
`mistralai` package -- that package has no __init__.py in this
version, it's a namespace package; the real client class lives under
the `client` submodule) before writing anything against it, not
guessed from memory of an older/different SDK layout.
"""
from __future__ import annotations

from mistralai.client import Mistral

from ..config import MISTRAL_API_KEY, MISTRAL_MODEL


def get_client() -> Mistral:
    if not MISTRAL_API_KEY:
        raise RuntimeError(
            "MISTRAL_API_KEY is not set. Copy .env.example to .env in the repo root "
            "and fill in a real key from https://console.mistral.ai/."
        )
    return Mistral(api_key=MISTRAL_API_KEY)


MODEL = MISTRAL_MODEL
