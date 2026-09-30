"""Bring the database up to date: pull every completed race that isn't stored yet.

Run it any time; it only downloads what's missing, so the first run back-fills history
and later runs (e.g. weekly, from GitHub Actions) just add the latest race.

It also pulls any new FIA upgrade documents (etl/pull_upgrades.py). The 2010-2017 races
come from a one-off Jolpica load (etl/pull_history.py) and never change, so they aren't here.

Usage (from the project root):
    python -m etl.update          # 2018 season -> today
    python -m etl.update 2024     # start from a different season

Why 2018: it's the first season FastF1 has lap timing for.
Back-filled races skip telemetry (much faster to download); telemetry is only needed to
line laps up with OpenF1, which we keep for the 2023 reference races only.
"""

import sys
import time

import fastf1
import pandas as pd
from fastf1.req import RateLimitExceededError

from etl.db import get_connection, read_sql
from etl.pull_sessions import make_session_id, pull_session
from etl.pull_standings import pull_rounds
from etl.pull_upgrades import update as update_upgrades

FIRST_YEAR = 2018
# Timing data and official results usually appear within a few hours of the flag;
# waiting 6 hours avoids storing a half-published race.
DATA_DELAY = pd.Timedelta(hours=6)
# FastF1 allows 500 API calls per hour (~10 per race). A back-fill of many seasons runs into
# that, so on a rate-limit error we wait and retry the same race instead of skipping it.
RATE_LIMIT_WAIT_S = 15 * 60


def completed_races(year):
    """(year, round) for every race in `year` that finished at least DATA_DELAY ago."""
    schedule = fastf1.get_event_schedule(year, include_testing=False)
    cutoff = pd.Timestamp.now(tz="UTC").tz_localize(None) - DATA_DELAY
    # Session5 is the race on every weekend format (conventional and sprint).
    done = schedule[schedule["Session5DateUtc"] < cutoff]
    return [(year, int(r)) for r in done["RoundNumber"]]


def missing_races(first_year):
    stored = set(read_sql("SELECT session_id FROM sessions")["session_id"])
    races = []
    for year in range(first_year, pd.Timestamp.now(tz="UTC").year + 1):
        races += [(y, r) for y, r in completed_races(year) if make_session_id(y, r, "R") not in stored]
    return races


def races_without_results():
    """Races whose laps are stored but whose Jolpica results weren't published yet."""
    return list(read_sql(
        """SELECT s.year, s.round FROM sessions s
           LEFT JOIN race_results r ON r.year = s.year AND r.round = s.round
           WHERE r.driver IS NULL ORDER BY s.year, s.round"""
    ).itertuples(index=False, name=None))


def update(first_year=FIRST_YEAR):
    races = missing_races(first_year)
    print(f"{len(races)} completed races missing from the database")
    failed = []
    for year, round_number in races:
        # One bad race (e.g. data not published yet) must not stop the rest of the run.
        while True:
            try:
                pull_session(year, round_number, "R", load_telemetry=False)
            except RateLimitExceededError:
                print(f"  FastF1 rate limit reached; waiting {RATE_LIMIT_WAIT_S // 60} min", flush=True)
                time.sleep(RATE_LIMIT_WAIT_S)
                continue
            except Exception as exc:
                failed.append((year, round_number))
                print(f"  skipped {year} round {round_number}: {exc}")
            break

    for year, round_number in races_without_results():
        try:
            pull_rounds([(year, round_number)])
        except Exception as exc:   # Jolpica can lag a few hours behind the race
            print(f"  no results yet for {year} round {round_number}: {exc}")

    try:
        update_upgrades()
    except Exception as exc:   # the FIA site is often slow; next week's run catches up
        print(f"  upgrade documents not updated: {exc}")

    with get_connection() as conn:
        conn.execute("VACUUM")   # reclaim space so the committed snapshot stays small
    print(f"Done. {len(races) - len(failed)} races added, {len(failed)} skipped: {failed}")


if __name__ == "__main__":
    fastf1.set_log_level("WARNING")
    update(int(sys.argv[1]) if len(sys.argv) > 1 else FIRST_YEAR)
