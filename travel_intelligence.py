from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import app as core_app
import bazaar_watch_runtime

app = bazaar_watch_runtime.app

STOCK_URL = "https://torn-intel.com/api/v1/public/foreign-stock"
HISTORY_URL = "https://torn-intel.com/api/v1/public/foreign-stock/history"
TORN_USER_URL = "https://api.torn.com/user/"
USER_AGENT = "TornTools-Local/0.6.1 TravelIntelligence"
CACHE_SECONDS = 20
TRAVEL_CACHE_SECONDS = 12
_stock_cache: dict[str, Any] = {"at": 0.0, "data": None}
_travel_cache: dict[str, Any] = {"at": 0.0, "data": None}

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
COUNTRY_CODES = {v.lower(): k for k, v in COUNTRY_NAMES.items()}
COUNTRY_CODES.update({"united arab emirates": "uae", "south africa": "saf", "cayman islands": "cay"})


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


def _country_code(name: Any) -> str | None:
    value = str(name or "").strip().lower()
    if not value or value in {"torn", "torn city"}:
        return None
    if value in COUNTRY_CODES:
        return COUNTRY_CODES[value]
    for code, label in COUNTRY_NAMES.items():
        if value == label.lower():
            return code
    return None


def _travel_state() -> dict:
    now = time.time()
    if _travel_cache["data"] is not None and now - float(_travel_cache["at"]) < TRAVEL_CACHE_SECONDS:
        return _travel_cache["data"]
    key = core_app._api_key
    if not key:
        data = {"available": False, "state": "UNKNOWN", "reason": "TORN_API_KEY is not loaded"}
        _travel_cache.update(at=now, data=data)
        return data
    q = urllib.parse.urlencode({"selections": "travel,basic", "key": key})
    try:
        raw = _get_json(f"{TORN_USER_URL}?{q}")
    except Exception as exc:
        data = {"available": False, "state": "UNKNOWN", "reason": f"Could not read Torn travel state: {exc}"}
        _travel_cache.update(at=now, data=data)
        return data
    if isinstance(raw.get("error"), dict):
        err = raw["error"]
        data = {"available": False, "state": "UNKNOWN", "reason": err.get("error") or err.get("message") or "Torn API error"}
        _travel_cache.update(at=now, data=data)
        return data

    travel = raw.get("travel") if isinstance(raw.get("travel"), dict) else {}
    status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
    state_raw = str(status.get("state") or "").strip()
    description = str(status.get("description") or "").strip()
    destination_name = str(travel.get("destination") or "").strip()
    destination_code = _country_code(destination_name)
    timestamp = int(travel.get("timestamp") or 0)
    departed = int(travel.get("departed") or 0)
    time_left = max(0, int(travel.get("time_left") or 0))
    method = travel.get("method")
    desc_lower = description.lower()
    is_return = "returning to torn" in desc_lower or "returning" in desc_lower
    is_traveling = state_raw.lower() == "traveling" or time_left > 0
    is_abroad = state_raw.lower() == "abroad"

    if is_traveling:
        state = "FLYING_HOME" if is_return else "FLYING_OUT"
    elif is_abroad:
        state = "ABROAD"
    else:
        state = "IN_TORN"

    data = {
        "available": True,
        "state": state,
        "status_state": state_raw,
        "description": description,
        "destination": destination_name or None,
        "destination_code": destination_code,
        "method": method,
        "timestamp": timestamp or None,
        "departed": departed or None,
        "time_left": time_left,
        "is_return": is_return,
        "is_traveling": is_traveling,
        "is_abroad": is_abroad,
        "checked_at": int(now),
    }
    _travel_cache.update(at=now, data=data)
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


@app.get("/api/travel-intelligence/state")
def travel_state():
    return _travel_state()


@app.get("/api/travel-intelligence")
def travel_intelligence(capacity: int = 17, speed: float = 1.0, sale_fee: float = 0.05):
    capacity = max(1, min(100, int(capacity)))
    speed = max(.25, min(3.0, float(speed)))
    sale_fee = max(0.0, min(.25, float(sale_fee)))
    data = _stock()
    travel = _travel_state()
    trips = [x for x in (_optimize_country(c, capacity, speed, sale_fee) for c in data.get("countries") or []) if x]
    trips.sort(key=lambda x: x["score"], reverse=True)
    destination_trip = None
    dest_code = travel.get("destination_code") if travel.get("available") else None
    if dest_code:
        destination_trip = next((x for x in trips if x.get("country") == dest_code), None)
    return {
        "generated_at": data.get("generatedAt"), "source": data.get("source"),
        "capacity": capacity, "speed": speed, "sale_fee": sale_fee,
        "best": trips[0] if trips else None, "trips": trips,
        "travel": travel, "destination_trip": destination_trip,
        "model": "TT-Travel-v1.1", "note": "Adjusted profit is conservative and confidence-weighted; it is not a guaranteed sale price.",
    }


@app.get("/api/travel-intelligence/history")
def travel_history(country: str, item_id: int, hours: int = 24):
    hours = max(1, min(48, int(hours)))
    q = urllib.parse.urlencode({"country": country, "itemId": int(item_id), "hours": hours})
    return _get_json(f"{HISTORY_URL}?{q}")
