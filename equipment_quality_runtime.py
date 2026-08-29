"""Final runtime overlay for Equipment Checker quality handling.

Torn equipment quality is not capped at 100, so this layer removes the old
0-100 API restriction and extends the smoothed quality pricing curve to the
actual observed/user quality range.
"""
from __future__ import annotations

import math

import httpx
from fastapi import HTTPException

import app as core
import travel_state_runtime as travel_runtime

app = travel_runtime.app


def _monotonic_quality_ask_extended(rows, quality):
    """Build a smoothed non-decreasing ask curve across the real quality range."""
    observed_max = max([float(r.get("quality") or 0) for r in rows] + [float(quality or 0), 100.0])
    max_center = int(math.ceil(observed_max / 5.0) * 5)
    anchors = []
    for center in range(0, max_center + 1, 5):
        nearby = [r["price"] for r in rows if abs(float(r["quality"]) - center) <= 5]
        if len(nearby) < 3:
            continue
        anchors.append([float(center), core._conservative_price(nearby), len(nearby)])
    if not anchors:
        return None, 0
    running = 0
    for anchor in anchors:
        running = max(running, anchor[1])
        anchor[1] = running
    lower = [a for a in anchors if a[0] <= quality]
    upper = [a for a in anchors if a[0] >= quality]
    if lower and upper:
        lo = lower[-1]
        hi = upper[0]
        if hi[0] == lo[0]:
            return int(lo[1]), lo[2]
        ratio = (quality - lo[0]) / (hi[0] - lo[0])
        return int(lo[1] + (hi[1] - lo[1]) * ratio), min(lo[2], hi[2])
    anchor = lower[-1] if lower else upper[0]
    return int(anchor[1]), anchor[2]


core._monotonic_quality_ask = _monotonic_quality_ask_extended


async def check_equipment_unbounded(
    item_id: int,
    quality: float,
    damage: float | None = None,
    accuracy: float | None = None,
    armor: float | None = None,
    vendor_sell: int | None = None,
):
    if item_id <= 0:
        raise HTTPException(400, "Enter a valid item ID")
    if quality < 0:
        raise HTTPException(400, "Quality cannot be negative")
    if vendor_sell is not None and vendor_sell < 0:
        raise HTTPException(400, "Vendor value cannot be negative")

    async with httpx.AsyncClient(timeout=20) as client:
        market_data, base_item = await __import__("asyncio").gather(
            core.fetch_market(client, item_id, 100),
            core.get_base_item(client, item_id),
        )

    rows, meta = core.equipment_rows(market_data)
    if not rows:
        raise HTTPException(400, "No plain weapon/armor listings with stats were returned for this item")

    vals = core.base_item_values(base_item)
    resolved_vendor = vendor_sell if vendor_sell is not None and vendor_sell > 0 else (vals.get("sell_price") or 0)
    result = core.equipment_verdict(rows, quality, damage, accuracy, armor, resolved_vendor)
    return {
        "ok": True,
        "item_id": item_id,
        "name": meta.get("name") or (base_item or {}).get("name") or f"Item {item_id}",
        "type": meta.get("type"),
        "market_average": meta.get("average_price"),
        "torn_market_price": vals.get("market_price"),
        "vendor_sell": resolved_vendor,
        "vendor_sell_source": "manual" if vendor_sell is not None and vendor_sell > 0 else "torn",
        "buy_price": vals.get("buy_price"),
        "vendor": vals.get("vendor"),
        "your_stats": {"quality": quality, "damage": damage, "accuracy": accuracy, "armor": armor},
        "market_url": core.market_url(item_id),
        "cache": core.market_cache_meta(market_data),
        **result,
    }


# Keep FastAPI's already-built dependency model, but swap the callable used by
# the existing route. The signature intentionally matches app.py exactly.
for route in app.routes:
    if getattr(route, "path", None) == "/api/equipment/check" and "GET" in getattr(route, "methods", set()):
        route.endpoint = check_equipment_unbounded
        if getattr(route, "dependant", None) is not None:
            route.dependant.call = check_equipment_unbounded
        break
