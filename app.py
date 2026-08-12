from pathlib import Path
import asyncio, json, os, sqlite3, time, statistics
from typing import Any
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE=Path(__file__).resolve().parent; WEB=BASE/"web"; DB_PATH=BASE/"torntools.sqlite3"; ENV_PATH=BASE/".env"
API_BASE="https://api.torn.com/v2"; MARKET_FEE=.05
ITEMS={206:{"name":"Xanax","mode":"stock","enabled":True,"note":"Personal use / jumps"},366:{"name":"Erotic DVD","mode":"stock","enabled":True,"note":"Personal use / happy jumps"},370:{"name":"Drug Pack","mode":"flip","enabled":True,"note":"Resale candidate"},283:{"name":"Donator Pack","mode":"flip","enabled":False,"note":"Higher-capital resale candidate"}}
LEARN_ITEMS={365:"Box of Medical Supplies",367:"Feathery Hotel Coupon",369:"Lottery Voucher",530:"Can of Munster",532:"Can of Red Cow",533:"Can of Taurine Elite",555:"Can of X-MASS",818:"Six-Pack of Energy Drink"}
DISCOVERY_ITEMS={1219:{"name":"Oxygen Tank"},1460:{"name":"Methane Tank"},1200:{"name":"Nitrous Tank"},883:{"name":"Bank Statement"},1344:{"name":"Medical Bill"},1348:{"name":"Aluminum Plate"},1321:{"name":"Adhesive Plastic"},1082:{"name":"Zip Wallet"},1381:{"name":"ID Badge","hard_floor":105000},1350:{"name":"Police Badge","hard_floor":230000},1379:{"name":"ATM Key","hard_floor":195000},1339:{"name":"Bank Check","hard_floor":180000},1343:{"name":"Passport","hard_floor":580000},1342:{"name":"Travel Visa","hard_floor":122500},1086:{"name":"Driver's License","hard_floor":5000},1345:{"name":"Prescription","hard_floor":75000},1349:{"name":"License Plate","hard_floor":95000}}
INVENTORY_CATEGORIES=("Primary","Secondary","Melee","Defensive")
MAX_INVENTORY_DETAILS=80
ITEM_VALUE_CACHE_SECONDS=600

def load_local_env_key():
    key=os.environ.get("TORN_API_KEY","").strip()
    if key:return key
    if not ENV_PATH.exists():return None
    try:
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line=raw.strip()
            if not line or line.startswith("#") or "=" not in line:continue
            name,value=line.split("=",1)
            if name.strip()=="TORN_API_KEY":
                value=value.strip().strip('"').strip("'")
                return value or None
    except OSError:return None
    return None

app=FastAPI(title="TornTools Local Scanner",version="0.5.4"); app.mount("/static",StaticFiles(directory=WEB),name="static")
_api_key:str|None=load_local_env_key(); _last_scan:dict[str,Any]|None=None
_item_value_cache={}
class KeyPayload(BaseModel): api_key:str

def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS market_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL NOT NULL,item_id INTEGER NOT NULL,lowest INTEGER,qty_floor INTEGER,next_higher INTEGER,average_price INTEGER,listing_count INTEGER,listing_ids TEXT NOT NULL,total_top_qty INTEGER NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_market_snapshots_item_ts ON market_snapshots(item_id,ts)")
init_db()

def market_url(i): return f"https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID={i}&sortField=price&sortOrder=ASC"
def torn_error(d):
    if not isinstance(d,dict) or not d.get("error"):return None
    e=d["error"]
    if isinstance(e,dict):
        code=e.get("code"); msg=e.get("error") or e.get("message") or str(e)
        return f"Torn API error {code}: {msg}" if code is not None else msg
    return str(e)

def _num(v):
    try:return float(v) if v is not None else None
    except (TypeError,ValueError):return None

def parse_itemmarket(d):
    m=d.get("itemmarket") if isinstance(d,dict) else None
    if not isinstance(m,dict):return [],None
    avg=None
    try:
        raw=(m.get("item") or {}).get("average_price"); avg=int(raw) if raw is not None else None
    except (TypeError,ValueError,AttributeError):pass
    out=[]
    for r in m.get("listings") or []:
        try:
            p=int(r["price"]); a=max(1,int(r.get("amount",1) or 1))
            if p>0:out.append({"id":r.get("id"),"price":p,"amount":a})
        except (KeyError,TypeError,ValueError):pass
    out.sort(key=lambda x:x["price"]); return out,avg

