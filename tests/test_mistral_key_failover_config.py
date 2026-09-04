"""app/config.py's Mistral key loading -- the ordering and de-duplication
that quota failover depends on.

Reloads the module under a patched environment rather than importing it
once, since the key list is resolved at import time.
"""
from __future__ import annotations

import importlib
from unittest.mock import patch

import app.config as config


def _reload_with_env(env: dict[str, str]):
    """Reload app.config under exactly `env`.

    Two things are essential here. clear=True so the developer's own real
    key can't leak into an assertion (or a failure message). And
    `dotenv.load_dotenv` is patched at its SOURCE module, not as an
    attribute of app.config: importlib.reload re-executes the import line,
    which would rebind the real function and read the real .env straight
    back in -- which is exactly what happened before this was fixed.
    """
    with patch.dict("os.environ", env, clear=True):
        with patch("dotenv.load_dotenv", lambda *a, **k: None):
            return importlib.reload(config)


def _keys_with_env(env: dict[str, str]) -> list[str]:
    return list(_reload_with_env(env).MISTRAL_API_KEYS)


def teardown_module():
    importlib.reload(config)  # restore the real process environment's view


def test_no_keys_configured_is_empty_not_an_error():
    assert _keys_with_env({}) == []


def test_single_key_is_the_existing_behaviour():
    assert _keys_with_env({"MISTRAL_API_KEY": "k1"}) == ["k1"]


def test_numbered_keys_load_in_ascending_order():
    keys = _keys_with_env({
        "MISTRAL_API_KEY": "k1",
        "MISTRAL_API_KEY_2": "k2",
        "MISTRAL_API_KEY_3": "k3",
    })
    assert keys == ["k1", "k2", "k3"]  # order matters: k1 is the preferred key


def test_numbered_keys_stop_at_a_gap():
    """Contiguous only -- a gap ends the list rather than silently skipping,
    so a typo'd MISTRAL_API_KEY_3 can't leave a key quietly unused.
    """
    keys = _keys_with_env({
        "MISTRAL_API_KEY": "k1",
        "MISTRAL_API_KEY_3": "k3",  # no _2
    })
    assert keys == ["k1"]


def test_comma_separated_variable_is_supported():
    assert _keys_with_env({"MISTRAL_API_KEYS": "k1, k2 ,k3"}) == ["k1", "k2", "k3"]


def test_comma_separated_takes_precedence_over_numbered():
    keys = _keys_with_env({
        "MISTRAL_API_KEYS": "a,b",
        "MISTRAL_API_KEY": "ignored",
        "MISTRAL_API_KEY_2": "ignored-too",
    })
    assert keys == ["a", "b"]


def test_duplicate_keys_are_collapsed():
    """The same key twice would otherwise burn two failover attempts on one
    already-exhausted quota.
    """
    keys = _keys_with_env({
        "MISTRAL_API_KEY": "same",
        "MISTRAL_API_KEY_2": "same",
        "MISTRAL_API_KEY_3": "different",
    })
    assert keys == ["same", "different"]


def test_blank_and_whitespace_only_values_are_dropped():
    keys = _keys_with_env({
        "MISTRAL_API_KEY": "  k1  ",
        "MISTRAL_API_KEY_2": "   ",
        "MISTRAL_API_KEY_3": "k3",
    })
    assert keys == ["k1", "k3"]


def test_legacy_mistral_api_key_still_points_at_the_primary():
    reloaded = _reload_with_env({"MISTRAL_API_KEY": "k1", "MISTRAL_API_KEY_2": "k2"})
    assert reloaded.MISTRAL_API_KEY == "k1"


def test_legacy_mistral_api_key_is_none_when_unconfigured():
    reloaded = _reload_with_env({})
    assert reloaded.MISTRAL_API_KEY is None
