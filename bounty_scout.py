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


async def _fetch_bounty_pages(client: httpx.AsyncClient, max_pages: int = 10) -> tuple[list[dict[str, Any]], int]:
    all_rows: list[dict[str, Any]] = []
    pages = 0
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
        if not links.get("next") or len(rows) < 100:
            break
    return all_rows, pages


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


async def _target_statuses(client: httpx.AsyncClient, ids: list[int]) -> tuple[dict[int, dict], list[str]]:
    results: dict[int, dict] = {}
    errors: list[str] = []
    sem = asyncio.Semaphore(6)

    async def one(player_id: int):
        async with sem:
            try:
                data = await app_module._torn_get(
                    client,
                    "/user",
                    {"id": int(player_id), "selections": "basic"},
                    error_text="target status",
                )
                profile = data.get("profile") if isinstance(data, dict) and isinstance(data.get("profile"), dict) else {}
                status = profile.get("status") if isinstance(profile.get("status"), dict) else {}
                results[int(player_id)] = {
                    "state": str(status.get("state") or "Unknown"),
                    "description": str(status.get("description") or status.get("state") or "Unknown"),
                    "until": int(status.get("until")) if status.get("until") is not None else None,
                }
            except Exception as exc:
                errors.append(f"{player_id}: {exc}")

    await asyncio.gather(*(one(pid) for pid in dict.fromkeys(int(x) for x in ids if int(x) > 0)))
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
    limit: int = Query(50, ge=1, le=100),
    include_unknown: int = Query(0, ge=0, le=1),
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
            app_module._torn_get(client, "/user/battlestats", error_text="battle stats"),
            _fetch_bounty_pages(client, max_pages=10),
        )
        bounty_rows_all, bounty_pages = bounty_page_result

        own_total, own_stats = _own_total_battlestats(own_data)
        if own_total <= 0:
            raise HTTPException(502, "Could not determine your total battle stats from Torn")

        bounties = [
            row for row in bounty_rows_all
            if row["reward"] >= int(min_reward)
            and (row["target_level"] is None or row["target_level"] <= int(max_level))
        ]

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
        total_reward = reward_each * quantity
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
    items = items[: int(limit)]

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
        },
        "source_bounties": len(bounties),
        "bounty_pages_scanned": bounty_pages,
        "unknown_estimates": unknown_count,
        "items": items,
        "warnings": ff_errors[:5],
        "notes": [
            "Your battle stats come directly from Torn.",
            "Target battle stats are FFScouter estimates and can be stale or inaccurate.",
            "A target being below your total stats does not guarantee a win; stat distribution, merits, weapons, armor, temporary effects, and passives matter.",
        ],
    }
