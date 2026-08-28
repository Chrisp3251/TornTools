"""Robust Travel Intelligence runtime.

Wraps travel_intelligence with a state machine so flight direction and the
current foreign destination do not depend on a single Torn API field.
"""
from __future__ import annotations

import re
import time
from typing import Any

import travel_intelligence as ti

app = ti.app
_original_travel_state = ti._travel_state
_last: dict[str, Any] = {"state": None, "destination_code": None, "foreign_country_code": None, "timestamp": None, "departed": None}


def _route_parts(description: str) -> tuple[str | None, str | None]:
    """Parse strings such as 'Traveling from Torn to Canada'."""
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)\s*$", description or "", re.I)
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
    explicit_return = (
        "returning to torn" in desc
        or desc.startswith("returning")
        or destination_is_torn
        or route_to_torn
    )
    transition_return = traveling and previous == "ABROAD"
    same_return_flight = (
        traveling and previous == "FLYING_HOME" and departed > 0
        and departed == int(_last.get("departed") or 0)
    )

    if traveling:
        if explicit_return or transition_return or same_return_flight:
            state = "FLYING_HOME"
            if destination_is_torn:
                direction_source = "travel.destination_torn"
            elif route_to_torn:
                direction_source = "status_route_to_torn"
            elif "returning" in desc:
                direction_source = "status_description"
            elif transition_return:
                direction_source = "state_transition"
            else:
                direction_source = "sticky_return_leg"
        else:
            state = "FLYING_OUT"
            direction_source = "status_route_from_torn" if route_from_torn and route_to_code else "traveling_default_outbound"
    elif abroad:
        state = "ABROAD"
        direction_source = "api_status_abroad"
    else:
        state = "IN_TORN"
        direction_source = "api_status_torn"

    # Canonical foreign-country code. For outbound flights the route's TO country
    # wins. For return flights the route's FROM country wins. This avoids losing
    # Canada when Torn reports travel.destination='Torn' on the return leg, and
    # also recovers the destination when the travel destination field is stale.
    if state == "FLYING_OUT":
        destination_code = route_to_code or api_destination_code
        foreign_country_code = destination_code
        if route_to_code:
            raw["destination"] = ti.COUNTRY_NAMES.get(route_to_code, route_to)
    elif state == "FLYING_HOME":
        foreign_country_code = route_from_code or _last.get("foreign_country_code") or _last.get("destination_code")
        destination_code = None
    elif state == "ABROAD":
        destination_code = api_destination_code or route_to_code or _last.get("foreign_country_code")
        foreign_country_code = destination_code
    else:
        destination_code = None
        foreign_country_code = None

    available_at = None
    available_source = None
    available_exact = False
    if state == "IN_TORN":
        available_at = now; available_source = "in_torn_now"; available_exact = True
    elif state == "FLYING_HOME" and timestamp:
        available_at = timestamp; available_source = "travel.timestamp"; available_exact = True
    elif state == "FLYING_OUT" and timestamp:
        leg_seconds = timestamp - departed if departed and timestamp > departed else 0
        if leg_seconds:
            available_at = timestamp + leg_seconds
            available_source = "outbound_leg_projection"
    elif state == "ABROAD":
        minutes = ti.AIRSTRIP_MINUTES.get(foreign_country_code) if foreign_country_code else None
        if minutes:
            available_at = now + int(minutes * 60)
            available_source = "abroad_return_projection"

    raw.update({
        "state": state,
        "destination_code": destination_code,
        "foreign_country_code": foreign_country_code,
        "is_return": state == "FLYING_HOME",
        "is_traveling": traveling,
        "is_abroad": state == "ABROAD",
        "direction_source": direction_source,
        "available_in_torn_at": available_at,
        "available_in_torn_source": available_source,
        "available_in_torn_exact": available_exact,
        "diagnostic": {
            "interpreted_state": state,
            "previous_state": previous,
            "direction_source": direction_source,
            "api_status_state": raw.get("status_state"),
            "api_description": raw.get("description"),
            "api_travel_destination": destination_name or None,
            "route_from": route_from,
            "route_to": route_to,
            "destination_code": destination_code,
            "foreign_country_code": foreign_country_code,
            "travel_timestamp": timestamp or None,
            "travel_departed": departed or None,
            "travel_time_left": time_left,
            "available_in_torn_at": available_at,
            "available_in_torn_source": available_source,
            "available_in_torn_exact": available_exact,
        },
    })
    _last.update(state=state, destination_code=destination_code, foreign_country_code=foreign_country_code,
                 timestamp=timestamp or None, departed=departed or None)
    return raw


ti._travel_state = _enhanced_travel_state
