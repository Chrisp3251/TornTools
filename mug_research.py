import asyncio
import math
import sqlite3
import time

import httpx
from pathlib import Path

from fastapi import HTTPException, Query
from pydantic import BaseModel

import mug_scout_v036
import app as app_module
import mug_scout
import evidence_hardening
from mug_scout_v036 import app

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "torntools.sqlite3"
MUG_RESEARCH_VERSION = "0.4.5"


class MugResultPayload(BaseModel):
    player_id: int
    player_name: str | None = None
    amount: int
    fair_fight: float | None = None
    bs_estimate: int | float | None = None
    inactive_days: float | None = None
    property_name: str | None = None
    property_ownership: str | None = None
    property_market_price: int | None = None
    target_score: float | None = None
    status: str | None = None


def init_mug_research_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS mug_results(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                player_id INTEGER NOT NULL,
                player_name TEXT,
                amount INTEGER NOT NULL,
                fair_fight REAL,
                bs_estimate REAL,
                inactive_days REAL,
                property_name TEXT,
                property_ownership TEXT,
                property_market_price INTEGER,
                target_score REAL,
                status TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mug_results_player_ts ON mug_results(player_id, ts)")


init_mug_research_db()


def mug_history_map(player_ids):
    ids = sorted({int(x) for x in player_ids if x is not None})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            f"""SELECT player_id, COUNT(*), COALESCE(SUM(amount),0), MAX(ts), MAX(amount)
                FROM mug_results WHERE player_id IN ({placeholders}) GROUP BY player_id""",
            ids,
        ).fetchall()
    return {
        int(pid): {
            "mug_count": int(count),
            "total_mugged": int(total),
            "last_mug_ts": last_ts,
            "best_mug": int(best or 0),
        }
        for pid, count, total, last_ts, best in rows
    }


def _latest_reference(player_id: int):
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            """SELECT ts,player_id,player_name,amount,fair_fight,bs_estimate,inactive_days,
                      property_name,property_ownership,property_market_price,target_score,status
               FROM mug_results WHERE player_id=? ORDER BY ts DESC LIMIT 1""",
            (int(player_id),),
        ).fetchone()
    if not row:
        return None
    return {
        "ts": row[0], "player_id": int(row[1]), "player_name": row[2] or f"Player {row[1]}",
        "amount": int(row[3] or 0), "fair_fight": row[4], "bs_estimate": row[5], "inactive_days": row[6],
        "property_name": row[7], "property_ownership": row[8], "property_market_price": row[9],
        "target_score": row[10], "status": row[11],
    }


def _safe_positive(v):
    try:
        n=float(v)
        return n if n>0 else None
    except (TypeError,ValueError):
        return None


def _similarity(item, ref):
    parts=[]
    ff=item.get("fair_fight")
    if ff is not None and ref.get("fair_fight") is not None:
        parts.append((0.24, max(0.0, 1.0-abs(float(ff)-float(ref["fair_fight"]))/0.75)))

    age=item.get("last_action_age_seconds")
    if age is not None and ref.get("inactive_days") is not None:
        days=float(age)/86400.0
        parts.append((0.28, max(0.0, 1.0-abs(days-float(ref["inactive_days"]))/18.0)))

    # FFScouter's human BS label is often a coarse bucket. Prefer its raw numeric
    # estimate when present so two players in the same displayed bucket can still differ.
    cb=_safe_positive(item.get("bs_estimate")); rb=_safe_positive(ref.get("bs_estimate"))
    if cb and rb:
        parts.append((0.22, max(0.0, 1.0-abs(math.log10(cb)-math.log10(rb))/0.9)))

    prop=item.get("property") or {}
    cp=_safe_positive(prop.get("market_price")); rp=_safe_positive(ref.get("property_market_price"))
    if cp and rp:
        parts.append((0.15, max(0.0, 1.0-abs(math.log10(cp)-math.log10(rp))/1.0)))

    own=str(prop.get("ownership") or "").lower(); rown=str(ref.get("property_ownership") or "").lower()
    if own and rown and own!="unknown" and rown!="unknown":
        parts.append((0.05, 1.0 if own==rown else 0.2))

    score=(item.get("scores") or {}).get("mug")
    if score is not None and ref.get("target_score") is not None:
        parts.append((0.06, max(0.0, 1.0-abs(float(score)-float(ref["target_score"]))/30.0)))

    if not parts:
        return 0.0
    total=sum(w for w,_ in parts)
    return round(sum(w*v for w,v in parts)/total*100.0,1)