def market_cache_meta(d):
    candidates=[]
    if isinstance(d,dict):
        candidates.append(d)
        if isinstance(d.get("itemmarket"),dict):candidates.append(d["itemmarket"])
    cache_ts=cache_delay=None
    for obj in candidates:
        if cache_ts is None:
            try:
                raw=obj.get("cache_timestamp"); cache_ts=int(raw) if raw is not None else None
            except (TypeError,ValueError):pass
        if cache_delay is None:
            try:
                raw=obj.get("cache_delay"); cache_delay=int(raw) if raw is not None else None
            except (TypeError,ValueError):pass
    if cache_delay is None:cache_delay=30
    age=max(0,int(time.time()-cache_ts)) if cache_ts else None
    label="UNKNOWN" if age is None else "FRESH" if age<10 else "RECENT" if age<cache_delay else "CACHE DUE"
    return {"cache_timestamp":cache_ts,"cache_delay":cache_delay,"cache_age_seconds":age,"freshness":label}

async def _torn_get(client,path,params=None,error_text="data"):
    if not _api_key:raise HTTPException(401,"Load your Torn API key first")
    try:
        r=await client.get(f"{API_BASE}{path}",headers={"Authorization":f"ApiKey {_api_key}"},params=params or {}); d=r.json()
    except httpx.RequestError as e:raise HTTPException(502,f"Could not reach Torn API: {e}") from e
    except ValueError as e:raise HTTPException(502,f"Torn API returned unreadable {error_text}") from e
    err=torn_error(d)
    if err:raise HTTPException(400,err)
    if r.status_code>=400:raise HTTPException(r.status_code,f"Torn API returned HTTP {r.status_code}")
    return d

async def fetch_market(client,i,limit=100):return await _torn_get(client,f"/market/{i}/itemmarket",{"limit":limit,"offset":0})
async def fetch_inventory_category(client,cat):return await _torn_get(client,"/user/inventory",{"cat":cat,"limit":250,"offset":0},"inventory data")
async def fetch_item_details(client,uid):return await _torn_get(client,f"/torn/{uid}/itemdetails",error_text="item details")

async def get_base_item(client,item_id):
    item_id=int(item_id); now=time.time(); cached=_item_value_cache.get(item_id)
    if cached and now-cached["ts"]<ITEM_VALUE_CACHE_SECONDS:return cached["item"]
    d=await _torn_get(client,f"/torn/{item_id}/items",error_text="item values")
    items=d.get("items") if isinstance(d,dict) else None
    item=None
    if isinstance(items,list):
        for candidate in items:
            if not isinstance(candidate,dict):continue
            try:
                if int(candidate.get("id"))==item_id:item=candidate;break
            except (TypeError,ValueError):continue
        if item is None and len(items)==1 and isinstance(items[0],dict):item=items[0]
    elif isinstance(items,dict):
        item=items.get(str(item_id)) or items.get(item_id)
        if item is None and len(items)==1:item=next(iter(items.values()))
    if isinstance(item,dict):_item_value_cache[item_id]={"ts":now,"item":item}
    return item

def base_item_values(item):
    if not isinstance(item,dict):return {"sell_price":None,"market_price":None,"buy_price":None,"vendor":None}
    value=item.get("value") if isinstance(item.get("value"),dict) else {}
    def as_int(name):
        try:
            raw=value.get(name); return int(raw) if raw is not None else None
        except (TypeError,ValueError):return None
    return {"sell_price":as_int("sell_price"),"market_price":as_int("market_price"),"buy_price":as_int("buy_price"),"vendor":value.get("vendor")}

def inventory_entries(d):
    inv=d.get("inventory") if isinstance(d,dict) else None
    return inv.get("items") or [] if isinstance(inv,dict) else []

