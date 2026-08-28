from __future__ import annotations

import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import app as core_app
import bazaar_watch_runtime

app = bazaar_watch_runtime.app

STOCK_URL = "https://torn-intel.com/api/v1/public/foreign-stock"
HISTORY_URL = "https://torn-intel.com/api/v1/public/foreign-stock/history"
TORN_USER_URL = "https://api.torn.com/user/"
USER_AGENT = "TornTools-Local/0.6.4 TravelIntelligence"
CACHE_SECONDS = 20
TRAVEL_CACHE_SECONDS = 12
HISTORY_CACHE_SECONDS = 90
RESTOCK_HISTORY_HOURS = 48
RESTOCKS_PER_COUNTRY = 5
RESTOCKS_CURRENT_DESTINATION = 8
_stock_cache: dict[str, Any] = {"at": 0.0, "data": None}
_travel_cache: dict[str, Any] = {"at": 0.0, "data": None}
_history_cache: dict[tuple[str, int, int], dict[str, Any]] = {}

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


def _history(country: str, item_id: int, hours: int = RESTOCK_HISTORY_HOURS) -> dict:
    key = (str(country).lower(), int(item_id), int(hours))
    now = time.time()
    cached = _history_cache.get(key)
    if cached and now - float(cached.get("at") or 0) < HISTORY_CACHE_SECONDS:
        return cached.get("data") or {}
    q = urllib.parse.urlencode({"country": key[0], "itemId": key[1], "hours": key[2]})
    try:
        data = _get_json(f"{HISTORY_URL}?{q}")
    except Exception as exc:
        data = {"points": [], "_error": str(exc)}
    _history_cache[key] = {"at": now, "data": data}
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
    data = {"available": True, "state": state, "status_state": state_raw, "description": description,
            "destination": destination_name or None, "destination_code": destination_code, "method": method,
            "timestamp": timestamp or None, "departed": departed or None, "time_left": time_left,
            "is_return": is_return, "is_traveling": is_traveling, "is_abroad": is_abroad, "checked_at": int(now)}
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


def _timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _confidence(age_s: float, qty: int) -> tuple[float, str]:
    # Foreign quantity determines how many can be bought, not how trustworthy the
    # Torn market resale snapshot is. Using quantity as resale confidence caused
    # plentiful lower-profit items to outrank scarcer higher-profit items.
    score = max(0.45, min(1.0, 1.0 - max(0.0, age_s - 20.0) / 300.0))
    label = "HIGH" if score >= .82 else "MEDIUM" if score >= .62 else "LOW"
    return score, label


def _adjusted_sale(market: int, cost: int, age_s: float, qty: int) -> tuple[int, float, str]:
    conf, label = _confidence(age_s, qty)
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
        candidates.append({"item_id": int(raw.get("itemId") or 0), "name": raw.get("itemName") or "Unknown",
            "category": raw.get("category") or "", "stock": qty, "cost": cost, "market_value": market,
            "adjusted_sale": adjusted_sale, "net_sale": net_sale, "profit_each": profit,
            "headline_profit": int(raw.get("profitPerItem") or market - cost), "confidence": round(conf, 3),
            "confidence_label": conf_label})
    # Fill each slot with the highest expected net profit available. Confidence is
    # already represented by the conservative adjusted sale and trip-level score;
    # it must not reorder items within the same destination.
    candidates.sort(key=lambda x: (x["profit_each"], x["headline_profit"], x["market_value"]), reverse=True)
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
    return {"country": code, "country_name": COUNTRY_NAMES.get(code, code.upper()), "updated_at": country.get("updatedAt"),
        "age_seconds": round(age_s), "capacity": capacity, "filled": fill, "remaining": remaining,
        "one_way_minutes": round(base_minutes / max(.25, speed), 1), "round_trip_minutes": round(trip_minutes, 1),
        "spend": spend, "headline_profit": headline_profit, "expected_profit": expected_profit,
        "profit_per_hour": round(profit_hour), "confidence": round(confidence, 3),
        "confidence_label": "HIGH" if confidence >= .82 else "MEDIUM" if confidence >= .62 else "LOW",
        "score": round(score), "load": load, "restocks": []}


def _sold_out_candidates(country: dict, sale_fee: float, limit: int) -> list[dict]:
    ranked = []
    observed_out_at = _timestamp(country.get("updatedAt")) or time.time()
    for raw in country.get("items") or []:
        try:
            qty = max(0, int(raw.get("quantity") or 0)); cost = max(0, int(raw.get("cost") or 0)); market = max(0, int(raw.get("marketValue") or 0)); item_id = int(raw.get("itemId") or 0)
        except (TypeError, ValueError):
            continue
        if qty != 0 or not item_id or cost <= 0 or market <= 0:
            continue
        net_profit = int(market * (1.0 - sale_fee)) - cost
        if net_profit <= 0:
            continue
        desirability = net_profit + int(market * 0.08)
        ranked.append({"item_id": item_id, "name": raw.get("itemName") or "Unknown", "cost": cost,
            "market_value": market, "profit_each": net_profit, "desirability": desirability, "observed_out_at": round(observed_out_at)})
    ranked.sort(key=lambda x: (x["desirability"], x["profit_each"], x["market_value"]), reverse=True)
    return ranked[:limit]


