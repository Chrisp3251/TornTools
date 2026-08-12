import asyncio
import json
import sqlite3
import time

import httpx
from fastapi import HTTPException

from app import (
    app,
    _torn_get,
    DB_PATH,
    DISCOVERY_ITEMS,
    LEARN_ITEMS,
    fetch_market,
    liquidity_stats,
    market_url,
    parse_itemmarket,
    save_snapshot,
)


# -----------------------------------------------------------------------------
# Equipment metadata compatibility route
# -----------------------------------------------------------------------------

def _base_item_from_response(data, item_id: int):
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            if int(item.get("id")) == int(item_id):
                return item
        except (TypeError, ValueError):
            continue
    return None


@app.get("/api/equipment/meta")
async def equipment_meta(item_id: int):
    if item_id <= 0:
        raise HTTPException(400, "Enter a valid item ID")
    async with httpx.AsyncClient(timeout=15) as client:
        data = await _torn_get(client, f"/torn/{item_id}/items", error_text="item metadata")
    item = _base_item_from_response(data, item_id)
    if not item:
        raise HTTPException(404, "Torn did not return metadata for that item")
    value = item.get("value") if isinstance(item.get("value"), dict) else {}
    return {
        "ok": True,
        "item_id": item_id,
        "name": item.get("name"),
        "type": item.get("type"),
        "sub_type": item.get("sub_type"),
        "vendor": value.get("vendor"),
        "buy_price": value.get("buy_price"),
        "sell_price": value.get("sell_price"),
        "market_price": value.get("market_price"),
    }


# -----------------------------------------------------------------------------
# Research Lab -> Hidden Deals graduation pipeline
# -----------------------------------------------------------------------------

RESEARCH_MIN_SAMPLES = 12
RESEARCH_MIN_ACTIVITY = 30
RESEARCH_MIN_BARGAIN_EVENTS = 3
RESEARCH_MIN_STRONG_EVENTS = 1
RESEARCH_MIN_HIT_RATE = 0.18
RESEARCH_MIN_BEST_DISCOUNT = 12.0


def _init_research_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS research_promotions(
                item_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                promoted_at REAL NOT NULL,
                promotion_score REAL NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )


def _load_research_promotions():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT item_id,name,promoted_at,promotion_score,reason FROM research_promotions"
        ).fetchall()
    for item_id, name, promoted_at, score, reason in rows:
        if item_id not in DISCOVERY_ITEMS:
            DISCOVERY_ITEMS[item_id] = {
                "name": name,
                "research_graduate": True,
                "promoted_at": promoted_at,
                "promotion_score": score,
                "promotion_reason": reason,
            }


_init_research_db()
_load_research_promotions()


