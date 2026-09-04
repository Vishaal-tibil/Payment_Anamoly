"""Verify every configured Mistral API key actually works.

Makes one real, minimal API call per key and reports each key's status
independently -- so a key that is present but dead, out of quota, or
lacking access to the configured model is caught here rather than
discovered mid-demo when failover silently walks past it.

Key VALUES are never printed. Each key is shown as its position plus a
short fingerprint (first 4 / last 4 characters), which is enough to tell
two keys apart in the output without putting a credential in a terminal
scrollback or a CI log.

    python scripts/check_mistral_keys.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mistralai.client import Mistral  # noqa: E402

from app.config import MISTRAL_API_KEYS, MISTRAL_MODEL  # noqa: E402
from app.agent.narration import _classify  # noqa: E402


def fingerprint(key: str) -> str:
    """Enough to distinguish keys, never enough to use one."""
    if len(key) < 12:
        return "****"
    return f"{key[:4]}...{key[-4:]}"


# Retries, matching app/agent/narration.py. Without this the check lies:
# this tier's "403 tier_not_allowed" is intermittent (measured: 3 of 5
# back-to-back calls succeeded on a key that works), so a single-shot
# probe reports a healthy key as FAIL roughly two times in five. A checker
# that cries wolf is worse than no checker.
_ATTEMPTS = 3
_BACKOFF_SECONDS = 3.0


async def check(index: int, key: str) -> tuple[bool, str, float]:
    client = Mistral(api_key=key)
    started = time.perf_counter()
    last_exc: Exception | None = None

    for attempt in range(_ATTEMPTS):
        try:
            # Smallest call that still proves this key can reach THIS model
            # -- a key can be valid yet lack access to mistral-large, which
            # is exactly the failure this script exists to surface.
            response = await client.chat.complete_async(
                model=MISTRAL_MODEL,
                messages=[{"role": "user", "content": "Reply with the single word: ok"}],
                max_tokens=5,
                temperature=0,
            )
            elapsed = time.perf_counter() - started
            reply = (response.choices[0].message.content or "").strip()
            note = f"replied {reply!r}"
            if attempt:
                note += f" (after {attempt} transient failure{'s' if attempt > 1 else ''})"
            return True, note, elapsed
        except Exception as exc:  # noqa: BLE001 -- reporting tool, every failure is interesting
            last_exc = exc
            if _classify(exc) == "invalid":
                break  # a rejected credential will not become valid on retry
            if attempt < _ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_SECONDS * (attempt + 1))

    elapsed = time.perf_counter() - started
    return False, f"{_classify(last_exc)} after {_ATTEMPTS} attempts: {str(last_exc)[:150]}", elapsed


async def main() -> int:
    if not MISTRAL_API_KEYS:
        print("No Mistral API key configured.")
        print("Set MISTRAL_API_KEY (and optionally MISTRAL_API_KEY_2 / _3) in .env.")
        return 1

    print(f"model: {MISTRAL_MODEL}")
    print(f"keys configured: {len(MISTRAL_API_KEYS)} (checked in this order; #1 is preferred)\n")

    results = []
    for index, key in enumerate(MISTRAL_API_KEYS, start=1):
        ok, detail, elapsed = await check(index, key)
        results.append(ok)
        status = "OK  " if ok else "FAIL"
        print(f"  [{status}] key #{index} ({fingerprint(key)})  {elapsed:5.1f}s  {detail}")

    working = sum(results)
    print(f"\n{working}/{len(results)} keys working.")

    if working == 0:
        print("Narration will fail. Every configured key is unusable.")
        return 1
    if working < len(results):
        print("Narration works, but a failover key is unusable -- fix it before")
        print("relying on it, or the fallback will be thinner than it looks.")
        return 1
    if working == 1:
        print("Narration works, but there is no failover key: if this one is")
        print("exhausted mid-session, narration goes dark. Add MISTRAL_API_KEY_2.")
    else:
        print("Failover is real: if one key is exhausted, the next takes over.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
