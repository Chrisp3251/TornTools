import json
import sqlite3
import time
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

import app as app_module
import request_broker

app = request_broker.app
DB_PATH = app_module.DB_PATH
BAZAAR_API_V1 = "https://api.torn.com/user/{player_id}"
BAZAAR_MIN_POLL_SECONDS = 30
BAZAAR_EVENT_KEEP = 250


class BazaarWatchConfigPayload(BaseModel):
    player_id: int = Field(gt=0)
    min_value: int = Field(default=5_000_000, ge=1)
    enabled: bool = True


def _init_bazaar_watch_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS bazaar_watch_config(
                id INTEGER PRIMARY KEY CHECK(id=1),
                player_id INTEGER,
                min_value INTEGER NOT NULL DEFAULT 5000000,
                enabled INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS bazaar_watch_state(
                player_id INTEGER NOT NULL,
                listing_key TEXT NOT NULL,
                item_id INTEGER,
                uid TEXT,
                name TEXT,
                item_type TEXT,
                quantity INTEGER NOT NULL,
                price INTEGER NOT NULL,
                market_price INTEGER,
                last_seen REAL NOT NULL,
                PRIMARY KEY(player_id, listing_key)
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS bazaar_watch_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                player_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                listing_key TEXT NOT NULL,
                item_id INTEGER,
                uid TEXT,
                name TEXT,
                item_type TEXT,
                quantity INTEGER NOT NULL,
                price INTEGER NOT NULL,
                market_price INTEGER,
                prior_quantity INTEGER,
                prior_price INTEGER,
                estimated_value INTEGER NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_bazaar_watch_events_ts ON bazaar_watch_events(ts DESC)")
        c.execute(
            "INSERT OR IGNORE INTO bazaar_watch_config(id,player_id,min_value,enabled,updated_at) VALUES(1,NULL,5000000,0,?)",
            (time.time(),),
        )


_init_bazaar_watch_db()


def _config():
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT player_id,min_value,enabled,updated_at FROM bazaar_watch_config WHERE id=1"
        ).fetchone()
    return {
        "player_id": int(row[0]) if row and row[0] else None,
        "min_value": int(row[1] or 5_000_000) if row else 5_000_000,
        "enabled": bool(row[2]) if row else False,
        "updated_at": row[3] if row else None,
    }


def _bazaar_url(player_id: int):
    return f"https://www.torn.com/bazaar.php?userId={int(player_id)}#/"


def _as_int(value, default=None):
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _normalize_bazaar(data: Any):
    if not isinstance(data, dict):
        return [], None, None

    raw = data.get("bazaar")
    is_open = data.get("bazaar_is_open")
    bazaar_timestamp = _as_int(data.get("bazaar_timestamp"))

    # Be tolerant if Torn later wraps v2-style data in an object.
    if isinstance(raw, dict):
        is_open = raw.get("is_open", raw.get("bazaar_is_open", is_open))
        bazaar_timestamp = _as_int(raw.get("timestamp", raw.get("bazaar_timestamp", bazaar_timestamp)))
        raw = raw.get("items") or raw.get("listings") or []

    if not isinstance(raw, list):
        raw = []

    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        item_id = _as_int(entry.get("ID", entry.get("id")))
        uid_raw = entry.get("UID", entry.get("uid"))
        uid = str(uid_raw) if uid_raw is not None else None
        quantity = max(1, _as_int(entry.get("quantity", entry.get("amount")), 1) or 1)
        price = _as_int(entry.get("price"), 0) or 0
        market_price = _as_int(entry.get("market_price"))
        if price < 0:
            continue
        name = str(entry.get("name") or (f"Item {item_id}" if item_id else "Unknown item"))
        item_type = entry.get("type")

        # UID is stable for unique equipment. Commodity bazaar rows generally
        # do not have a UID, so item + ask price distinguishes parallel stacks.
        listing_key = f"uid:{uid}" if uid else f"item:{item_id}:price:{price}"
        per_item_value = max(price, market_price or 0)
        stack_value = per_item_value * quantity
        market_stack_value = (market_price or 0) * quantity
        ask_stack_value = price * quantity

        out.append({
            "listing_key": listing_key,
            "item_id": item_id,
            "uid": uid,
            "name": name,
            "type": item_type,
            "quantity": quantity,
            "price": price,
            "market_price": market_price,
            "per_item_value": per_item_value,
            "stack_value": stack_value,
            "market_stack_value": market_stack_value,
            "ask_stack_value": ask_stack_value,
        })
    return out, bool(is_open) if is_open is not None else None, bazaar_timestamp


def _load_previous(player_id: int):
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            """
            SELECT listing_key,item_id,uid,name,item_type,quantity,price,market_price,last_seen
            FROM bazaar_watch_state WHERE player_id=?
            """,
            (int(player_id),),
        ).fetchall()
    return {
        row[0]: {
            "listing_key": row[0],
            "item_id": row[1],
            "uid": row[2],
            "name": row[3],
            "type": row[4],
            "quantity": int(row[5] or 0),
            "price": int(row[6] or 0),
            "market_price": row[7],
            "last_seen": row[8],
        }
        for row in rows
    }


