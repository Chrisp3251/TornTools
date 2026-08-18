import sqlite3

import server

EXTREME_EDGE_PCT = 60.0
EXTREME_MIN_CONFIRMED_RECOVERIES = 2

_base_evidence_profile = server.evidence_profile


def _median(values):
    return server._median(values)


def _event_model(item_id: int):
    rows = list(reversed(server._research_rows(item_id)))
    n = len(rows)
    if not rows:
        return [], 0.0

    baselines = []
    discounts = []
    deal_flags = []
    for idx, row in enumerate(rows):
        baseline = server._baseline_for(rows, idx)
        baselines.append(baseline)
        low = row[1]
        discount = ((baseline - low) / baseline * 100.0) if baseline and low and baseline > 0 else 0.0
        discount = max(-100.0, min(100.0, discount))
        discounts.append(discount)
        deal_flags.append(discount >= server.DEAL_EDGE_PCT)

    events = []
    idx = 0
    while idx < n:
        if not deal_flags[idx]:
            idx += 1
            continue

        start = idx
        start_sig = server._listing_signature(rows[idx])
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
        completed = end < n - 1

        if events and events[-1]["signature"] == start_sig:
            seconds_since_prior = float(rows[start][0]) - float(events[-1]["end_ts"])
            if seconds_since_prior <= server.EVENT_SIGNATURE_COOLDOWN_SECONDS:
                prior = events[-1]
                prior["end_ts"] = rows[end][0]
                valid_prices = [x for x in (prior.get("min_price"), min_low) if x is not None]
                prior["min_price"] = min(valid_prices) if valid_prices else None
                prior["best_edge_pct"] = max(float(prior.get("best_edge_pct") or 0), best_edge)
                prior["extreme"] = prior["best_edge_pct"] >= EXTREME_EDGE_PCT
                prior["completed"] = completed
                idx += 1
                continue

        evaluated = False
        recovered = False
        false_positive = False
        if completed and baseline:
            recovery_deadline = float(rows[end][0]) + server.RECOVERY_SECONDS
            future_lows = []
            for future_row in rows[end + 1:]:
                if float(future_row[0]) > recovery_deadline:
                    break
                if future_row[1]:
                    future_lows.append(future_row[1])
            if future_lows:
                evaluated = True
                recovered = max(future_lows) >= baseline * 0.95
                false_positive = not recovered

        events.append({
            "start_ts": rows[start][0],
            "end_ts": rows[end][0],
            "signature": start_sig,
            "baseline": baseline,
            "min_price": min_low,
            "best_edge_pct": best_edge,
            "completed": completed,
            "evaluated": evaluated,
            "recovered": recovered,
            "false_positive": false_positive,
            "extreme": best_edge >= EXTREME_EDGE_PCT,
        })
        idx += 1

    latest_ts = float(rows[-1][0])
    earliest_ts = float(rows[0][0])
    recent_start = max(earliest_ts, latest_ts - server.OPPORTUNITY_RATE_HORIZON_SECONDS)
    recent_span_hours = max(0.0, (latest_ts - recent_start) / 3600.0)
    return events, recent_span_hours


