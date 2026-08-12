import sqlite3
import statistics
import time

from fastapi import HTTPException
from pydantic import BaseModel

# Import server first so the Research Lab routes / promotion loader are registered.
import server  # noqa: F401
import app as app_module
from app import app, DB_PATH, DISCOVERY_ITEMS, market_url


# -----------------------------------------------------------------------------
# Smarter equipment vendor-vs-market decision model
# -----------------------------------------------------------------------------


def _equipment_conservative_price(values):
    values = sorted(int(v) for v in values if v and v > 0)
    if not values:
        return None
    return values[int((len(values) - 1) * 0.25)]


def _equipment_monotonic_quality_ask(rows, quality):
    """Smoothed non-decreasing quality/price curve from nearby plain listings."""
    anchors = []
    for center in range(0, 101, 5):
        nearby = [r["price"] for r in rows if abs(r["quality"] - center) <= 5]
        if len(nearby) < 3:
            continue
        anchors.append([float(center), _equipment_conservative_price(nearby), len(nearby)])
    if not anchors:
        return None, 0

    running = 0
    for anchor in anchors:
        running = max(running, anchor[1])
        anchor[1] = running

    lower = [a for a in anchors if a[0] <= quality]
    upper = [a for a in anchors if a[0] >= quality]
    if lower and upper:
        lo = lower[-1]
        hi = upper[0]
        if hi[0] == lo[0]:
            return int(lo[1]), lo[2]
        ratio = (quality - lo[0]) / (hi[0] - lo[0])
        return int(lo[1] + (hi[1] - lo[1]) * ratio), min(lo[2], hi[2])
    anchor = lower[-1] if lower else upper[0]
    return int(anchor[1]), anchor[2]


