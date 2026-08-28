"""Robust Travel Intelligence runtime.

Wraps travel_intelligence with a small state machine so return flights do not
rely only on one Torn status-description phrase. Exposes a canonical
available_in_torn_at value for every frontend planner.
"""
from __future__ import annotations

import time
from typing import Any

import travel_intelligence as ti

app = ti.app
_original_travel_state = ti._travel_state
_last: dict[str, Any] = {"state": None, "destination_code": None, "timestamp": None, "departed": None}


def _enhanced_travel_state() -> dict:
    raw = dict(_original_travel_state() or {})
    if not raw.get("available"):
        return raw

    now = int(time.time())
    api_state = str(raw.get("status_state") or "").strip().lower()
    desc = str(raw.get("description") or "").strip().lower()
    destination_name = str(raw.get("destination") or "").strip().lower()
    timestamp = int(raw.get("timestamp") or 0)
    departed = int(raw.get("departed") or 0)
    time_left = max(0, int(raw.get("time_left") or 0))
    destination_code = raw.get("destination_code")
    previous = _last.get("state")

    traveling = api_state == "traveling" or time_left > 0 or timestamp > now
    abroad = api_state == "abroad"

    # Direction evidence, strongest first. Torn currently may report a return as
    # "Traveling from Canada to Torn" with travel.destination == "Torn", so
    # destination/route text are authoritative return signals too.
    destination_is_torn = destination_name in {"torn", "torn city"}
    route_to_torn = " to torn" in desc or desc.endswith("to torn") or " to torn city" in desc
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
            direction_source = "traveling_default_outbound"
    elif abroad:
        state = "ABROAD"
        direction_source = "api_status_abroad"
    else:
        state = "IN_TORN"
        direction_source = "api_status_torn"

    available_at = None
    available_source = None
    available_exact = False

    if state == "IN_TORN":
        available_at = now
        available_source = "in_torn_now"
        available_exact = True
    elif state == "FLYING_HOME" and timestamp:
        # travel.timestamp is the landing time for the CURRENT flight leg.
        available_at = timestamp
        available_source = "travel.timestamp"
        available_exact = True
    elif state == "FLYING_OUT" and timestamp:
        leg_seconds = timestamp - departed if departed and timestamp > departed else 0
        if leg_seconds:
            available_at = timestamp + leg_seconds
            available_source = "outbound_leg_projection"
            available_exact = False
    elif state == "ABROAD":
        code = destination_code or _last.get("destination_code")
        minutes = ti.AIRSTRIP_MINUTES.get(code) if code else None
        if minutes:
            available_at = now + int(minutes * 60)
            available_source = "abroad_return_projection"
            available_exact = False

    raw.update({
        "state": state,
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
            "travel_destination": raw.get("destination"),
            "travel_timestamp": timestamp or None,
            "travel_departed": departed or None,
            "travel_time_left": time_left,
            "available_in_torn_at": available_at,
            "available_in_torn_source": available_source,
            "available_in_torn_exact": available_exact,
        },
    })

    _last.update(state=state, destination_code=destination_code, timestamp=timestamp or None, departed=departed or None)
    return raw


# Existing endpoints resolve this global at request time, so replacing it here
# upgrades /state and /api/travel-intelligence without duplicating routes.
ti._travel_state = _enhanced_travel_state
