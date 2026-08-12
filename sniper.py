import sqlite3
import time

from fastapi import HTTPException
from pydantic import BaseModel

# Import server first so the Research Lab routes / promotion loader are registered.
import server  # noqa: F401
from app import app, DB_PATH, DISCOVERY_ITEMS, market_url


class SniperTargetPayload(BaseModel):
    item_id: int
    name: str | None = None
    max_price: int
    enabled: bool = True


def _init_sniper_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sniper_targets(
                item_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                max_price INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        # Seed the first known sniper target without overwriting later user edits.
        now = time.time()
        c.execute(
            """
            INSERT OR IGNORE INTO sniper_targets(item_id,name,max_price,enabled,created_at,updated_at)
            VALUES(?,?,?,?,?,?)
            """,
            (1086, "Driver's License", 4999, 1, now, now),
        )


def _sniper_rows():
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            "SELECT item_id,name,max_price,enabled,created_at,updated_at FROM sniper_targets ORDER BY enabled DESC,name COLLATE NOCASE"
        ).fetchall()


def _target_dict(row):
    return {
        "item_id": int(row[0]),
        "name": row[1],
        "max_price": int(row[2]),
        "enabled": bool(row[3]),
        "created_at": row[4],
        "updated_at": row[5],
        "market_url": market_url(int(row[0])),
    }


def _sync_sniper_targets_into_discovery():
    for row in _sniper_rows():
        target = _target_dict(row)
        item_id = target["item_id"]
        if target["enabled"]:
            meta = dict(DISCOVERY_ITEMS.get(item_id) or {})
            meta.update(
                {
                    "name": target["name"],
                    "sniper": True,
                    "sniper_max_price": target["max_price"],
                }
            )
            DISCOVERY_ITEMS[item_id] = meta
        elif item_id in DISCOVERY_ITEMS and DISCOVERY_ITEMS[item_id].get("sniper"):
            # Remove only sniper-specific metadata. Keep items that belong to the
            # normal Hidden Deals pool for another reason (hard floor / graduate).
            meta = dict(DISCOVERY_ITEMS[item_id])
            meta.pop("sniper", None)
            meta.pop("sniper_max_price", None)
            if meta.get("hard_floor") or meta.get("research_graduate"):
                DISCOVERY_ITEMS[item_id] = meta
            else:
                DISCOVERY_ITEMS.pop(item_id, None)


_init_sniper_db()
_sync_sniper_targets_into_discovery()


@app.get("/api/sniper/watchlist")
async def sniper_watchlist():
    _sync_sniper_targets_into_discovery()
    items = [_target_dict(row) for row in _sniper_rows()]
    return {
        "ok": True,
        "items": items,
        "enabled_count": sum(1 for x in items if x["enabled"]),
        "discovery_ids": list(DISCOVERY_ITEMS),
    }


@app.post("/api/sniper/watchlist")
async def save_sniper_target(payload: SniperTargetPayload):
    if payload.item_id <= 0:
        raise HTTPException(400, "Enter a valid item ID")
    if payload.max_price <= 0:
        raise HTTPException(400, "Max buy price must be greater than zero")
    name = (payload.name or "").strip() or f"Item {payload.item_id}"
    now = time.time()
    with sqlite3.connect(DB_PATH) as c:
        existing = c.execute(
            "SELECT created_at FROM sniper_targets WHERE item_id=?", (payload.item_id,)
        ).fetchone()
        created_at = existing[0] if existing else now
        c.execute(
            """
            INSERT OR REPLACE INTO sniper_targets(item_id,name,max_price,enabled,created_at,updated_at)
            VALUES(?,?,?,?,?,?)
            """,
            (
                payload.item_id,
                name,
                payload.max_price,
                1 if payload.enabled else 0,
                created_at,
                now,
            ),
        )
    _sync_sniper_targets_into_discovery()
    return {
        "ok": True,
        "item": {
            "item_id": payload.item_id,
            "name": name,
            "max_price": payload.max_price,
            "enabled": payload.enabled,
            "market_url": market_url(payload.item_id),
        },
        "discovery_ids": list(DISCOVERY_ITEMS),
    }


@app.delete("/api/sniper/watchlist/{item_id}")
async def delete_sniper_target(item_id: int):
    if item_id <= 0:
        raise HTTPException(400, "Enter a valid item ID")
    with sqlite3.connect(DB_PATH) as c:
        c.execute("DELETE FROM sniper_targets WHERE item_id=?", (item_id,))
    # Remove sniper metadata, but preserve normal Hidden Deals membership.
    if item_id in DISCOVERY_ITEMS and DISCOVERY_ITEMS[item_id].get("sniper"):
        meta = dict(DISCOVERY_ITEMS[item_id])
        meta.pop("sniper", None)
        meta.pop("sniper_max_price", None)
        if meta.get("hard_floor") or meta.get("research_graduate"):
            DISCOVERY_ITEMS[item_id] = meta
        else:
            DISCOVERY_ITEMS.pop(item_id, None)
    return {"ok": True, "item_id": item_id, "discovery_ids": list(DISCOVERY_ITEMS)}


@app.get("/api/sniper/config")
async def sniper_config():
    """Small config endpoint consumed by the Torn userscript companion."""
    items = [_target_dict(row) for row in _sniper_rows() if bool(row[3])]
    return {"ok": True, "items": items, "refreshed_at": time.time()}