def smarter_equipment_verdict(rows, quality, damage, accuracy, armor, vendor_sell):
    def distance(row):
        score = abs(row["quality"] - quality)
        if damage is not None and row["damage"] is not None:
            score += 2.0 * abs(row["damage"] - damage)
        if accuracy is not None and row["accuracy"] is not None:
            score += 1.5 * abs(row["accuracy"] - accuracy)
        if armor is not None and row["armor"] is not None:
            score += 2.0 * abs(row["armor"] - armor)
        return score

    ranked = sorted(rows, key=distance)
    close5 = [r for r in ranked if abs(r["quality"] - quality) <= 5][:12]
    comps = close5 if len(close5) >= 4 else ranked[:12]
    chosen = comps[:8]
    prices = sorted(r["price"] for r in chosen)

    median_ask = int(statistics.median(prices)) if prices else None
    raw_ask = _equipment_conservative_price(prices)
    smooth_ask, smooth_support = _equipment_monotonic_quality_ask(rows, quality)
    competitive_ask = smooth_ask if smooth_ask is not None else raw_ask
    net = int(competitive_ask * (1 - app_module.MARKET_FEE)) if competitive_ask else None
    premium = (net - vendor_sell) if net is not None else None
    premium_pct = (premium / vendor_sell * 100) if premium is not None and vendor_sell > 0 else None

    percentile = (
        round(sum(1 for r in rows if r["quality"] < quality) / len(rows) * 100, 1)
        if rows
        else None
    )
    close_count = len(close5)

    # Nearby ask dispersion is a useful proxy for how trustworthy the market estimate is.
    ask_spread_pct = None
    if len(prices) >= 4 and median_ask:
        q1 = prices[int((len(prices) - 1) * 0.25)]
        q3 = prices[int((len(prices) - 1) * 0.75)]
        ask_spread_pct = round((q3 - q1) / median_ask * 100, 1)

    support_points = smooth_support + min(close_count, 8)
    if support_points >= 12 and (ask_spread_pct is None or ask_spread_pct <= 25):
        confidence = "HIGH"
    elif support_points >= 7 and (ask_spread_pct is None or ask_spread_pct <= 45):
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    # Dynamic hurdle instead of the old flat 20%-of-vendor rule.
    # Reliable/tight markets need less extra profit; noisy markets need more cushion.
    required_pct = {"HIGH": 3.0, "MEDIUM": 5.0, "LOW": 8.0}[confidence]

    # Better rolls deserve more protection because vendoring permanently destroys the roll.
    if percentile is not None:
        if percentile >= 90:
            required_pct -= 2.0
        elif percentile >= 75:
            required_pct -= 1.0
        elif percentile >= 60:
            required_pct -= 0.5

    # Very scattered asks make a market listing less certain, so demand a little more edge.
    if ask_spread_pct is not None:
        if ask_spread_pct >= 60:
            required_pct += 2.0
        elif ask_spread_pct >= 40:
            required_pct += 1.0

    required_pct = max(1.5, required_pct)

    # A modest absolute floor prevents wasting time for tiny gains, but it is capped so
    # expensive gear no longer creates absurd requirements like $64k just because vendor is high.
    absolute_floor = max(500, min(15000, int(vendor_sell * 0.015))) if vendor_sell > 0 else 500
    required_premium = (
        max(absolute_floor, int(vendor_sell * required_pct / 100))
        if vendor_sell > 0
        else absolute_floor
    )

    if not rows:
        verdict = "DON'T VENDOR YET" if quality >= 65 else "CHECK MANUALLY"
        reason = "No plain market comparables were returned."
    elif competitive_ask is None:
        verdict = "CHECK MANUALLY"
        reason = "There were not enough usable plain listings to estimate a stable market price."
    elif premium is not None and premium >= required_premium:
        verdict = "MARKET IT"
        reason = (
            f"After the 5% fee, the quality-adjusted market estimate is about ${premium:,} "
            f"({premium_pct:.1f}%) better than vendoring. With {confidence.lower()} market "
            f"confidence, the current listing hurdle is about {required_pct:.1f}% / ${required_premium:,}."
        )
    elif premium is not None and premium > 0 and (
        (percentile is not None and percentile >= 70)
        or premium >= required_premium * 0.60
        or (confidence == "LOW" and quality >= 60)
    ):
        verdict = "DON'T VENDOR YET"
        reason = (
            f"The market still appears to beat vendor by about ${premium:,} ({premium_pct:.1f}%), "
            f"but it falls short of the ${required_premium:,} confidence-adjusted listing target. "
            "This roll/value is close enough that vendoring immediately would be too aggressive."
        )
    elif percentile is not None and percentile >= 85 and (
        premium is None or premium > -required_premium
    ):
        verdict = "DON'T VENDOR YET"
        reason = (
            "This is a high-percentile roll and the current market sample is not strong enough "
            "to justify destroying it for vendor cash."
        )
    elif premium is not None and premium <= 0:
        verdict = "VENDOR"
        reason = (
            f"After the 5% fee, the current quality-adjusted market estimate is "
            f"${abs(premium):,} worse than the guaranteed vendor value."
        )
    else:
        verdict = "VENDOR"
        reason = (
            f"The estimated market edge is too small for the current {confidence.lower()}-confidence "
            f"market. It would need about {required_pct:.1f}% / ${required_premium:,} over vendor "
            "to justify the listing risk and wait."
        )

    return {
        "verdict": verdict,
        "reason": reason,
        "quality_percentile": percentile,
        "confidence": confidence,
        "plain_listings": len(rows),
        "close_comparables": close_count,
        "median_ask": median_ask,
        "competitive_ask": competitive_ask,
        "raw_local_ask": raw_ask,
        "pricing_model": "smoothed_monotonic_quality_v2",
        "minimum_listing_premium": required_premium,
        "required_premium_pct": round(required_pct, 2),
        "ask_spread_pct": ask_spread_pct,
        "net_after_fee": net,
        "premium_over_vendor": premium,
        "premium_over_vendor_pct": round(premium_pct, 2) if premium_pct is not None else None,
        "comps": chosen,
    }


# app.py's equipment route resolves equipment_verdict from the app module at request time,
# so replacing it here upgrades the existing route without duplicating endpoint definitions.
app_module.equipment_verdict = smarter_equipment_verdict


class SniperTargetPayload(BaseModel):
    item_id: int
    name: str | None = None
    max_price: int
    enabled: bool = True


def _init_sniper_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sniper_targets(
                item_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                max_price INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        # Seed the first known sniper target without overwriting later user edits.
        now = time.time()
        c.execute(
            """
            INSERT OR IGNORE INTO sniper_targets(item_id,name,max_price,enabled,created_at,updated_at)
            VALUES(?,?,?,?,?,?)
            """,
            (1086, "Driver's License", 4999, 1, now, now),
        )


def _sniper_rows():
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            "SELECT item_id,name,max_price,enabled,created_at,updated_at FROM sniper_targets ORDER BY enabled DESC,name COLLATE NOCASE"
        ).fetchall()


