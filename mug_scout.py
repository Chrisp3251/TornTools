import asyncio
import math
import time
from typing import Any

import httpx
from fastapi import HTTPException, Query

import app as app_module
import reports
from reports import app

FFSCOUTER_BASE = "https://ffscouter.com/api/v1"
TORN_API_BASE = "https://api.torn.com/v2"
MUG_SCOUT_VERSION = "0.3.1"


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


def _property_signal(prop: dict[str, Any] | None, player_id: int) -> dict[str, Any]:
    if not isinstance(prop, dict):
        return {"score": 0.0, "name": "Unknown", "market_price": None, "ownership": "Unknown"}
    basic = prop.get("property") if isinstance(prop.get("property"), dict) else {}
    name = basic.get("name") or "Unknown"
    market_price = prop.get("market_price")
    try:
        market_price = int(market_price) if market_price is not None else None
    except (TypeError, ValueError):
        market_price = None
    owner = prop.get("owner") if isinstance(prop.get("owner"), dict) else {}
    try:
        owner_id = int(owner.get("id")) if owner.get("id") is not None else None
    except (TypeError, ValueError):
        owner_id = None
    ownership = "Owned" if owner_id == int(player_id) else "Rented" if owner_id else "Unknown"

    # Wealth hint only. Log scale prevents a PI from dominating the whole score.
    if market_price and market_price > 0:
        score = _clamp((math.log10(max(1, market_price)) - 5.0) * 24.0)
    else:
        score = 0.0
    if ownership == "Owned":
        score = _clamp(score + 12)
    elif ownership == "Rented":
        score = _clamp(score * 0.72)
    return {"score": round(score, 1), "name": name, "market_price": market_price, "ownership": ownership}


def _fight_score(ff: float | None, minff: float, maxff: float) -> float:
    if ff is None:
        return 0.0
    ff = float(ff)
    if ff < minff or ff > maxff:
        return 0.0
    # Sweet spot toward the upper-middle of the chosen range: useful FF without
    # pushing all the way to the riskiest edge.
    sweet = min(maxff, max(minff, minff + (maxff - minff) * 0.68))
    span = max(0.25, maxff - minff)
    return _clamp(100.0 - abs(ff - sweet) / span * 70.0)


def _activity_score(last_action: int | None) -> tuple[float, str, int | None]:
    if not last_action:
        return 20.0, "Unknown", None
    age = max(0, int(time.time() - int(last_action)))
    if age <= 15 * 60:
        return 100.0, "Very recent", age
    if age <= 60 * 60:
        return 92.0, "Recent", age
    if age <= 6 * 3600:
        return 78.0, "Today", age
    if age <= 24 * 3600:
        return 62.0, "Within 24h", age
    if age <= 3 * 86400:
        return 45.0, "1–3 days", age
    if age <= 14 * 86400:
        return 30.0, "3–14 days", age
    return 15.0, "14+ days", age


def _availability_score(status: dict[str, Any] | None, hospital_until: int | None) -> tuple[float, str]:
    now = int(time.time())
    if hospital_until and int(hospital_until) > now:
        return 0.0, "Hospitalized"
    if not isinstance(status, dict):
        return 60.0, "Unknown"
    state = str(status.get("state") or "").strip()
    description = str(status.get("description") or state or "Unknown").strip()
    lowered = f"{state} {description}".lower()
    if any(x in lowered for x in ("hospital", "jail", "travel", "abroad", "federal")):
        return 0.0, description
    if any(x in lowered for x in ("okay", "idle")):
        return 100.0, description
    return 70.0, description


def _level_score(level: int | None) -> float:
    if level is None:
        return 40.0
    level = int(level)
    # Mild signal only: higher level can correlate with established wealth, but
    # FF already controls fight suitability and level should never dominate.
    return _clamp(25 + min(level, 100) * 0.7)


def _profile_url(player_id: int) -> str:
    return f"https://www.torn.com/profiles.php?XID={int(player_id)}"


def _attack_url(player_id: int) -> str:
    return f"https://www.torn.com/loader.php?sid=attack&user2ID={int(player_id)}"


async def _torn_enrich(client: httpx.AsyncClient, player_id: int) -> dict[str, Any]:
    key = app_module._api_key
    if not key:
        return {"profile": {}, "property": {}, "error": "Torn API key not loaded"}
    try:
        r = await client.get(
            f"{TORN_API_BASE}/user",
            headers={"Authorization": f"ApiKey {key}"},
            params={"id": int(player_id), "selections": "basic,property"},
            timeout=8.0,
        )
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            return {"profile": {}, "property": {}, "error": str(data.get("error"))}
        return {
            "profile": data.get("profile") if isinstance(data, dict) and isinstance(data.get("profile"), dict) else {},
            "property": data.get("property") if isinstance(data, dict) and isinstance(data.get("property"), dict) else {},
            "error": None,
        }
    except Exception as e:
        return {"profile": {}, "property": {}, "error": str(e)}


