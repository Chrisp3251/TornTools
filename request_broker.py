import asyncio
import time
from typing import Any

import mug_research
import app as app_module
import server

app = mug_research.app

# One Torn market response should feed every TornTools consumer until that
# underlying Torn cache is actually due to change. Hidden Deals, Sniper, and
# Research previously could request the same item independently.
FALLBACK_CACHE_SECONDS = 3.0
CACHE_DUE_GRACE_SECONDS = 0.20

_original_fetch_market = app_module.fetch_market
_market_cache: dict[tuple[int, int], dict[str, Any]] = {}
_inflight: dict[tuple[int, int], asyncio.Task] = {}
_stats = {
    "upstream_requests": 0,
    "cache_reuses": 0,
    "inflight_reuses": 0,
    "started_at": time.time(),
}


def _cache_key(item_id: int, limit: int) -> tuple[int, int]:
    # A larger response can satisfy a smaller consumer, but keeping the key by
    # limit avoids silently changing endpoint semantics. Current market scanners
    # mostly use 60 or 100.
    return int(item_id), int(limit)


def _usable(entry: dict[str, Any] | None) -> bool:
    if not entry:
        return False
    return time.time() < float(entry.get("expires_at") or 0)


def _expiry_for(data: Any) -> float:
    now = time.time()
    try:
        meta = app_module.market_cache_meta(data)
        cache_ts = meta.get("cache_timestamp")
        cache_delay = meta.get("cache_delay")
        if cache_ts is not None and cache_delay is not None:
            due = float(cache_ts) + max(1.0, float(cache_delay)) + CACHE_DUE_GRACE_SECONDS
            # Never cache a response that Torn already says is due. A tiny
            # fallback still collapses callers that arrive simultaneously.
            return max(now + FALLBACK_CACHE_SECONDS, due)
    except Exception:
        pass
    return now + FALLBACK_CACHE_SECONDS


async def shared_fetch_market(client, item_id: int, limit: int = 100):
    key = _cache_key(item_id, limit)
    cached = _market_cache.get(key)
    if _usable(cached):
        _stats["cache_reuses"] += 1
        return cached["data"]

    task = _inflight.get(key)
    if task and not task.done():
        _stats["inflight_reuses"] += 1
        return await task

    async def do_fetch():
        _stats["upstream_requests"] += 1
        data = await _original_fetch_market(client, int(item_id), int(limit))
        _market_cache[key] = {
            "data": data,
            "fetched_at": time.time(),
            "expires_at": _expiry_for(data),
        }
        return data

    task = asyncio.create_task(do_fetch())
    _inflight[key] = task
    try:
        return await task
    finally:
        if _inflight.get(key) is task:
            _inflight.pop(key, None)


# Patch both modules because app routes resolve app.fetch_market at runtime,
# while Research Lab imported fetch_market into the server module directly.
app_module.fetch_market = shared_fetch_market
server.fetch_market = shared_fetch_market

# Research Lab is the discovery stage. Once an item is already in Hidden Deals,
# Hidden's snapshots are the proving data, so a second dedicated research fetch
# is redundant. Mutating this shared dict also updates server.LEARN_ITEMS.
_research_overlap = sorted(set(app_module.LEARN_ITEMS) & set(app_module.DISCOVERY_ITEMS))
for _item_id in _research_overlap:
    app_module.LEARN_ITEMS.pop(_item_id, None)


@app.get("/api/request-broker/status")
async def request_broker_status():
    now = time.time()
    live_cache = sum(1 for entry in _market_cache.values() if _usable(entry))
    upstream = int(_stats["upstream_requests"])
    reused = int(_stats["cache_reuses"]) + int(_stats["inflight_reuses"])
    total_served = upstream + reused
    return {
        "ok": True,
        "mode": "shared Torn market cache",
        "upstream_requests": upstream,
        "cache_reuses": int(_stats["cache_reuses"]),
        "inflight_reuses": int(_stats["inflight_reuses"]),
        "total_market_fetches_served": total_served,
        "estimated_requests_avoided": reused,
        "reuse_pct": round((reused / total_served * 100.0) if total_served else 0.0, 1),
        "live_cached_items": live_cache,
        "inflight_items": len(_inflight),
        "research_overlap_removed": _research_overlap,
        "research_overlap_count": len(_research_overlap),
        "uptime_seconds": int(now - float(_stats["started_at"])),
    }