_original_search = mug_scout_v036.mug_scout_search_v3


@app.get("/api/mug-scout/search-v4")
async def mug_scout_search_v4(
    minff: float = 1.8,
    maxff: float = 3.0,
    minlevel: int = 15,
    maxlevel: int = 100,
    limit: int = 20,
    factionless: int = 0,
    mininactive_days: int = 15,
    maxinactive_days: int = 100,
    reference_player_id: int | None = None,
):
    ref = _latest_reference(reference_player_id) if reference_player_id else None
    if reference_player_id and not ref:
        raise HTTPException(404, f"No recorded Mug Result found for player #{reference_player_id}. Record that mug first so TornTools has the original signals.")

    search_minff=minff; search_maxff=maxff; search_mininactive=mininactive_days; search_maxinactive=maxinactive_days
    if ref:
        if ref.get("fair_fight") is not None:
            center=float(ref["fair_fight"])
            search_minff=max(1.0, center-0.65)
            search_maxff=min(10.0, center+0.65)
        if ref.get("inactive_days") is not None:
            center=float(ref["inactive_days"])
            search_mininactive=max(15, int(math.floor(center-18)))
            search_maxinactive=min(100, int(math.ceil(center+18)))

    result = await _original_search(
        minff=search_minff,
        maxff=search_maxff,
        minlevel=minlevel,
        maxlevel=maxlevel,
        limit=50 if ref else limit,
        factionless=factionless,
        mininactive_days=search_mininactive,
        maxinactive_days=search_maxinactive,
    )
    items = result.get("items") or []
    history = mug_history_map([x.get("player_id") for x in items])
    for item in items:
        h = history.get(int(item.get("player_id")), {})
        item["mug_history"] = {
            "mug_count": h.get("mug_count", 0),
            "total_mugged": h.get("total_mugged", 0),
            "last_mug_ts": h.get("last_mug_ts"),
            "best_mug": h.get("best_mug", 0),
        }
        if ref:
            item["similarity_score"]=_similarity(item,ref)

    if ref:
        items=[x for x in items if int(x.get("player_id") or 0)!=int(ref["player_id"])]
        items.sort(key=lambda x:(float(x.get("similarity_score") or 0), float((x.get("scores") or {}).get("mug") or 0)), reverse=True)
        items=items[:int(limit)]
        result["items"]=items
        result["reference_target"]={
            **ref,
            "search_window":{
                "minff":round(search_minff,2),"maxff":round(search_maxff,2),
                "mininactive_days":search_mininactive,"maxinactive_days":search_maxinactive,
            },
        }
        result.setdefault("notes", []).insert(0,
            f"Similarity mode: matching {ref['player_name']} #{ref['player_id']} using FF, inactivity, raw FFScouter BS estimate, property value/ownership, and target score across the full eligible candidate pool.")

    result["version"] = MUG_RESEARCH_VERSION
    result.setdefault("notes", []).append("Mug counters come from results you manually record in TornTools.")
    return result


@app.get("/api/mug-research/reference/{player_id}")
async def mug_reference(player_id:int):
    ref=_latest_reference(player_id)
    if not ref:
        raise HTTPException(404, f"No recorded Mug Result found for player #{player_id}")
    return {"ok":True,"reference":ref}


