import json
import sqlite3
import time

import runtime
import telemetry
import sniper
import server
from runtime import app
from telemetry import DB_PATH

REPORT_KEEP_SESSIONS = 50
TELEMETRY_KEEP_DAYS = 14
TELEMETRY_MAX_ROWS = 20000


def _init_reports_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sniper_report_sessions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at REAL NOT NULL,
                stopped_at REAL,
                status TEXT NOT NULL DEFAULT 'running',
                summary_json TEXT
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_sniper_reports_started ON sniper_report_sessions(started_at DESC)")


def _prune_storage(c):
    cutoff = time.time() - TELEMETRY_KEEP_DAYS * 86400
    c.execute("DELETE FROM alert_telemetry WHERE ts < ?", (cutoff,))
    c.execute(
        """
        DELETE FROM alert_telemetry
        WHERE id NOT IN (
            SELECT id FROM alert_telemetry ORDER BY id DESC LIMIT ?
        )
        """,
        (TELEMETRY_MAX_ROWS,),
    )
    c.execute(
        """
        DELETE FROM sniper_report_sessions
        WHERE id NOT IN (
            SELECT id FROM sniper_report_sessions ORDER BY id DESC LIMIT ?
        )
        """,
        (REPORT_KEEP_SESSIONS,),
    )


def _current_profiles():
    out = []
    with sqlite3.connect(DB_PATH) as c:
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
            "independent_events": p.get("independent_events", 0),
            "recovered_events": p.get("recovered_events", 0),
            "completed_events": p.get("completed_events", 0),
            "recovery_rate": p.get("recovery_rate", 0),
            "false_positive_rate": p.get("false_positive_rate", 0),
            "median_edge_pct": p.get("median_edge_pct", 0),
            "opportunities_per_hour": p.get("opportunities_per_hour", 0),
            "sniper_score": p.get("sniper_score", 0),
            "stage": p.get("stage", "—"),
            "sniper_candidate": bool(p.get("sniper_candidate")),
        })
    return out


def _build_summary(started_at: float, stopped_at: float):
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            """
            SELECT item_id,item_name,
                   SUM(CASE WHEN source='api_snapshot' AND event_type='qualifying_hit' THEN 1 ELSE 0 END) AS opportunities,
                   SUM(CASE WHEN source='dashboard_sniper' AND event_type='alert_fired' THEN 1 ELSE 0 END) AS dashboard_alerts,
                   SUM(CASE WHEN source='live_page' AND event_type IN ('alert','alert_fired') THEN 1 ELSE 0 END) AS live_alerts,
                   SUM(CASE WHEN event_type='buy_clicked' THEN 1 ELSE 0 END) AS buy_clicks,
                   MIN(CASE WHEN price > 0 THEN price END) AS best_price,
                   MAX(edge_pct) AS best_edge_pct,
                   AVG(CASE WHEN edge_pct IS NOT NULL THEN edge_pct END) AS avg_edge_pct
            FROM alert_telemetry
            WHERE ts>=? AND ts<=?
            GROUP BY item_id,item_name
            HAVING opportunities > 0 OR dashboard_alerts > 0 OR live_alerts > 0 OR buy_clicks > 0
            ORDER BY opportunities DESC, dashboard_alerts DESC, item_name COLLATE NOCASE
            """,
            (started_at, stopped_at),
        ).fetchall()
    items = [dict(r) for r in rows]
    return {
        "duration_seconds": max(0, int(stopped_at - started_at)),
        "opportunities": sum(int(x.get("opportunities") or 0) for x in items),
        "dashboard_alerts": sum(int(x.get("dashboard_alerts") or 0) for x in items),
        "live_alerts": sum(int(x.get("live_alerts") or 0) for x in items),
        "buy_clicks": sum(int(x.get("buy_clicks") or 0) for x in items),
        "items": items,
        "evidence": _current_profiles(),
    }


_init_reports_db()


@app.post("/api/sniper/reports/start")
async def start_sniper_report():
    now = time.time()
    with sqlite3.connect(DB_PATH) as c:
        running = c.execute(
            "SELECT id,started_at FROM sniper_report_sessions WHERE status='running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if running:
            _prune_storage(c)
            return {"ok": True, "session_id": int(running[0]), "started_at": float(running[1]), "reused": True}
        cur = c.execute(
            "INSERT INTO sniper_report_sessions(started_at,status) VALUES(?, 'running')",
            (now,),
        )
        session_id = int(cur.lastrowid)
        _prune_storage(c)
    return {"ok": True, "session_id": session_id, "started_at": now, "reused": False}


@app.post("/api/sniper/reports/stop")
async def stop_sniper_report():
    now = time.time()
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT id,started_at FROM sniper_report_sessions WHERE status='running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {"ok": True, "stopped": False, "message": "No running sniper report session."}
    session_id, started_at = int(row[0]), float(row[1])
    summary = _build_summary(started_at, now)
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "UPDATE sniper_report_sessions SET stopped_at=?,status='complete',summary_json=? WHERE id=?",
            (now, json.dumps(summary, separators=(",", ":")), session_id),
        )
        _prune_storage(c)
    return {"ok": True, "stopped": True, "session_id": session_id, "summary": summary}


@app.get("/api/sniper/reports")
async def sniper_reports(limit: int = 20):
    limit = max(1, min(REPORT_KEEP_SESSIONS, int(limit)))
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id,started_at,stopped_at,status,summary_json FROM sniper_report_sessions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    items = []
    for row in rows:
        x = dict(row)
        raw = x.pop("summary_json", None)
        try:
            x["summary"] = json.loads(raw) if raw else None
        except Exception:
            x["summary"] = None
        items.append(x)
    return {
        "ok": True,
        "retention": {
            "report_sessions": REPORT_KEEP_SESSIONS,
            "telemetry_days": TELEMETRY_KEEP_DAYS,
            "telemetry_max_rows": TELEMETRY_MAX_ROWS,
        },
        "items": items,
    }


@app.get("/api/sniper/reports/{session_id}")
async def sniper_report(session_id: int):
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT id,started_at,stopped_at,status,summary_json FROM sniper_report_sessions WHERE id=?",
            (int(session_id),),
        ).fetchone()
    if not row:
        return {"ok": False, "error": "Report not found"}
    x = dict(row)
    raw = x.pop("summary_json", None)
    try:
        x["summary"] = json.loads(raw) if raw else None
    except Exception:
        x["summary"] = None
    return {"ok": True, "item": x}
