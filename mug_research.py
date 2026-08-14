import sqlite3
import time
from pathlib import Path

from fastapi import HTTPException, Query
from pydantic import BaseModel

import mug_scout_v036
from mug_scout_v036 import app

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "torntools.sqlite3"
MUG_RESEARCH_VERSION = "0.4.1"


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


_original_search = mug_scout_v036.mug_scout_search_v3


@app.get("/api/mug-scout/search-v4")
async def mug_scout_search_v4(
    minff: float = 1.8,
    maxff: float = 3.0,
    minlevel: int = 15,
    maxlevel: int = 100,
    limit: int = 20,
    factionless: int = 0,
    mininactive_days: int = 14,
):
    result = await _original_search(
        minff=minff,
        maxff=maxff,
        minlevel=minlevel,
        maxlevel=maxlevel,
        limit=limit,
        factionless=factionless,
        mininactive_days=mininactive_days,
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
    result["version"] = MUG_RESEARCH_VERSION
    result.setdefault("notes", []).append("Mug counters come from results you manually record in TornTools.")
    return result


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
