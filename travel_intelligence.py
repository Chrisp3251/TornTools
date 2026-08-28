from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import bazaar_watch_runtime

app = bazaar_watch_runtime.app

STOCK_URL = "https://torn-intel.com/api/v1/public/foreign-stock"
HISTORY_URL = "https://torn-intel.com/api/v1/public/foreign-stock/history"
USER_AGENT = "TornTools-Local/0.6 TravelIntelligence"
CACHE_SECONDS = 20
_stock_cache: dict[str, Any] = {"at": 0.0, "data": None}

# Approximate one-way minutes with a Private Island airstrip. The UI exposes a
# speed multiplier so the ranking can be calibrated to the player's actual trip.
AIRSTRIP_MINUTES = {
    "mex": 18, "cay": 25, "can": 29, "haw": 53, "uk": 111,
    "arg": 117, "swi": 123, "jap": 158, "chi": 169, "uae": 176, "saf": 178,
}
COUNTRY_NAMES = {
    "mex": "Mexico", "cay": "Cayman Islands", "can": "Canada", "haw": "Hawaii",
    "uk": "United Kingdom", "arg": "Argentina", "swi": "Switzerland", "jap": "Japan",
    "chi": "China", "uae": "UAE", "saf": "South Africa",
}


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))


def _stock() -> dict:
    now = time.time()
    if _stock_cache["data"] is not None and now - float(_stock_cache["at"]) < CACHE_SECONDS:
        return _stock_cache["data"]
    data = _get_json(STOCK_URL)
    _stock_cache.update(at=now, data=data)
    return data


def _age_seconds(value: Any) -> float:
    if not value:
        return 999999.0
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return 999999.0


def _confidence(age_s: float, qty: int) -> tuple[float, str]:
    freshness = max(0.45, 1.0 - max(0.0, age_s - 20.0) / 300.0)
    stock = min(1.0, 0.55 + math.log1p(max(0, qty)) / 8.0)
    score = max(0.35, min(1.0, freshness * stock))
    label = "HIGH" if score >= .82 else "MEDIUM" if score >= .62 else "LOW"
    return score, label


def _adjusted_sale(market: int, cost: int, age_s: float, qty: int) -> tuple[int, float, str]:
    conf, label = _confidence(age_s, qty)
    # Conservative realization haircut. Until our own resale evidence exists,
    # don't treat a community marketValue snapshot as guaranteed cash.
    haircut = 0.985 - (1.0 - conf) * 0.045
    sale = max(cost, int(market * haircut))
    return sale, conf, label


def _optimize_country(country: dict, capacity: int, speed: float, sale_fee: float) -> dict | None:
    code = str(country.get("country") or "").lower()
    base_minutes = AIRSTRIP_MINUTES.get(code)
    if not base_minutes:
        return None
    age_s = _age_seconds(country.get("updatedAt"))
    candidates = []
    for raw in country.get("items") or []:
        qty = max(0, int(raw.get("quantity") or 0))
        cost = max(0, int(raw.get("cost") or 0))
        market = max(0, int(raw.get("marketValue") or 0))
        if qty <= 0 or cost <= 0 or market <= 0:
            continue
        adjusted_sale, conf, conf_label = _adjusted_sale(market, cost, age_s, qty)
        net_sale = int(adjusted_sale * (1.0 - sale_fee))
        profit = net_sale - cost
        if profit <= 0:
            continue
        candidates.append({
            "item_id": int(raw.get("itemId") or 0), "name": raw.get("itemName") or "Unknown",
            "category": raw.get("category") or "", "stock": qty, "cost": cost,
            "market_value": market, "adjusted_sale": adjusted_sale, "net_sale": net_sale,
            "profit_each": profit, "headline_profit": int(raw.get("profitPerItem") or market - cost),
            "confidence": round(conf, 3), "confidence_label": conf_label,
        })
    candidates.sort(key=lambda x: (x["profit_each"] * x["confidence"], x["profit_each"]), reverse=True)
    remaining = capacity
    load = []
    spend = expected_profit = headline_profit = 0
    weighted_conf = 0.0
    for item in candidates:
        take = min(remaining, item["stock"])
        if take <= 0:
            continue
        row = dict(item)
        row["buy"] = take
        row["load_profit"] = take * item["profit_each"]
        load.append(row)
        spend += take * item["cost"]
        expected_profit += row["load_profit"]
        headline_profit += take * item["headline_profit"]
        weighted_conf += take * item["confidence"]
        remaining -= take
        if remaining <= 0:
            break
    if not load:
        return None
    trip_minutes = (base_minutes * 2.0) / max(.25, speed)
    fill = capacity - remaining
    confidence = weighted_conf / max(1, fill)
    profit_hour = expected_profit / max(1 / 60, trip_minutes / 60.0)
    # Ranking rewards realizable hourly profit, full loads, and confidence.
    score = profit_hour * (0.70 + 0.30 * fill / capacity) * (0.72 + 0.28 * confidence)
    return {
        "country": code, "country_name": COUNTRY_NAMES.get(code, code.upper()),
        "updated_at": country.get("updatedAt"), "age_seconds": round(age_s),
        "capacity": capacity, "filled": fill, "remaining": remaining,
        "one_way_minutes": round(base_minutes / max(.25, speed), 1), "round_trip_minutes": round(trip_minutes, 1),
        "spend": spend, "headline_profit": headline_profit, "expected_profit": expected_profit,
        "profit_per_hour": round(profit_hour), "confidence": round(confidence, 3),
        "confidence_label": "HIGH" if confidence >= .82 else "MEDIUM" if confidence >= .62 else "LOW",
        "score": round(score), "load": load,
    }


@app.get("/api/travel-intelligence")
def travel_intelligence(capacity: int = 17, speed: float = 1.0, sale_fee: float = 0.05):
    capacity = max(1, min(100, int(capacity)))
    speed = max(.25, min(3.0, float(speed)))
    sale_fee = max(0.0, min(.25, float(sale_fee)))
    data = _stock()
    trips = [x for x in (_optimize_country(c, capacity, speed, sale_fee) for c in data.get("countries") or []) if x]
    trips.sort(key=lambda x: x["score"], reverse=True)
    return {
        "generated_at": data.get("generatedAt"), "source": data.get("source"),
        "capacity": capacity, "speed": speed, "sale_fee": sale_fee,
        "best": trips[0] if trips else None, "trips": trips,
        "model": "TT-Travel-v1", "note": "Adjusted profit is conservative and confidence-weighted; it is not a guaranteed sale price.",
    }


@app.get("/api/travel-intelligence/history")
def travel_history(country: str, item_id: int, hours: int = 24):
    hours = max(1, min(48, int(hours)))
    q = urllib.parse.urlencode({"country": country, "itemId": int(item_id), "hours": hours})
    return _get_json(f"{HISTORY_URL}?{q}")
