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

LEARN_ITEMS = {
    365: "Box of Medical Supplies", 367: "Feathery Hotel Coupon", 369: "Lottery Voucher",
    530: "Can of Munster", 532: "Can of Red Cow", 533: "Can of Taurine Elite",
    555: "Can of X-MASS", 818: "Six-Pack of Energy Drink",
}

# Lower-profile markets where price mistakes are more plausible. hard_floor is a
# known direct cash-out value (Print Store / NPC) when one exists.
DISCOVERY_ITEMS = {
    1219: {"name": "Oxygen Tank"},
    1460: {"name": "Methane Tank"},
    1200: {"name": "Nitrous Tank"},
    883: {"name": "Bank Statement"},
    1344: {"name": "Medical Bill"},
    1348: {"name": "Aluminum Plate"},
    1321: {"name": "Adhesive Plastic"},
    1082: {"name": "Zip Wallet"},
    1381: {"name": "ID Badge", "hard_floor": 105000},
    1350: {"name": "Police Badge", "hard_floor": 230000},
    1379: {"name": "ATM Key", "hard_floor": 195000},
    1339: {"name": "Bank Check", "hard_floor": 180000},
    1343: {"name": "Passport", "hard_floor": 580000},
    1342: {"name": "Travel Visa", "hard_floor": 122500},
    1086: {"name": "Driver's License", "hard_floor": 5000},
    1345: {"name": "Prescription", "hard_floor": 75000},
    1349: {"name": "License Plate", "hard_floor": 95000},
}

app = FastAPI(title="TornTools Local Scanner", version="0.4.0")
app.mount("/static", StaticFiles(directory=WEB), name="static")
_api_key: str | None = None
_last_scan: dict[str, Any] | None = None

class KeyPayload(BaseModel):
    api_key: str

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, item_id INTEGER NOT NULL,
            lowest INTEGER, qty_floor INTEGER, next_higher INTEGER, average_price INTEGER,
            listing_count INTEGER, listing_ids TEXT NOT NULL, total_top_qty INTEGER NOT NULL)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_market_snapshots_item_ts ON market_snapshots(item_id, ts)")
init_db()

def market_url(item_id: int) -> str:
    return "https://www.torn.com/page.php?sid=ItemMarket" f"#/market/view=search&itemID={item_id}&sortField=price&sortOrder=ASC"

def torn_error(data: Any) -> str | None:
    if not isinstance(data, dict) or not data.get("error"): return None
    err = data["error"]
    if isinstance(err, dict):
        code = err.get("code"); message = err.get("error") or err.get("message") or str(err)
        return f"Torn API error {code}: {message}" if code is not None else message
    return str(err)

def parse_itemmarket(data: dict) -> tuple[list[dict], int | None]:
    if not isinstance(data, dict): return [], None
    itemmarket = data.get("itemmarket") or {}
    if not isinstance(itemmarket, dict): return [], None
    item = itemmarket.get("item") or {}; average_price = None
    if isinstance(item, dict):
        try:
            raw = item.get("average_price"); average_price = int(raw) if raw is not None else None
        except (TypeError, ValueError): pass
    clean = []
    for row in itemmarket.get("listings") or []:
        if not isinstance(row, dict): continue
        try:
            price = int(row["price"]); amount = int(row.get("amount", 1) or 1)
            if price > 0: clean.append({"id": row.get("id"), "price": price, "amount": max(1, amount)})
        except (KeyError, TypeError, ValueError): continue
    clean.sort(key=lambda x: x["price"])
    return clean, average_price

async def fetch_item_market(client: httpx.AsyncClient, item_id: int, limit: int = 100) -> dict:
    if not _api_key: raise HTTPException(401, "Load your Torn API key first")
    try:
        r = await client.get(f"{API_BASE}/market/{item_id}/itemmarket", headers={"Authorization": f"ApiKey {_api_key}"}, params={"limit": limit, "offset": 0})
        data = r.json()
    except httpx.RequestError as exc: raise HTTPException(502, f"Could not reach Torn API: {exc}") from exc
    except ValueError as exc: raise HTTPException(502, "Torn API returned unreadable data") from exc
    err = torn_error(data)
    if err: raise HTTPException(400, err)
    if r.status_code >= 400: raise HTTPException(r.status_code, f"Torn API returned HTTP {r.status_code}")
    return data