def hydrate_inventory_entry(entry,detail_response,category):
    if not isinstance(entry,dict) or not isinstance(detail_response,dict):return None
    details=detail_response.get("itemdetails")
    if not isinstance(details,dict):return None
    stats=details.get("stats") if isinstance(details.get("stats"),dict) else {}
    quality=_num(stats.get("quality")); damage=_num(stats.get("damage")); accuracy=_num(stats.get("accuracy")); armor=_num(stats.get("armor"))
    if quality is None:return None
    try:item_id=int(details.get("id") or entry.get("id"))
    except (TypeError,ValueError):return None
    bonuses=details.get("bonuses") or []; rarity=details.get("rarity"); uid=details.get("uid") or entry.get("uid")
    return {"item_id":item_id,"name":details.get("name") or entry.get("name") or f"Item {item_id}","category":category,"type":details.get("type") or category,"sub_type":details.get("sub_type"),"uid":uid,"quality":quality,"damage":damage,"accuracy":accuracy,"armor":armor,"rarity":rarity,"bonuses":bonuses,"equipped":bool(entry.get("equipped")),"faction_owned":bool(entry.get("faction_owned")),"plain":not bool(rarity or bonuses),"market_url":market_url(item_id)}

def floor_data(listings):
    if not listings:return None,None,None
    low=listings[0]["price"]; qty=sum(x["amount"] for x in listings if x["price"]==low); nxt=next((x["price"] for x in listings if x["price"]>low),None); return low,qty,nxt

def save_snapshot(i,listings,avg):
    if not listings:return
    low,qty,nxt=floor_data(listings); top=listings[:30]; ids=[str(x["id"]) for x in top if x.get("id") is not None]; total=sum(x["amount"] for x in top)
    with sqlite3.connect(DB_PATH) as c:c.execute("INSERT INTO market_snapshots(ts,item_id,lowest,qty_floor,next_higher,average_price,listing_count,listing_ids,total_top_qty) VALUES(?,?,?,?,?,?,?,?,?)",(time.time(),i,low,qty,nxt,avg,len(listings),json.dumps(ids),total))

def liquidity_stats(i):
    with sqlite3.connect(DB_PATH) as c:rows=c.execute("SELECT ts,lowest,qty_floor,next_higher,average_price,listing_count,listing_ids,total_top_qty FROM market_snapshots WHERE item_id=? ORDER BY ts DESC LIMIT 120",(i,)).fetchall()
    if not rows:return {"observations":0,"score":0,"label":"Learning","gap_events":0,"largest_gap_pct":0}
    rows=list(reversed(rows)); changes=floors=gaps=0; largest=0.; prev=None
    for r in rows:
        _,low,qty,nxt,_,_,ids,total=r
        if low and nxt and nxt>low:
            gp=(nxt-low)/low*100; largest=max(largest,gp); gaps+=gp>=1
        if prev:
            floors+=low!=prev[1] or qty!=prev[2]; changes+=low!=prev[1] or qty!=prev[2] or ids!=prev[6] or total!=prev[7]
        prev=r
    n=max(1,len(rows)-1); score=round(min(100,(changes/n)*65+(floors/n)*35)*100)/100
    label="Learning" if len(rows)<4 else "Very active" if score>=70 else "Active" if score>=45 else "Moderate" if score>=20 else "Slow"
    return {"observations":len(rows),"score":score,"label":label,"gap_events":gaps,"largest_gap_pct":round(largest,2),"last_seen":rows[-1][0]}

def analyze_main(i,listings,avg):
    meta=ITEMS[i]
    if not listings:return {"id":i,**meta,"market_url":market_url(i),"error":"No readable listings returned"}
    low,qty,nxt=floor_data(listings); ref=avg or nxt or low; disc=(ref-low)/ref*100 if ref else 0; profit=roi=None
    if nxt:
        each=int(nxt*(1-MARKET_FEE))-low; roi=each/low*100; profit=each*qty
    return {"id":i,**meta,"lowest":low,"qty_floor":qty,"next_higher":nxt,"reference":ref,"average_price":avg,"discount_pct":disc,"net_roi_after_fee":roi,"floor_clear_capital":low*qty,"floor_clear_profit_after_fee":profit,"market_url":market_url(i)}

