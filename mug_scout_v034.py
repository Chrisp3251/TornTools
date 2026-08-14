from fastapi import Query

import mug_scout
from mug_scout import app

MUG_SCOUT_VERSION = "0.3.4"


@app.get("/api/mug-scout/search-v2")
async def mug_scout_search_v2(
    minff: float = Query(1.8, ge=1.0, le=10.0),
    maxff: float = Query(3.0, ge=1.0, le=10.0),
    minlevel: int = Query(15, ge=1, le=100),
    maxlevel: int = Query(60, ge=1, le=100),
    limit: int = Query(12, ge=1, le=30),
    factionless: int = Query(0, ge=0, le=1),
    mininactive_days: int = Query(0, ge=0, le=3650),
):
    # Pull a wider source pool when inactivity filtering is enabled so a 30-day
    # filter still has a useful chance of returning the requested number of rows.
    source_limit = int(limit)
    if mininactive_days > 0:
        source_limit = min(30, max(int(limit), int(limit) * 3))

    result = await mug_scout.mug_scout_search(
        minff=minff,
        maxff=maxff,
        minlevel=minlevel,
        maxlevel=maxlevel,
        limit=source_limit,
        factionless=factionless,
    )

    rows = list(result.get("items") or [])
    if mininactive_days > 0:
        cutoff_seconds = int(mininactive_days) * 86400
        rows = [
            row for row in rows
            if row.get("last_action_age_seconds") is not None
            and int(row.get("last_action_age_seconds") or 0) >= cutoff_seconds
        ]

    rows = rows[: int(limit)]
    result["items"] = rows
    result["version"] = MUG_SCOUT_VERSION
    result.setdefault("criteria", {})["mininactive_days"] = int(mininactive_days)
    result["criteria"]["limit"] = int(limit)
    result.setdefault("notes", []).insert(
        0,
        f"Minimum inactivity filter: {int(mininactive_days)} day(s)." if mininactive_days else "Minimum inactivity filter: off.",
    )
    return result