def analyze_item(item_id: int, listings: list[dict], average_price: int | None) -> dict:
    meta = ITEMS[item_id]
    if not listings: return {"id": item_id, **meta, "market_url": market_url(item_id), "error": "No readable listings returned"}
    lowest = listings[0]["price"]; qty_floor = sum(x["amount"] for x in listings if x["price"] == lowest)
    next_higher = next((x["price"] for x in listings if x["price"] > lowest), None)
    reference = average_price or next_higher or lowest; discount_pct = ((reference-lowest)/reference*100) if reference else 0.0
    capital = lowest * qty_floor; profit = roi = None
    if next_higher is not None:
        net = int(next_higher*(1-MARKET_FEE)); each = net-lowest; roi = each/lowest*100; profit = each*qty_floor
    return {"id":item_id,**meta,"lowest":lowest,"qty_floor":qty_floor,"next_higher":next_higher,"reference":reference,
            "average_price":average_price,"discount_pct":discount_pct,"net_roi_after_fee":roi,"floor_clear_capital":capital,
            "floor_clear_profit_after_fee":profit,"market_url":market_url(item_id),"top":listings[:10]}

def save_snapshot(item_id: int, listings: list[dict], average_price: int | None) -> None:
    if not listings: return
    lowest=listings[0]["price"]; qty=sum(x["amount"] for x in listings if x["price"]==lowest)
    nxt=next((x["price"] for x in listings if x["price"]>lowest),None); top=listings[:30]
    ids=[str(x.get("id")) for x in top if x.get("id") is not None]; total=sum(x["amount"] for x in top)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT INTO market_snapshots (ts,item_id,lowest,qty_floor,next_higher,average_price,listing_count,listing_ids,total_top_qty) VALUES (?,?,?,?,?,?,?,?,?)",
                    (time.time(),item_id,lowest,qty,nxt,average_price,len(listings),json.dumps(ids),total))

def liquidity_stats(item_id: int) -> dict:
    with sqlite3.connect(DB_PATH) as con:
        rows=con.execute("SELECT ts,lowest,qty_floor,next_higher,average_price,listing_count,listing_ids,total_top_qty FROM market_snapshots WHERE item_id=? ORDER BY ts DESC LIMIT 120",(item_id,)).fetchall()
    if not rows: return {"observations":0,"changes":0,"score":0,"label":"Learning","gap_events":0,"largest_gap_pct":0}
    rows=list(reversed(rows)); changes=floor_changes=gap_events=0; largest=0.0; prev=None
    for row in rows:
        _,low,qty,nxt,_,_,ids,total=row
        if low and nxt and nxt>low:
            gp=(nxt-low)/low*100; largest=max(largest,gp); gap_events += 1 if gp>=1 else 0
        if prev:
            if low!=prev[1] or qty!=prev[2]: floor_changes+=1
            if low!=prev[1] or qty!=prev[2] or ids!=prev[6] or total!=prev[7]: changes+=1
        prev=row
    trans=max(1,len(rows)-1); score=round(min(100,(changes/trans)*65+(floor_changes/trans)*35)*100)/100
    label="Learning" if len(rows)<4 else "Very active" if score>=70 else "Active" if score>=45 else "Moderate" if score>=20 else "Slow"
    return {"observations":len(rows),"changes":changes,"score":score,"label":label,"gap_events":gap_events,"largest_gap_pct":round(largest,2),"last_seen":rows[-1][0]}

def discovery_result(item_id:int, meta:dict, listings:list[dict], average_price:int|None)->dict:
    if not listings: return {"id":item_id,"name":meta["name"],"error":"No listings","market_url":market_url(item_id)}
    low=listings[0]["price"]; qty=sum(x["amount"] for x in listings if x["price"]==low); floor=meta.get("hard_floor")
    ref=average_price or low; discount=((ref-low)/ref*100) if ref else 0
    floor_profit=((floor-low)*qty) if floor and low<floor else 0
    floor_discount=((floor-low)/floor*100) if floor and low<floor else 0
    learned=liquidity_stats(item_id)
    # Simple ranking: guaranteed cash-out dominates; otherwise large discount + evidence of activity.
    score=(1000 + floor_discount*10 + min(300,floor_profit/1000)) if floor_profit>0 else max(0,discount*8 + learned.get("score",0)*0.6)
    kind="NPC FLOOR" if floor_profit>0 else "UNDER MARKET" if discount>=8 else "WATCH"
    return {"id":item_id,"name":meta["name"],"lowest":low,"qty_floor":qty,"market_value":average_price,"discount_pct":round(discount,2),
            "hard_floor":floor,"floor_profit":floor_profit,"floor_discount_pct":round(floor_discount,2),"kind":kind,"deal_score":round(score,2),
            "activity":learned.get("label","Learning"),"samples":learned.get("observations",0),"market_url":market_url(item_id)}

