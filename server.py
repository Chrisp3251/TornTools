import asyncio
import json
import sqlite3
import statistics
import time

import httpx
from fastapi import HTTPException

from app import (
    app,
    _torn_get,
    DB_PATH,
    DISCOVERY_ITEMS,
    LEARN_ITEMS,
    fetch_market,
    liquidity_stats,
    market_url,
    parse_itemmarket,
    save_snapshot,
)


# -----------------------------------------------------------------------------
# Equipment metadata compatibility route
# -----------------------------------------------------------------------------

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


# -----------------------------------------------------------------------------
# Research / proof engine
# -----------------------------------------------------------------------------
# A snapshot being cheap is not proof by itself.  The case-maker now tries to
# identify independent bargain EVENTS and then checks whether the market
# recovered after each event.  That keeps one stale cheap listing from being
# counted over and over as evidence.

DEAL_EDGE_PCT = 8.0
STRONG_EDGE_PCT = 15.0
BASELINE_WINDOW = 12
RECOVERY_WINDOW = 3

# Research -> Hidden Deals: useful evidence, but deliberately less strict than
# Sniper Candidate.  Hidden Deals becomes the proving ground.
RESEARCH_MIN_SAMPLES = 16
RESEARCH_MIN_ACTIVITY = 20
RESEARCH_MIN_EVENTS = 2
RESEARCH_MIN_RECOVERED = 1
RESEARCH_MIN_MEDIAN_EDGE = 8.0
RESEARCH_MAX_FALSE_POSITIVE_RATE = 0.50

# Hidden Deals -> Sniper Candidate: intentionally difficult.  Promotion remains
# manual in the UI even after these requirements are met.
SNIPER_MIN_SAMPLES = 30
SNIPER_MIN_EVENTS = 4
SNIPER_MIN_RECOVERED = 3
SNIPER_MIN_RECOVERY_RATE = 0.70
SNIPER_MAX_FALSE_POSITIVE_RATE = 0.20
SNIPER_MIN_MEDIAN_EDGE = 10.0
SNIPER_MIN_ACTIVITY = 30
SNIPER_MIN_SCORE = 72.0


def _init_research_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS research_promotions(
                item_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                promoted_at REAL NOT NULL,
                promotion_score REAL NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )


def _load_research_promotions():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT item_id,name,promoted_at,promotion_score,reason FROM research_promotions"
        ).fetchall()
    for item_id, name, promoted_at, score, reason in rows:
        if item_id not in DISCOVERY_ITEMS:
            DISCOVERY_ITEMS[item_id] = {
                "name": name,
                "research_graduate": True,
                "promoted_at": promoted_at,
                "promotion_score": score,
                "promotion_reason": reason,
            }


_init_research_db()
_load_research_promotions()


