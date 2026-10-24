from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from typing import Any

import httpx
from fastapi import HTTPException, Query

import app as core
import bounty_status_runtime as runtime
import travel_intelligence as ti
import mug_scout

app = runtime.app

DB_PATH = core.DB_PATH
DISCOVERY_PAGE_SIZE = 25
DISCOVERY_BASIC_BUDGET = 16
TRACKED_REFRESH_BUDGET = 14
TRACKED_TTL_SECONDS = 24 * 3600
LANDED_TTL_SECONDS = 3 * 3600
FF_BATCH_SIZE = 50
_own_bs_cache: dict[str, Any] = {"at": 0.0, "total": None}
_OWN_BS_TTL = 300

_COUNTRY_ALIASES = {
    "can": ["canada"],
    "mex": ["mexico"],
    "cay": ["cayman", "cayman islands"],
    "haw": ["hawaii"],
    "uk": ["united kingdom", "uk"],
    "arg": ["argentina"],
    "swi": ["switzerland"],
    "jap": ["japan"],
    "chi": ["china"],
    "uae": ["uae", "united arab emirates"],
    "saf": ["south africa"],
}


def _init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS foreign_monitor_players(
                player_id INTEGER PRIMARY KEY,
                name TEXT,
                level INTEGER,
                destination_code TEXT,
                state TEXT,
                description TEXT,
                first_seen REAL,
                last_seen REAL,
                first_landed REAL,
                last_checked REAL,
                source TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS foreign_monitor_meta(
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)


_init_db()


def _meta_get(key: str, default: str = "") -> str:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT value FROM foreign_monitor_meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def _meta_set(key: str, value: Any):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT OR REPLACE INTO foreign_monitor_meta(key,value) VALUES(?,?)", (key, str(value)))


def _country_from_text(text: Any) -> str | None:
    value = str(text or "").lower()
    for code, aliases in _COUNTRY_ALIASES.items():
        if any(alias in value for alias in aliases):
            return code
    return None


def _basic_parts(data: dict[str, Any]) -> dict[str, Any]:
    root = data.get("profile") if isinstance(data.get("profile"), dict) else data
    status = root.get("status") if isinstance(root, dict) and isinstance(root.get("status"), dict) else {}
    last_action = root.get("last_action") if isinstance(root, dict) and isinstance(root.get("last_action"), dict) else {}
    state = str(status.get("state") or "Unknown")
    desc = str(status.get("description") or state)
    try:
        level = int(root.get("level")) if root.get("level") is not None else None
    except Exception:
        level = None
    return {
        "name": root.get("name") if isinstance(root, dict) else None,
        "level": level,
        "state": state,
        "description": desc,
        "country": _country_from_text(desc),
        "last_action": last_action,
    }


def _is_landed_in_destination(parts: dict[str, Any], destination_code: str) -> bool:
    state = str(parts.get("state") or "").lower()
    desc = str(parts.get("description") or "").lower()
    country = parts.get("country")
    return country == destination_code and ("abroad" in state or "in " in desc)


def _is_heading_to_destination(parts: dict[str, Any], destination_code: str) -> bool:
    state = str(parts.get("state") or "").lower()
    desc = str(parts.get("description") or "").lower()
    return parts.get("country") == destination_code and ("travel" in state or "travel" in desc or "flying" in desc)


def _upsert(player_id: int, parts: dict[str, Any], destination_code: str, source: str):
    now = time.time()
    landed = _is_landed_in_destination(parts, destination_code)
    with sqlite3.connect(DB_PATH) as c:
        old = c.execute("SELECT first_seen,first_landed FROM foreign_monitor_players WHERE player_id=?", (player_id,)).fetchone()
        first_seen = float(old[0]) if old and old[0] else now
        first_landed = float(old[1]) if old and old[1] else (now if landed else None)
        c.execute("""
            INSERT OR REPLACE INTO foreign_monitor_players
            (player_id,name,level,destination_code,state,description,first_seen,last_seen,first_landed,last_checked,source)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (
            int(player_id), parts.get("name"), parts.get("level"), destination_code,
            parts.get("state"), parts.get("description"), first_seen, now,
            first_landed, now, source,
        ))


def _tracked(destination_code: str, limit: int = 100) -> list[dict[str, Any]]:
    cutoff = time.time() - TRACKED_TTL_SECONDS
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("""
            SELECT player_id,name,level,destination_code,state,description,first_seen,last_seen,first_landed,last_checked,source
            FROM foreign_monitor_players
            WHERE destination_code=? AND last_seen>=?
            ORDER BY CASE WHEN first_landed IS NULL THEN 0 ELSE 1 END, last_checked ASC
            LIMIT ?
        """, (destination_code, cutoff, int(limit))).fetchall()
    keys = ["player_id","name","level","destination_code","state","description","first_seen","last_seen","first_landed","last_checked","source"]
    return [dict(zip(keys, row)) for row in rows]


async def _search_travelers(client: httpx.AsyncClient, offset: int) -> tuple[list[dict[str, Any]], int | None]:
    data = await core._torn_get(
        client,
        "/user/search",
        {"filters": "traveling,lastActionRecent", "offset": int(offset)},
        error_text="traveler search",
    )
    rows = data.get("search") if isinstance(data, dict) else []
    meta = data.get("_metadata") if isinstance(data, dict) else {}
    total = meta.get("total") if isinstance(meta, dict) else None
    return [x for x in (rows or []) if isinstance(x, dict)], int(total) if total is not None else None


async def _read_basic(client: httpx.AsyncClient, player_id: int) -> dict[str, Any] | None:
    try:
        data = await core._torn_get(client, f"/user/{int(player_id)}/basic", error_text="foreign player")
        return _basic_parts(data)
    except Exception:
        return None


async def _refresh_ids(client: httpx.AsyncClient, ids: list[int], destination_code: str, source: str):
    sem = asyncio.Semaphore(5)
    out = []
    async def one(pid: int):
        async with sem:
            parts = await _read_basic(client, pid)
            if not parts:
                return
            if parts.get("country") == destination_code or source == "tracked":
                _upsert(pid, parts, destination_code, source)
                out.append(pid)
    await asyncio.gather(*(one(int(pid)) for pid in ids))
    return out


def _rows_for_display(destination_code: str, limit: int = 100):
    now = time.time()
    cutoff = now - LANDED_TTL_SECONDS
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("""
            SELECT player_id,name,level,state,description,first_seen,last_seen,first_landed,last_checked,source
            FROM foreign_monitor_players
            WHERE destination_code=? AND (first_landed IS NULL OR first_landed>=?)
            ORDER BY
              CASE WHEN first_landed IS NOT NULL THEN 0 ELSE 1 END,
              COALESCE(first_landed, first_seen) DESC
            LIMIT ?
        """, (destination_code, cutoff, int(limit))).fetchall()
    out = []
    for row in rows:
        pid,name,level,state,desc,first_seen,last_seen,first_landed,last_checked,source = row
        landed_age = int(now - first_landed) if first_landed else None
        first_seen_age = int(now - first_seen) if first_seen else None
        out.append({
            "player_id": pid,
            "name": name or f"Player {pid}",
            "level": level,
            "state": state,
            "description": desc,
            "first_seen": first_seen,
            "first_landed": first_landed,
            "landed_age_seconds": landed_age,
            "tracked_age_seconds": first_seen_age,
            "freshness": "JUST LANDED" if landed_age is not None and landed_age <= 120 else "FRESH" if landed_age is not None and landed_age <= 600 else "LANDED" if landed_age is not None else "INBOUND",
            "profile_url": f"https://www.torn.com/profiles.php?XID={pid}",
            "attack_url": f"https://www.torn.com/loader.php?sid=attack&user2ID={pid}",
            "source": source,
        })
    return out



async def _own_total_battlestats(client: httpx.AsyncClient) -> int | None:
    now = time.time()
    cached = _own_bs_cache.get("total")
    if cached is not None and now - float(_own_bs_cache.get("at") or 0) < _OWN_BS_TTL:
        return int(cached)
    try:
        data = await core._torn_get(client, "/user/battlestats", error_text="battle stats")
        root = data.get("battlestats") if isinstance(data, dict) and isinstance(data.get("battlestats"), dict) else data
        total = 0
        for key in ("strength", "defense", "speed", "dexterity"):
            try:
                raw = root.get(key) if isinstance(root, dict) else 0
                if isinstance(raw, dict):
                    raw = raw.get("value") or raw.get("amount") or raw.get("total") or 0
                total += max(0, int(float(raw or 0)))
            except Exception:
                pass
        if total > 0:
            _own_bs_cache.update({"at": now, "total": total})
            return total
    except Exception:
        pass
    return None


async def _ff_enrich(client: httpx.AsyncClient, ids: list[int]) -> tuple[dict[int, dict[str, Any]], list[str]]:
    key, _ = mug_scout._ffscouter_key()
    if not key:
        return {}, ["FFScouter API key not loaded"]
    unique = list(dict.fromkeys(int(x) for x in ids if int(x) > 0))
    out: dict[int, dict[str, Any]] = {}
    errors: list[str] = []
    for start in range(0, len(unique), FF_BATCH_SIZE):
        batch = unique[start:start + FF_BATCH_SIZE]
        try:
            r = await client.get(
                f"{mug_scout.FFSCOUTER_BASE}/get-stats",
                params={"key": key, "targets": ",".join(str(x) for x in batch)},
                timeout=12.0,
            )
            data = r.json()
            if isinstance(data, dict) and data.get("error"):
                errors.append(str(data.get("error")))
                continue
            if not isinstance(data, list):
                errors.append("FFScouter returned an unexpected response")
                continue
            for row in data:
                if not isinstance(row, dict):
                    continue
                try:
                    pid = int(row.get("player_id"))
                except Exception:
                    continue
                out[pid] = row
        except Exception as exc:
            errors.append(str(exc))
    return out, errors


def _interest_score(item: dict[str, Any], own_total: int | None) -> dict[str, Any]:
    estimate = item.get("bs_estimate")
    ratio_pct = None
    try:
        if estimate is not None and own_total:
            ratio_pct = float(estimate) / float(own_total) * 100.0
    except Exception:
        ratio_pct = None

    ff = item.get("fair_fight")
    try:
        ff = float(ff) if ff is not None else None
    except Exception:
        ff = None

    landed_age = item.get("landed_age_seconds")
    score = 0
    reasons = []

    if landed_age is not None:
        if landed_age <= 120:
            score += 5; reasons.append("just landed")
        elif landed_age <= 300:
            score += 4; reasons.append("very fresh")
        elif landed_age <= 600:
            score += 3; reasons.append("fresh")
        elif landed_age <= 1800:
            score += 1
    else:
        score += 1; reasons.append("inbound")

    if ratio_pct is not None:
        if ratio_pct <= 30:
            score += 5; reasons.append("very low BS")
        elif ratio_pct <= 45:
            score += 4; reasons.append("low BS")
        elif ratio_pct <= 60:
            score += 3
        elif ratio_pct <= 75:
            score += 1
        elif ratio_pct > 100:
            score -= 4

    if ff is not None:
        if 2.0 <= ff <= 3.0:
            score += 2; reasons.append("good FF")
        elif ff >= 1.5:
            score += 1

    if score >= 10:
        label = "HIGH INTEREST"
    elif score >= 7:
        label = "WATCH"
    else:
        label = "STANDARD"
    return {"score": score, "label": label, "reasons": reasons, "ratio_pct": round(ratio_pct, 1) if ratio_pct is not None else None}


async def _enrich_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int | None, list[str]]:
    if not rows:
        return rows, None, []
    async with httpx.AsyncClient(timeout=15.0) as client:
        own_total, ff_result = await asyncio.gather(
            _own_total_battlestats(client),
            _ff_enrich(client, [int(x["player_id"]) for x in rows]),
        )
    ff_map, ff_errors = ff_result
    for item in rows:
        ff = ff_map.get(int(item["player_id"])) or {}
        estimate = ff.get("bs_estimate")
        try:
            estimate = int(float(estimate)) if estimate is not None else None
        except Exception:
            estimate = None
        fair_fight = ff.get("fair_fight")
        try:
            fair_fight = round(float(fair_fight), 2) if fair_fight is not None else None
        except Exception:
            fair_fight = None
        item["bs_estimate"] = estimate
        item["bs_estimate_human"] = ff.get("bs_estimate_human")
        item["fair_fight"] = fair_fight
        item["ff_source"] = ff.get("source")
        interest = _interest_score(item, own_total)
        item["bs_ratio_pct"] = interest["ratio_pct"]
        item["interest_score"] = interest["score"]
        item["interest_label"] = interest["label"]
        item["interest_reasons"] = interest["reasons"]
    rows.sort(
        key=lambda x: (
            1 if x.get("first_landed") is not None else 0,
            int(x.get("interest_score") or 0),
            -int(x.get("landed_age_seconds") or 10**9),
        ),
        reverse=True,
    )
    return rows, own_total, ff_errors


@app.post("/api/foreign-monitor/scan")
async def foreign_monitor_scan(
    country: str | None = Query(default=None),
    limit: int = Query(100, ge=1, le=250),
):
    travel = ti._travel_state()
    destination_code = (country or travel.get("destination_code") or "").strip().lower()
    if destination_code not in ti.COUNTRY_NAMES:
        raise HTTPException(400, "Could not determine a foreign destination. Pick a country manually or start traveling first.")

    tracked = _tracked(destination_code, 150)
    tracked_ids = [int(x["player_id"]) for x in tracked[:TRACKED_REFRESH_BUDGET]]

    offset = int(_meta_get(f"cursor:{destination_code}", "0") or 0)
    discovered = []
    total = None
    warnings = []

    async with httpx.AsyncClient(timeout=15.0) as client:
        if tracked_ids:
            await _refresh_ids(client, tracked_ids, destination_code, "tracked")
        try:
            search_rows, total = await _search_travelers(client, offset)
            discovered = search_rows[:DISCOVERY_BASIC_BUDGET]
            discovery_ids = []
            for row in discovered:
                try:
                    discovery_ids.append(int(row.get("id")))
                except Exception:
                    pass
            if discovery_ids:
                await _refresh_ids(client, discovery_ids, destination_code, "search")
        except Exception as exc:
            warnings.append(str(getattr(exc, "detail", exc)))
            search_rows = []

    next_offset = offset + DISCOVERY_PAGE_SIZE
    if total is not None and next_offset >= total:
        next_offset = 0
    elif next_offset >= 500:
        next_offset = 0
    _meta_set(f"cursor:{destination_code}", next_offset)

    rows = _rows_for_display(destination_code, limit)
    rows, own_total, ff_errors = await _enrich_rows(rows)
    warnings.extend(ff_errors)
    landed = [x for x in rows if x.get("first_landed")]
    inbound = [x for x in rows if not x.get("first_landed") and _country_from_text(x.get("description")) == destination_code]

    return {
        "ok": True,
        "destination_code": destination_code,
        "destination_name": ti.COUNTRY_NAMES.get(destination_code, destination_code.upper()),
        "your_travel": travel,
        "search_offset": offset,
        "next_search_offset": next_offset,
        "search_total": total,
        "searched_users": len(discovered),
        "tracked_refreshed": len(tracked_ids),
        "tracked_total": len(_tracked(destination_code, 500)),
        "own_total_battlestats": own_total,
        "landed_count": len(landed),
        "inbound_count": len(inbound),
        "items": rows,
        "warnings": warnings[:6],
        "note": "Landing time is TornTools first-observed landing time, not an official Torn landing timestamp.",
    }


@app.get("/api/foreign-monitor/status")
async def foreign_monitor_status(country: str | None = Query(default=None), limit: int = Query(100, ge=1, le=250)):
    travel = ti._travel_state()
    destination_code = (country or travel.get("destination_code") or "").strip().lower()
    if destination_code not in ti.COUNTRY_NAMES:
        return {"ok": True, "destination_code": None, "destination_name": None, "your_travel": travel, "items": []}
    rows = _rows_for_display(destination_code, limit)
    rows, own_total, ff_errors = await _enrich_rows(rows)
    return {
        "ok": True,
        "destination_code": destination_code,
        "destination_name": ti.COUNTRY_NAMES.get(destination_code, destination_code.upper()),
        "your_travel": travel,
        "own_total_battlestats": own_total,
        "items": rows,
        "warnings": ff_errors[:6],
    }