@app.post("/api/mug-research/result")
async def record_mug_result(payload: MugResultPayload):
    if payload.player_id <= 0:
        raise HTTPException(400, "Invalid player ID")
    if payload.amount < 0:
        raise HTTPException(400, "Mug amount cannot be negative")
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            """INSERT INTO mug_results(ts,player_id,player_name,amount,fair_fight,bs_estimate,inactive_days,property_name,property_ownership,property_market_price,target_score,status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                time.time(), payload.player_id, payload.player_name, payload.amount,
                payload.fair_fight, payload.bs_estimate, payload.inactive_days,
                payload.property_name, payload.property_ownership, payload.property_market_price,
                payload.target_score, payload.status,
            ),
        )
        result_id = cur.lastrowid
    history = mug_history_map([payload.player_id]).get(payload.player_id, {})
    return {"ok": True, "id": result_id, "history": history}


@app.get("/api/mug-research/results")
async def mug_research_results(limit: int = Query(250, ge=1, le=2000)):
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            """SELECT id,ts,player_id,player_name,amount,fair_fight,bs_estimate,inactive_days,
                      property_name,property_ownership,property_market_price,target_score,status
               FROM mug_results ORDER BY ts DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        summary = c.execute(
            """SELECT COUNT(*), COALESCE(SUM(amount),0), COALESCE(AVG(amount),0), COALESCE(MAX(amount),0), COUNT(DISTINCT player_id)
               FROM mug_results"""
        ).fetchone()
        targets = c.execute(
            """SELECT player_id, COALESCE(MAX(player_name),''), COUNT(*), COALESCE(SUM(amount),0),
                      COALESCE(AVG(amount),0), COALESCE(MAX(amount),0), MAX(ts)
               FROM mug_results GROUP BY player_id ORDER BY SUM(amount) DESC, COUNT(*) DESC"""
        ).fetchall()
    return {
        "ok": True,
        "version": MUG_RESEARCH_VERSION,
        "summary": {
            "mugs": int(summary[0] or 0),
            "total_mugged": int(summary[1] or 0),
            "average_mug": round(float(summary[2] or 0), 2),
            "best_mug": int(summary[3] or 0),
            "unique_targets": int(summary[4] or 0),
        },
        "targets": [
            {
                "player_id": int(r[0]), "player_name": r[1] or f"Player {r[0]}", "mug_count": int(r[2]),
                "total_mugged": int(r[3]), "average_mug": round(float(r[4]), 2), "best_mug": int(r[5]), "last_mug_ts": r[6],
            }
            for r in targets
        ],
        "items": [
            {
                "id": r[0], "ts": r[1], "player_id": r[2], "player_name": r[3] or f"Player {r[2]}",
                "amount": r[4], "fair_fight": r[5], "bs_estimate": r[6], "inactive_days": r[7],
                "property_name": r[8], "property_ownership": r[9], "property_market_price": r[10],
                "target_score": r[11], "status": r[12],
            }
            for r in rows
        ],
    }


@app.get("/api/mug-research/player/{player_id}")
async def player_mug_history(player_id: int):
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            """SELECT id,ts,amount,fair_fight,inactive_days,property_name,property_ownership,target_score,status
               FROM mug_results WHERE player_id=? ORDER BY ts DESC LIMIT 100""",
            (player_id,),
        ).fetchall()
    return {
        "ok": True,
        "player_id": player_id,
        "summary": mug_history_map([player_id]).get(player_id, {"mug_count":0,"total_mugged":0,"last_mug_ts":None,"best_mug":0}),
        "items": [
            {"id":r[0],"ts":r[1],"amount":r[2],"fair_fight":r[3],"inactive_days":r[4],"property_name":r[5],"property_ownership":r[6],"target_score":r[7],"status":r[8]}
            for r in rows
        ],
    }


