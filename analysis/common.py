"""Data access and the shared definition of a 'clean' lap, used by every analysis."""

from etl.db import read_sql


def load_sessions():
    return read_sql("SELECT * FROM sessions ORDER BY year, round")


def load_laps(session_id):
    return read_sql("SELECT * FROM laps WHERE session_id = ?", (session_id,))


def clean_laps(laps):
    """Keep laps that represent genuine racing pace.

    - is_accurate: FastF1's own check (timing is reliable; not lap 1, not a pit in/out lap).
    - track_status == '1': green flag for the whole lap. IsAccurate already drops safety-car
      laps, but it keeps yellow-flag laps (328 of 10,594 accurate laps in our 10 races),
      where drivers must slow down in the flagged zone.

    2010-2017 races (timing_source 'jolpica') have no flag data, so track_status is NULL and
    is_accurate carries the whole definition (see etl/pull_history.py).
    """
    mask = (
        (laps["is_accurate"] == 1)
        & ((laps["track_status"] == "1") | laps["track_status"].isna())
        & laps["lap_time_s"].notna()
    )
    return laps[mask].copy()
