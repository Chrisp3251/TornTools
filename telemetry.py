import asyncio
import json
import sqlite3
import time
from typing import Any

from pydantic import BaseModel

import sniper
import server
from sniper import app, DB_PATH


# -----------------------------------------------------------------------------
# Sniper alert hardening / telemetry v2
# -----------------------------------------------------------------------------
# Keep this layer deliberately separate from the market learner. It changes how
# actionable sniper opportunities are surfaced/measured, not how deal events,
# recoveries, baselines, or evidence scores are learned.

MIN_LIVE_EDGE_PCT = 5.0
OPPORTUNITY_REARM_SECONDS = 20.0
MATERIAL_PRICE_IMPROVEMENT_PCT = 5.0
SNIPER_MIN_OPPORTUNITIES_PER_HOUR = 1.0
SESSION_STARTED_AT = time.time()


class TelemetryPayload(BaseModel):
    item_id: int
    source: str
    event_type: str = "alert"
    price: int | None = None
    max_price: int | None = None
    baseline: int | None = None
    edge_pct: float | None = None
    cache_age_seconds: int | None = None
    cache_timestamp: int | None = None
    signature: str | None = None
    metadata: dict[str, Any] | None = None


def _init_telemetry_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_telemetry(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                item_id INTEGER NOT NULL,
                item_name TEXT,
                source TEXT NOT NULL,
                event_type TEXT NOT NULL,
                price INTEGER,
                max_price INTEGER,
                baseline INTEGER,
                edge_pct REAL,
                cache_age_seconds INTEGER,
                cache_timestamp INTEGER,
                signature TEXT,
                snapshot_id INTEGER,
                metadata_json TEXT
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_alert_telemetry_item_ts ON alert_telemetry(item_id,ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_alert_telemetry_signature ON alert_telemetry(signature)")


def _item_name(item_id: int) -> str:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT name FROM sniper_targets WHERE item_id=?", (item_id,)).fetchone()
    if row and row[0]:
        return str(row[0])
    meta = sniper.DISCOVERY_ITEMS.get(item_id) or {}
    return meta.get("name") or f"Item {item_id}"


def _record_event(*, item_id: int, source: str, event_type: str, price=None, max_price=None,
                  baseline=None, edge_pct=None, cache_age_seconds=None, cache_timestamp=None,
                  signature=None, snapshot_id=None, metadata=None):
    now = time.time()
    with sqlite3.connect(DB_PATH) as c:
        if signature:
            duplicate = c.execute(
                "SELECT id FROM alert_telemetry WHERE signature=? AND source=? AND event_type=? LIMIT 1",
                (signature, source, event_type),
            ).fetchone()
            if duplicate:
                return False
        c.execute(
            """
            INSERT INTO alert_telemetry(
                ts,item_id,item_name,source,event_type,price,max_price,baseline,edge_pct,
                cache_age_seconds,cache_timestamp,signature,snapshot_id,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                now, item_id, _item_name(item_id), source, event_type, price, max_price,
                baseline, edge_pct, cache_age_seconds, cache_timestamp, signature,
                snapshot_id, json.dumps(metadata or {}, separators=(",", ":")),
            ),
        )
    return True


def _profile_for(item_id: int, name: str | None = None):
    try:
        return server.evidence_profile(int(item_id), name)
    except Exception:
        return {}


def _live_edge(profile, price):
    baseline = profile.get("rolling_baseline")
    if not baseline or not price:
        return None
    return (float(baseline) - float(price)) / float(baseline) * 100.0


def _effective_max(configured_max: int, profile: dict):
    """Respect the user's cap, but never buy into a learned zero/negative edge."""
    configured_max = max(1, int(configured_max))
    baseline = profile.get("rolling_baseline")
    if not baseline or float(baseline) <= 0:
        return configured_max
    edge_cap = int(float(baseline) * (1.0 - MIN_LIVE_EDGE_PCT / 100.0))
    return max(1, min(configured_max, edge_cap))


# -----------------------------------------------------------------------------
# Candidate activity-gate correction
# -----------------------------------------------------------------------------
# Generic listing churn is useful, but a market that repeatedly produces real,
# recovering bargains should not fail only because unrelated churn is low.

_original_evidence_profile = server.evidence_profile
_original_sniper_requirements_text = server._sniper_requirements_text


def _evidence_profile_with_opportunity_gate(item_id: int, name: str | None = None):
    profile = _original_evidence_profile(item_id, name)
    activity_score = float(profile.get("activity_score") or 0)
    opportunity_rate = float(profile.get("opportunities_per_hour") or 0)
    activity_gate_met = (
        activity_score >= server.SNIPER_MIN_ACTIVITY
        or opportunity_rate >= SNIPER_MIN_OPPORTUNITIES_PER_HOUR
    )
    profile["activity_gate_met"] = activity_gate_met
    profile["activity_gate_source"] = (
        "market activity" if activity_score >= server.SNIPER_MIN_ACTIVITY
        else "proven opportunity frequency" if opportunity_rate >= SNIPER_MIN_OPPORTUNITIES_PER_HOUR
        else "not met"
    )

    candidate = (
        int(profile.get("observations") or 0) >= server.SNIPER_MIN_SAMPLES
        and int(profile.get("independent_events") or 0) >= server.SNIPER_MIN_EVENTS
        and int(profile.get("recovered_events") or 0) >= server.SNIPER_MIN_RECOVERED
        and float(profile.get("recovery_rate") or 0) >= server.SNIPER_MIN_RECOVERY_RATE * 100.0
        and float(profile.get("false_positive_rate") or 0) <= server.SNIPER_MAX_FALSE_POSITIVE_RATE * 100.0
        and float(profile.get("median_edge_pct") or 0) >= server.SNIPER_MIN_MEDIAN_EDGE
        and activity_gate_met
        and float(profile.get("sniper_score") or 0) >= server.SNIPER_MIN_SCORE
    )
    profile["sniper_candidate"] = candidate
    if candidate:
        profile["stage"] = "SNIPER CANDIDATE"
    return profile


def _sniper_requirements_text_v2():
    return (
        f"{server.SNIPER_MIN_SAMPLES}+ snapshots, {server.SNIPER_MIN_EVENTS}+ independent events, "
        f"{server.SNIPER_MIN_RECOVERED}+ recoveries, {int(server.SNIPER_MIN_RECOVERY_RATE*100)}%+ recovery rate, "
        f"<= {int(server.SNIPER_MAX_FALSE_POSITIVE_RATE*100)}% false positives, "
        f"{server.SNIPER_MIN_MEDIAN_EDGE:.0f}%+ median edge, "
        f"activity {server.SNIPER_MIN_ACTIVITY}+ OR {SNIPER_MIN_OPPORTUNITIES_PER_HOUR:.1f}+ proven opportunities/hr, "
        f"score {server.SNIPER_MIN_SCORE:.0f}+"
    )


server.evidence_profile = _evidence_profile_with_opportunity_gate
server._sniper_requirements_text = _sniper_requirements_text_v2


# -----------------------------------------------------------------------------
# Dynamic live edge cap for dashboard + userscript sniper targets
# -----------------------------------------------------------------------------

_original_target_dict = sniper._target_dict


def _edge_gated_target_dict(row):
    target = _original_target_dict(row)
    configured_max = int(target["max_price"])
    profile = _profile_for(int(target["item_id"]), target.get("name"))
    effective_max = _effective_max(configured_max, profile)
    target["configured_max_price"] = configured_max
    target["max_price"] = effective_max
    target["effective_max_price"] = effective_max
    target["live_edge_gate_pct"] = MIN_LIVE_EDGE_PCT
    target["learned_baseline"] = profile.get("rolling_baseline")
    target["max_limited_by_live_edge"] = effective_max < configured_max
    return target


sniper._target_dict = _edge_gated_target_dict


# -----------------------------------------------------------------------------
# Dashboard alert signature stabilization
# -----------------------------------------------------------------------------
# web/sniper.js already suppresses an unchanged signature. Hidden Deals used the
# API cache timestamp and floor quantity in that signature, though, so the same
# economic opportunity could look "new" every few seconds. Stabilize only the
# returned display signature fields while an actionable opportunity remains.
# Raw market snapshots are saved before discovery_result runs and remain intact.

_original_discovery_result = sniper.app_module.discovery_result
_display_opportunities: dict[int, dict[str, Any]] = {}


def _hardened_discovery_result(item_id, meta, listings, avg, cache=None):
    result = _original_discovery_result(item_id, meta, listings, avg, cache)
    if not meta.get("sniper") or result.get("error"):
        return result

    low = result.get("lowest")
    configured_max = int(meta.get("sniper_max_price") or 0)
    profile = _profile_for(int(item_id), meta.get("name"))
    effective_max = _effective_max(configured_max, profile) if configured_max > 0 else 0
    edge = _live_edge(profile, low)
    actionable = bool(
        low and effective_max and int(low) <= effective_max
        and (edge is None or edge >= MIN_LIVE_EDGE_PCT)
    )
    result["sniper_effective_max"] = effective_max or None
    result["sniper_live_edge_pct"] = round(edge, 3) if edge is not None else None
    result["sniper_actionable"] = actionable

    now = time.time()
    state = _display_opportunities.get(int(item_id))
    if actionable:
        materially_better = bool(
            state and state.get("active") and state.get("price")
            and float(low) <= float(state["price"]) * (1.0 - MATERIAL_PRICE_IMPROVEMENT_PCT / 100.0)
        )
        if not state or not state.get("active") or materially_better:
            state = {
                "active": True,
                "price": int(low),
                "stable_cache_timestamp": result.get("cache_timestamp") or int(now),
                "stable_qty_floor": result.get("qty_floor"),
                "last_actionable_at": now,
                "nonqualifying_since": None,
            }
            _display_opportunities[int(item_id)] = state
        else:
            state["last_actionable_at"] = now
            state["nonqualifying_since"] = None
        result["cache_timestamp"] = state.get("stable_cache_timestamp")
        result["qty_floor"] = state.get("stable_qty_floor")
    elif state and state.get("active"):
        if state.get("nonqualifying_since") is None:
            state["nonqualifying_since"] = now
        elif now - float(state["nonqualifying_since"]) >= OPPORTUNITY_REARM_SECONDS:
            state["active"] = False

    return result


sniper.app_module.discovery_result = _hardened_discovery_result


# Re-sync after installing the dynamic target adapter so Discovery metadata uses
# the effective live edge cap immediately after this module loads.
try:
    sniper._sync_sniper_targets_into_discovery()
except Exception:
    pass


# -----------------------------------------------------------------------------
# Opportunity-level API telemetry
# -----------------------------------------------------------------------------


def _current_max_snapshot_id() -> int:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT COALESCE(MAX(id),0) FROM market_snapshots").fetchone()
    return int(row[0] or 0)


async def _snapshot_telemetry_loop():
    """Measure economic opportunities, not every repeated qualifying snapshot."""
    last_snapshot_id = _current_max_snapshot_id()
    states: dict[int, dict[str, Any]] = {}
    sequence: dict[int, int] = {}

    while True:
        try:
            with sqlite3.connect(DB_PATH) as c:
                rows = c.execute(
                    """
                    SELECT s.id,s.ts,s.item_id,s.lowest,s.qty_floor,s.next_higher,s.average_price,
                           t.name,t.max_price
                    FROM market_snapshots s
                    JOIN sniper_targets t ON t.item_id=s.item_id AND t.enabled=1
                    WHERE s.id>? ORDER BY s.id ASC LIMIT 500
                    """,
                    (last_snapshot_id,),
                ).fetchall()

            for row in rows:
                snapshot_id, snapshot_ts, item_id, low, qty, next_higher, avg, name, configured_max = row
                item_id = int(item_id)
                last_snapshot_id = max(last_snapshot_id, int(snapshot_id))
                profile = _profile_for(item_id, name)
                baseline = profile.get("rolling_baseline") or avg or next_higher
                edge_pct = None
                if baseline and low:
                    edge_pct = round((float(baseline) - float(low)) / float(baseline) * 100.0, 3)
                effective_max = _effective_max(int(configured_max), profile)
                actionable = bool(
                    low and int(low) <= effective_max
                    and (edge_pct is None or edge_pct >= MIN_LIVE_EDGE_PCT)
                )
                state = states.get(item_id)

                if actionable:
                    materially_better = bool(
                        state and state.get("active") and state.get("price")
                        and float(low) <= float(state["price"]) * (1.0 - MATERIAL_PRICE_IMPROVEMENT_PCT / 100.0)
                    )
                    is_new = not state or not state.get("active")
                    if is_new or materially_better:
                        sequence[item_id] = sequence.get(item_id, 0) + 1
                        opportunity_id = f"{item_id}:{sequence[item_id]}:{int(snapshot_ts)}"
                        event_kind = "opportunity_started" if is_new else "price_improved"
                        common_meta = {
                            "opportunity_id": opportunity_id,
                            "snapshot_ts": snapshot_ts,
                            "qty_floor": qty,
                            "next_higher": next_higher,
                            "average_price": avg,
                            "configured_max_price": int(configured_max),
                            "effective_max_price": effective_max,
                            "live_edge_gate_pct": MIN_LIVE_EDGE_PCT,
                        }
                        _record_event(
                            item_id=item_id,
                            source="api_snapshot",
                            event_type="qualifying_hit",
                            price=int(low),
                            max_price=effective_max,
                            baseline=int(baseline) if baseline else None,
                            edge_pct=edge_pct,
                            signature=f"qualifying:{opportunity_id}",
                            snapshot_id=int(snapshot_id),
                            metadata={**common_meta, "kind": event_kind},
                        )
                        _record_event(
                            item_id=item_id,
                            source="dashboard_sniper",
                            event_type="alert_fired",
                            price=int(low),
                            max_price=effective_max,
                            baseline=int(baseline) if baseline else None,
                            edge_pct=edge_pct,
                            signature=f"alert:{opportunity_id}",
                            snapshot_id=int(snapshot_id),
                            metadata={**common_meta, "kind": event_kind},
                        )
                        states[item_id] = {
                            "active": True,
                            "price": int(low),
                            "opportunity_id": opportunity_id,
                            "started_at": float(snapshot_ts),
                            "last_actionable_at": float(snapshot_ts),
                            "nonqualifying_since": None,
                        }
                    else:
                        state["last_actionable_at"] = float(snapshot_ts)
                        state["nonqualifying_since"] = None

                elif state and state.get("active"):
                    if state.get("nonqualifying_since") is None:
                        state["nonqualifying_since"] = float(snapshot_ts)
                    elif float(snapshot_ts) - float(state["nonqualifying_since"]) >= OPPORTUNITY_REARM_SECONDS:
                        opportunity_id = state.get("opportunity_id") or f"{item_id}:unknown"
                        _record_event(
                            item_id=item_id,
                            source="api_snapshot",
                            event_type="opportunity_ended",
                            price=int(low) if low else None,
                            max_price=effective_max,
                            baseline=int(baseline) if baseline else None,
                            edge_pct=edge_pct,
                            signature=f"ended:{opportunity_id}",
                            snapshot_id=int(snapshot_id),
                            metadata={
                                "opportunity_id": opportunity_id,
                                "started_at": state.get("started_at"),
                                "ended_at": float(snapshot_ts),
                                "duration_seconds": max(0.0, float(snapshot_ts) - float(state.get("started_at") or snapshot_ts)),
                                "configured_max_price": int(configured_max),
                                "effective_max_price": effective_max,
                            },
                        )
                        state["active"] = False
                        state["nonqualifying_since"] = None
        except Exception:
            pass
        await asyncio.sleep(2)


@app.on_event("startup")
async def _start_telemetry():
    _init_telemetry_db()
    asyncio.create_task(_snapshot_telemetry_loop())


@app.post("/api/sniper/telemetry")
async def record_sniper_telemetry(payload: TelemetryPayload):
    if payload.item_id <= 0:
        return {"ok": False, "recorded": False, "error": "invalid item_id"}
    recorded = _record_event(
        item_id=int(payload.item_id),
        source=(payload.source or "unknown")[:64],
        event_type=(payload.event_type or "alert")[:64],
        price=payload.price,
        max_price=payload.max_price,
        baseline=payload.baseline,
        edge_pct=payload.edge_pct,
        cache_age_seconds=payload.cache_age_seconds,
        cache_timestamp=payload.cache_timestamp,
        signature=(payload.signature or "")[:500] or None,
        metadata=payload.metadata,
    )
    return {"ok": True, "recorded": recorded}


@app.get("/api/sniper/telemetry")
async def sniper_telemetry(limit: int = 100, include_history: bool = False):
    limit = max(1, min(1000, int(limit)))
    where = "" if include_history else "WHERE ts>=?"
    params = (limit,) if include_history else (SESSION_STARTED_AT, limit)
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            f"SELECT * FROM alert_telemetry {where} ORDER BY ts DESC LIMIT ?", params
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except Exception:
            item["metadata"] = {}
        items.append(item)
    return {
        "ok": True,
        "count": len(items),
        "session_started_at": SESSION_STARTED_AT,
        "history_included": include_history,
        "items": items,
    }


@app.get("/api/sniper/telemetry/summary")
async def sniper_telemetry_summary(include_history: bool = False):
    where = "" if include_history else "WHERE ts>=?"
    params = () if include_history else (SESSION_STARTED_AT,)
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            f"""
            SELECT item_id,item_name,source,event_type,COUNT(*) AS events,
                   MIN(price) AS best_price,AVG(price) AS avg_price,
                   AVG(edge_pct) AS avg_edge_pct,MAX(ts) AS last_ts
            FROM alert_telemetry
            {where}
            GROUP BY item_id,item_name,source,event_type
            ORDER BY events DESC,last_ts DESC
            """,
            params,
        ).fetchall()
    return {
        "ok": True,
        "session_started_at": SESSION_STARTED_AT,
        "history_included": include_history,
        "items": [dict(r) for r in rows],
    }
