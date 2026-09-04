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

from ..config import MISTRAL_API_KEYS, MISTRAL_MODEL

_MISSING_KEY_MESSAGE = (
    "No Mistral API key is configured. Copy .env.example to .env in the repo "
    "root and set MISTRAL_API_KEY (optionally MISTRAL_API_KEY_2/_3 for quota "
    "failover) with a real key from https://console.mistral.ai/."
)

# One client per key, built once and reused. The SDK client holds an HTTP
# connection pool, so rebuilding it per request would throw away keep-alive
# on a call that already takes seconds.
_clients: dict[str, Mistral] = {}


def get_client() -> Mistral:
    """The primary (first configured) key's client.

    Kept for callers that just need "a client" -- narration itself uses
    get_clients() so it can fail over to the next key.
    """
    if not MISTRAL_API_KEYS:
        raise RuntimeError(_MISSING_KEY_MESSAGE)
    return _client_for(MISTRAL_API_KEYS[0])


def get_clients() -> list[Mistral]:
    """Every configured key's client, in the configured order.

    Narration walks these in order on quota exhaustion -- see
    app/agent/narration.py's _complete_with_retry.
    """
    if not MISTRAL_API_KEYS:
        raise RuntimeError(_MISSING_KEY_MESSAGE)
    return [_client_for(key) for key in MISTRAL_API_KEYS]


def _client_for(api_key: str) -> Mistral:
    if api_key not in _clients:
        _clients[api_key] = Mistral(api_key=api_key)
    return _clients[api_key]


def configured_key_count() -> int:
    """How many keys are available for failover. Used by the ops endpoint
    to report readiness without ever exposing a key value.
    """
    return len(MISTRAL_API_KEYS)


MODEL = MISTRAL_MODEL
