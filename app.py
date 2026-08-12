from pathlib import Path
import asyncio
import json
import sqlite3
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
DB_PATH = BASE / "torntools.sqlite3"
API_BASE = "https://api.torn.com/v2"
MARKET_FEE = 0.05

ITEMS = {
    206: {"name": "Xanax", "mode": "stock", "enabled": True, "note": "Personal use / jumps"},
    366: {"name": "Erotic DVD", "mode": "stock", "enabled": True, "note": "Personal use / happy jumps"},
    370: {"name": "Drug Pack", "mode": "flip", "enabled": True, "note": "Resale candidate"},
    283: {"name": "Donator Pack", "mode": "flip", "enabled": False, "note": "Higher-capital resale candidate"},
}

# Quiet research pool. These are measured for market churn and pricing behavior
# but do not create buy alerts unless later promoted to the main watchlist.
LEARN_ITEMS = {
    365: "Box of Medical Supplies",
    367: "Feathery Hotel Coupon",
    369: "Lottery Voucher",
    530: "Can of Munster",
    532: "Can of Red Cow",
    533: "Can of Taurine Elite",
    555: "Can of X-MASS",
    818: "Six-Pack of Energy Drink",
}

app = FastAPI(title="TornTools Local Scanner", version="0.3.0")
app.mount("/static", StaticFiles(directory=WEB), name="static")

_api_key: str | None = None
_last_scan: dict[str, Any] | None = None


class KeyPayload(BaseModel):
    api_key: str


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                item_id INTEGER NOT NULL,
                lowest INTEGER,
                qty_floor INTEGER,
                next_higher INTEGER,
                average_price INTEGER,
                listing_count INTEGER,
                listing_ids TEXT NOT NULL,
                total_top_qty INTEGER NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_market_snapshots_item_ts ON market_snapshots(item_id, ts)")


init_db()


def market_url(item_id: int) -> str:
    return (
        "https://www.torn.com/page.php?sid=ItemMarket"
        f"#/market/view=search&itemID={item_id}&sortField=price&sortOrder=ASC"
    )


def torn_error(data: Any) -> str | None:
    if not isinstance(data, dict) or not data.get("error"):
        return None
    err = data["error"]
    if isinstance(err, dict):
        code = err.get("code")
        message = err.get("error") or err.get("message") or str(err)
        return f"Torn API error {code}: {message}" if code is not None else message
    return str(err)


def parse_itemmarket(data: dict) -> tuple[list[dict], int | None]:
    if not isinstance(data, dict):
        return [], None
    itemmarket = data.get("itemmarket") or {}
    if not isinstance(itemmarket, dict):
        return [], None

    item = itemmarket.get("item") or {}
    average_price = None
    if isinstance(item, dict):
        try:
            raw_avg = item.get("average_price")
            average_price = int(raw_avg) if raw_avg is not None else None
        except (TypeError, ValueError):
            pass

    clean = []
    for row in itemmarket.get("listings") or []:
        if not isinstance(row, dict):
            continue
        try:
            price = int(row["price"])
            amount = int(row.get("amount", 1) or 1)
            if price <= 0:
                continue
            clean.append({"id": row.get("id"), "price": price, "amount": max(1, amount)})
        except (KeyError, TypeError, ValueError):
            continue
    clean.sort(key=lambda x: x["price"])
    return clean, average_price


async def fetch_item_market(client: httpx.AsyncClient, item_id: int, limit: int = 100) -> dict:
    if not _api_key:
        raise HTTPException(401, "Load your Torn API key first")
    try:
        response = await client.get(
            f"{API_BASE}/market/{item_id}/itemmarket",
            headers={"Authorization": f"ApiKey {_api_key}"},
            params={"limit": limit, "offset": 0},
        )
        data = response.json()
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Could not reach Torn API: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(502, "Torn API returned unreadable data") from exc

    err = torn_error(data)
    if err:
        raise HTTPException(400, err)
    if response.status_code >= 400:
        raise HTTPException(response.status_code, f"Torn API returned HTTP {response.status_code}")
    return data


