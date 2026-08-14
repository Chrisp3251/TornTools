"""TornTools runtime performance shim.

Telemetry v2 needs evidence profiles for live-edge gating, but recomputing the
full 240-snapshot profile on every scanner snapshot can starve the local API.
This module adds a short TTL cache without changing learner/scoring behavior.
"""

import time

import telemetry
from telemetry import app

_PROFILE_CACHE_SECONDS = 10.0
_profile_cache = {}
_original_profile_for = telemetry._profile_for


def _cached_profile_for(item_id: int, name: str | None = None):
    key = (int(item_id), name or "")
    now = time.time()
    cached = _profile_cache.get(key)
    if cached and now - cached[0] < _PROFILE_CACHE_SECONDS:
        return cached[1]
    profile = _original_profile_for(int(item_id), name)
    _profile_cache[key] = (now, profile)
    return profile


# telemetry's background loop and dynamic target mapper resolve this global at
# runtime, so both automatically use the cache after this module imports.
telemetry._profile_for = _cached_profile_for
