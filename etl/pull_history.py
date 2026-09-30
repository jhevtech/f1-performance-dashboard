"""Load the 2010-2017 races, which FastF1 has no lap timing for, from the Jolpica-F1 dump.

FastF1's lap timing starts in 2018. For earlier seasons the only lap-level source is
Jolpica (the Ergast replacement), which has every driver's lap time and position, pit
stops from 2011, and the official classification. It has no sector times, tire compounds
or flag data, so these races are stored with `sessions.timing_source = 'jolpica'` and the
analyses adapt (see analysis/common.py).

Jolpica's API returns 100 rows per request (~15 requests per race, at 500 requests/hour),
so instead of crawling it this reads the full database dump Jolpica publishes as one zip.

Derived columns, since Jolpica has no equivalents:
  - pit_in / pit_out: Jolpica records a stop on the in-lap (lap N); the out-lap (N+1) is the
    slow one (median +18.5 s vs +5 s, measured on 2011-2017). Same meaning as FastF1's flags.
    2010 has no pit stop data, so a stop is inferred when a lap is > 12 s slower than that
    lap's field median. Comparing with the field, not the driver, still catches stops made
    under a safety car, when every car is slow.
  - stint / tyre_life: stint = 1 + stops so far; tyre_life = laps since the stint started.
    The true age of the set is unknown (cars started on used qualifying tires), which shifts
    the intercept of a degradation fit but not its slope.
  - is_accurate: no flag data exists, so a lap counts as accurate unless it's lap 1, a pit
    in/out lap, or a lap where the field median was > 107% of normal (safety car, virtual
    safety car or red flag; heavy rain looks the same and is also dropped).
  - lap_end_time_s: cumulative race time, so gap_tracking works unchanged.

Usage (from the project root):
    python -m etl.pull_history            # 2010-2017
    python -m etl.pull_history 2012       # one season
    python -m etl.pull_history 2018 14    # one race FastF1 has no timing for (2018 Italian GP)
"""

import io
import sys
import zipfile
from datetime import datetime, timezone

import pandas as pd
import requests

from etl.db import PROJECT_ROOT, get_connection
from etl.pull_sessions import make_session_id, upsert_laps

DUMP_URL = "https://api.jolpi.ca/data/dumps/download/delayed/?dump_type=csv"
DUMP_PATH = PROJECT_ROOT / "cache" / "jolpica-f1-csv.zip"
YEARS = range(2010, 2018)          # 2018 onward comes from FastF1
PIT_OUT_LAP_EXCESS_S = 12          # 2010 only: out-lap threshold vs the lap's field median
NEUTRALISED_LAP_RATIO = 1.07       # field median above this x normal = SC / VSC / red flag


def load_dump():
    """{table name: DataFrame} from the Jolpica CSV dump, downloaded once and cached."""
    if not DUMP_PATH.exists():
        DUMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(DUMP_URL, timeout=300)
        response.raise_for_status()
        DUMP_PATH.write_bytes(response.content)
    tables = {}
    with zipfile.ZipFile(DUMP_PATH) as archive:
        for name in ["season", "round", "circuit", "session", "sessionentry", "roundentry",
                     "teamdriver", "driver", "team", "lap", "pitstop"]:
            with archive.open(f"formula_one_{name}.csv") as f:
                tables[name] = pd.read_csv(io.TextIOWrapper(f, "utf-8"), low_memory=False)
    return tables


def race_entries(t, years):
    """One row per driver per race: who, which team, and their official result."""
    rounds = (t["round"][t["round"]["is_cancelled"] == "f"]
              .merge(t["season"][["id", "year"]].rename(columns={"id": "season_id"}))
              .merge(t["circuit"][["id", "locality"]].rename(columns={"id": "circuit_id"})))
    rounds = rounds[rounds["year"].isin(years)].astype({"number": int})
    races = t["session"][t["session"]["type"] == "R"].merge(
        rounds[["id", "year", "number", "name", "date", "locality"]]
        .rename(columns={"id": "round_id", "number": "round"}))
    drivers = t["driver"].assign(
        # A few drivers have no official three-letter code; use the surname like FIA timing does.
        code=lambda d: d["abbreviation"].fillna(d["surname"].str.normalize("NFKD")
                                                .str.encode("ascii", "ignore").str.decode("ascii")
                                                .str[:3].str.upper()),
        driver_name=lambda d: d["forename"] + " " + d["surname"],
    )
    return (
        t["sessionentry"]
        .merge(races[["id", "year", "round", "name", "date", "locality"]]
               .rename(columns={"id": "session_id"}))
        .merge(t["roundentry"][["id", "team_driver_id", "car_number"]]
               .rename(columns={"id": "round_entry_id"}))
        .merge(t["teamdriver"][["id", "driver_id", "team_id"]].rename(columns={"id": "team_driver_id"}))
        .merge(drivers[["id", "code", "driver_name"]].rename(columns={"id": "driver_id"}))
        .merge(t["team"][["id", "name"]].rename(columns={"id": "team_id", "name": "team"}))
        .rename(columns={"id": "session_entry_id"})
    )


