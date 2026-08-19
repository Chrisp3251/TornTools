import sqlite3

import bazaar_watch

app = bazaar_watch.app

_original_save_state_and_events = bazaar_watch._save_state_and_events


def _save_state_and_events_with_baseline(player_id: int, current: list[dict], events: list[dict], now: float):
    _original_save_state_and_events(player_id, current, events, now)
    # Persist a sentinel so an intentionally empty bazaar still counts as a
    # completed baseline. Otherwise the first later listing would be mistaken
    # for another first-run baseline and no alert would fire.
    with sqlite3.connect(bazaar_watch.DB_PATH) as c:
        c.execute(
            """
            INSERT OR REPLACE INTO bazaar_watch_state(
                player_id,listing_key,item_id,uid,name,item_type,quantity,price,market_price,last_seen
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (int(player_id), "__baseline__", None, None, "__baseline__", None, 0, 0, None, now),
        )


bazaar_watch._save_state_and_events = _save_state_and_events_with_baseline