@app.get("/api/mug-scout/search")
async def mug_scout_search(
    minff: float = Query(1.8, ge=1.0, le=10.0),
    maxff: float = Query(3.0, ge=1.0, le=10.0),
    minlevel: int = Query(15, ge=1, le=100),
    maxlevel: int = Query(60, ge=1, le=100),
    limit: int = Query(12, ge=1, le=30),
    factionless: int = Query(0, ge=0, le=1),
):
    if minff > maxff:
        raise HTTPException(400, "Minimum FF cannot exceed maximum FF")
    if minlevel > maxlevel:
        raise HTTPException(400, "Minimum level cannot exceed maximum level")
    if not app_module._api_key:
        raise HTTPException(401, "Load your Torn API key first")

    # FFScouter uses registered Torn API keys. We never auto-register or send a
    # registration request; if the current key is not registered, surface that
    # clearly to the user.
    params = {
        "key": app_module._api_key,
        "minlevel": int(minlevel),
        "maxlevel": int(maxlevel),
        "inactiveonly": 0,
        "minff": float(minff),
        "maxff": float(maxff),
        "limit": int(limit),
        "factionless": int(factionless),
    }
    async with httpx.AsyncClient() as client:
        try:
            ff_resp = await client.get(f"{FFSCOUTER_BASE}/get-targets", params=params, timeout=10.0)
            ff_data = ff_resp.json()
        except Exception as e:
            raise HTTPException(502, f"Could not reach FFScouter: {e}") from e
        if isinstance(ff_data, dict) and ff_data.get("error"):
            raise HTTPException(ff_resp.status_code if ff_resp.status_code >= 400 else 400, f"FFScouter: {ff_data.get('error')}")
        raw_targets = ff_data.get("targets") if isinstance(ff_data, dict) else None
        if not isinstance(raw_targets, list):
            raise HTTPException(502, "FFScouter returned an unexpected target response")

        sem = asyncio.Semaphore(4)
        async def enrich_one(t):
            async with sem:
                return await _torn_enrich(client, int(t.get("player_id")))
        enriched = await asyncio.gather(*(enrich_one(t) for t in raw_targets))

    rows = []
    for t, extra in zip(raw_targets, enriched):
        try:
            player_id = int(t.get("player_id"))
        except (TypeError, ValueError):
            continue
        ff = t.get("fair_fight")
        try:
            ff = float(ff) if ff is not None else None
        except (TypeError, ValueError):
            ff = None
        profile = extra.get("profile") or {}
        prop = extra.get("property") or {}
        status = profile.get("status") if isinstance(profile.get("status"), dict) else {}
        property_signal = _property_signal(prop, player_id)
        fight = _fight_score(ff, minff, maxff)
        activity, activity_label, last_action_age = _activity_score(t.get("last_action"))
        availability, availability_label = _availability_score(status, t.get("hospital_until"))
        level = t.get("level") or profile.get("level")
        level_sig = _level_score(level)

        # Fight suitability dominates. Property helps but cannot rescue an
        # unavailable or poor-match target. Activity is a cash-opportunity hint,
        # not a claim about wallet balance.
        base_score = fight * 0.45 + activity * 0.20 + property_signal["score"] * 0.15 + level_sig * 0.10 + availability * 0.10
        if availability <= 0:
            base_score *= 0.35
        score = round(_clamp(base_score), 1)

        rows.append({
            "player_id": player_id,
            "name": t.get("name") or profile.get("name") or f"Player {player_id}",
            "level": int(level) if level is not None else None,
            "fair_fight": ff,
            "bs_estimate": t.get("bs_estimate"),
            "bs_estimate_human": t.get("bs_estimate_human"),
            "ff_source": t.get("source"),
            "last_action": t.get("last_action"),
            "last_action_age_seconds": last_action_age,
            "activity_label": activity_label,
            "status": availability_label,
            "hospital_until": t.get("hospital_until"),
            "property": property_signal,
            "scores": {
                "fight": round(fight, 1),
                "activity": round(activity, 1),
                "property": property_signal["score"],
                "availability": round(availability, 1),
                "level": round(level_sig, 1),
                "mug": score,
            },
            "profile_url": _profile_url(player_id),
            "attack_url": _attack_url(player_id),
            "enrichment_error": extra.get("error"),
        })
    rows.sort(key=lambda x: (x["scores"]["mug"], x["scores"]["fight"]), reverse=True)
    return {
        "ok": True,
        "version": MUG_SCOUT_VERSION,
        "criteria": {
            "minff": minff,
            "maxff": maxff,
            "minlevel": minlevel,
            "maxlevel": maxlevel,
            "limit": limit,
            "factionless": bool(factionless),
        },
        "items": rows,
        "notes": [
            "Mug Score ranks public signals; it does not know cash on hand.",
            "Property is a secondary wealth hint and is down-weighted when rented.",
            "Torn Item Market API listings are anonymous, so Item Market seller value is not used in this version.",
        ],
    }
