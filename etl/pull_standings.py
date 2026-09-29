"""Pull official race classifications from Jolpica-F1 into the `race_results` table.

Jolpica is the community-run replacement for the Ergast API (shut down early 2025);
FastF1's `fastf1.ergast` module now points at it, so the interface is unchanged.

Usage (from the project root):
    python -m etl.pull_standings          # every round already present in `sessions`
    python -m etl.pull_standings 2023     # a full season
"""

import sys
import time

from fastf1.ergast import Ergast

from etl.db import get_connection, read_sql


def fetch_race_results(year, round_number):
    """Return the classification for one race as a DataFrame shaped like `race_results`."""
    response = Ergast(result_type="pandas", auto_cast=True).get_race_results(
        season=year, round=round_number
    )
    df = response.content[0]
    return df.assign(
        year=year,
        round=round_number,
        driver=df["driverCode"],
        driver_name=df["givenName"] + " " + df["familyName"],
        team=df["constructorName"],
    )[["year", "round", "driver", "driver_name", "team", "grid", "position", "points", "status"]]


def upsert_results(results):
    with get_connection() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO race_results
               (year, round, driver, driver_name, team, grid, position, points, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            results.astype(object).where(results.notna(), None).itertuples(index=False),
        )


def pull_rounds(rounds):
    for year, round_number in rounds:
        results = fetch_race_results(year, round_number)
        upsert_results(results)
        print(f"{year} round {round_number}: {len(results)} classified drivers stored")
        time.sleep(0.5)   # stay well under Jolpica's rate limit (4 requests/second)


if __name__ == "__main__":
    if len(sys.argv) == 2:
        year = int(sys.argv[1])
        n_rounds = Ergast().get_race_schedule(season=year).shape[0]
        pull_rounds([(year, r) for r in range(1, n_rounds + 1)])
    else:
        # Default: results for every race we already have lap data for.
        sessions = read_sql("SELECT DISTINCT year, round FROM sessions ORDER BY year, round")
        pull_rounds(sessions.itertuples(index=False))