# --- Mug Scout v5: wealth/inactivity + company hunting ----------------------
MUG_SCOUT_V5_VERSION = "0.5.0"
_oil_type_cache = {"at": 0.0, "id": None}
_medal_cache = {"at": 0.0, "billionaire_id": None}
_own_bs_cache = {"at": 0.0, "total": None}
_oil_company_cursor = 0


def _extract_last_action(value):
    if isinstance(value, dict):
        for key in ("timestamp", "time", "last_action"):
            if value.get(key) is not None:
                try:
                    return int(value[key])
                except Exception:
                    pass
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _player_from_search_row(row):
    if not isinstance(row, dict):
        return None
    root = row.get("user") if isinstance(row.get("user"), dict) else row
    try:
        pid = int(root.get("id") or row.get("id"))
    except Exception:
        return None
    la = _extract_last_action(root.get("last_action") or row.get("last_action"))
    return {
        "player_id": pid,
        "name": root.get("name") or row.get("name") or f"Player {pid}",
        "level": root.get("level") or row.get("level"),
        "last_action": la,
    }


async def _own_bs_total_v5(client):
    now=time.time()
    if _own_bs_cache["total"] and now-_own_bs_cache["at"]<300:
        return int(_own_bs_cache["total"])
    data=await app_module._torn_get(client,"/user/battlestats",error_text="battle stats")
    root=data.get("battlestats") if isinstance(data,dict) and isinstance(data.get("battlestats"),dict) else data
    total=0
    for k in ("strength","defense","speed","dexterity"):
        raw=root.get(k) if isinstance(root,dict) else 0
        if isinstance(raw,dict):
            raw=raw.get("value") or raw.get("amount") or 0
        try: total+=max(0,int(float(raw or 0)))
        except Exception: pass
    if total>0:_own_bs_cache.update({"at":now,"total":total})
    return total


async def _ff_map_v5(client, ids):
    key,_=mug_scout._ffscouter_key()
    if not key:return {}
    out={}
    unique=list(dict.fromkeys(int(x) for x in ids if int(x)>0))
    for start in range(0,len(unique),50):
        batch=unique[start:start+50]
        try:
            r=await client.get(f"{mug_scout.FFSCOUTER_BASE}/get-stats",params={"key":key,"targets":",".join(map(str,batch))},timeout=12.0)
            data=r.json()
            if isinstance(data,list):
                for row in data:
                    if not isinstance(row,dict):continue
                    try: out[int(row.get("player_id"))]=row
                    except Exception: pass
        except Exception:
            pass
    return out


async def _billionaire_medal_id(client):
    now=time.time()
    if _medal_cache["billionaire_id"] and now-_medal_cache["at"]<86400:
        return int(_medal_cache["billionaire_id"])
    data=await app_module._torn_get(client,"/torn/medals",error_text="medal catalog")
    rows=data.get("medals") if isinstance(data,dict) else None
    found=None
    if isinstance(rows,list):
        for row in rows:
            if str(row.get("name") or "").strip().lower()=="billionaire":
                try: found=int(row.get("id"))
                except Exception: pass
                break
    elif isinstance(rows,dict):
        for key,row in rows.items():
            if isinstance(row,dict) and str(row.get("name") or "").strip().lower()=="billionaire":
                try: found=int(row.get("id") or key)
                except Exception: pass
                break
    if found:_medal_cache.update({"at":now,"billionaire_id":found})
    return found


async def _has_medal(client, player_id, medal_id):
    try:
        data=await app_module._torn_get(client,f"/user/{int(player_id)}/medals",error_text="player medals")
    except Exception:
        return False
    rows=data.get("medals") if isinstance(data,dict) else None
    ids=set()
    if isinstance(rows,list):
        for row in rows:
            if isinstance(row,dict):
                try: ids.add(int(row.get("id")))
                except Exception: pass
            else:
                try: ids.add(int(row))
                except Exception: pass
    elif isinstance(rows,dict):
        for key,row in rows.items():
            try: ids.add(int(row.get("id") if isinstance(row,dict) else key))
            except Exception: pass
    return int(medal_id) in ids