def _qualifies(item: dict, min_value: int):
    # One threshold covers an expensive single item and an expensive stack.
    basis = max(
        int(item.get("price") or 0),
        int(item.get("market_price") or 0),
        int(item.get("ask_stack_value") or 0),
        int(item.get("market_stack_value") or 0),
    )
    return basis >= int(min_value), basis


def _reason(item: dict, min_value: int):
    price = int(item.get("price") or 0)
    market = int(item.get("market_price") or 0)
    qty = int(item.get("quantity") or 1)
    ask_stack = price * qty
    market_stack = market * qty
    reasons = []
    if market >= min_value:
        reasons.append("market value")
    if price >= min_value:
        reasons.append("asking price")
    if qty > 1 and market_stack >= min_value:
        reasons.append("market stack value")
    if qty > 1 and ask_stack >= min_value:
        reasons.append("asking stack value")
    return ", ".join(dict.fromkeys(reasons)) or "value threshold"


async def _fetch_bazaar(player_id: int):
    key = (app_module._api_key or "").strip()
    if not key:
        raise HTTPException(401, "Load your Torn API key first")
    url = BAZAAR_API_V1.format(player_id=int(player_id))
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, params={"selections": "bazaar", "key": key})
            data = response.json()
    except httpx.RequestError as e:
        raise HTTPException(502, f"Could not reach Torn bazaar API: {e}") from e
    except ValueError as e:
        raise HTTPException(502, "Torn returned unreadable bazaar data") from e

    err = app_module.torn_error(data)
    if err:
        raise HTTPException(400, err)
    if response.status_code >= 400:
        raise HTTPException(response.status_code, f"Torn API returned HTTP {response.status_code}")
    return data


def _save_state_and_events(player_id: int, current: list[dict], events: list[dict], now: float):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("DELETE FROM bazaar_watch_state WHERE player_id=?", (int(player_id),))
        for item in current:
            c.execute(
                """
                INSERT INTO bazaar_watch_state(
                    player_id,listing_key,item_id,uid,name,item_type,quantity,price,market_price,last_seen
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(player_id), item["listing_key"], item.get("item_id"), item.get("uid"),
                    item.get("name"), item.get("type"), int(item.get("quantity") or 1),
                    int(item.get("price") or 0), item.get("market_price"), now,
                ),
            )
        for event in events:
            c.execute(
                """
                INSERT INTO bazaar_watch_events(
                    ts,player_id,event_type,listing_key,item_id,uid,name,item_type,quantity,price,
                    market_price,prior_quantity,prior_price,estimated_value,reason
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    now, int(player_id), event["event_type"], event["listing_key"], event.get("item_id"),
                    event.get("uid"), event.get("name"), event.get("type"), int(event.get("quantity") or 1),
                    int(event.get("price") or 0), event.get("market_price"), event.get("prior_quantity"),
                    event.get("prior_price"), int(event.get("estimated_value") or 0), event.get("reason") or "",
                ),
            )
        c.execute(
            """
            DELETE FROM bazaar_watch_events
            WHERE id NOT IN (SELECT id FROM bazaar_watch_events ORDER BY id DESC LIMIT ?)
            """,
            (BAZAAR_EVENT_KEEP,),
        )