def _hardened_evidence_profile(item_id: int, name: str | None = None):
    profile = _base_evidence_profile(item_id, name)
    events, recent_span_hours = _event_model(int(item_id))
    if not events:
        profile.update({
            "evaluated_events": 0,
            "trusted_events": 0,
            "quarantined_events": 0,
            "extreme_events": 0,
            "extreme_recovered_events": 0,
            "extreme_training_unlocked": False,
            "extreme_edge_threshold_pct": EXTREME_EDGE_PCT,
        })
        return profile

    extreme = [e for e in events if e["extreme"]]
    extreme_recovered = [e for e in extreme if e["evaluated"] and e["recovered"]]
    extreme_unlocked = len(extreme_recovered) >= EXTREME_MIN_CONFIRMED_RECOVERIES

    trusted = [e for e in events if not e["extreme"] or extreme_unlocked]
    quarantined = [e for e in events if e["extreme"] and not extreme_unlocked]
    evaluated = [e for e in trusted if e["evaluated"]]
    recovered = [e for e in evaluated if e["recovered"]]
    false_positives = [e for e in evaluated if e["false_positive"]]
    trusted_edges = [e["best_edge_pct"] for e in trusted]
    trusted_strong = [e for e in trusted if e["best_edge_pct"] >= server.STRONG_EDGE_PCT]

    recovery_rate = len(recovered) / len(evaluated) if evaluated else 0.0
    false_positive_rate = len(false_positives) / len(evaluated) if evaluated else 0.0
    median_edge = _median(trusted_edges) or 0.0
    raw_best_edge = max((e["best_edge_pct"] for e in events), default=0.0)
    trusted_best_edge = max(trusted_edges, default=0.0)

    latest_ts = max(float(e["start_ts"]) for e in events)
    recent_start = latest_ts - server.OPPORTUNITY_RATE_HORIZON_SECONDS
    recent_trusted = [e for e in trusted if float(e["start_ts"]) >= recent_start]
    opportunities_per_hour = len(recent_trusted) / recent_span_hours if recent_span_hours >= 0.25 else 0.0

    activity_score = float(profile.get("activity_score") or 0)
    event_score = min(28.0, len(trusted) * 6.0)
    recovery_score = min(20.0, recovery_rate * 20.0)
    edge_score = min(18.0, median_edge * 1.2)
    strong_score = min(10.0, len(trusted_strong) * 4.0)
    activity_component = min(12.0, activity_score * 0.12)
    frequency_score = min(7.0, opportunities_per_hour * 3.5)
    false_penalty = min(25.0, false_positive_rate * 35.0)
    floor_volatility = float(profile.get("floor_volatility_pct") or 0)
    volatility_penalty = min(10.0, max(0.0, floor_volatility - 12.0) * 0.5)
    score = max(0.0, min(100.0, event_score + recovery_score + edge_score + strong_score + activity_component + frequency_score - false_penalty - volatility_penalty))

    observations = int(profile.get("observations") or 0)
    activity_gate_met = bool(activity_score >= server.SNIPER_MIN_ACTIVITY or opportunities_per_hour >= 1.0)
    sniper_candidate = (
        observations >= server.SNIPER_MIN_SAMPLES
        and len(trusted) >= server.SNIPER_MIN_EVENTS
        and len(recovered) >= server.SNIPER_MIN_RECOVERED
        and recovery_rate >= server.SNIPER_MIN_RECOVERY_RATE
        and false_positive_rate <= server.SNIPER_MAX_FALSE_POSITIVE_RATE
        and median_edge >= server.SNIPER_MIN_MEDIAN_EDGE
        and activity_gate_met
        and score >= server.SNIPER_MIN_SCORE
    )

    research_ready = (
        observations >= server.RESEARCH_MIN_SAMPLES
        and activity_score >= server.RESEARCH_MIN_ACTIVITY
        and len(trusted) >= server.RESEARCH_MIN_EVENTS
        and len(recovered) >= server.RESEARCH_MIN_RECOVERED
        and median_edge >= server.RESEARCH_MIN_MEDIAN_EDGE
        and false_positive_rate <= server.RESEARCH_MAX_FALSE_POSITIVE_RATE
    )

    recommended_max = None
    max_source = None
    rolling_baseline = profile.get("rolling_baseline")
    if rolling_baseline and trusted:
        training_events = recovered or [e for e in trusted if e["evaluated"] and not e["false_positive"]]
        if training_events:
            event_prices = [e["min_price"] for e in training_events if e["min_price"]]
            event_price_reference = _median(event_prices)
            baseline_cap = float(rolling_baseline) * 0.90
            observed_cap = event_price_reference * 1.05 if event_price_reference else baseline_cap
            recommended_max = max(1, int(min(baseline_cap, observed_cap)))
            max_source = "confirmed non-extreme recoveries" if recovered else "evaluated non-false-positive events"
            if extreme_unlocked and any(e["extreme"] for e in training_events):
                max_source = "confirmed recoveries (extreme pattern independently repeated)"

    graduated = bool(profile.get("graduated"))
    if sniper_candidate:
        stage = "SNIPER CANDIDATE"
    elif graduated:
        stage = "PROVING IN HIDDEN"
    elif research_ready:
        stage = "PROVEN MARKET"
    elif observations < server.RESEARCH_MIN_SAMPLES:
        stage = "LEARNING"
    elif trusted:
        stage = "BUILDING CASE"
    else:
        stage = "NO TRUSTED EDGE YET"

    data_quality = profile.get("data_quality") or "GOOD"
    if quarantined and not trusted:
        data_quality = "EXTREME-ONLY / QUARANTINED"
    elif quarantined:
        data_quality = "GOOD + EXTREME QUARANTINE" if observations >= server.SNIPER_MIN_SAMPLES else "EARLY + EXTREME QUARANTINE"
    elif len(evaluated) < 3 and observations >= server.SNIPER_MIN_SAMPLES:
        data_quality = "THIN EVALUATED HISTORY"

    recent_events = []
    for e in reversed(events[-5:]):
        recent_events.append({
            **e,
            "trusted_for_learning": (not e["extreme"] or extreme_unlocked),
            "quarantine_reason": "edge >= 60% until independently recovered twice" if e["extreme"] and not extreme_unlocked else None,
        })

    profile.update({
        "independent_events": len(events),
        "bargain_events": len(events),
        "trusted_events": len(trusted),
        "quarantined_events": len(quarantined),
        "evaluated_events": len(evaluated),
        "recovered_events": len(recovered),
        "completed_events": len([e for e in events if e["completed"]]),
        "recovery_rate": round(recovery_rate * 100.0, 1),
        "false_positive_events": len(false_positives),
        "false_positive_rate": round(false_positive_rate * 100.0, 1),
        "median_edge_pct": round(median_edge, 2),
        "best_edge_pct": round(raw_best_edge, 2),
        "trusted_best_edge_pct": round(trusted_best_edge, 2),
        "strong_events": len(trusted_strong),
        "opportunities_per_hour": round(opportunities_per_hour, 2),
        "promotion_score": round(score, 1),
        "sniper_score": round(score, 1),
        "ready": research_ready,
        "sniper_candidate": sniper_candidate,
        "stage": stage,
        "recommended_sniper_max": recommended_max,
        "recommended_max_source": max_source,
        "data_quality": data_quality,
        "activity_gate_met": activity_gate_met,
        "extreme_events": len(extreme),
        "extreme_recovered_events": len(extreme_recovered),
        "extreme_training_unlocked": extreme_unlocked,
        "extreme_edge_threshold_pct": EXTREME_EDGE_PCT,
        "recent_events": recent_events,
    })
    return profile