def analyze_item(item_id: int, listings: list[dict], average_price: int | None) -> dict:
    meta = ITEMS[item_id]
    if not listings:
        return {"id": item_id, **meta, "market_url": market_url(item_id), "error": "No readable listings returned"}

    lowest = listings[0]["price"]
    qty_floor = sum(x["amount"] for x in listings if x["price"] == lowest)
    next_higher = next((x["price"] for x in listings if x["price"] > lowest), None)
    reference = average_price or next_higher or lowest
    discount_pct = ((reference - lowest) / reference * 100) if reference else 0.0

    floor_clear_capital = lowest * qty_floor
    net_profit_each = None
    net_roi = None
    floor_clear_profit = None
    if next_higher is not None:
        net_exit_each = int(next_higher * (1 - MARKET_FEE))
        net_profit_each = net_exit_each - lowest
        net_roi = (net_profit_each / lowest) * 100
        floor_clear_profit = net_profit_each * qty_floor

    return {
        "id": item_id,
        **meta,
        "lowest": lowest,
        "qty_floor": qty_floor,
        "next_higher": next_higher,
        "reference": reference,
        "average_price": average_price,
        "discount_pct": discount_pct,
        "net_profit_each_after_fee": net_profit_each,
        "net_roi_after_fee": net_roi,
        "floor_clear_capital": floor_clear_capital,
        "floor_clear_profit_after_fee": floor_clear_profit,
        "market_url": market_url(item_id),
        "top": listings[:10],
    }