@app.get("/")
async def home(): return FileResponse(WEB/"index.html")

@app.get("/api/status")
async def status():
    return {"ok":True,"version":"0.4.0","key_loaded":bool(_api_key),"market_fee_pct":MARKET_FEE*100,
            "items":[{"id":i,**m} for i,m in ITEMS.items()],"learn_items":[{"id":i,"name":n} for i,n in LEARN_ITEMS.items()],
            "discovery_count":len(DISCOVERY_ITEMS)}

@app.post("/api/key")
async def set_key(payload:KeyPayload):
    global _api_key
    c=payload.api_key.strip()
    if not c: raise HTTPException(400,"API key is blank")
    _api_key=c; return {"ok":True,"message":"API key loaded into memory."}

@app.delete("/api/key")
async def forget_key():
    global _api_key,_last_scan
    _api_key=None; _last_scan=None; return {"ok":True}

@app.get("/api/scan")
async def scan(ids:str=Query(default="206,366,370")):
    global _last_scan
    if not _api_key: raise HTTPException(401,"Load your Torn API key first")
    try:
        requested=[]
        for p in ids.split(","):
            i=int(p.strip())
            if i in ITEMS and i not in requested: requested.append(i)
    except ValueError as exc: raise HTTPException(400,"Invalid item ID list") from exc
    async with httpx.AsyncClient(timeout=15) as client: results=await asyncio.gather(*(fetch_item_market(client,i) for i in requested),return_exceptions=True)
    out=[]
    for i,r in zip(requested,results):
        if isinstance(r,Exception): out.append({"id":i,**ITEMS[i],"error":str(getattr(r,"detail",r))}); continue
        listings,avg=parse_itemmarket(r); out.append(analyze_item(i,listings,avg))
    _last_scan={"ok":True,"scanned_at":time.time(),"market_fee_pct":MARKET_FEE*100,"items":out}; return _last_scan

@app.post("/api/learn")
async def learn_markets():
    if not _api_key: raise HTTPException(401,"Load your Torn API key first")
    ids=list(LEARN_ITEMS); async with httpx.AsyncClient(timeout=20) as client: results=await asyncio.gather(*(fetch_item_market(client,i) for i in ids),return_exceptions=True)
    out=[]
    for i,r in zip(ids,results):
        if isinstance(r,Exception): out.append({"id":i,"name":LEARN_ITEMS[i],"error":str(getattr(r,"detail",r))}); continue
        listings,avg=parse_itemmarket(r); save_snapshot(i,listings,avg); out.append({"id":i,"name":LEARN_ITEMS[i],"lowest":listings[0]["price"] if listings else None,"average_price":avg,"market_url":market_url(i),**liquidity_stats(i)})
    out.sort(key=lambda x:(x.get("score",-1),x.get("gap_events",-1)),reverse=True); return {"ok":True,"learned_at":time.time(),"items":out}

@app.get("/api/liquidity")
async def get_liquidity():
    out=[{"id":i,"name":n,"market_url":market_url(i),**liquidity_stats(i)} for i,n in LEARN_ITEMS.items()]
    out.sort(key=lambda x:(x.get("score",-1),x.get("gap_events",-1)),reverse=True); return {"ok":True,"items":out}

@app.post("/api/discover")
async def discover_hidden_deals():
    if not _api_key: raise HTTPException(401,"Load your Torn API key first")
    ids=list(DISCOVERY_ITEMS)
    async with httpx.AsyncClient(timeout=25) as client: results=await asyncio.gather(*(fetch_item_market(client,i,60) for i in ids),return_exceptions=True)
    out=[]
    for i,r in zip(ids,results):
        meta=DISCOVERY_ITEMS[i]
        if isinstance(r,Exception): out.append({"id":i,"name":meta["name"],"error":str(getattr(r,"detail",r))}); continue
        listings,avg=parse_itemmarket(r); save_snapshot(i,listings,avg); out.append(discovery_result(i,meta,listings,avg))
    out.sort(key=lambda x:x.get("deal_score",-1),reverse=True)
    return {"ok":True,"scanned_at":time.time(),"items":out}

@app.get("/api/last-scan")
async def last_scan(): return _last_scan or {"ok":True,"scanned_at":None,"items":[]}
