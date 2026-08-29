"""Robust Travel Intelligence runtime.

Wraps travel_intelligence with a state machine so flight direction and the
current foreign destination do not depend on a single Torn API field.
Also exposes a direct destination-stock fallback for the live landing target.
"""
from __future__ import annotations

import re
import time
from typing import Any

import travel_intelligence as ti

app = ti.app
_original_travel_state = ti._travel_state
_original_optimize_country = ti._optimize_country
_last: dict[str, Any] = {"state": None, "destination_code": None, "foreign_country_code": None, "timestamp": None, "departed": None}

# Exact one-way Torn travel times in minutes for the normal travel choices.
# The main optimizer historically used Airstrip minutes plus a free-form speed
# multiplier. Keep the API backward compatible, but map the UI's discrete values
# to the real Torn modes and country-specific times instead.
TRAVEL_MODE_MINUTES = {
    "standard": {"mex": 26, "cay": 35, "can": 41, "haw": 134, "uk": 159, "arg": 167, "swi": 175, "jap": 225, "chi": 242, "uae": 271, "saf": 297},
    "airstrip": {"mex": 18, "cay": 25, "can": 29, "haw": 94, "uk": 111, "arg": 117, "swi": 123, "jap": 158, "chi": 169, "uae": 190, "saf": 208},
    "wlt":      {"mex": 13, "cay": 18, "can": 20, "haw": 67, "uk": 80, "arg": 83, "swi": 88, "jap": 113, "chi": 121, "uae": 135, "saf": 149},
    "business": {"mex": 8,  "cay": 11, "can": 12, "haw": 40, "uk": 48, "arg": 50, "swi": 53, "jap": 68,  "chi": 72,  "uae": 81,  "saf": 89},
}
TRAVEL_MODE_SENTINELS = {0.5: "standard", 1.0: "airstrip", 2.0: "wlt", 3.0: "business"}
TRAVEL_MODE_LABELS = {"standard": "Standard", "airstrip": "Airstrip", "wlt": "WLT Private", "business": "Business Class"}

# Correct the shared Airstrip table too. This is used for fallback return-time
# projections while abroad and was previously wrong for several long-haul routes.
ti.AIRSTRIP_MINUTES.update(TRAVEL_MODE_MINUTES["airstrip"])


def _travel_mode_for_speed(speed: float) -> str | None:
    value = float(speed)
    for sentinel, mode in TRAVEL_MODE_SENTINELS.items():
        if abs(value - sentinel) < 0.001:
            return mode
    return None


def _optimize_country_with_mode(country: dict, capacity: int, speed: float, sale_fee: float) -> dict | None:
    mode = _travel_mode_for_speed(speed)
    code = str(country.get("country") or "").lower()
    desired = TRAVEL_MODE_MINUTES.get(mode or "", {}).get(code)
    if not desired:
        return _original_optimize_country(country, capacity, speed, sale_fee)

    base = float(ti.AIRSTRIP_MINUTES.get(code) or desired)
    effective_speed = base / float(desired)
    result = _original_optimize_country(country, capacity, effective_speed, sale_fee)
    if result:
        result["travel_mode"] = mode
        result["travel_mode_label"] = TRAVEL_MODE_LABELS[mode]
        result["one_way_minutes"] = float(desired)
        result["round_trip_minutes"] = float(desired * 2)
        result["profit_per_hour"] = round(float(result.get("expected_profit") or 0) / max(1 / 60, (desired * 2) / 60.0))
        fill = float(result.get("filled") or 0); cap = max(1.0, float(result.get("capacity") or capacity)); conf = float(result.get("confidence") or 0)
        result["score"] = round(result["profit_per_hour"] * (0.70 + 0.30 * fill / cap) * (0.72 + 0.28 * conf))
    return result


ti._optimize_country = _optimize_country_with_mode