def _estimate_restock(country: str, item: dict, arrival_ts: float) -> dict:
    history = _history(country, item["item_id"], RESTOCK_HISTORY_HOURS)
    raw_points = history.get("points") if isinstance(history, dict) else []
    points = []
    for raw in raw_points or []:
        ts = _timestamp(raw.get("t")) if isinstance(raw, dict) else None
        if ts is None: continue
        try: qty = max(0, int(raw.get("quantity") or 0))
        except (TypeError, ValueError): continue
        points.append((ts, qty))
    points.sort(key=lambda x: x[0])
    zero_start = None; completed = []; previous_qty = None
    for ts, qty in points:
        if qty == 0 and (previous_qty is None or previous_qty > 0): zero_start = ts
        elif qty > 0 and previous_qty == 0 and zero_start is not None:
            duration = ts - zero_start
            if 30 <= duration <= 12 * 3600: completed.append(duration)
            zero_start = None
        previous_qty = qty
    anchor_quality = "HISTORY"
    current_feed_anchor = float(item.get("observed_out_at") or time.time())
    if zero_start is None and (not points or points[-1][1] > 0):
        zero_start = current_feed_anchor; anchor_quality = "CURRENT_FEED"
    result = {**item, "sample_cycles": len(completed), "history_points": len(points), "status": "LEARNING",
        "confidence_label": "LOW", "estimated_at": None, "arrival_ts": round(arrival_ts), "seconds_from_landing": None,
        "stockout_since": zero_start, "anchor_quality": anchor_quality}
    if not completed: return result
    median_delay = float(statistics.median(completed)); estimate = zero_start + median_delay
    if len(completed) >= 3:
        spread = statistics.pstdev(completed) / max(1.0, median_delay); confidence = "HIGH" if spread <= .22 else "MEDIUM" if spread <= .45 else "LOW"
    elif len(completed) == 2:
        spread = abs(completed[0] - completed[1]) / max(1.0, median_delay); confidence = "MEDIUM" if spread <= .35 else "LOW"
    else:
        spread = None; confidence = "LOW"
    if anchor_quality == "CURRENT_FEED" and confidence == "HIGH": confidence = "MEDIUM"
    now = time.time()
    result.update({"status": "OVERDUE" if estimate <= now else "ESTIMATED", "confidence_label": confidence,
        "estimated_at": round(estimate), "median_zero_seconds": round(median_delay), "seconds_from_landing": round(estimate-arrival_ts),
        "overdue_seconds": round(max(0.0, now-estimate)), "dispersion": round(spread,3) if spread is not None else None})
    return result


def _attach_restocks(trips: list[dict], countries: list[dict], travel: dict, speed: float, sale_fee: float) -> None:
    trip_by_code = {t["country"]: t for t in trips}; country_by_code = {str(c.get("country") or "").lower(): c for c in countries}
    jobs = []; now = time.time(); current_code = travel.get("destination_code") if travel.get("available") else None
    for code, trip in trip_by_code.items():
        country = country_by_code.get(code)
        if not country: continue
        limit = RESTOCKS_CURRENT_DESTINATION if code == current_code else RESTOCKS_PER_COUNTRY
        candidates = _sold_out_candidates(country, sale_fee, limit)
        if not candidates: continue
        if code == current_code and travel.get("state") == "FLYING_OUT" and travel.get("timestamp"): arrival_ts = float(travel["timestamp"])
        elif code == current_code and travel.get("state") == "ABROAD": arrival_ts = now
        else: arrival_ts = now + float(trip["one_way_minutes"]) * 60.0
        for item in candidates: jobs.append((code, item, arrival_ts))
    if not jobs: return
    results: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {pool.submit(_estimate_restock, code, item, arrival_ts): code for code, item, arrival_ts in jobs}
        for future in as_completed(future_map):
            code = future_map[future]
            try: result = future.result()
            except Exception: continue
            if result.get("status") in {"ESTIMATED", "OVERDUE", "LEARNING"}: results.setdefault(code, []).append(result)
    for code, rows in results.items():
        rows.sort(key=lambda x: (-int(x.get("desirability") or 0), abs(int(x.get("seconds_from_landing") or 10**9)), -int(x.get("sample_cycles") or 0)))
        if code in trip_by_code: trip_by_code[code]["restocks"] = rows


@app.get("/api/travel-intelligence/state")
def travel_state(): return _travel_state()


@app.get("/api/travel-intelligence")
def travel_intelligence(capacity: int = 17, speed: float = 1.0, sale_fee: float = 0.05):
    capacity = max(1, min(100, int(capacity))); speed = max(.25, min(3.0, float(speed))); sale_fee = max(0.0, min(.25, float(sale_fee)))
    data = _stock(); travel = _travel_state(); countries = data.get("countries") or []
    trips = [x for x in (_optimize_country(c, capacity, speed, sale_fee) for c in countries) if x]
    trips.sort(key=lambda x: x["score"], reverse=True); _attach_restocks(trips, countries, travel, speed, sale_fee)
    destination_trip = None; dest_code = travel.get("destination_code") if travel.get("available") else None
    if dest_code: destination_trip = next((x for x in trips if x.get("country") == dest_code), None)
    return {"generated_at": data.get("generatedAt"), "source": data.get("source"), "capacity": capacity, "speed": speed,
        "sale_fee": sale_fee, "best": trips[0] if trips else None, "trips": trips, "travel": travel, "destination_trip": destination_trip,
        "model": "TT-Travel-v1.7", "note": "Load slots are filled by highest conservative net profit available; restock estimates are learned from observed history."}


@app.get("/api/travel-intelligence/history")
def travel_history(country: str, item_id: int, hours: int = 24):
    hours = max(1, min(48, int(hours))); return _history(country, int(item_id), hours)