server.evidence_profile = _hardened_evidence_profile


# Reports are generated later, so make the saved evidence snapshot expose the
# same quality controls the learner is actually using.
try:
    import reports

    def _hardened_report_profiles():
        out = []
        with sqlite3.connect(server.DB_PATH) as c:
            targets = c.execute(
                "SELECT item_id,name,max_price,enabled FROM sniper_targets ORDER BY name COLLATE NOCASE"
            ).fetchall()
        for item_id, name, configured_max, enabled in targets:
            try:
                p = server.evidence_profile(int(item_id), name)
            except Exception:
                p = {}
            out.append({
                "item_id": int(item_id),
                "name": name,
                "enabled": bool(enabled),
                "configured_max": int(configured_max),
                "rolling_baseline": p.get("rolling_baseline"),
                "recommended_sniper_max": p.get("recommended_sniper_max"),
                "recommended_max_source": p.get("recommended_max_source"),
                "independent_events": p.get("independent_events", 0),
                "trusted_events": p.get("trusted_events", 0),
                "quarantined_events": p.get("quarantined_events", 0),
                "evaluated_events": p.get("evaluated_events", 0),
                "recovered_events": p.get("recovered_events", 0),
                "completed_events": p.get("completed_events", 0),
                "recovery_rate": p.get("recovery_rate", 0),
                "false_positive_events": p.get("false_positive_events", 0),
                "false_positive_rate": p.get("false_positive_rate", 0),
                "median_edge_pct": p.get("median_edge_pct", 0),
                "best_edge_pct": p.get("best_edge_pct", 0),
                "trusted_best_edge_pct": p.get("trusted_best_edge_pct", 0),
                "extreme_events": p.get("extreme_events", 0),
                "extreme_recovered_events": p.get("extreme_recovered_events", 0),
                "extreme_training_unlocked": bool(p.get("extreme_training_unlocked")),
                "opportunities_per_hour": p.get("opportunities_per_hour", 0),
                "sniper_score": p.get("sniper_score", 0),
                "data_quality": p.get("data_quality", "—"),
                "stage": p.get("stage", "—"),
                "sniper_candidate": bool(p.get("sniper_candidate")),
            })
        return out

    reports._current_profiles = _hardened_report_profiles
except Exception:
    pass
