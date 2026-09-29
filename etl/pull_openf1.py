"""Pull OpenF1 interval data (gap to leader over time) into SQLite.

OpenF1 identifies races by its own `session_key`, so we first look up the key for each
race in our `sessions` table by matching year + race date, then download /intervals.

Usage (from the project root):
    python -m etl.pull_openf1        # every race session already in the database
"""

import time

import pandas as pd
import requests

from etl.db import get_connection, read_sql

OPENF1_URL = "https://api.openf1.org/v1"


def find_session_key(year, session_date):
    """Map one of our races to OpenF1's session_key via the race date."""
    races = requests.get(
        f"{OPENF1_URL}/sessions", params={"year": year, "session_name": "Race"}, timeout=30
    ).json()
    for race in races:
        if race["date_start"][:10] == session_date:
            return race["session_key"]
    raise ValueError(f"No OpenF1 race found for {year} on {session_date}")


def _to_float(value):
    """OpenF1 reports lapped cars as strings like '+1 LAP'; those become NULL."""
    return value if isinstance(value, (int, float)) else None


def fetch_intervals(session_key):
    rows = requests.get(
        f"{OPENF1_URL}/intervals", params={"session_key": session_key}, timeout=60
    ).json()
    return pd.DataFrame({
        "driver_number": [str(r["driver_number"]) for r in rows],
        "date": [r["date"] for r in rows],
        "gap_to_leader": [_to_float(r["gap_to_leader"]) for r in rows],
        "interval_s": [_to_float(r["interval"]) for r in rows],
    })


def pull_intervals(session_id, year, session_date):
    key = find_session_key(year, session_date)
    intervals = fetch_intervals(key).assign(session_id=session_id)
    with get_connection() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO openf1_intervals
               (driver_number, date, gap_to_leader, interval_s, session_id)
               VALUES (?, ?, ?, ?, ?)""",
            intervals.astype(object).where(intervals.notna(), None).itertuples(index=False),
        )
    print(f"{session_id}: OpenF1 session_key {key}, {len(intervals)} interval samples stored")


if __name__ == "__main__":
    sessions = read_sql("SELECT session_id, year, session_date FROM sessions WHERE session_type = 'R'")
    for s in sessions.itertuples(index=False):
        pull_intervals(s.session_id, s.year, s.session_date)
        time.sleep(1)   # be polite to the free API
