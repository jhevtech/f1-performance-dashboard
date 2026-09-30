"""Gap tracking: how the time gap between two drivers evolves over a race.

Two independent sources, compared lap by lap:

  FastF1: `lap_end_time_s` is the session clock when a driver crossed the line to finish
  a lap, i.e. the cumulative sum of their lap times. So the gap at the end of lap n is
  simply  end_time(B, lap n) - end_time(A, lap n). Using the timing clock directly avoids
  the classic cumsum problem where one missing lap time breaks every later value.

  OpenF1 /intervals: the official live-timing gap to the leader, emitted whenever it
  changes. gap(A->B) = gap_to_leader(B) - gap_to_leader(A), each read at the moment that
  driver crosses the line.

Which one is plotted: OpenF1 was tried first, as planned. Validated on five 2023 races,
the two agree to a median of 0.000 s per lap, but FastF1 has complete coverage and matched
the official finishing margin in all five (e.g. Spain VER-HAM 24.090 s, Monza VER-PER
6.064 s), while OpenF1's final-lap value was off at Monza (6.802 s). So FastF1 is primary
and OpenF1 is kept as an independent cross-check that fills any lap FastF1 is missing.
Positive gap = A is ahead.
"""

import pandas as pd

from analysis.common import load_laps
from etl.db import read_sql


def gap_from_fastf1(laps, driver_a, driver_b):
    """Per-lap gap from FastF1 timing: B's line-crossing time minus A's."""
    wide = (
        laps[laps["driver"].isin([driver_a, driver_b])]
        .pivot(index="lap_number", columns="driver", values="lap_end_time_s")
        .dropna()
    )
    return (wide[driver_b] - wide[driver_a]).rename("gap_fastf1_s")


def _openf1_gaps(session_id, driver_number):
    df = read_sql(
        """SELECT date, gap_to_leader FROM openf1_intervals
           WHERE session_id = ? AND driver_number = ? AND gap_to_leader IS NOT NULL
           ORDER BY date""",
        (session_id, driver_number),
    )
    df["date"] = pd.to_datetime(df["date"], format="ISO8601", utc=True)
    return df


LINE_CROSSING_LATENCY_S = 1.0   # OpenF1 posts the new gap shortly after the car crosses the line


def _openf1_gap_at_lap_ends(laps, session_id, driver):
    """One driver's OpenF1 gap_to_leader at the moment they finish each lap."""
    driver_laps = laps[laps["driver"] == driver]
    lap_ends = pd.DataFrame({
        "lap_number": driver_laps["lap_number"],
        "date": pd.to_datetime(driver_laps["lap_start_date"], utc=True)
        + pd.to_timedelta(driver_laps["lap_time_s"] + LINE_CROSSING_LATENCY_S, unit="s"),
    }).dropna().sort_values("date")
    samples = _openf1_gaps(session_id, driver_laps["driver_number"].iloc[0])
    if samples.empty or lap_ends.empty:   # OpenF1 not stored, or laps lack UTC timestamps
        return pd.Series(dtype=float, name="gap_to_leader")
    # merge_asof: take the most recent sample at or before each lap end. OpenF1 only emits a
    # sample when a value changes (the leader's gap stays 0 for minutes), so carrying the
    # last value forward is correct; the 120 s cap stops a stale value surviving a data gap.
    merged = pd.merge_asof(lap_ends, samples, on="date", tolerance=pd.Timedelta(seconds=120))
    return merged.set_index("lap_number")["gap_to_leader"]


def gap_from_openf1(laps, session_id, driver_a, driver_b):
    """Per-lap gap from OpenF1: B's gap to the leader minus A's, each read as that driver
    crosses the line (reading both at A's crossing would use a stale value for B).

    Returns an empty Series if OpenF1 has no numeric gap data for either driver.
    """
    gap_a = _openf1_gap_at_lap_ends(laps, session_id, driver_a)
    gap_b = _openf1_gap_at_lap_ends(laps, session_id, driver_b)
    return (gap_b - gap_a).rename("gap_openf1_s").dropna()


def gap_series(session_id, driver_a, driver_b, laps=None):
    """Lap-by-lap gap from both sources, plus the value to plot (FastF1, OpenF1 as fallback)."""
    laps = load_laps(session_id) if laps is None else laps
    result = pd.concat(
        [gap_from_fastf1(laps, driver_a, driver_b),
         gap_from_openf1(laps, session_id, driver_a, driver_b)],
        axis=1,
    )
    result["gap_s"] = result["gap_fastf1_s"].fillna(result["gap_openf1_s"])
    result["source"] = result["gap_fastf1_s"].notna().map({True: "FastF1", False: "OpenF1"})
    result.index.name = "lap_number"
    return result.reset_index()


def source_agreement(gaps):
    """How closely the two sources agree on laps where both have a value."""
    both = gaps.dropna(subset=["gap_fastf1_s", "gap_openf1_s"])
    diff = (both["gap_openf1_s"] - both["gap_fastf1_s"]).abs()
    return {"laps_compared": len(both), "median_abs_diff_s": diff.median(), "max_abs_diff_s": diff.max()}


if __name__ == "__main__":
    import sys

    session_id, a, b = (sys.argv[1:4] if len(sys.argv) == 4 else ("2023_07_R", "VER", "HAM"))
    gaps = gap_series(session_id, a, b)
    print(f"{session_id}: gap {a} -> {b} (positive = {a} ahead)\n")
    print(gaps.iloc[::5].round(3).to_string(index=False))
    print(f"\nFinal gap: {gaps['gap_s'].iloc[-1]:.3f} s")
    print("Source agreement:", {k: round(float(v), 3) for k, v in source_agreement(gaps).items()})