def discovery_result(i,meta,listings,avg,cache=None):
    if not listings:return {"id":i,"name":meta["name"],"error":"No listings","market_url":market_url(i),**(cache or {})}
    low,qty,_=floor_data(listings); floor=meta.get("hard_floor"); ref=avg or low; disc=(ref-low)/ref*100 if ref else 0
    fp=(floor-low)*qty if floor and low<floor else 0; fd=(floor-low)/floor*100 if fp else 0; liq=liquidity_stats(i)
    score=1000+fd*10+min(300,fp/1000) if fp else max(0,disc*8+liq["score"]*.6); kind="NPC FLOOR" if fp else "UNDER MARKET" if disc>=8 else "WATCH"
    return {"id":i,"name":meta["name"],"lowest":low,"qty_floor":qty,"market_value":avg,"discount_pct":round(disc,2),"hard_floor":floor,"floor_profit":fp,"floor_discount_pct":round(fd,2),"kind":kind,"deal_score":round(score,2),"activity":liq["label"],"samples":liq["observations"],"market_url":market_url(i),**(cache or {})}

def equipment_rows(d):
    m=d.get("itemmarket") if isinstance(d,dict) else None
    if not isinstance(m,dict):return [],{}
    meta=m.get("item") or {}; out=[]
    for r in m.get("listings") or []:
        details=r.get("item_details")
        if not isinstance(details,dict) or details.get("rarity") or (details.get("bonuses") or []):continue
        stats=details.get("stats") or {}
        try:price=int(r.get("price") or 0); quality=float(stats.get("quality"))
        except (TypeError,ValueError):continue
        if price<=0:continue
        out.append({"price":price,"quality":quality,"damage":_num(stats.get("damage")),"accuracy":_num(stats.get("accuracy")),"armor":_num(stats.get("armor"))})
    return out,meta

def equipment_verdict(rows,quality,damage,accuracy,armor,vendor_sell):
    def distance(r):
        score=abs(r["quality"]-quality)
        if damage is not None and r["damage"] is not None:score+=2.0*abs(r["damage"]-damage)
        if accuracy is not None and r["accuracy"] is not None:score+=1.5*abs(r["accuracy"]-accuracy)
        if armor is not None and r["armor"] is not None:score+=2.0*abs(r["armor"]-armor)
        return score
    ranked=sorted(rows,key=distance); close5=[r for r in ranked if abs(r["quality"]-quality)<=5][:8]; comps=close5 if len(close5)>=3 else ranked[:8]; chosen=comps[:5]
    prices=sorted(r["price"] for r in chosen); median_ask=int(statistics.median(prices)) if prices else None; competitive_ask=prices[1] if len(prices)>=2 else (prices[0] if prices else None)
    net=int(competitive_ask*(1-MARKET_FEE)) if competitive_ask else None; premium=(net-vendor_sell) if net is not None else None
    percentile=round(sum(1 for r in rows if r["quality"]<quality)/len(rows)*100,1) if rows else None; close_count=len(close5); confidence="HIGH" if close_count>=5 else "MEDIUM" if close_count>=3 else "LOW"; threshold=max(1000,int(vendor_sell*.25))
    if not rows:verdict="DON'T VENDOR YET" if quality>=65 else "CHECK MANUALLY"; reason="No plain market comparables were returned."
    elif premium is not None and premium>=threshold:verdict="MARKET IT"; reason=f"A competitive comparable ask would net about ${premium:,} more than vendoring after the 5% fee."
    elif quality>=80:verdict="DON'T VENDOR YET"; reason="This is an unusually high-quality roll; the current comparable asks do not justify an instant vendor decision."
    elif quality>=65 and premium is not None and premium>0:verdict="MARKET IT"; reason="Above-average quality plus current comparable asks gives it a positive premium over vendoring."
    else:verdict="VENDOR"; reason="Current plain comparable asks do not show enough premium over the guaranteed vendor value."
    return {"verdict":verdict,"reason":reason,"quality_percentile":percentile,"confidence":confidence,"plain_listings":len(rows),"close_comparables":close_count,"median_ask":median_ask,"competitive_ask":competitive_ask,"net_after_fee":net,"premium_over_vendor":premium,"comps":chosen}