def save_snapshot(item_id: int, listings: list[dict], average_price: int | None) -> None:
    if not listings:
        return
    lowest = listings[0]["price"]
    qty_floor = sum(x["amount"] for x in listings if x["price"] == lowest)
    next_higher = next((x["price"] for x in listings if x["price"] > lowest), None)
    top = listings[:30]
    listing_ids = [str(x.get("id")) for x in top if x.get("id") is not None]
    total_top_qty = sum(x["amount"] for x in top)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """INSERT INTO market_snapshots
            (ts,item_id,lowest,qty_floor,next_higher,average_price,listing_count,listing_ids,total_top_qty)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (time.time(), item_id, lowest, qty_floor, next_higher, average_price, len(listings), json.dumps(listing_ids), total_top_qty),
        )


def liquidity_stats(item_id: int) -> dict:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """SELECT ts,lowest,qty_floor,next_higher,average_price,listing_count,listing_ids,total_top_qty
               FROM market_snapshots WHERE item_id=? ORDER BY ts DESC LIMIT 120""",
            (item_id,),
        ).fetchall()

    if not rows:
        return {"observations": 0, "changes": 0, "score": 0, "label": "Learning", "gap_events": 0}

    rows = list(reversed(rows))
    changes = 0
    floor_changes = 0
    book_changes = 0
    gap_events = 0
    largest_gap_pct = 0.0

    previous = None
    for row in rows:
        ts, lowest, qty_floor, next_higher, avg, listing_count, listing_ids, total_top_qty = row
        if lowest and next_higher and next_higher > lowest:
            gap_pct = ((next_higher - lowest) / lowest) * 100
            largest_gap_pct = max(largest_gap_pct, gap_pct)
            if gap_pct >= 1.0:
                gap_events += 1
        if previous:
            if lowest != previous[1] or qty_floor != previous[2]:
                floor_changes += 1
            if listing_ids != previous[6] or total_top_qty != previous[7]:
                book_changes += 1
            if lowest != previous[1] or qty_floor != previous[2] or listing_ids != previous[6] or total_top_qty != previous[7]:
                changes += 1
        previous = row

    transitions = max(1, len(rows) - 1)
    churn_rate = changes / transitions
    floor_rate = floor_changes / transitions
    score = round(min(100, churn_rate * 65 + floor_rate * 35) * 100) / 100
    if len(rows) < 4:
        label = "Learning"
    elif score >= 70:
        label = "Very active"
    elif score >= 45:
        label = "Active"
    elif score >= 20:
        label = "Moderate"
    else:
        label = "Slow"

    return {
        "observations": len(rows),
        "changes": changes,
        "score": score,
        "label": label,
        "gap_events": gap_events,
        "largest_gap_pct": round(largest_gap_pct, 2),
        "first_seen": rows[0][0],
        "last_seen": rows[-1][0],
    }


@app.get("/")
async def home():
    return FileResponse(WEB / "index.html")


@app.get("/api/status")
async def status():
    return {
        "ok": True,
        "version": "0.3.0",
        "key_loaded": bool(_api_key),
        "market_fee_pct": MARKET_FEE * 100,
        "items": [{"id": item_id, **meta} for item_id, meta in ITEMS.items()],
        "learn_items": [{"id": item_id, "name": name} for item_id, name in LEARN_ITEMS.items()],
    }


@app.post("/api/key")
async def set_key(payload: KeyPayload):
    global _api_key
    candidate = payload.api_key.strip()
    if not candidate:
        raise HTTPException(400, "API key is blank")
    _api_key = candidate
    return {"ok": True, "message": "API key loaded into memory."}


@app.delete("/api/key")
async def forget_key():
    global _api_key, _last_scan
    _api_key = None
    _last_scan = None
    return {"ok": True}


@app.get("/api/scan")
async def scan(ids: str = Query(default="206,366,370")):
    global _last_scan
    if not _api_key:
        raise HTTPException(401, "Load your Torn API key first")

    try:
        requested = []
        for part in ids.split(","):
            item_id = int(part.strip())
            if item_id in ITEMS and item_id not in requested:
                requested.append(item_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid item ID list") from exc
    if not requested:
        raise HTTPException(400, "No supported items selected")

    async with httpx.AsyncClient(timeout=15) as client:
        results = await asyncio.gather(*(fetch_item_market(client, item_id) for item_id in requested), return_exceptions=True)

    analyzed = []
    for item_id, result in zip(requested, results):
        if isinstance(result, Exception):
            analyzed.append({"id": item_id, **ITEMS[item_id], "market_url": market_url(item_id), "error": str(getattr(result, "detail", result))})
            continue
        listings, average_price = parse_itemmarket(result)
        analyzed.append(analyze_item(item_id, listings, average_price))

    payload = {"ok": True, "scanned_at": time.time(), "market_fee_pct": MARKET_FEE * 100, "items": analyzed}
    _last_scan = payload
    return payload


@app.post("/api/learn")
async def learn_markets():
    if not _api_key:
        raise HTTPException(401, "Load your Torn API key first")

    ids = list(LEARN_ITEMS.keys())
    async with httpx.AsyncClient(timeout=20) as client:
        results = await asyncio.gather(*(fetch_item_market(client, item_id) for item_id in ids), return_exceptions=True)

    output = []
    for item_id, result in zip(ids, results):
        if isinstance(result, Exception):
            output.append({"id": item_id, "name": LEARN_ITEMS[item_id], "error": str(getattr(result, "detail", result))})
            continue
        listings, average_price = parse_itemmarket(result)
        if listings:
            save_snapshot(item_id, listings, average_price)
        stats = liquidity_stats(item_id)
        output.append({
            "id": item_id,
            "name": LEARN_ITEMS[item_id],
            "lowest": listings[0]["price"] if listings else None,
            "average_price": average_price,
            "market_url": market_url(item_id),
            **stats,
        })

    output.sort(key=lambda x: (x.get("score", -1), x.get("gap_events", -1)), reverse=True)
    return {"ok": True, "learned_at": time.time(), "items": output}


@app.get("/api/liquidity")
async def get_liquidity():
    items = []
    for item_id, name in LEARN_ITEMS.items():
        items.append({"id": item_id, "name": name, "market_url": market_url(item_id), **liquidity_stats(item_id)})
    items.sort(key=lambda x: (x.get("score", -1), x.get("gap_events", -1)), reverse=True)
    return {"ok": True, "items": items}


@app.get("/api/last-scan")
async def last_scan():
    return _last_scan or {"ok": True, "scanned_at": None, "items": []}
