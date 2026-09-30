"""Load F1 sessions with FastF1 and upsert every lap into SQLite.

Idempotent: laps are keyed on (session_id, driver, lap_number), so running this twice
for the same race overwrites rows instead of duplicating them.

Usage (from the project root):
    python -m etl.pull_sessions                 # pulls the default race list below
    python -m etl.pull_sessions 2023 Spain R    # pulls one session
"""

import sys
from datetime import datetime, timezone

import fastf1
import pandas as pd

from etl.db import PROJECT_ROOT, get_connection

# 2023 rounds 5-14. Chosen so that:
#   - McLaren's Austria upgrade (round 9) has 4 races before and 6 after it,
#   - the set includes a street circuit (Monaco), a high-speed circuit (Monza),
#     and a wet/mixed race (Zandvoort), plus the reference race (Spain).
DEFAULT_RACES = [
    (2023, "Miami"),
    (2023, "Monaco"),
    (2023, "Spain"),
    (2023, "Canada"),
    (2023, "Austria"),
    (2023, "Great Britain"),
    (2023, "Hungary"),
    (2023, "Belgium"),
    (2023, "Netherlands"),
    (2023, "Italy"),
]


def _seconds(series):
    """Convert a Timedelta column to float seconds (NaT becomes NaN)."""
    return series.dt.total_seconds()


def make_session_id(year, round_number, session_type):
    return f"{year}_{round_number:02d}_{session_type}"


def load_session(year, event, session_type="R", load_telemetry=True):
    """Download (or read from cache) one session via FastF1."""
    fastf1.Cache.enable_cache(str(PROJECT_ROOT / "cache"))
    session = fastf1.get_session(year, event, session_type)
    # Telemetry is slow to download and we don't store it, but FastF1 needs it to anchor
    # the session clock to real UTC time (LapStartDate), which we use to join with OpenF1.
    # Without it, lap_start_date is stored as NULL and every other column is unaffected.
    session.load(laps=True, telemetry=load_telemetry, weather=True, messages=True)
    return session


def transform_laps(session, session_id):
    """Map FastF1's lap DataFrame onto the `laps` table columns."""
    laps = session.laps
    return pd.DataFrame({
        "session_id": session_id,
        "driver": laps["Driver"],
        "driver_number": laps["DriverNumber"],
        "team": laps["Team"],
        "lap_number": laps["LapNumber"].astype(int),
        "lap_time_s": _seconds(laps["LapTime"]),
        "sector1_s": _seconds(laps["Sector1Time"]),
        "sector2_s": _seconds(laps["Sector2Time"]),
        "sector3_s": _seconds(laps["Sector3Time"]),
        "lap_end_time_s": _seconds(laps["Time"]),
        # FastF1 stores LapStartDate as naive UTC; ISO text sorts and parses cleanly.
        "lap_start_date": laps["LapStartDate"].dt.strftime("%Y-%m-%dT%H:%M:%S.%f"),
        "stint": laps["Stint"],
        "compound": laps["Compound"],
        "tyre_life": laps["TyreLife"],
        "fresh_tyre": laps["FreshTyre"].astype("boolean").astype("Int64"),
        "position": laps["Position"],
        # A PitInTime on this lap means the car entered the pits at the end of it;
        # a PitOutTime means the lap started from the pit lane.
        "pit_in": laps["PitInTime"].notna().astype(int),
        "pit_out": laps["PitOutTime"].notna().astype(int),
        "track_status": laps["TrackStatus"],
        # Keep FastF1's quality flag as data instead of filtering on it here.
        "is_accurate": laps["IsAccurate"].fillna(False).astype(int),
        "deleted": laps["Deleted"].astype("boolean").astype("Int64"),
    })


def upsert_session(conn, session, session_id, session_type):
    """Write the session metadata row. INSERT OR REPLACE = upsert on the primary key."""
    event = session.event
    rainfall = session.weather_data["Rainfall"].any() if session.weather_data is not None else None
    conn.execute(
        """INSERT OR REPLACE INTO sessions
           (session_id, year, round, event_name, location, session_type,
            session_date, had_rainfall, loaded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            int(event["EventDate"].year),
            int(event["RoundNumber"]),
            event["EventName"],
            event["Location"],
            session_type,
            session.date.date().isoformat(),
            None if rainfall is None else int(rainfall),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )


def upsert_laps(conn, laps_df):
    """Insert laps, overwriting any existing row with the same (session, driver, lap)."""
    columns = list(laps_df.columns)
    placeholders = ", ".join("?" for _ in columns)
    # Convert pandas NA/NaN to None so sqlite3 stores proper NULLs.
    rows = laps_df.astype(object).where(laps_df.notna(), None).itertuples(index=False)
    conn.executemany(
        f"INSERT OR REPLACE INTO laps ({', '.join(columns)}) VALUES ({placeholders})",
        rows,
    )


def pull_session(year, event, session_type="R", load_telemetry=True):
    """Full ETL for one session: extract with FastF1, transform, load into SQLite."""
    session = load_session(year, event, session_type, load_telemetry)
    session_id = make_session_id(year, int(session.event["RoundNumber"]), session_type)
    laps_df = transform_laps(session, session_id)

    with get_connection() as conn:   # the `with` block commits on success
        upsert_session(conn, session, session_id, session_type)
        upsert_laps(conn, laps_df)

    n_accurate = int(laps_df["is_accurate"].sum())
    print(f"{session_id} {session.event['EventName']}: "
          f"{len(laps_df)} laps stored ({n_accurate} flagged accurate)")
    return session_id


if __name__ == "__main__":
    fastf1.set_log_level("WARNING")
    if len(sys.argv) == 4:
        pull_session(int(sys.argv[1]), sys.argv[2], sys.argv[3])
    else:
        for year, event in DEFAULT_RACES:
            pull_session(year, event, "R")
