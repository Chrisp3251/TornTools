import asyncio
import time

import httpx
from fastapi import HTTPException, Query

import mug_scout
import mug_scout_v034
from mug_scout_v034 import app

MUG_SCOUT_VERSION = "0.4.1"
MIN_INACTIVE_DAYS = 15
MAX_INACTIVE_DAYS = 100


def _retaliation_score(age_seconds):
    if age_seconds is None:
        return 0.0, "Unknown"
    days = float(age_seconds) / 86400.0
    if days >= 60:
        return 100.0, "Very cold"
    if days >= 30:
        return 92.0, "Cold"
    if days >= 21:
        return 86.0, "Inactive"
    if days >= 15:
        return 80.0, "Inactive"
    return 10.0, "Recent"


@app.get("/api/mug-scout/search-v3")
async def mug_scout_search_v3(
    minff: float = Query(1.8, ge=1.0, le=10.0),
    maxff: float = Query(3.0, ge=1.0, le=10.0),
    minlevel: int = Query(15, ge=1, le=100),
    maxlevel: int = Query(60, ge=1, le=100),
    limit: int = Query(12, ge=1, le=30),
    factionless: int = Query(0, ge=0, le=1),
    mininactive_days: int = Query(15, ge=15, le=100),
    maxinactive_days: int = Query(100, ge=15, le=100),
):
    mininactive_days = max(MIN_INACTIVE_DAYS, int(mininactive_days))
    maxinactive_days = min(MAX_INACTIVE_DAYS, int(maxinactive_days))
    if mininactive_days > maxinactive_days:
        raise HTTPException(400, "Minimum inactive days cannot exceed maximum inactive days")
    if minff > maxff:
        raise HTTPException(400, "Minimum FF cannot exceed maximum FF")
    if minlevel > maxlevel:
        raise HTTPException(400, "Minimum level cannot exceed maximum level")

    ff_key, ff_key_source = mug_scout._ffscouter_key()
    if not ff_key:
        raise HTTPException(401, "No FFScouter API key found. Add FFSCOUTER_API_KEY=... to TornTools .env and restart.")

    # FFScouter accepts at most 50 targets per request. TornTools then filters
    # that inactive pool down to only targets inside the requested 15-100 day window.
    params = {
        "key": ff_key,
        "minlevel": int(minlevel),
        "maxlevel": int(maxlevel),
        "inactiveonly": 1,
        "minff": float(minff),
        "maxff": float(maxff),
        "limit": 50,
        "factionless": int(factionless),
    }

    async with httpx.AsyncClient() as client:
        try:
            ff_resp = await client.get(f"{mug_scout.FFSCOUTER_BASE}/get-targets", params=params, timeout=10.0)
            ff_data = ff_resp.json()
        except Exception as e:
            raise HTTPException(502, f"Could not reach FFScouter: {e}") from e

        if isinstance(ff_data, dict) and ff_data.get("error"):
            raise HTTPException(
                ff_resp.status_code if ff_resp.status_code >= 400 else 400,
                f"FFScouter: {ff_data.get('error')}",
            )
        raw_targets = ff_data.get("targets") if isinstance(ff_data, dict) else None
        if not isinstance(raw_targets, list):
            raise HTTPException(502, "FFScouter returned an unexpected target response")

        now = int(time.time())
        min_seconds = mininactive_days * 86400
        max_seconds = maxinactive_days * 86400
        eligible = []
        for target in raw_targets:
            try:
                last_action = int(target.get("last_action"))
            except (TypeError, ValueError):
                continue
            age = max(0, now - last_action)
            if min_seconds <= age <= max_seconds:
                eligible.append((target, age))

        # Prefer the more recently inactive portion of the window instead of
        # letting the oldest eligible accounts dominate the result set.
        eligible.sort(key=lambda pair: pair[1])
        eligible = eligible[:30]
        sem = asyncio.Semaphore(4)

        async def enrich_one(pair):
            target, age = pair
            async with sem:
                extra = await mug_scout._torn_enrich(client, int(target.get("player_id")))
                return target, age, extra

        enriched = await asyncio.gather(*(enrich_one(pair) for pair in eligible))

    rows = []
    for target, age, extra in enriched:
        try:
            player_id = int(target.get("player_id"))
        except (TypeError, ValueError):
            continue
        try:
            ff = float(target.get("fair_fight")) if target.get("fair_fight") is not None else None
        except (TypeError, ValueError):
            ff = None

        profile = extra.get("profile") or {}
        prop = extra.get("property") or {}
        status = profile.get("status") if isinstance(profile.get("status"), dict) else {}
        property_signal = mug_scout._property_signal(prop, player_id)
        fight = mug_scout._fight_score(ff, minff, maxff)
        availability, availability_label = mug_scout._availability_score(status, target.get("hospital_until"))
        level = target.get("level") or profile.get("level")
        level_sig = mug_scout._level_score(level)
        retaliation, retaliation_label = _retaliation_score(age)

        score = mug_scout._clamp(
            fight * 0.45
            + property_signal["score"] * 0.20
            + retaliation * 0.15
            + level_sig * 0.10
            + availability * 0.10
        )
        if availability <= 0:
            score *= 0.35
        score = round(score, 1)

        rows.append({
            "player_id": player_id,
            "name": target.get("name") or profile.get("name") or f"Player {player_id}",
            "level": int(level) if level is not None else None,
            "fair_fight": ff,
            "bs_estimate": target.get("bs_estimate"),
            "bs_estimate_human": target.get("bs_estimate_human"),
            "ff_source": target.get("source"),
            "last_action": target.get("last_action"),
            "last_action_age_seconds": int(age),
            "activity_label": retaliation_label,
            "status": availability_label,
            "hospital_until": target.get("hospital_until"),
            "property": property_signal,
            "scores": {
                "fight": round(fight, 1),
                "activity": 0.0,
                "retaliation": round(retaliation, 1),
                "property": property_signal["score"],
                "availability": round(availability, 1),
                "level": round(level_sig, 1),
                "mug": score,
            },
            "profile_url": mug_scout._profile_url(player_id),
            "attack_url": mug_scout._attack_url(player_id),
            "enrichment_error": extra.get("error"),
        })

    rows.sort(key=lambda x: (x["scores"]["mug"], x["scores"]["fight"]), reverse=True)
    rows = rows[: int(limit)]

    return {
        "ok": True,
        "version": MUG_SCOUT_VERSION,
        "ffscouter_key_source": ff_key_source,
        "criteria": {
            "minff": minff,
            "maxff": maxff,
            "minlevel": minlevel,
            "maxlevel": maxlevel,
            "limit": limit,
            "factionless": bool(factionless),
            "mininactive_days": mininactive_days,
            "maxinactive_days": maxinactive_days,
            "ffscouter_inactive_pool": True,
        },
        "items": rows,
        "notes": [
            f"Inactive window: {mininactive_days}-{maxinactive_days} days since last action.",
            "Accounts inactive longer than the maximum are excluded instead of dominating the results.",
            f"FFScouter key source: {ff_key_source}.",
            "Mug Score ranks matchup and public wealth/safety hints; it still cannot know cash on hand.",
        ],
    }