async def _inactive_search_candidates(client, min_days, max_days, max_pages=8):
    now=int(time.time()); out=[]; seen=set(); raw_seen=0
    # Torn's last-action filters are buckets. For a 15-30 day target window,
    # lastActionWeekAgo is the correct broad source bucket; MonthAgo starts too late.
    if max_days <= 7:
        source_filter="lastActionDayAgo"
    elif max_days <= 30:
        source_filter="lastActionWeekAgo"
    elif max_days <= 365:
        source_filter="lastActionMonthAgo"
    else:
        source_filter="lastActionYearAgo"
    for page in range(max_pages):
        try:
            data=await app_module._torn_get(client,"/user/search",{"filters":source_filter,"offset":page*25},error_text="user search")
        except Exception:
            break
        rows=data.get("search") if isinstance(data,dict) else None
        if not isinstance(rows,list) or not rows:break
        raw_seen += len(rows)
        for row in rows:
            p=_player_from_search_row(row)
            if not p or p["player_id"] in seen or p["last_action"] is None:continue
            age=(now-int(p["last_action"]))/86400.0
            if min_days<=age<=max_days:
                p["inactive_days"]=round(age,1);out.append(p);seen.add(p["player_id"])
        meta=data.get("_metadata") if isinstance(data,dict) else {}
        links=meta.get("links") if isinstance(meta,dict) else {}
        if not isinstance(links,dict) or not links.get("next"):break
    return out, {"source_filter":source_filter,"raw_seen":raw_seen}


async def _oil_type_id(client):
    now=time.time()
    if _oil_type_cache["id"] and now-_oil_type_cache["at"]<86400:
        return int(_oil_type_cache["id"])
    data=await app_module._torn_get(client,"/torn/companies",error_text="company types")
    rows=data.get("companies") if isinstance(data,dict) else None
    found=None
    iterable=rows.values() if isinstance(rows,dict) else (rows or [])
    for row in iterable:
        if not isinstance(row,dict):continue
        if "oil rig" in str(row.get("name") or row.get("type") or "").lower():
            try: found=int(row.get("id") or row.get("type_id"))
            except Exception: pass
            if found:break
    if found:_oil_type_cache.update({"at":now,"id":found})
    return found


def _employee_rows(data):
    rows=data.get("employees") if isinstance(data,dict) else None
    if isinstance(rows,dict):
        tmp=[]
        for key,row in rows.items():
            if isinstance(row,dict):
                x=dict(row);x.setdefault("id",key);tmp.append(x)
        rows=tmp
    return [x for x in (rows or []) if isinstance(x,dict)]


def _employee_player(row):
    root=row.get("user") if isinstance(row.get("user"),dict) else row
    try: pid=int(root.get("id") or row.get("id"))
    except Exception:return None
    la=_extract_last_action(root.get("last_action") or row.get("last_action"))
    return {"player_id":pid,"name":root.get("name") or row.get("name") or f"Player {pid}","level":root.get("level") or row.get("level"),"last_action":la}