def _route_parts(description: str) -> tuple[str | None, str | None]:
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:\s*[\(\[]|$)", description or "", re.I)
    if not m:
        return None, None
    return m.group(1).strip(), m.group(2).strip()


def _enhanced_travel_state() -> dict:
    raw = dict(_original_travel_state() or {})
    if not raw.get("available"):
        return raw

    now = int(time.time())
    api_state = str(raw.get("status_state") or "").strip().lower()
    description = str(raw.get("description") or "").strip()
    desc = description.lower()
    destination_name = str(raw.get("destination") or "").strip()
    destination_lower = destination_name.lower()
    timestamp = int(raw.get("timestamp") or 0)
    departed = int(raw.get("departed") or 0)
    time_left = max(0, int(raw.get("time_left") or 0))
    previous = _last.get("state")

    route_from, route_to = _route_parts(description)
    route_from_code = ti._country_code(route_from) if route_from else None
    route_to_code = ti._country_code(route_to) if route_to else None
    api_destination_code = raw.get("destination_code")

    traveling = api_state == "traveling" or time_left > 0 or timestamp > now
    abroad = api_state == "abroad"

    destination_is_torn = destination_lower in {"torn", "torn city"}
    route_to_torn = str(route_to or "").strip().lower() in {"torn", "torn city"}
    route_from_torn = str(route_from or "").strip().lower() in {"torn", "torn city"}
    explicit_return = "returning to torn" in desc or desc.startswith("returning") or destination_is_torn or route_to_torn
    transition_return = traveling and previous == "ABROAD"
    same_return_flight = traveling and previous == "FLYING_HOME" and departed > 0 and departed == int(_last.get("departed") or 0)

    if traveling:
        if explicit_return or transition_return or same_return_flight:
            state = "FLYING_HOME"
            if destination_is_torn: direction_source = "travel.destination_torn"
            elif route_to_torn: direction_source = "status_route_to_torn"
            elif "returning" in desc: direction_source = "status_description"
            elif transition_return: direction_source = "state_transition"
            else: direction_source = "sticky_return_leg"
        else:
            state = "FLYING_OUT"
            direction_source = "status_route_from_torn" if route_from_torn and route_to_code else "traveling_default_outbound"
    elif abroad:
        state = "ABROAD"; direction_source = "api_status_abroad"
    else:
        state = "IN_TORN"; direction_source = "api_status_torn"

    if state == "FLYING_OUT":
        destination_code = route_to_code or api_destination_code
        foreign_country_code = destination_code
        if route_to_code: raw["destination"] = ti.COUNTRY_NAMES.get(route_to_code, route_to)
    elif state == "FLYING_HOME":
        foreign_country_code = route_from_code or _last.get("foreign_country_code") or _last.get("destination_code")
        destination_code = None
    elif state == "ABROAD":
        destination_code = api_destination_code or route_to_code or _last.get("foreign_country_code")
        foreign_country_code = destination_code
    else:
        destination_code = None; foreign_country_code = None

    available_at = None; available_source = None; available_exact = False
    if state == "IN_TORN":
        available_at = now; available_source = "in_torn_now"; available_exact = True
    elif state == "FLYING_HOME" and timestamp:
        available_at = timestamp; available_source = "travel.timestamp"; available_exact = True
    elif state == "FLYING_OUT" and timestamp:
        leg_seconds = timestamp - departed if departed and timestamp > departed else 0
        if leg_seconds:
            available_at = timestamp + leg_seconds; available_source = "outbound_leg_projection"
    elif state == "ABROAD":
        minutes = ti.AIRSTRIP_MINUTES.get(foreign_country_code) if foreign_country_code else None
        if minutes:
            available_at = now + int(minutes * 60); available_source = "abroad_return_projection"

    raw.update({
        "state": state, "destination_code": destination_code, "foreign_country_code": foreign_country_code,
        "is_return": state == "FLYING_HOME", "is_traveling": traveling, "is_abroad": state == "ABROAD",
        "direction_source": direction_source, "available_in_torn_at": available_at,
        "available_in_torn_source": available_source, "available_in_torn_exact": available_exact,
        "diagnostic": {"interpreted_state": state, "previous_state": previous, "direction_source": direction_source,
            "api_status_state": raw.get("status_state"), "api_description": raw.get("description"),
            "api_travel_destination": destination_name or None, "route_from": route_from, "route_to": route_to,
            "destination_code": destination_code, "foreign_country_code": foreign_country_code,
            "travel_timestamp": timestamp or None, "travel_departed": departed or None, "travel_time_left": time_left,
            "available_in_torn_at": available_at, "available_in_torn_source": available_source,
            "available_in_torn_exact": available_exact}}
    _last.update(state=state, destination_code=destination_code, foreign_country_code=foreign_country_code,
                 timestamp=timestamp or None, departed=departed or None)
    return raw


ti._travel_state = _enhanced_travel_state


def _destination_stock_payload(capacity: int = 17, sale_fee: float = 0.05) -> dict:
    """Read the current foreign feed directly for the player's active destination."""
    travel = _enhanced_travel_state()
    code = travel.get("foreign_country_code") or travel.get("destination_code")
    if not code:
        return {"ok": False, "reason": "foreign destination not resolved", "travel": travel, "country": None, "items": []}
    data = ti._stock()
    countries = data.get("countries") or []
    country = next((c for c in countries if str(c.get("country") or "").lower() == str(code).lower()), None)
    if not country:
        return {"ok": False, "reason": f"{code} missing from foreign-stock feed", "travel": travel, "country": code, "items": []}

    age_s = ti._age_seconds(country.get("updatedAt"))
    rows = []
    for raw in country.get("items") or []:
        try:
            qty = max(0, int(raw.get("quantity") or 0)); cost = max(0, int(raw.get("cost") or 0)); market = max(0, int(raw.get("marketValue") or 0))
        except (TypeError, ValueError):
            continue
        if qty <= 0 or cost <= 0:
            continue
        adjusted_sale, conf, conf_label = ti._adjusted_sale(market, cost, age_s, qty) if market > 0 else (cost, .45, "LOW")
        net_sale = int(adjusted_sale * (1.0 - max(0.0, min(.25, float(sale_fee)))))
        conservative_profit = net_sale - cost
        headline_profit = int(raw.get("profitPerItem") or (market - cost))
        rows.append({"item_id": int(raw.get("itemId") or 0), "name": raw.get("itemName") or "Unknown",
            "stock": qty, "cost": cost, "market_value": market, "headline_profit_each": headline_profit,
            "profit_each": conservative_profit, "confidence": round(conf, 3), "confidence_label": conf_label})

    profitable = [x for x in rows if x["profit_each"] > 0]
    candidates = profitable if profitable else [x for x in rows if x["headline_profit_each"] > 0]
    candidates.sort(key=lambda x: (x["profit_each"] if profitable else x["headline_profit_each"], x["stock"]), reverse=True)
    remaining = max(1, min(100, int(capacity)))
    load = []
    for x in candidates:
        take = min(remaining, x["stock"])
        if take <= 0: continue
        row = dict(x); row["buy"] = take; load.append(row); remaining -= take
        if remaining <= 0: break
    return {"ok": True, "country": code, "country_name": ti.COUNTRY_NAMES.get(code, str(code).upper()),
        "updated_at": country.get("updatedAt"), "age_seconds": round(age_s), "stocked_count": len(rows),
        "profitable_count": len(profitable), "used_headline_fallback": bool(candidates and not profitable),
        "load": load, "items": rows, "travel": travel}


@app.get("/api/travel-intelligence/destination-stock")
def destination_stock(capacity: int = 17, sale_fee: float = 0.05):
    return _destination_stock_payload(capacity, sale_fee)
