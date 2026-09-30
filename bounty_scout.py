from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import httpx
from fastapi import HTTPException, Query

import app as app_module
import mug_scout
import travel_state_runtime

app = travel_state_runtime.app

FFSCOUTER_BASE = mug_scout.FFSCOUTER_BASE
BATCH_SIZE = 50

# Torn API conservation. The bounty board changes often, but re-downloading up
# to 25 pages on every button click is wasteful. Statuses are also briefly
# cached so repeated scans do not re-query the same players immediately.
_BOUNTY_CACHE_TTL = 60
_OWN_STATS_CACHE_TTL = 300
_STATUS_READY_TTL = 60
_STATUS_TRANSIENT_TTL = 120
_STATUS_UNKNOWN_TTL = 45
_STATUS_BUDGET_PER_SCAN = 20
_bounty_cache: dict[str, Any] = {"at": 0.0, "rows": [], "pages": 0, "has_more": False}
_own_stats_cache: dict[str, Any] = {"at": 0.0, "data": None}
_status_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_bounty_fetch_lock = asyncio.Lock()
_status_inflight: dict[int, asyncio.Task] = {}


def _num(value):
    try:
        if isinstance(value, dict):
            for key in ("value", "amount", "total"):
                if value.get(key) is not None:
                    return float(value[key])
            return None
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _own_total_battlestats(data: dict[str, Any]) -> tuple[int, dict[str, int]]:
    root = data.get("battlestats") if isinstance(data.get("battlestats"), dict) else data
    stats = {}
    for key in ("strength", "defense", "speed", "dexterity"):
        raw = root.get(key) if isinstance(root, dict) else None
        value = _num(raw)
        stats[key] = max(0, int(value or 0))
    return sum(stats.values()), stats


async def _fetch_bounty_pages(client: httpx.AsyncClient, max_pages: int = 25) -> tuple[list[dict[str, Any]], int, bool]:
    now = time.time()
    if _bounty_cache["rows"] and now - float(_bounty_cache["at"]) < _BOUNTY_CACHE_TTL:
        return list(_bounty_cache["rows"]), int(_bounty_cache["pages"]), bool(_bounty_cache["has_more"])

    # Multiple browser clicks can overlap. Only one request is allowed to
    # refresh the board; everyone else reuses that completed snapshot.
    async with _bounty_fetch_lock:
        now = time.time()
        if _bounty_cache["rows"] and now - float(_bounty_cache["at"]) < _BOUNTY_CACHE_TTL:
            return list(_bounty_cache["rows"]), int(_bounty_cache["pages"]), bool(_bounty_cache["has_more"])

        all_rows: list[dict[str, Any]] = []
        pages = 0
        has_more = False
        for page in range(max_pages):
            offset = page * 100
            data = await app_module._torn_get(
                client,
                "/torn/bounties",
                {"limit": 100, "offset": offset},
                error_text="bounties",
            )
            rows = _bounty_rows(data)
            all_rows.extend(rows)
            pages += 1

            metadata = data.get("_metadata") if isinstance(data, dict) else None
            links = metadata.get("links") if isinstance(metadata, dict) and isinstance(metadata.get("links"), dict) else {}
            has_more = bool(links.get("next")) and len(rows) >= 100
            if not has_more:
                break
        _bounty_cache.update({"at": time.time(), "rows": list(all_rows), "pages": pages, "has_more": has_more})
        return all_rows, pages, has_more


async def _get_own_battlestats(client: httpx.AsyncClient) -> dict[str, Any]:
    now = time.time()
    cached = _own_stats_cache.get("data")
    if cached is not None and now - float(_own_stats_cache["at"]) < _OWN_STATS_CACHE_TTL:
        return cached
    data = await app_module._torn_get(client, "/user/battlestats", error_text="battle stats")
    _own_stats_cache.update({"at": time.time(), "data": data})
    return data