@app.get("/")
async def home():return FileResponse(WEB/"index.html")
@app.get("/api/status")
async def status():return {"ok":True,"version":"0.5.4","key_loaded":bool(_api_key),"key_source":"local .env / environment" if _api_key else None,"market_fee_pct":5,"items":[{"id":i,**m} for i,m in ITEMS.items()],"discovery_count":len(DISCOVERY_ITEMS),"discovery_ids":list(DISCOVERY_ITEMS)}
@app.post("/api/key")
async def set_key(p:KeyPayload):
    global _api_key
    k=p.api_key.strip()
    if not k:raise HTTPException(400,"API key is blank")
    _api_key=k;return {"ok":True,"message":"API key loaded into memory."}
@app.delete("/api/key")
async def forget_key():
    global _api_key,_last_scan
    _api_key=None;_last_scan=None;return {"ok":True}
@app.get("/api/scan")
async def scan(ids:str=Query(default="206,366,370")):
    global _last_scan
    req=[]
    try:
        for p in ids.split(","):
            i=int(p.strip())
            if i in ITEMS and i not in req:req.append(i)
    except ValueError as e:raise HTTPException(400,"Invalid item ID list") from e
    async with httpx.AsyncClient(timeout=15) as client:results=await asyncio.gather(*(fetch_market(client,i) for i in req),return_exceptions=True)
    out=[]
    for i,r in zip(req,results):
        if isinstance(r,Exception):out.append({"id":i,**ITEMS[i],"error":str(getattr(r,"detail",r))});continue
        l,a=parse_itemmarket(r);out.append(analyze_main(i,l,a))
    _last_scan={"ok":True,"scanned_at":time.time(),"items":out};return _last_scan
@app.post("/api/learn")
async def learn_markets():
    ids=list(LEARN_ITEMS)
    async with httpx.AsyncClient(timeout=20) as client:results=await asyncio.gather(*(fetch_market(client,i) for i in ids),return_exceptions=True)
    out=[]
    for i,r in zip(ids,results):
        if isinstance(r,Exception):out.append({"id":i,"name":LEARN_ITEMS[i],"error":str(getattr(r,"detail",r))});continue
        l,a=parse_itemmarket(r);save_snapshot(i,l,a);out.append({"id":i,"name":LEARN_ITEMS[i],"lowest":l[0]["price"] if l else None,"average_price":a,"market_url":market_url(i),**liquidity_stats(i)})
    out.sort(key=lambda x:(x.get("score",-1),x.get("gap_events",-1)),reverse=True);return {"ok":True,"learned_at":time.time(),"items":out}
@app.get("/api/liquidity")
async def get_liquidity():
    out=[]
    with sqlite3.connect(DB_PATH) as c:
        for i,n in LEARN_ITEMS.items():
            last=c.execute("SELECT lowest,average_price FROM market_snapshots WHERE item_id=? ORDER BY ts DESC LIMIT 1",(i,)).fetchone(); low=last[0] if last else None; avg=last[1] if last else None
            out.append({"id":i,"name":n,"lowest":low,"average_price":avg,"market_url":market_url(i),**liquidity_stats(i)})
    out.sort(key=lambda x:x.get("score",-1),reverse=True);return {"ok":True,"items":out}
@app.post("/api/discover")
async def discover_hidden_deals(ids:str=Query(default="")):
    if ids.strip():
        requested=[]
        try:
            for p in ids.split(","):
                i=int(p.strip())
                if i in DISCOVERY_ITEMS and i not in requested:requested.append(i)
        except ValueError as e:raise HTTPException(400,"Invalid discovery item ID list") from e
        if not requested:raise HTTPException(400,"No supported discovery items selected")
    else:requested=list(DISCOVERY_ITEMS)
    async with httpx.AsyncClient(timeout=25) as client:results=await asyncio.gather(*(fetch_market(client,i,60) for i in requested),return_exceptions=True)
    out=[]
    for i,r in zip(requested,results):
        meta=DISCOVERY_ITEMS[i]
        if isinstance(r,Exception):out.append({"id":i,"name":meta["name"],"error":str(getattr(r,"detail",r))});continue
        l,a=parse_itemmarket(r); cache=market_cache_meta(r); save_snapshot(i,l,a); out.append(discovery_result(i,meta,l,a,cache))
    out.sort(key=lambda x:x.get("deal_score",-1),reverse=True);return {"ok":True,"scanned_at":time.time(),"items":out,"batch_ids":requested,"pool_count":len(DISCOVERY_ITEMS)}