def _research_rows(item_id: int, limit: int = 120):
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            """
            SELECT ts,lowest,qty_floor,next_higher,average_price,listing_count,listing_ids,total_top_qty
            FROM market_snapshots
            WHERE item_id=?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (item_id, limit),
        ).fetchall()


def research_profile(item_id: int, name: str):
    rows = list(reversed(_research_rows(item_id)))
    liq = liquidity_stats(item_id)
    if not rows:
        return {
            "id": item_id,
            "name": name,
            "market_url": market_url(item_id),
            "observations": 0,
            "activity": "Learning",
            "activity_score": 0,
            "bargain_events": 0,
            "strong_events": 0,
            "hit_rate": 0,
            "best_discount_pct": 0,
            "median_discount_pct": 0,
            "floor_changes": 0,
            "listing_churn_events": 0,
            "gap_events": 0,
            "largest_gap_pct": 0,
            "promotion_score": 0,
            "stage": "LEARNING",
            "graduated": item_id in DISCOVERY_ITEMS and bool(DISCOVERY_ITEMS[item_id].get("research_graduate")),
            "requirements": _research_requirements_text(),
        }

    discounts = []
    bargain_events = strong_events = floor_changes = churn_events = 0
    prev = None
    latest_low = latest_avg = None

    for row in rows:
        ts, low, qty, nxt, avg, listing_count, listing_ids, total_qty = row
        latest_low, latest_avg = low, avg
        if low and avg and avg > 0:
            discount = max(-100.0, min(100.0, (avg - low) / avg * 100))
            discounts.append(discount)
            if discount >= 8:
                bargain_events += 1
            if discount >= 15:
                strong_events += 1
        if prev:
            if low != prev[1] or qty != prev[2]:
                floor_changes += 1
            if listing_ids != prev[6] or total_qty != prev[7]:
                churn_events += 1
        prev = row

    n = len(rows)
    positive_discounts = sorted(discounts)
    median_discount = 0.0
    if positive_discounts:
        middle = len(positive_discounts) // 2
        if len(positive_discounts) % 2:
            median_discount = positive_discounts[middle]
        else:
            median_discount = (positive_discounts[middle - 1] + positive_discounts[middle]) / 2
    best_discount = max(discounts) if discounts else 0.0
    hit_rate = bargain_events / n if n else 0.0
    strong_rate = strong_events / n if n else 0.0
    floor_change_rate = floor_changes / max(1, n - 1)
    churn_rate = churn_events / max(1, n - 1)

    # Score rewards repeatable discounts and a market active enough to exit,
    # while limiting the effect of a single freak observation.
    score = (
        min(30, hit_rate * 100)
        + min(20, strong_rate * 120)
        + min(15, best_discount * 0.75)
        + min(15, float(liq.get("score") or 0) * 0.15)
        + min(10, churn_rate * 20)
        + min(10, float(liq.get("gap_events") or 0) * 1.5)
    )
    score = round(min(100, score), 1)

    graduated = item_id in DISCOVERY_ITEMS and bool(DISCOVERY_ITEMS[item_id].get("research_graduate"))
    ready = (
        n >= RESEARCH_MIN_SAMPLES
        and float(liq.get("score") or 0) >= RESEARCH_MIN_ACTIVITY
        and bargain_events >= RESEARCH_MIN_BARGAIN_EVENTS
        and strong_events >= RESEARCH_MIN_STRONG_EVENTS
        and hit_rate >= RESEARCH_MIN_HIT_RATE
        and best_discount >= RESEARCH_MIN_BEST_DISCOUNT
    )

    if graduated:
        stage = "GRADUATED"
    elif ready:
        stage = "READY"
    elif n < RESEARCH_MIN_SAMPLES:
        stage = "LEARNING"
    elif bargain_events == 0:
        stage = "NO EDGE YET"
    else:
        stage = "BUILDING CASE"

    return {
        "id": item_id,
        "name": name,
        "market_url": market_url(item_id),
        "lowest": latest_low,
        "average_price": latest_avg,
        "observations": n,
        "activity": liq.get("label", "Learning"),
        "activity_score": round(float(liq.get("score") or 0), 1),
        "bargain_events": bargain_events,
        "strong_events": strong_events,
        "hit_rate": round(hit_rate * 100, 1),
        "best_discount_pct": round(best_discount, 2),
        "median_discount_pct": round(median_discount, 2),
        "floor_changes": floor_changes,
        "floor_change_rate": round(floor_change_rate * 100, 1),
        "listing_churn_events": churn_events,
        "listing_churn_rate": round(churn_rate * 100, 1),
        "gap_events": int(liq.get("gap_events") or 0),
        "largest_gap_pct": round(float(liq.get("largest_gap_pct") or 0), 2),
        "promotion_score": score,
        "stage": stage,
        "graduated": graduated,
        "ready": ready,
        "requirements": _research_requirements_text(),
    }


def _research_requirements_text():
    return (
        f"{RESEARCH_MIN_SAMPLES}+ samples, activity {RESEARCH_MIN_ACTIVITY}+, "
        f"{RESEARCH_MIN_BARGAIN_EVENTS}+ 8% bargain snapshots, "
        f"{RESEARCH_MIN_STRONG_EVENTS}+ 15% strong snapshot, "
        f"{int(RESEARCH_MIN_HIT_RATE*100)}%+ hit rate, "
        f"{RESEARCH_MIN_BEST_DISCOUNT:.0f}%+ best discount"
    )


def _promote_if_ready(profile):
    if not profile.get("ready") or profile.get("graduated"):
        return False
    item_id = int(profile["id"])
    name = profile["name"]
    reason = (
        f"Research score {profile['promotion_score']}; "
        f"{profile['bargain_events']}/{profile['observations']} bargain snapshots; "
        f"best discount {profile['best_discount_pct']:.1f}%"
    )
    now = time.time()
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR REPLACE INTO research_promotions(item_id,name,promoted_at,promotion_score,reason) VALUES(?,?,?,?,?)",
            (item_id, name, now, profile["promotion_score"], reason),
        )
    DISCOVERY_ITEMS[item_id] = {
        "name": name,
        "research_graduate": True,
        "promoted_at": now,
        "promotion_score": profile["promotion_score"],
        "promotion_reason": reason,
    }
    return True


@app.post("/api/research/sample")
async def research_sample():
    ids = list(LEARN_ITEMS)
    async with httpx.AsyncClient(timeout=25) as client:
        results = await asyncio.gather(
            *(fetch_market(client, item_id) for item_id in ids),
            return_exceptions=True,
        )

    errors = []
    for item_id, result in zip(ids, results):
        if isinstance(result, Exception):
            errors.append({"id": item_id, "name": LEARN_ITEMS[item_id], "error": str(getattr(result, "detail", result))})
            continue
        listings, avg = parse_itemmarket(result)
        save_snapshot(item_id, listings, avg)

    profiles = [research_profile(item_id, name) for item_id, name in LEARN_ITEMS.items()]
    newly_graduated = []
    for profile in profiles:
        if _promote_if_ready(profile):
            newly_graduated.append({"id": profile["id"], "name": profile["name"]})

    # Rebuild profiles so newly promoted rows immediately show GRADUATED.
    profiles = [research_profile(item_id, name) for item_id, name in LEARN_ITEMS.items()]
    profiles.sort(key=lambda x: (x.get("graduated", False), x.get("promotion_score", 0)), reverse=True)
    return {
        "ok": True,
        "sampled_at": time.time(),
        "items": profiles,
        "errors": errors,
        "newly_graduated": newly_graduated,
        "discovery_ids": list(DISCOVERY_ITEMS),
        "requirements": _research_requirements_text(),
    }


@app.get("/api/research/status")
async def research_status():
    profiles = [research_profile(item_id, name) for item_id, name in LEARN_ITEMS.items()]
    profiles.sort(key=lambda x: (x.get("graduated", False), x.get("promotion_score", 0)), reverse=True)
    with sqlite3.connect(DB_PATH) as c:
        promoted = c.execute(
            "SELECT item_id,name,promoted_at,promotion_score,reason FROM research_promotions ORDER BY promoted_at DESC"
        ).fetchall()
    return {
        "ok": True,
        "items": profiles,
        "graduates": [
            {
                "id": row[0],
                "name": row[1],
                "promoted_at": row[2],
                "promotion_score": row[3],
                "reason": row[4],
            }
            for row in promoted
        ],
        "discovery_ids": list(DISCOVERY_ITEMS),
        "requirements": _research_requirements_text(),
    }
