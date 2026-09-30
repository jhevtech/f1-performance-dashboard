"""Upgrade impact: after a team brought an upgrade, was its car faster than the version
it ran in the races before?

Upgrades come from the FIA's Car Presentation Submissions (etl/pull_upgrades.py), which
list every part each team declares as new at each Grand Prix. The FIA publishes them from
2024; earlier seasons have no upgrade data.

Method, per race: each team's race pace is the median of its drivers' clean dry-tire laps
(slow outliers > 107% of the driver's median removed), expressed as % slower than the
fastest team that day. Comparing with the fastest team rather than using raw lap times
removes most of the track-to-track difference. The upgrade's effect is then the change in
that gap between the races before the upgrade and the races from the upgrade onward.

Limits (the dashboard repeats these):
  - Other teams upgrade too, so the fastest team is a moving benchmark; a team can improve
    and still see its gap grow.
  - Tracks suit cars differently, so a few races either side is a noisy comparison.
  - Many packages are circuit-specific (a low-drag rear wing for Monza) and aren't meant to
    carry over, so they're flagged by their declared reason.
  - An upgrade is usually one of several changes in a window; this can't separate them.
"""

import numpy as np
import pandas as pd

from analysis.common import clean_laps, load_laps, load_sessions
from analysis.tire_degradation import with_dry_compounds
from etl.db import read_sql
from etl.teams import team_key

FIRST_UPGRADE_YEAR = 2024   # first season the FIA publishes Car Presentation Submissions
WINDOW = 3                  # races compared on each side of the upgrade
OUTLIER_RATIO = 1.07
# Below this many clean dry laps a team's race median isn't trustworthy: at the wet 2025
# British GP Racing Bulls had 2, which made them look 13.8% off the pace. Such races are
# left out, and the before/after windows reach past them to the next usable race.
MIN_CLEAN_LAPS = 20


def load_upgrades(year):
    return read_sql("SELECT * FROM upgrades WHERE year = ? ORDER BY round, team, item", (year,))


def upgrade_packages(upgrades):
    """One row per team per Grand Prix: how many parts, and how many were declared as
    performance upgrades (reason 'Performance - ...') rather than circuit-specific,
    reliability or cooling changes."""
    if upgrades.empty:
        return pd.DataFrame(columns=["round", "event_name", "team", "parts",
                                     "performance_parts", "components"])
    upgrades = upgrades.assign(performance=upgrades["reason"].str.startswith("Performance"))
    return (
        upgrades.groupby(["round", "event_name", "team"])
        .agg(parts=("item", "size"),
             performance_parts=("performance", "sum"),
             components=("component", lambda c: ", ".join(dict.fromkeys(c))))
        .reset_index()
        .sort_values(["round", "performance_parts"], ascending=[True, False])
    )


def team_race_pace(laps):
    """One race -> each team's median clean lap and its gap to the fastest team (%)."""
    laps = with_dry_compounds(clean_laps(laps))
    laps = laps[laps["lap_time_s"] <= OUTLIER_RATIO * laps.groupby("driver")["lap_time_s"].transform("median")]
    # Normalise names so FastF1's 'RB' and the FIA's 'Visa Cash App RB' are the same team.
    laps = laps.assign(team=laps["team"].map(lambda t: team_key(t) or t))
    pace = laps.groupby("team").agg(median_lap_s=("lap_time_s", "median"),
                                    clean_laps=("lap_time_s", "size")).reset_index()
    enough = pace["clean_laps"] >= MIN_CLEAN_LAPS
    reliable = pace["median_lap_s"].where(enough)
    pace["gap_to_fastest_pct"] = 100 * (reliable / reliable.min() - 1)
    pace["pace_rank"] = reliable.rank()          # NaN where there are too few laps
    return pace


def season_pace(year, sessions=None):
    """team_race_pace for every race of a season, stacked (one row per team per race)."""
    sessions = load_sessions() if sessions is None else sessions
    races = sessions[(sessions["year"] == year) & (sessions["session_type"] == "R")]
    frames = [
        team_race_pace(load_laps(s.session_id)).assign(
            round=s.round, event=s.event_name.replace(" Grand Prix", ""), wet=s.had_rainfall == 1)   # NULL (unknown) before 2018
        for s in races.itertuples(index=False)
    ]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def upgrade_effect(pace, team, upgrade_round, window=WINDOW):
    """Compare a team's gap to the fastest team before vs from the upgrade race onward.

    `change_pct` < 0 means the car got closer to the front after the upgrade. As in the
    2023 McLaren study, a straight line through the races before is extended to the upgrade
    race, to show whether the gap was already shrinking before the new parts arrived.
    """
    team_pace = pace[pace["team"] == team].dropna(subset=["gap_to_fastest_pct"]).sort_values("round")
    before = team_pace[team_pace["round"] < upgrade_round].tail(window)
    after = team_pace[team_pace["round"] >= upgrade_round].head(window)
    result = {
        "races_before": len(before),
        "races_after": len(after),
        "gap_before_pct": before["gap_to_fastest_pct"].mean(),
        "gap_after_pct": after["gap_to_fastest_pct"].mean(),
        "rank_before": before["pace_rank"].mean(),
        "rank_after": after["pace_rank"].mean(),
        "trend_predicted_pct": np.nan,
    }
    result["change_pct"] = result["gap_after_pct"] - result["gap_before_pct"]
    if len(before) >= 3 and len(after):
        slope, intercept = np.polyfit(before["round"], before["gap_to_fastest_pct"], deg=1)
        result["trend_predicted_pct"] = slope * after["round"].iloc[0] + intercept
    return result


def season_upgrade_effects(pace, packages, window=WINDOW):
    """upgrade_effect for every package with at least one performance part."""
    rows = []
    for p in packages[packages["performance_parts"] > 0].itertuples(index=False):
        rows.append({"round": p.round, "event_name": p.event_name, "team": p.team,
                     "parts": p.parts, "performance_parts": p.performance_parts,
                     **upgrade_effect(pace, p.team, p.round, window)})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import sys

    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2024
    pd.set_option("display.width", 180)
    pace = season_pace(year)
    effects = season_upgrade_effects(pace, upgrade_packages(load_upgrades(year)))
    print(f"{year}: {len(effects)} performance upgrade packages\n")
    print(effects.round(3).to_string(index=False))