@app.get("/api/equipment/inventory")
async def equipment_inventory():
    async with httpx.AsyncClient(timeout=30) as client:
        category_results=await asyncio.gather(*(fetch_inventory_category(client,cat) for cat in INVENTORY_CATEGORIES),return_exceptions=True)
        entries=[]; category_errors=[]
        for cat,result in zip(INVENTORY_CATEGORIES,category_results):
            if isinstance(result,Exception):category_errors.append(f"{cat}: {str(getattr(result,'detail',result))}");continue
            for e in inventory_entries(result):
                if isinstance(e,dict) and e.get("uid") is not None:entries.append((cat,e))
        if not entries and category_errors:raise HTTPException(400,"; ".join(category_errors))
        truncated=len(entries)>MAX_INVENTORY_DETAILS; entries=entries[:MAX_INVENTORY_DETAILS]
        detail_results=await asyncio.gather(*(fetch_item_details(client,e.get("uid")) for _,e in entries),return_exceptions=True)
    items=[]; detail_errors=0
    for (cat,e),detail in zip(entries,detail_results):
        if isinstance(detail,Exception):detail_errors+=1;continue
        row=hydrate_inventory_entry(e,detail,cat)
        if row:items.append(row)
    items.sort(key=lambda x:(not x["plain"],x["name"].lower(),-(x["quality"] or 0)))
    return {"ok":True,"items":items,"count":len(items),"inventory_candidates":len(entries),"truncated":truncated,"detail_errors":detail_errors,"category_errors":category_errors,"categories":list(INVENTORY_CATEGORIES)}

@app.get("/api/equipment/values")
async def equipment_values(item_id:int):
    if item_id<=0:raise HTTPException(400,"Enter a valid item ID")
    async with httpx.AsyncClient(timeout=20) as client:item=await get_base_item(client,item_id)
    if not item:raise HTTPException(404,f"Torn item {item_id} was not found")
    vals=base_item_values(item)
    return {"ok":True,"item_id":item_id,"name":item.get("name") or f"Item {item_id}",**vals}

@app.get("/api/equipment/check")
async def check_equipment(item_id:int,quality:float,damage:float|None=None,accuracy:float|None=None,armor:float|None=None,vendor_sell:int|None=None):
    if item_id<=0:raise HTTPException(400,"Enter a valid item ID")
    if quality<0 or quality>100:raise HTTPException(400,"Quality must be between 0 and 100")
    if vendor_sell is not None and vendor_sell<0:raise HTTPException(400,"Vendor value cannot be negative")
    async with httpx.AsyncClient(timeout=20) as client:market_data,base_item=await asyncio.gather(fetch_market(client,item_id,100),get_base_item(client,item_id))
    rows,meta=equipment_rows(market_data)
    if not rows:raise HTTPException(400,"No plain weapon/armor listings with stats were returned for this item")
    vals=base_item_values(base_item); resolved_vendor=vendor_sell if vendor_sell is not None and vendor_sell>0 else (vals.get("sell_price") or 0)
    result=equipment_verdict(rows,quality,damage,accuracy,armor,resolved_vendor)
    return {"ok":True,"item_id":item_id,"name":meta.get("name") or (base_item or {}).get("name") or f"Item {item_id}","type":meta.get("type"),"market_average":meta.get("average_price"),"torn_market_price":vals.get("market_price"),"vendor_sell":resolved_vendor,"vendor_sell_source":"manual" if vendor_sell is not None and vendor_sell>0 else "torn","buy_price":vals.get("buy_price"),"vendor":vals.get("vendor"),"your_stats":{"quality":quality,"damage":damage,"accuracy":accuracy,"armor":armor},"market_url":market_url(item_id),"cache":market_cache_meta(market_data),**result}
@app.get("/api/last-scan")
async def last_scan():return _last_scan or {"ok":True,"scanned_at":None,"items":[]}
