import httpx
from fastapi import HTTPException

from app import app, _torn_get


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
