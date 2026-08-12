from pathlib import Path
import asyncio
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
API_BASE = "https://api.torn.com/v2"
MARKET_FEE = 0.05

ITEMS = {
    206: {"name": "Xanax", "mode": "stock", "enabled": True, "note": "Keep for jumps / train stack"},
    366: {"name": "Erotic DVD", "mode": "stock", "enabled": True, "note": "Keep for happy jumps"},
    370: {"name": "Drug Pack", "mode": "flip", "enabled": True, "note": "Liquid supply-pack flip candidate"},
    283: {"name": "Donator Pack", "mode": "flip", "enabled": False, "note": "High-capital flip candidate"},
}

app = FastAPI(title="TornTools Local Scanner", version="0.2.2")
app.mount("/static", StaticFiles(directory=WEB), name="static")

_api_key: str | None = None
_last_scan_at = 0.0
_last_scan: dict[str, Any] | None = None


class KeyPayload(BaseModel):
    api_key: str


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
    """Parse Torn API v2 market/{id}/itemmarket response.

    Current v2 shape:
      {"itemmarket": {"item": {..., "average_price": n}, "listings": [...]}}
    """
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
            average_price = None

    rows = itemmarket.get("listings") or []
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            price = int(row["price"])
            amount = int(row.get("amount", 1) or 1)
            if price <= 0:
                continue
            clean.append({
                "id": row.get("id"),
                "price": price,
                "amount": max(1, amount),
            })
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
        return {
            "id": item_id,
            **meta,
            "market_url": market_url(item_id),
            "error": "No readable listings returned",
        }

    lowest = listings[0]["price"]
    qty_floor = sum(x["amount"] for x in listings if x["price"] == lowest)
    next_higher = next((x["price"] for x in listings if x["price"] > lowest), None)

    # Prefer Torn's own returned average_price as the reference. If it is
    # unavailable, fall back to the first higher listing / current floor.
    reference = average_price or next_higher or lowest
    discount_pct = ((reference - lowest) / reference * 100) if reference else 0.0

    gross_gap = None
    net_exit_each = None
    net_profit_each = None
    net_roi = None
    floor_clear_capital = lowest * qty_floor
    floor_clear_profit = None

    if next_higher is not None:
        gross_gap = next_higher - lowest
        net_exit_each = int(next_higher * (1 - MARKET_FEE))
        net_profit_each = net_exit_each - lowest
        net_roi = (net_profit_each / lowest) * 100
        floor_clear_profit = net_profit_each * qty_floor

    if meta["mode"] == "stock":
        opportunity_type = "stock_deal" if discount_pct > 0 else "normal"
        opportunity_value = discount_pct
    else:
        opportunity_type = "flip" if (floor_clear_profit or 0) > 0 else "normal"
        opportunity_value = net_roi or 0.0

    return {
        "id": item_id,
        **meta,
        "lowest": lowest,
        "qty_floor": qty_floor,
        "next_higher": next_higher,
        "reference": reference,
        "average_price": average_price,
        "discount_pct": discount_pct,
        "gross_gap": gross_gap,
        "net_exit_each_after_fee": net_exit_each,
        "net_profit_each_after_fee": net_profit_each,
        "net_roi_after_fee": net_roi,
        "floor_clear_capital": floor_clear_capital,
        "floor_clear_profit_after_fee": floor_clear_profit,
        "opportunity_type": opportunity_type,
        "opportunity_value": opportunity_value,
        "market_url": market_url(item_id),
        "top": listings[:10],
    }


@app.get("/")
async def home():
    return FileResponse(WEB / "index.html")


@app.get("/api/status")
async def status():
    return {
        "ok": True,
        "version": "0.2.2",
        "key_loaded": bool(_api_key),
        "market_fee_pct": MARKET_FEE * 100,
        "items": [{"id": item_id, **meta} for item_id, meta in ITEMS.items()],
    }


@app.post("/api/key")
async def set_key(payload: KeyPayload):
    global _api_key
    candidate = payload.api_key.strip()
    if not candidate:
        raise HTTPException(400, "API key is blank")

    _api_key = candidate
    return {
        "ok": True,
        "message": "API key loaded into memory. Run a scan to verify market access.",
    }


@app.delete("/api/key")
async def forget_key():
    global _api_key, _last_scan, _last_scan_at
    _api_key = None
    _last_scan = None
    _last_scan_at = 0.0
    return {"ok": True}


@app.get("/api/scan")
async def scan(ids: str = Query(default="206,366,370")):
    global _last_scan, _last_scan_at

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
        results = await asyncio.gather(
            *(fetch_item_market(client, item_id) for item_id in requested),
            return_exceptions=True,
        )

    analyzed = []
    for item_id, result in zip(requested, results):
        if isinstance(result, Exception):
            analyzed.append({
                "id": item_id,
                **ITEMS[item_id],
                "market_url": market_url(item_id),
                "error": str(getattr(result, "detail", result)),
            })
            continue

        listings, average_price = parse_itemmarket(result)
        analyzed.append(analyze_item(item_id, listings, average_price))

    now = time.time()
    payload = {
        "ok": True,
        "scanned_at": now,
        "market_fee_pct": MARKET_FEE * 100,
        "items": analyzed,
    }
    _last_scan = payload
    _last_scan_at = now
    return payload


@app.get("/api/last-scan")
async def last_scan():
    return _last_scan or {"ok": True, "scanned_at": None, "items": []}
