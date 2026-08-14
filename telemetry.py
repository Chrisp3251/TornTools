import asyncio
import json
import sqlite3
import time
from typing import Any

from pydantic import BaseModel

import sniper
import server
from sniper import app, DB_PATH


class TelemetryPayload(BaseModel):
    item_id: int
    source: str
    event_type: str = "alert"
    price: int | None = None
    max_price: int | None = None
    baseline: int | None = None
    edge_pct: float | None = None
    cache_age_seconds: int | None = None
    cache_timestamp: int | None = None
    signature: str | None = None
    metadata: dict[str, Any] | None = None


def _init_telemetry_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_telemetry(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                item_id INTEGER NOT NULL,
                item_name TEXT,
                source TEXT NOT NULL,
                event_type TEXT NOT NULL,
                price INTEGER,
                max_price INTEGER,
                baseline INTEGER,
                edge_pct REAL,
                cache_age_seconds INTEGER,
                cache_timestamp INTEGER,
                signature TEXT,
                snapshot_id INTEGER,
                metadata_json TEXT
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_alert_telemetry_item_ts ON alert_telemetry(item_id,ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_alert_telemetry_signature ON alert_telemetry(signature)")


def _item_name(item_id: int) -> str:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT name FROM sniper_targets WHERE item_id=?", (item_id,)).fetchone()
    if row and row[0]:
        return str(row[0])
    meta = sniper.DISCOVERY_ITEMS.get(item_id) or {}
    return meta.get("name") or f"Item {item_id}"


def _record_event(*, item_id: int, source: str, event_type: str, price=None, max_price=None,
                  baseline=None, edge_pct=None, cache_age_seconds=None, cache_timestamp=None,
                  signature=None, snapshot_id=None, metadata=None):
    now = time.time()
    with sqlite3.connect(DB_PATH) as c:
        if signature:
            duplicate = c.execute(
                "SELECT id FROM alert_telemetry WHERE signature=? AND source=? AND event_type=? LIMIT 1",
                (signature, source, event_type),
            ).fetchone()
            if duplicate:
                return False
        c.execute(
            """
            INSERT INTO alert_telemetry(
                ts,item_id,item_name,source,event_type,price,max_price,baseline,edge_pct,
                cache_age_seconds,cache_timestamp,signature,snapshot_id,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                now, item_id, _item_name(item_id), source, event_type, price, max_price,
                baseline, edge_pct, cache_age_seconds, cache_timestamp, signature,
                snapshot_id, json.dumps(metadata or {}, separators=(",", ":")),
            ),
        )
    return True


async def _snapshot_telemetry_loop():
    """Record qualifying API-observed sniper opportunities without changing scan behavior."""
    last_snapshot_id = 0
    while True:
        try:
            with sqlite3.connect(DB_PATH) as c:
                rows = c.execute(
                    """
                    SELECT s.id,s.ts,s.item_id,s.lowest,s.qty_floor,s.next_higher,s.average_price,
                           t.name,t.max_price
                    FROM market_snapshots s
                    JOIN sniper_targets t ON t.item_id=s.item_id AND t.enabled=1
                    WHERE s.id>? ORDER BY s.id ASC LIMIT 500
                    """,
                    (last_snapshot_id,),
                ).fetchall()
            for row in rows:
                snapshot_id, snapshot_ts, item_id, low, qty, next_higher, avg, name, max_price = row
                last_snapshot_id = max(last_snapshot_id, int(snapshot_id))
                if not low or int(low) > int(max_price):
                    continue
                try:
                    profile = server.evidence_profile(int(item_id), name)
                except Exception:
                    profile = {}
                baseline = profile.get("rolling_baseline") or avg or next_higher
                edge_pct = None
                if baseline and low:
                    edge_pct = round((float(baseline) - float(low)) / float(baseline) * 100, 3)
                signature = f"api:{item_id}:{snapshot_id}:{low}:{qty or 0}"
                _record_event(
                    item_id=int(item_id),
                    source="api_snapshot",
                    event_type="qualifying_hit",
                    price=int(low),
                    max_price=int(max_price),
                    baseline=int(baseline) if baseline else None,
                    edge_pct=edge_pct,
                    signature=signature,
                    snapshot_id=int(snapshot_id),
                    metadata={
                        "snapshot_ts": snapshot_ts,
                        "qty_floor": qty,
                        "next_higher": next_higher,
                        "average_price": avg,
                    },
                )
        except Exception:
            pass
        await asyncio.sleep(2)


@app.on_event("startup")
async def _start_telemetry():
    _init_telemetry_db()
    asyncio.create_task(_snapshot_telemetry_loop())


@app.post("/api/sniper/telemetry")
async def record_sniper_telemetry(payload: TelemetryPayload):
    if payload.item_id <= 0:
        return {"ok": False, "recorded": False, "error": "invalid item_id"}
    recorded = _record_event(
        item_id=int(payload.item_id),
        source=(payload.source or "unknown")[:64],
        event_type=(payload.event_type or "alert")[:64],
        price=payload.price,
        max_price=payload.max_price,
        baseline=payload.baseline,
        edge_pct=payload.edge_pct,
        cache_age_seconds=payload.cache_age_seconds,
        cache_timestamp=payload.cache_timestamp,
        signature=(payload.signature or "")[:500] or None,
        metadata=payload.metadata,
    )
    return {"ok": True, "recorded": recorded}


@app.get("/api/sniper/telemetry")
async def sniper_telemetry(limit: int = 100):
    limit = max(1, min(1000, int(limit)))
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM alert_telemetry ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except Exception:
            item["metadata"] = {}
        items.append(item)
    return {"ok": True, "count": len(items), "items": items}


@app.get("/api/sniper/telemetry/summary")
async def sniper_telemetry_summary():
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            """
            SELECT item_id,item_name,source,event_type,COUNT(*) AS events,
                   MIN(price) AS best_price,AVG(price) AS avg_price,
                   AVG(edge_pct) AS avg_edge_pct,MAX(ts) AS last_ts
            FROM alert_telemetry
            GROUP BY item_id,item_name,source,event_type
            ORDER BY events DESC,last_ts DESC
            """
        ).fetchall()
    return {"ok": True, "items": [dict(r) for r in rows]}
