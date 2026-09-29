from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import Query

import app as app_module
import equipment_quality_runtime as eq_runtime

app = eq_runtime.app


async def _read_status(client: httpx.AsyncClient, player_id: int):
    data = await app_module._torn_get(
        client,
        "/user",
        {"id": int(player_id), "selections": "basic"},
        error_text="player status",
    )
    profile = data.get("profile") if isinstance(data, dict) and isinstance(data.get("profile"), dict) else {}
    status = profile.get("status") if isinstance(profile.get("status"), dict) else {}
    until = status.get("until")
    try:
        until = int(until) if until is not None else None
    except (TypeError, ValueError):
        until = None
    state = str(status.get("state") or "Unknown")
    description = str(status.get("description") or state)
    lowered = f"{state} {description}".lower()
    if any(x in lowered for x in ("travel", "abroad")):
        kind = "traveling"
    elif "hospital" in lowered:
        kind = "hospital"
    elif any(x in lowered for x in ("jail", "federal")):
        kind = "jail"
    elif state.lower() in ("okay", "idle") or "okay" in lowered:
        kind = "ready"
    else:
        kind = "unknown"
    seconds_left = max(0, until - int(time.time())) if until else None
    return {
        "player_id": int(player_id),
        "state": state,
        "description": description,
        "kind": kind,
        "until": until,
        "seconds_left": seconds_left,
        "ready": kind == "ready",
    }


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
        if len(parsed) >= 50:
            break

    sem = asyncio.Semaphore(6)
    results = []
    errors = []

    async with httpx.AsyncClient(timeout=12.0) as client:
        async def one(player_id: int):
            async with sem:
                try:
                    results.append(await _read_status(client, player_id))
                except Exception as exc:
                    errors.append(f"{player_id}: {exc}")
        await asyncio.gather(*(one(pid) for pid in parsed))

    results.sort(key=lambda x: parsed.index(x["player_id"]) if x["player_id"] in parsed else 9999)
    return {"ok": True, "items": results, "errors": errors[:8], "checked_at": int(time.time())}