async def _oil_candidates(client, min_days, max_days, companies_per_scan=8):
    global _oil_company_cursor
    type_id=await _oil_type_id(client)
    if not type_id:return [],0,0
    data=await app_module._torn_get(client,f"/company/{type_id}/companies",{"limit":100,"offset":0},error_text="oil rig companies")
    rows=data.get("companies") if isinstance(data,dict) else None
    if isinstance(rows,dict): rows=list(rows.values())
    companies=[x for x in (rows or []) if isinstance(x,dict)]
    companies.sort(key=lambda x:(int(x.get("rating") or x.get("stars") or 0),int(x.get("id") or 0)),reverse=True)
    if not companies:return [],0,0
    start=_oil_company_cursor%len(companies)
    selected=[companies[(start+i)%len(companies)] for i in range(min(companies_per_scan,len(companies)))]
    _oil_company_cursor=(start+len(selected))%len(companies)
    now=int(time.time());out=[]
    sem=asyncio.Semaphore(4)
    async def one(co):
        try:
            cid=int(co.get("id"))
            d=await app_module._torn_get(client,f"/company/{cid}/employees",error_text="oil rig employees")
        except Exception:return
        for erow in _employee_rows(d):
            p=_employee_player(erow)
            if not p or p["last_action"] is None:continue
            age=(now-int(p["last_action"]))/86400.0
            if min_days<=age<=max_days:
                p.update({"inactive_days":round(age,2),"company_id":cid,"company_name":co.get("name") or f"Oil Rig {cid}","company_rating":co.get("rating") or co.get("stars")})
                out.append(p)
    await asyncio.gather(*(one(co) for co in selected))
    return out,len(companies),len(selected)


def _build_v5_row(p, ff, own_bs, source, billionaire=False):
    est=ff.get("bs_estimate") if isinstance(ff,dict) else None
    try: est=float(est) if est is not None else None
    except Exception: est=None
    ratio=(est/own_bs) if est is not None and own_bs else None
    # Wealth/activity opportunity score; FF is intentionally not a factor.
    score=0
    reasons=[]
    days=float(p.get("inactive_days") or 0)
    if source=="oil":
        if 1<=days<=3:score+=45;reasons.append("Oil Rig 1-3d inactive")
        if p.get("company_rating"):
            try: score+=min(15,float(p["company_rating"])*1.5)
            except Exception: pass
    else:
        if 15<=days<=30:score+=40;reasons.append("15-30d inactive")
        if billionaire:score+=40;reasons.append("Billionaire medal")
    if ratio is not None:
        if ratio<=0.5:score+=15;reasons.append("well below your BS")
        elif ratio<=0.8:score+=10;reasons.append("below your BS")
        elif ratio<=1.0:score+=5;reasons.append("within your BS")
    score=min(100,round(score,1))
    return {
        **p,
        "source_mode":source,
        "billionaire_medal":bool(billionaire),
        "bs_estimate":int(est) if est is not None else None,
        "bs_estimate_human":ff.get("bs_estimate_human") if isinstance(ff,dict) else None,
        "bs_ratio_pct":round(ratio*100,1) if ratio is not None else None,
        "fair_fight":ff.get("fair_fight") if isinstance(ff,dict) else None,
        "ff_source":ff.get("source") if isinstance(ff,dict) else None,
        "scores":{"mug":score},
        "signals":reasons,
        "profile_url":mug_scout._profile_url(p["player_id"]),
        "attack_url":mug_scout._attack_url(p["player_id"]),
    }