def _recent_events(limit: int = 50):
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            """
            SELECT id,ts,player_id,event_type,listing_key,item_id,uid,name,item_type,quantity,price,
                   market_price,prior_quantity,prior_price,estimated_value,reason
            FROM bazaar_watch_events ORDER BY id DESC LIMIT ?
            """,
            (max(1, min(250, int(limit))),),
        ).fetchall()
    return [
        {
            "id": r[0], "ts": r[1], "player_id": r[2], "event_type": r[3], "listing_key": r[4],
            "item_id": r[5], "uid": r[6], "name": r[7], "type": r[8], "quantity": r[9],
            "price": r[10], "market_price": r[11], "prior_quantity": r[12], "prior_price": r[13],
            "estimated_value": r[14], "reason": r[15], "bazaar_url": _bazaar_url(r[2]),
        }
        for r in rows
    ]


@app.get("/api/bazaar-watch/config")
async def bazaar_watch_config():
    cfg = _config()
    return {
        "ok": True,
        **cfg,
        "poll_seconds": BAZAAR_MIN_POLL_SECONDS,
        "bazaar_url": _bazaar_url(cfg["player_id"]) if cfg.get("player_id") else None,
    }


@app.post("/api/bazaar-watch/config")
async def save_bazaar_watch_config(payload: BazaarWatchConfigPayload):
    old = _config()
    now = time.time()
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "UPDATE bazaar_watch_config SET player_id=?,min_value=?,enabled=?,updated_at=? WHERE id=1",
            (int(payload.player_id), int(payload.min_value), 1 if payload.enabled else 0, now),
        )
        if old.get("player_id") != int(payload.player_id):
            c.execute("DELETE FROM bazaar_watch_state")
    return {
        "ok": True,
        "player_id": int(payload.player_id),
        "min_value": int(payload.min_value),
        "enabled": bool(payload.enabled),
        "poll_seconds": BAZAAR_MIN_POLL_SECONDS,
        "bazaar_url": _bazaar_url(payload.player_id),
    }


@app.post("/api/bazaar-watch/check")
async def check_bazaar_watch():
    cfg = _config()
    player_id = cfg.get("player_id")
    if not player_id:
        raise HTTPException(400, "Set a player ID first")

    data = await _fetch_bazaar(player_id)
    current, is_open, bazaar_timestamp = _normalize_bazaar(data)
    previous = _load_previous(player_id)
    first_baseline = len(previous) == 0
    min_value = int(cfg.get("min_value") or 5_000_000)
    now = time.time()
    events = []

    if not first_baseline:
        for item in current:
            prior = previous.get(item["listing_key"])
            qualifies, basis = _qualifies(item, min_value)
            if not qualifies:
                continue

            event_type = None
            prior_qty = int(prior.get("quantity") or 0) if prior else None
            prior_price = int(prior.get("price") or 0) if prior else None
            if prior is None:
                event_type = "NEW"
            elif int(item.get("quantity") or 0) > prior_qty:
                event_type = "QUANTITY INCREASED"
            elif int(item.get("price") or 0) != prior_price:
                event_type = "PRICE CHANGED"

            if event_type:
                events.append({
                    **item,
                    "event_type": event_type,
                    "prior_quantity": prior_qty,
                    "prior_price": prior_price,
                    "estimated_value": basis,
                    "reason": _reason(item, min_value),
                    "bazaar_url": _bazaar_url(player_id),
                })

    _save_state_and_events(player_id, current, events, now)

    expensive = []
    for item in current:
        qualifies, basis = _qualifies(item, min_value)
        if qualifies:
            expensive.append({**item, "estimated_value": basis, "reason": _reason(item, min_value)})
    expensive.sort(key=lambda x: int(x.get("estimated_value") or 0), reverse=True)

    return {
        "ok": True,
        "checked_at": now,
        "player_id": player_id,
        "enabled": bool(cfg.get("enabled")),
        "min_value": min_value,
        "bazaar_is_open": is_open,
        "bazaar_timestamp": bazaar_timestamp,
        "bazaar_url": _bazaar_url(player_id),
        "listing_count": len(current),
        "first_baseline": first_baseline,
        "events": events,
        "event_count": len(events),
        "expensive_items": expensive,
        "expensive_count": len(expensive),
        "poll_seconds": BAZAAR_MIN_POLL_SECONDS,
    }


@app.get("/api/bazaar-watch/events")
async def bazaar_watch_events(limit: int = 50):
    return {"ok": True, "items": _recent_events(limit)}