def _research_rows(item_id: int, limit: int = 240):
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            """
            SELECT ts,lowest,qty_floor,next_higher,average_price,listing_count,listing_ids,total_top_qty
            FROM market_snapshots
            WHERE item_id=?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (item_id, limit),
        ).fetchall()


def _median(values):
    values = [float(x) for x in values if x is not None and float(x) > 0]
    return float(statistics.median(values)) if values else None


def _listing_signature(row):
    low, qty, raw_ids = row[1], row[2], row[6]
    ids = []
    try:
        decoded = json.loads(raw_ids or "[]")
        if isinstance(decoded, list):
            ids = [str(x) for x in decoded[:4]]
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return f"{low}:{qty}:{','.join(ids)}"


def _baseline_for(rows, index):
    """Rolling normal floor using only PRIOR observations to avoid look-ahead."""
    prior = [r[1] for r in rows[max(0, index - BASELINE_WINDOW):index] if r[1]]
    if len(prior) >= 5:
        return _median(prior)
    avg = rows[index][4]
    if avg and avg > 0:
        return float(avg)
    return _median(prior) or (float(rows[index][1]) if rows[index][1] else None)


def evidence_profile(item_id: int, name: str | None = None):
    """Build event/recovery evidence from cached market snapshots."""
    name = name or DISCOVERY_ITEMS.get(item_id, {}).get("name") or LEARN_ITEMS.get(item_id) or f"Item {item_id}"
    rows = list(reversed(_research_rows(item_id)))
    liq = liquidity_stats(item_id)
    n = len(rows)

    if not rows:
        return {
            "id": item_id,
            "name": name,
            "market_url": market_url(item_id),
            "observations": 0,
            "activity": "Learning",
            "activity_score": 0,
            "independent_events": 0,
            "bargain_events": 0,
            "strong_events": 0,
            "recovered_events": 0,
            "completed_events": 0,
            "recovery_rate": 0,
            "false_positive_events": 0,
            "false_positive_rate": 0,
            "median_edge_pct": 0,
            "best_edge_pct": 0,
            "median_discount_pct": 0,
            "best_discount_pct": 0,
            "median_deal_lifetime_seconds": None,
            "opportunities_per_hour": 0,
            "rolling_baseline": None,
            "current_discount_pct": 0,
            "median_gap_after_deal_pct": 0,
            "floor_volatility_pct": 0,
            "promotion_score": 0,
            "sniper_score": 0,
            "stage": "LEARNING",
            "graduated": item_id in DISCOVERY_ITEMS and bool(DISCOVERY_ITEMS[item_id].get("research_graduate")),
            "sniper_candidate": False,
            "recommended_sniper_max": None,
            "requirements": _research_requirements_text(),
        }

    baselines = []
    discounts = []
    deal_flags = []
    floor_changes = churn_events = 0
    prev = None
    latest_low = rows[-1][1]
    latest_avg = rows[-1][4]

    for idx, row in enumerate(rows):
        baseline = _baseline_for(rows, idx)
        baselines.append(baseline)
        low = row[1]
        discount = ((baseline - low) / baseline * 100.0) if baseline and low and baseline > 0 else 0.0
        discount = max(-100.0, min(100.0, discount))
        discounts.append(discount)
        deal_flags.append(discount >= DEAL_EDGE_PCT)
        if prev:
            if row[1] != prev[1] or row[2] != prev[2]:
                floor_changes += 1
            if row[6] != prev[6] or row[7] != prev[7]:
                churn_events += 1
        prev = row

    # Independent deal events.  We require a return to non-deal state before a
    # repeated cheap observation can become a new event.  This is intentionally
    # conservative and prevents stale cached listings from inflating evidence.
    events = []
    idx = 0
    while idx < n:
        if not deal_flags[idx]:
            idx += 1
            continue
        start = idx
        start_sig = _listing_signature(rows[idx])
        while idx + 1 < n and deal_flags[idx + 1]:
            idx += 1
        end = idx
        event_rows = rows[start:end + 1]
        event_discounts = discounts[start:end + 1]
        event_baselines = [x for x in baselines[start:end + 1] if x]
        event_lows = [r[1] for r in event_rows if r[1]]
        baseline = _median(event_baselines)
        min_low = min(event_lows) if event_lows else None
        best_edge = max(event_discounts) if event_discounts else 0.0
        duration = max(0.0, float(rows[end][0]) - float(rows[start][0]))
        completed = end < n - 1

        recovery = False
        false_positive = False
        if completed and baseline:
            future = rows[end + 1:min(n, end + 1 + RECOVERY_WINDOW)]
            future_lows = [r[1] for r in future if r[1]]
            if future_lows:
                # Recovery means price moved back near the pre-deal normal floor.
                recovery = max(future_lows) >= baseline * 0.95
                # False positive means the cheap level persisted as the new normal.
                false_positive = _median(future_lows) <= baseline * 0.90

        gap_pcts = []
        for r in event_rows:
            low, nxt = r[1], r[3]
            if low and nxt and nxt > low:
                gap_pcts.append((nxt - low) / low * 100.0)

        events.append({
            "start_ts": rows[start][0],
            "end_ts": rows[end][0],
            "signature": start_sig,
            "baseline": baseline,
            "min_price": min_low,
            "best_edge_pct": best_edge,
            "strong": best_edge >= STRONG_EDGE_PCT,
            "duration_seconds": duration,
            "completed": completed,
            "recovered": recovery,
            "false_positive": false_positive,
            "gap_pct": _median(gap_pcts) or 0.0,
        })
        idx += 1

    completed = [e for e in events if e["completed"]]
    recovered = [e for e in completed if e["recovered"]]
    false_positives = [e for e in completed if e["false_positive"]]
    strong_events = [e for e in events if e["strong"]]
    event_edges = [e["best_edge_pct"] for e in events]
    lifetimes = [e["duration_seconds"] for e in completed]
    event_gaps = [e["gap_pct"] for e in events if e["gap_pct"] > 0]

    recovery_rate = len(recovered) / len(completed) if completed else 0.0
    false_positive_rate = len(false_positives) / len(completed) if completed else 0.0
    span_hours = max(0.0, (float(rows[-1][0]) - float(rows[0][0])) / 3600.0) if n > 1 else 0.0
    opportunities_per_hour = len(events) / span_hours if span_hours >= 0.25 else 0.0

    recent_lows = [r[1] for r in rows[-BASELINE_WINDOW:] if r[1]]
    rolling_baseline = _median(recent_lows)
    current_discount = ((rolling_baseline - latest_low) / rolling_baseline * 100.0) if rolling_baseline and latest_low else 0.0
    median_edge = _median(event_edges) or 0.0
    best_edge = max(event_edges) if event_edges else 0.0
    median_lifetime = _median(lifetimes)
    median_gap = _median(event_gaps) or 0.0

    # Robust floor volatility (median absolute deviation) rewards a stable normal
    # price because a stable baseline makes underpriced events more trustworthy.
    floor_volatility = 0.0
    if rolling_baseline and recent_lows:
        deviations = [abs(x - rolling_baseline) for x in recent_lows]
        floor_volatility = (_median(deviations) or 0.0) / rolling_baseline * 100.0

    activity_score = float(liq.get("score") or 0)
    event_score = min(28.0, len(events) * 6.0)
    recovery_score = min(20.0, recovery_rate * 20.0)
    edge_score = min(18.0, median_edge * 1.2)
    strong_score = min(10.0, len(strong_events) * 4.0)
    activity_component = min(12.0, activity_score * 0.12)
    frequency_score = min(7.0, opportunities_per_hour * 3.5)
    false_penalty = min(25.0, false_positive_rate * 35.0)
    volatility_penalty = min(10.0, max(0.0, floor_volatility - 12.0) * 0.5)
    score = max(0.0, min(100.0, event_score + recovery_score + edge_score + strong_score + activity_component + frequency_score - false_penalty - volatility_penalty))

    graduated = item_id in DISCOVERY_ITEMS and bool(DISCOVERY_ITEMS[item_id].get("research_graduate"))
    research_ready = (
        n >= RESEARCH_MIN_SAMPLES
        and activity_score >= RESEARCH_MIN_ACTIVITY
        and len(events) >= RESEARCH_MIN_EVENTS
        and len(recovered) >= RESEARCH_MIN_RECOVERED
        and median_edge >= RESEARCH_MIN_MEDIAN_EDGE
        and false_positive_rate <= RESEARCH_MAX_FALSE_POSITIVE_RATE
    )
    sniper_candidate = (
        n >= SNIPER_MIN_SAMPLES
        and len(events) >= SNIPER_MIN_EVENTS
        and len(recovered) >= SNIPER_MIN_RECOVERED
        and recovery_rate >= SNIPER_MIN_RECOVERY_RATE
        and false_positive_rate <= SNIPER_MAX_FALSE_POSITIVE_RATE
        and median_edge >= SNIPER_MIN_MEDIAN_EDGE
        and activity_score >= SNIPER_MIN_ACTIVITY
        and score >= SNIPER_MIN_SCORE
    )

    # Max buy is based on BOTH the learned normal floor and what actual bargain
    # events looked like.  It therefore cannot be inflated by one high average.
    recommended_max = None
    if rolling_baseline and events:
        event_prices = [e["min_price"] for e in events if e["min_price"]]
        event_price_reference = _median(event_prices)
        baseline_cap = rolling_baseline * 0.90
        observed_cap = event_price_reference * 1.05 if event_price_reference else baseline_cap
        recommended_max = max(1, int(min(baseline_cap, observed_cap)))

    if sniper_candidate:
        stage = "SNIPER CANDIDATE"
    elif graduated:
        stage = "PROVING IN HIDDEN"
    elif research_ready:
        stage = "PROVEN MARKET"
    elif n < RESEARCH_MIN_SAMPLES:
        stage = "LEARNING"
    elif events:
        stage = "BUILDING CASE"
    else:
        stage = "NO EDGE YET"

    return {
        "id": item_id,
        "name": name,
        "market_url": market_url(item_id),
        "lowest": latest_low,
        "average_price": latest_avg,
        "observations": n,
        "activity": liq.get("label", "Learning"),
        "activity_score": round(activity_score, 1),
        "independent_events": len(events),
        # Backwards-compatible names used by the current Research Lab UI.
        "bargain_events": len(events),
        "strong_events": len(strong_events),
        "hit_rate": round((len(events) / n * 100.0) if n else 0.0, 1),
        "recovered_events": len(recovered),
        "completed_events": len(completed),
        "recovery_rate": round(recovery_rate * 100.0, 1),
        "false_positive_events": len(false_positives),
        "false_positive_rate": round(false_positive_rate * 100.0, 1),
        "median_edge_pct": round(median_edge, 2),
        "best_edge_pct": round(best_edge, 2),
        "median_discount_pct": round(median_edge, 2),
        "best_discount_pct": round(best_edge, 2),
        "median_deal_lifetime_seconds": round(median_lifetime, 1) if median_lifetime is not None else None,
        "opportunities_per_hour": round(opportunities_per_hour, 2),
        "rolling_baseline": int(rolling_baseline) if rolling_baseline else None,
        "current_discount_pct": round(current_discount, 2),
        "median_gap_after_deal_pct": round(median_gap, 2),
        "floor_volatility_pct": round(floor_volatility, 2),
        "floor_changes": floor_changes,
        "floor_change_rate": round(floor_changes / max(1, n - 1) * 100.0, 1),
        "listing_churn_events": churn_events,
        "listing_churn_rate": round(churn_events / max(1, n - 1) * 100.0, 1),
        "gap_events": int(liq.get("gap_events") or 0),
        "largest_gap_pct": round(float(liq.get("largest_gap_pct") or 0), 2),
        "promotion_score": round(score, 1),
        "sniper_score": round(score, 1),
        "stage": stage,
        "graduated": graduated,
        "ready": research_ready,
        "sniper_candidate": sniper_candidate,
        "recommended_sniper_max": recommended_max,
        "requirements": _research_requirements_text(),
        "sniper_requirements": _sniper_requirements_text(),
        "recent_events": list(reversed(events[-5:])),
    }


def research_profile(item_id: int, name: str):
    return evidence_profile(item_id, name)


def _research_requirements_text():
    return (
        f"{RESEARCH_MIN_SAMPLES}+ useful snapshots, {RESEARCH_MIN_EVENTS}+ independent bargain events, "
        f"{RESEARCH_MIN_RECOVERED}+ confirmed recovery, activity {RESEARCH_MIN_ACTIVITY}+, "
        f"median edge {RESEARCH_MIN_MEDIAN_EDGE:.0f}%+, false-positive rate <= {int(RESEARCH_MAX_FALSE_POSITIVE_RATE*100)}%"
    )


def _sniper_requirements_text():
    return (
        f"{SNIPER_MIN_SAMPLES}+ snapshots, {SNIPER_MIN_EVENTS}+ independent events, "
        f"{SNIPER_MIN_RECOVERED}+ recoveries, {int(SNIPER_MIN_RECOVERY_RATE*100)}%+ recovery rate, "
        f"<= {int(SNIPER_MAX_FALSE_POSITIVE_RATE*100)}% false positives, "
        f"{SNIPER_MIN_MEDIAN_EDGE:.0f}%+ median edge, activity {SNIPER_MIN_ACTIVITY}+, score {SNIPER_MIN_SCORE:.0f}+"
    )


def _promote_if_ready(profile):
    if not profile.get("ready") or profile.get("graduated"):
        return False
    item_id = int(profile["id"])
    name = profile["name"]
    reason = (
        f"Evidence score {profile['promotion_score']}; "
        f"{profile['independent_events']} independent deal events; "
        f"{profile['recovered_events']} recoveries; median edge {profile['median_edge_pct']:.1f}%"
    )
    now = time.time()
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR REPLACE INTO research_promotions(item_id,name,promoted_at,promotion_score,reason) VALUES(?,?,?,?,?)",
            (item_id, name, now, profile["promotion_score"], reason),
        )
    existing = dict(DISCOVERY_ITEMS.get(item_id) or {})
    existing.update({
        "name": name,
        "research_graduate": True,
        "promoted_at": now,
        "promotion_score": profile["promotion_score"],
        "promotion_reason": reason,
    })
    DISCOVERY_ITEMS[item_id] = existing
    return True


@app.post("/api/research/sample")
async def research_sample():
    ids = list(LEARN_ITEMS)
    async with httpx.AsyncClient(timeout=25) as client:
        results = await asyncio.gather(
            *(fetch_market(client, item_id) for item_id in ids),
            return_exceptions=True,
        )

    errors = []
    for item_id, result in zip(ids, results):
        if isinstance(result, Exception):
            errors.append({"id": item_id, "name": LEARN_ITEMS[item_id], "error": str(getattr(result, "detail", result))})
            continue
        listings, avg = parse_itemmarket(result)
        save_snapshot(item_id, listings, avg)

    profiles = [research_profile(item_id, name) for item_id, name in LEARN_ITEMS.items()]
    newly_graduated = []
    for profile in profiles:
        if _promote_if_ready(profile):
            newly_graduated.append({"id": profile["id"], "name": profile["name"]})

    profiles = [research_profile(item_id, name) for item_id, name in LEARN_ITEMS.items()]
    profiles.sort(key=lambda x: (x.get("sniper_candidate", False), x.get("graduated", False), x.get("promotion_score", 0)), reverse=True)
    return {
        "ok": True,
        "sampled_at": time.time(),
        "items": profiles,
        "errors": errors,
        "newly_graduated": newly_graduated,
        "discovery_ids": list(DISCOVERY_ITEMS),
        "requirements": _research_requirements_text(),
        "sniper_requirements": _sniper_requirements_text(),
    }


@app.get("/api/research/status")
async def research_status():
    profiles = [research_profile(item_id, name) for item_id, name in LEARN_ITEMS.items()]
    profiles.sort(key=lambda x: (x.get("sniper_candidate", False), x.get("graduated", False), x.get("promotion_score", 0)), reverse=True)
    with sqlite3.connect(DB_PATH) as c:
        promoted = c.execute(
            "SELECT item_id,name,promoted_at,promotion_score,reason FROM research_promotions ORDER BY promoted_at DESC"
        ).fetchall()
    return {
        "ok": True,
        "items": profiles,
        "graduates": [
            {
                "id": row[0],
                "name": row[1],
                "promoted_at": row[2],
                "promotion_score": row[3],
                "reason": row[4],
            }
            for row in promoted
        ],
        "discovery_ids": list(DISCOVERY_ITEMS),
        "requirements": _research_requirements_text(),
        "sniper_requirements": _sniper_requirements_text(),
    }