@app.get("/api/mug-scout/search-v5")
async def mug_scout_search_v5(
    mode: str = Query("billionaire"),
    limit: int = Query(25, ge=1, le=50),
    mininactive_days: float = Query(15, ge=0, le=3650),
    maxinactive_days: float = Query(30, ge=0, le=3650),
):
    if mininactive_days>maxinactive_days:
        raise HTTPException(400,"Minimum inactive days cannot exceed maximum inactive days")
    if mode not in {"billionaire","oil"}:
        raise HTTPException(400,"Mode must be billionaire or oil")
    if not app_module._api_key:
        raise HTTPException(401,"Load your Torn API key first")

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            own_bs=await _own_bs_total_v5(client)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"Mug Scout stage 'own battle stats' failed: {type(exc).__name__}: {exc}") from exc
        if not own_bs:
            raise HTTPException(502,"Could not determine your battle stats")

        if mode=="oil":
            try:
                candidates,total_companies,companies_scanned=await _oil_candidates(client,mininactive_days,maxinactive_days)
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(502, f"Mug Scout stage 'Oil Rig discovery' failed: {type(exc).__name__}: {exc}") from exc
            inactive_diag={"source_filter":"oil-company-employees","raw_seen":None}
            medal_id=None
        else:
            try:
                candidates,inactive_diag=await _inactive_search_candidates(client,mininactive_days,maxinactive_days)
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(502, f"Mug Scout stage 'inactive user search' failed: {type(exc).__name__}: {exc}") from exc
            total_companies=companies_scanned=0
            try:
                medal_id=await _billionaire_medal_id(client)
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(502, f"Mug Scout stage 'Billionaire medal catalog' failed: {type(exc).__name__}: {exc}") from exc

        try:
            ffmap=await _ff_map_v5(client,[x["player_id"] for x in candidates])
        except Exception as exc:
            raise HTTPException(502, f"Mug Scout stage 'FFScouter enrichment' failed: {type(exc).__name__}: {exc}") from exc

        # Safety gate: known FFScouter estimate must be <= your total BS.
        safe=[]
        for p in candidates:
            ff=ffmap.get(int(p["player_id"])) or {}
            est=ff.get("bs_estimate")
            try: est=float(est) if est is not None else None
            except Exception: est=None
            if est is None or est>own_bs:
                continue
            safe.append((p,ff))

        rows=[]
        medal_checked=0
        if mode=="billionaire":
            if medal_id:
                sem=asyncio.Semaphore(5)
                async def check(pair):
                    nonlocal medal_checked
                    p,ff=pair
                    async with sem:
                        ok=await _has_medal(client,p["player_id"],medal_id)
                    medal_checked += 1
                    if ok: rows.append(_build_v5_row(p,ff,own_bs,"billionaire",True))
                try:
                    await asyncio.gather(*(check(pair) for pair in safe[:40]))
                except Exception as exc:
                    raise HTTPException(502, f"Mug Scout stage 'Billionaire medal verification' failed: {type(exc).__name__}: {exc}") from exc
        else:
            rows=[_build_v5_row(p,ff,own_bs,"oil",False) for p,ff in safe]

    rows.sort(key=lambda x:(float((x.get("scores") or {}).get("mug") or 0),-float(x.get("inactive_days") or 0)),reverse=True)
    rows=rows[:int(limit)]
    history=mug_history_map([x["player_id"] for x in rows])
    for item in rows:
        h=history.get(int(item["player_id"]),{})
        item["mug_history"]={"mug_count":h.get("mug_count",0),"total_mugged":h.get("total_mugged",0),"last_mug_ts":h.get("last_mug_ts"),"best_mug":h.get("best_mug",0)}

    return {
        "ok":True,
        "version":MUG_SCOUT_V5_VERSION,
        "mode":mode,
        "own_total_battlestats":own_bs,
        "source_candidates":len(candidates),
        "safe_candidates":len(safe),
        "diagnostics":{
            "source_filter":inactive_diag.get("source_filter"),
            "raw_source_rows":inactive_diag.get("raw_seen"),
            "inactive_window_matches":len(candidates),
            "ffscouter_known":sum(1 for p in candidates if int(p["player_id"]) in ffmap),
            "bs_safe":len(safe),
            "billionaire_medal_id":medal_id,
            "medals_checked":medal_checked,
            "verified_billionaires":len(rows) if mode=="billionaire" else None,
        },
        "items":rows,
        "oil_companies_total":total_companies,
        "oil_companies_scanned":companies_scanned,
        "notes":[
            "FF/fair-fight is not part of Mug Score; FFScouter is used only to reject targets estimated above your total battle stats.",
            "Billionaire mode requires Torn's Billionaire medal and the requested inactivity window.",
            "Diagnostics report each filtering stage so an empty result can be traced instead of silently returning nothing.",
            "Oil Rig mode rotates through public Oil Rig companies and looks for employees in the requested inactivity window.",
        ],
    }