def build_laps(entries, laps, pitstops, year):
    """Every lap of every driver in one race, shaped like the `laps` table."""
    df = laps.merge(entries[["session_entry_id", "code", "car_number", "team"]])
    df = df.rename(columns={"number": "lap_number"}).sort_values(["code", "lap_number"])
    df["lap_time_s"] = pd.to_timedelta(df["time"]).dt.total_seconds()

    # A lap where the whole field was slow = neutralised (safety car, VSC, red flag).
    field_median = df.groupby("lap_number")["lap_time_s"].transform("median")
    neutralised = field_median > NEUTRALISED_LAP_RATIO * df.groupby("lap_number")["lap_time_s"].median().median()

    if year >= 2011:
        # (pit stops have their own `number` column, the stop count, so rename the lap's.)
        stops = pitstops.merge(laps[["id", "number"]].rename(columns={"id": "lap_id", "number": "in_lap"}),
                               on="lap_id")
        in_laps = set(zip(stops["session_entry_id"], stops["in_lap"]))
        df["pit_in"] = [(e, n) in in_laps for e, n in zip(df["session_entry_id"], df["lap_number"])]
        df["pit_out"] = df.groupby("code")["pit_in"].shift(1, fill_value=False)
    else:
        df["pit_out"] = (df["lap_time_s"] - field_median > PIT_OUT_LAP_EXCESS_S) & (df["lap_number"] > 1)
        df["pit_in"] = df.groupby("code")["pit_out"].shift(-1, fill_value=False)

    df["stint"] = df.groupby("code")["pit_out"].cumsum() + 1
    df["tyre_life"] = df.groupby(["code", "stint"]).cumcount() + 1
    # Cumulative race time; after a missing lap time the sum would be wrong, so stop there.
    broken = df["lap_time_s"].isna().groupby(df["code"]).cummax()
    df["lap_end_time_s"] = df.groupby("code")["lap_time_s"].cumsum().mask(broken)
    df["is_accurate"] = ~(neutralised | df["pit_in"] | df["pit_out"]
                          | (df["lap_number"] == 1) | df["lap_time_s"].isna())

    return pd.DataFrame({
        "driver": df["code"],
        "driver_number": df["car_number"].astype("Int64").astype(str),
        "team": df["team"],
        "lap_number": df["lap_number"].astype(int),
        "lap_time_s": df["lap_time_s"],
        "lap_end_time_s": df["lap_end_time_s"],
        "stint": df["stint"].astype(int),
        "tyre_life": df["tyre_life"].astype(float),
        "position": df["position"],
        "pit_in": df["pit_in"].astype(int),
        "pit_out": df["pit_out"].astype(int),
        "is_accurate": df["is_accurate"].astype(int),
    })


def build_results(entries):
    """Official classification, shaped like `race_results`. Retired drivers have no position
    in the dump; like Ergast, rank them after the classified cars by laps completed."""
    df = entries.sort_values(["position", "laps_completed"], ascending=[True, False], na_position="last")
    return pd.DataFrame({
        "driver": df["code"],
        "driver_name": df["driver_name"],
        "team": df["team"],
        "grid": df["grid"],
        "position": range(1, len(df) + 1),
        "points": df["points"],
        "status": df["detail"],
    })


def pull_history(years=YEARS, round_number=None):
    t = load_dump()
    entries = race_entries(t, years)
    if round_number is not None:
        entries = entries[entries["round"] == round_number]
    loaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for (year, round_number), race in entries.groupby(["year", "round"]):
        year, round_number = int(year), int(round_number)   # numpy ints would bind as BLOBs
        session_id = make_session_id(year, round_number, "R")
        ids = set(race["session_entry_id"])
        laps = t["lap"][t["lap"]["session_entry_id"].isin(ids)]
        pitstops = t["pitstop"][t["pitstop"]["session_entry_id"].isin(ids)]
        if laps.empty:
            print(f"  {session_id}: no lap data in the dump, skipped")
            continue
        laps_df = build_laps(race, laps, pitstops, year).assign(session_id=session_id)
        results = build_results(race).assign(year=year, round=round_number)
        first = race.iloc[0]

        with get_connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO sessions
                   (session_id, year, round, event_name, location, session_type,
                    session_date, had_rainfall, loaded_at, timing_source)
                   VALUES (?, ?, ?, ?, ?, 'R', ?, NULL, ?, 'jolpica')""",
                (session_id, year, round_number, first["name"], first["locality"],
                 first["date"], loaded_at),
            )
            conn.execute("DELETE FROM laps WHERE session_id = ?", (session_id,))
            upsert_laps(conn, laps_df)
            conn.execute("DELETE FROM race_results WHERE year = ? AND round = ?", (year, round_number))
            conn.executemany(
                """INSERT INTO race_results
                   (year, round, driver, driver_name, team, grid, position, points, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                results[["year", "round", "driver", "driver_name", "team", "grid", "position",
                         "points", "status"]].astype(object).where(results.notna(), None)
                .itertuples(index=False),
            )
        print(f"{session_id} {first['name']}: {len(laps_df)} laps stored "
              f"({int(laps_df['is_accurate'].sum())} usable, {int(laps_df['pit_in'].sum())} pit stops)")


if __name__ == "__main__":
    args = [int(a) for a in sys.argv[1:]]
    pull_history(args[:1] or YEARS, args[1] if len(args) > 1 else None)
