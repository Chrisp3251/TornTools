import math
import sqlite3
import time
from pathlib import Path

from fastapi import HTTPException, Query
from pydantic import BaseModel

import mug_scout_v036
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