def _target_dict(row):
    return {
        "item_id": int(row[0]),
        "name": row[1],
        "max_price": int(row[2]),
        "enabled": bool(row[3]),
        "created_at": row[4],
        "updated_at": row[5],
        "market_url": market_url(int(row[0])),
    }


def _sync_sniper_targets_into_discovery():
    for row in _sniper_rows():
        target = _target_dict(row)
        item_id = target["item_id"]
        if target["enabled"]:
            meta = dict(DISCOVERY_ITEMS.get(item_id) or {})
            meta.update(
                {
                    "name": target["name"],
                    "sniper": True,
                    "sniper_max_price": target["max_price"],
                }
            )
            DISCOVERY_ITEMS[item_id] = meta
        elif item_id in DISCOVERY_ITEMS and DISCOVERY_ITEMS[item_id].get("sniper"):
            # Remove only sniper-specific metadata. Keep items that belong to the
            # normal Hidden Deals pool for another reason (hard floor / graduate).
            meta = dict(DISCOVERY_ITEMS[item_id])
            meta.pop("sniper", None)
            meta.pop("sniper_max_price", None)
            if meta.get("hard_floor") or meta.get("research_graduate"):
                DISCOVERY_ITEMS[item_id] = meta
            else:
                DISCOVERY_ITEMS.pop(item_id, None)


_init_sniper_db()
_sync_sniper_targets_into_discovery()


@app.get("/api/sniper/watchlist")
async def sniper_watchlist():
    _sync_sniper_targets_into_discovery()
    items = [_target_dict(row) for row in _sniper_rows()]
    return {
        "ok": True,
        "items": items,
        "enabled_count": sum(1 for x in items if x["enabled"]),
        "discovery_ids": list(DISCOVERY_ITEMS),
    }


@app.post("/api/sniper/watchlist")
async def save_sniper_target(payload: SniperTargetPayload):
    if payload.item_id <= 0:
        raise HTTPException(400, "Enter a valid item ID")
    if payload.max_price <= 0:
        raise HTTPException(400, "Max buy price must be greater than zero")
    name = (payload.name or "").strip() or f"Item {payload.item_id}"
    now = time.time()
    with sqlite3.connect(DB_PATH) as c:
        existing = c.execute(
            "SELECT created_at FROM sniper_targets WHERE item_id=?", (payload.item_id,)
        ).fetchone()
        created_at = existing[0] if existing else now
        c.execute(
            """
            INSERT OR REPLACE INTO sniper_targets(item_id,name,max_price,enabled,created_at,updated_at)
            VALUES(?,?,?,?,?,?)
            """,
            (
                payload.item_id,
                name,
                payload.max_price,
                1 if payload.enabled else 0,
                created_at,
                now,
            ),
        )
    _sync_sniper_targets_into_discovery()
    return {
        "ok": True,
        "item": {
            "item_id": payload.item_id,
            "name": name,
            "max_price": payload.max_price,
            "enabled": payload.enabled,
            "market_url": market_url(payload.item_id),
        },
        "discovery_ids": list(DISCOVERY_ITEMS),
    }


@app.delete("/api/sniper/watchlist/{item_id}")
async def delete_sniper_target(item_id: int):
    if item_id <= 0:
        raise HTTPException(400, "Enter a valid item ID")
    with sqlite3.connect(DB_PATH) as c:
        c.execute("DELETE FROM sniper_targets WHERE item_id=?", (item_id,))
    # Remove sniper metadata, but preserve normal Hidden Deals membership.
    if item_id in DISCOVERY_ITEMS and DISCOVERY_ITEMS[item_id].get("sniper"):
        meta = dict(DISCOVERY_ITEMS[item_id])
        meta.pop("sniper", None)
        meta.pop("sniper_max_price", None)
        if meta.get("hard_floor") or meta.get("research_graduate"):
            DISCOVERY_ITEMS[item_id] = meta
        else:
            DISCOVERY_ITEMS.pop(item_id, None)
    return {"ok": True, "item_id": item_id, "discovery_ids": list(DISCOVERY_ITEMS)}


@app.get("/api/sniper/config")
async def sniper_config():
    """Small config endpoint consumed by the Torn userscript companion."""
    items = [_target_dict(row) for row in _sniper_rows() if bool(row[3])]
    return {"ok": True, "items": items, "refreshed_at": time.time()}