def _bounty_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("bounties") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            target_id = int(row.get("target_id"))
        except (TypeError, ValueError):
            continue
        try:
            reward = int(row.get("reward") or 0)
        except (TypeError, ValueError):
            reward = 0
        try:
            quantity = max(1, int(row.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        try:
            level = int(row.get("target_level")) if row.get("target_level") is not None else None
        except (TypeError, ValueError):
            level = None
        out.append({
            "target_id": target_id,
            "target_name": row.get("target_name") or f"Player {target_id}",
            "target_level": level,
            "reward": reward,
            "quantity": quantity,
            "reason": row.get("reason"),
            "valid_until": row.get("valid_until"),
            "is_anonymous": bool(row.get("is_anonymous")),
            "lister_id": row.get("lister_id"),
            "lister_name": row.get("lister_name"),
        })
    return out


def _group_bounties_by_target(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        target_id = int(row["target_id"])
        current = grouped.get(target_id)
        reward = int(row.get("reward") or 0)
        quantity = max(1, int(row.get("quantity") or 1))
        if current is None:
            current = dict(row)
            current["bounty_entries"] = 0
            current["quantity"] = 0
            current["total_reward"] = 0
            current["highest_reward"] = 0
            current["reasons"] = []
            grouped[target_id] = current

        current["bounty_entries"] += 1
        current["quantity"] += quantity
        current["total_reward"] += reward * quantity
        current["highest_reward"] = max(int(current["highest_reward"]), reward)

        reason = row.get("reason")
        if reason and reason not in current["reasons"]:
            current["reasons"].append(reason)

        # Keep the best-paying listing as the representative row metadata.
        if reward >= int(current.get("reward") or 0):
            current["reward"] = reward
            current["valid_until"] = row.get("valid_until")
            current["is_anonymous"] = bool(row.get("is_anonymous"))
            current["lister_id"] = row.get("lister_id")
            current["lister_name"] = row.get("lister_name")

    out = []
    for row in grouped.values():
        row["reason"] = " | ".join(row.get("reasons") or []) or None
        out.append(row)
    return out


async def _ff_stats(client: httpx.AsyncClient, ids: list[int], key: str) -> tuple[dict[int, dict], list[str]]:
    results: dict[int, dict] = {}
    errors: list[str] = []
    unique = list(dict.fromkeys(int(x) for x in ids if int(x) > 0))
    for start in range(0, len(unique), BATCH_SIZE):
        batch = unique[start:start + BATCH_SIZE]
        try:
            response = await client.get(
                f"{FFSCOUTER_BASE}/get-stats",
                params={"key": key, "targets": ",".join(str(x) for x in batch)},
                timeout=12.0,
            )
            data = response.json()
            if isinstance(data, dict) and data.get("error"):
                errors.append(str(data.get("error")))
                continue
            if not isinstance(data, list):
                errors.append("FFScouter returned an unexpected get-stats response")
                continue
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    player_id = int(item.get("player_id"))
                except (TypeError, ValueError):
                    continue
                results[player_id] = item
        except Exception as exc:
            errors.append(str(exc))
        if start + BATCH_SIZE < len(unique):
            await asyncio.sleep(0.15)
    return results, errors


def _cached_status(player_id: int) -> dict[str, Any] | None:
    cached = _status_cache.get(int(player_id))
    if not cached:
        return None
    expires_at, parsed = cached
    if time.time() >= float(expires_at):
        return None
    return dict(parsed)


def _status_cache_ttl(parsed: dict[str, Any]) -> int:
    state = str(parsed.get("state") or "").lower()
    desc = str(parsed.get("description") or "").lower()
    lowered = f"{state} {desc}"
    if "hospital" in lowered or "jail" in lowered or "federal" in lowered or "travel" in lowered or "abroad" in lowered:
        return _STATUS_TRANSIENT_TTL
    if state in ("okay", "idle") or "okay" in lowered:
        return _STATUS_READY_TTL
    return _STATUS_UNKNOWN_TTL


async def _target_statuses(client: httpx.AsyncClient, ids: list[int]) -> tuple[dict[int, dict], list[str]]:
    results: dict[int, dict] = {}
    errors: list[str] = []
    sem = asyncio.Semaphore(6)
    now = time.time()
    unique_ids = list(dict.fromkeys(int(x) for x in ids if int(x) > 0))
    to_fetch: list[int] = []

    for player_id in unique_ids:
        cached = _status_cache.get(player_id)
        if cached and now < cached[0]:
            results[player_id] = dict(cached[1])
        else:
            to_fetch.append(player_id)

    async def fetch_one(player_id: int):
        try:
            data = await app_module._torn_get(
                client,
                f"/user/{int(player_id)}/basic",
                error_text="target status",
            )
            if isinstance(data, dict) and isinstance(data.get("status"), dict):
                status = data.get("status")
            elif isinstance(data, dict) and isinstance(data.get("profile"), dict) and isinstance(data["profile"].get("status"), dict):
                status = data["profile"].get("status")
            else:
                status = {}
            parsed = {
                "state": str(status.get("state") or "Unknown"),
                "description": str(status.get("description") or status.get("state") or "Unknown"),
                "until": int(status.get("until")) if status.get("until") is not None else None,
            }
            expires_at = time.time() + _status_cache_ttl(parsed)
            _status_cache[int(player_id)] = (expires_at, dict(parsed))
            return parsed
        finally:
            _status_inflight.pop(int(player_id), None)

    async def one(player_id: int):
        async with sem:
            try:
                task = _status_inflight.get(int(player_id))
                if task is None or task.done():
                    task = asyncio.create_task(fetch_one(int(player_id)))
                    _status_inflight[int(player_id)] = task
                parsed = await task
                results[int(player_id)] = dict(parsed)
            except Exception as exc:
                errors.append(f"{player_id}: {exc}")

    await asyncio.gather(*(one(pid) for pid in to_fetch))
    return results, errors


def _availability_info(status: dict | None, soon_minutes: int) -> dict[str, Any]:
    now = int(time.time())
    status = status or {}
    state = str(status.get("state") or "Unknown").strip()
    desc = str(status.get("description") or state or "Unknown").strip()
    lowered = f"{state} {desc}".lower()
    until = status.get("until")
    try:
        until = int(until) if until is not None else None
    except (TypeError, ValueError):
        until = None
    seconds_left = max(0, until - now) if until else None

    if any(x in lowered for x in ("travel", "abroad")):
        kind = "traveling"
    elif "hospital" in lowered:
        kind = "hospital"
    elif any(x in lowered for x in ("jail", "federal")):
        kind = "jail"
    elif state.lower() in ("okay", "idle") or "okay" in lowered:
        kind = "ready"
    else:
        kind = "unknown"

    return {
        "kind": kind,
        "ready": kind == "ready",
        "hospital_soon": kind == "hospital" and seconds_left is not None and seconds_left <= int(soon_minutes) * 60,
        "seconds_left": seconds_left,
        "label": desc,
        "until": until,
    }


def _human_number(value: int | float | None) -> str | None:
    if value is None:
        return None
    n = float(value)
    for suffix, divisor in (("b", 1_000_000_000), ("m", 1_000_000), ("k", 1_000)):
        if abs(n) >= divisor:
            return f"{n / divisor:.2f}{suffix}"
    return f"{int(n):,}"


@app.get("/api/bounty-scout/search")
async def bounty_scout_search(
    min_ratio: float = Query(0.10, ge=0.0, le=10.0),
    max_ratio: float = Query(1.00, ge=0.05, le=10.0),
    min_reward: int = Query(0, ge=0),
    max_level: int = Query(100, ge=1, le=100),
    limit: int = Query(250, ge=1, le=1000),
    include_unknown: int = Query(0, ge=0, le=1),
    availability: str = Query("ready_or_soon"),
    hospital_soon_minutes: int = Query(30, ge=1, le=240),
):
    if min_ratio > max_ratio:
        raise HTTPException(400, "Minimum stat ratio cannot exceed maximum stat ratio")
    if not app_module._api_key:
        raise HTTPException(401, "Load your Torn API key first")

    ff_key, ff_source = mug_scout._ffscouter_key()
    if not ff_key:
        raise HTTPException(401, "No FFScouter API key found. Add FFSCOUTER_API_KEY to .env and restart TornTools.")

    async with httpx.AsyncClient(timeout=15.0) as client:
        own_data, bounty_page_result = await asyncio.gather(
            _get_own_battlestats(client),
            _fetch_bounty_pages(client, max_pages=25),
        )
        bounty_rows_all, bounty_pages, bounty_has_more = bounty_page_result

        own_total, own_stats = _own_total_battlestats(own_data)
        if own_total <= 0:
            raise HTTPException(502, "Could not determine your total battle stats from Torn")

        bounty_rows_filtered = [
            row for row in bounty_rows_all
            if row["reward"] >= int(min_reward)
            and (row["target_level"] is None or row["target_level"] <= int(max_level))
        ]
        bounties = _group_bounties_by_target(bounty_rows_filtered)

        stat_map, ff_errors = await _ff_stats(client, [x["target_id"] for x in bounties], ff_key)

    items = []
    unknown_count = 0
    for row in bounties:
        ff = stat_map.get(row["target_id"])
        estimate = None
        source = None
        fair_fight = None
        estimate_human = None
        if isinstance(ff, dict):
            estimate = _num(ff.get("bs_estimate"))
            source = ff.get("source")
            fair_fight = _num(ff.get("fair_fight"))
            estimate_human = ff.get("bs_estimate_human")

        ratio = (float(estimate) / own_total) if estimate is not None and own_total > 0 else None
        if ratio is None:
            unknown_count += 1
            if not include_unknown:
                continue
        elif ratio < min_ratio or ratio > max_ratio:
            continue

        reward_each = int(row["reward"])
        quantity = int(row["quantity"])
        total_reward = int(row.get("total_reward") or (reward_each * quantity))
        value_score = (reward_each / max(float(estimate or own_total), 1.0)) * 1_000_000

        items.append({
            **row,
            "bs_estimate": int(estimate) if estimate is not None else None,
            "bs_estimate_human": estimate_human or _human_number(estimate),
            "bs_ratio": round(ratio, 4) if ratio is not None else None,
            "bs_ratio_pct": round(ratio * 100.0, 1) if ratio is not None else None,
            "fair_fight": round(fair_fight, 2) if fair_fight is not None else None,
            "ff_source": source,
            "total_reward": total_reward,
            "value_score": round(value_score, 2),
            "profile_url": f"https://www.torn.com/profiles.php?XID={row['target_id']}",
            "attack_url": f"https://www.torn.com/loader.php?sid=attack&user2ID={row['target_id']}",
        })

    items.sort(
        key=lambda x: (
            x["bs_ratio"] is not None,
            x["reward"],
            -float(x["bs_ratio"] if x["bs_ratio"] is not None else math.inf),
        ),
        reverse=True,
    )

    stat_matched_count = len(items)
    status_counts = {"ready": 0, "hospital": 0, "traveling": 0, "jail": 0, "unknown": 0, "pending": 0}
    status_errors: list[str] = []
    status_budget = _STATUS_BUDGET_PER_SCAN

    # Status is enrichment, never a gate. Return every stat-matched bounty up
    # to the requested display limit even when we do not have a fresh Torn
    # status for that target yet.
    visible_items = items[: int(limit)]

    # Spend this scan's small Torn request budget on targets whose status is not
    # currently cached. Repeated scans therefore walk forward through the
    # result set instead of repeatedly querying the same top players.
    status_refresh_ids = [
        int(item["target_id"])
        for item in visible_items
        if _cached_status(int(item["target_id"])) is None
    ][:status_budget]

    status_freshly_requested = len(status_refresh_ids)
    if status_refresh_ids:
        async with httpx.AsyncClient(timeout=15.0) as client:
            _, status_errors = await _target_statuses(client, status_refresh_ids)

    status_known = 0
    for item in visible_items:
        raw_status = _cached_status(int(item["target_id"]))
        if raw_status is None:
            item["status_kind"] = "pending"
            item["status"] = "Status pending"
            item["status_until"] = None
            item["status_seconds_left"] = None
            item["status_checked"] = False
            status_counts["pending"] += 1
            continue

        info = _availability_info(raw_status, hospital_soon_minutes)
        kind = info["kind"] if info["kind"] in status_counts else "unknown"
        item["status_kind"] = kind
        item["status"] = info["label"]
        item["status_until"] = info["until"]
        item["status_seconds_left"] = info["seconds_left"]
        item["status_checked"] = True
        item["matches_requested_availability"] = (
            True if availability == "all"
            else info["ready"] if availability == "ready"
            else info["hospital_soon"] if availability == "hospital_soon"
            else (info["ready"] or info["hospital_soon"])
        )
        status_counts[kind] += 1
        status_known += 1

    items = visible_items
    status_candidates_considered = len(visible_items)
    status_checked = status_known

    return {
        "ok": True,
        "own_total_battlestats": own_total,
        "own_total_human": _human_number(own_total),
        "own_stats": own_stats,
        "ffscouter_key_source": ff_source,
        "criteria": {
            "min_ratio": min_ratio,
            "max_ratio": max_ratio,
            "min_reward": min_reward,
            "max_level": max_level,
            "limit": limit,
            "include_unknown": bool(include_unknown),
            "availability": availability,
            "hospital_soon_minutes": hospital_soon_minutes,
        },
        "board_rows_scanned": len(bounty_rows_all),
        "source_bounties": len(bounty_rows_filtered),
        "unique_targets": len(bounties),
        "bounty_pages_scanned": bounty_pages,
        "bounty_board_truncated": bool(bounty_has_more),
        "unknown_estimates": unknown_count,
        "stat_matched_targets": stat_matched_count,
        "status_candidates_considered": status_candidates_considered,
        "status_budget": status_budget,
        "status_freshly_requested": status_freshly_requested,
        "status_checked": status_checked,
        "status_pending": status_counts["pending"],
        "status_coverage_pct": round((status_checked / len(items) * 100.0) if items else 100.0, 1),
        "status_counts": status_counts,
        "items": items,
        "warnings": (ff_errors + status_errors)[:8],
        "notes": [
            "Your battle stats come directly from Torn.",
            "Target battle stats are FFScouter estimates and can be stale or inaccurate.",
            "All matching bounty targets are returned without requiring a Torn status lookup first.",
            "Live status is progressively enriched from a bounded cache-backed request budget; pending status never hides a target.",
            "A target being below your total stats does not guarantee a win; stat distribution, merits, weapons, armor, temporary effects, and passives matter.",
        ],
    }
