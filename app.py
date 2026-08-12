from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
API_BASE = "https://api.torn.com/v2"
XANAX_ID = 206

app = FastAPI(title="TornTools Local Scanner")
app.mount("/static", StaticFiles(directory=WEB), name="static")

_api_key = None

class KeyPayload(BaseModel):
    api_key: str

@app.get("/")
async def home():
    return FileResponse(WEB / "index.html")

@app.get("/api/status")
async def status():
    return {"ok": True, "key_loaded": bool(_api_key)}

@app.post("/api/key")
async def set_key(payload: KeyPayload):
    global _api_key
    key = payload.api_key.strip()
    if not key:
        raise HTTPException(400, "API key is blank")
    _api_key = key
    return {"ok": True}

@app.delete("/api/key")
async def forget_key():
    global _api_key
    _api_key = None
    return {"ok": True}

@app.get("/api/xanax")
async def xanax():
    if not _api_key:
        raise HTTPException(401, "Load your Torn API key first")

    url = f"{API_BASE}/market/{XANAX_ID}/itemmarket"
    headers = {"Authorization": f"ApiKey {_api_key}"}
    params = {"limit": 100, "offset": 0}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers=headers, params=params)
            data = r.json()
    except Exception as exc:
        raise HTTPException(502, f"Could not query Torn API: {exc}")

    if isinstance(data, dict) and data.get("error"):
        raise HTTPException(400, str(data["error"]))

    rows = data.get("itemmarket", []) if isinstance(data, dict) else []
    clean = []
    for x in rows:
        try:
            clean.append({
                "price": int(x["price"]),
                "amount": int(x.get("amount", 1))
            })
        except Exception:
            pass

    clean.sort(key=lambda x: x["price"])
    if not clean:
        raise HTTPException(502, "No readable Xanax listings were returned")

    low = clean[0]["price"]
    next_price = next((x["price"] for x in clean if x["price"] > low), None)
    qty_floor = sum(x["amount"] for x in clean if x["price"] == low)

    net_profit = None
    net_roi = None
    if next_price:
        net_exit = int(next_price * 0.95)
        net_profit = net_exit - low
        net_roi = (net_profit / low) * 100

    return {
        "lowest": low,
        "qty_floor": qty_floor,
        "next_price": next_price,
        "net_profit_after_5pct": net_profit,
        "net_roi_after_5pct": net_roi,
        "top": clean[:12],
        "market_url": "https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID=206&sortField=price&sortOrder=ASC"
    }
