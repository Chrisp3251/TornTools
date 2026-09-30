from __future__ import annotations

import time

import httpx
from fastapi import Query

import app as app_module
import bounty_scout as bounty_runtime
import equipment_quality_runtime as eq_runtime

app = eq_runtime.app


@app.get("/api/bounty-scout/statuses")
async def bounty_scout_statuses(ids: str = Query(..., min_length=1, max_length=1200)):
    parsed = []
    for raw in ids.split(","):
        try:
            value = int(raw.strip())
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in parsed:
            parsed.append(value)
        if len(parsed) >= bounty_runtime._STATUS_BUDGET_PER_SCAN:
            break

    async with httpx.AsyncClient(timeout=12.0) as client:
        status_map, errors = await bounty_runtime._target_statuses(client, parsed)

    results = []
    for player_id in parsed:
        raw = status_map.get(player_id)
        if not raw:
            continue
        info = bounty_runtime._availability_info(raw, 30)
        results.append({
            "player_id": int(player_id),
            "state": str(raw.get("state") or "Unknown"),
            "description": str(raw.get("description") or raw.get("state") or "Unknown"),
            "kind": info["kind"],
            "until": info["until"],
            "seconds_left": info["seconds_left"],
            "ready": info["ready"],
        })

    return {"ok": True, "items": results, "errors": errors[:8], "checked_at": int(time.time())}
