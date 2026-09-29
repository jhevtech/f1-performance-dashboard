"""Upgrade attribution: did McLaren's 2023 Austria upgrade produce a measurable pace change?

The upgrade (verified): at the 2023 Austrian GP (round 9, 2 July 2023) McLaren brought a
major package (new sidepods, engine cover and floor) but only had enough parts for Lando
Norris's car. Oscar Piastri received it one race later at the British GP (round 10).
Sources: McLaren MCL60 on Wikipedia; RacingNews365 "Piastri misses out on McLaren upgrade".

That split makes this a useful natural experiment:

  1. Teammate delta (Norris median lap - Piastri median lap, as % of lap time).
     Same car, same track, same weather, same day, so those confounders cancel.
     Prediction if the upgrade worked: Norris gains relative to Piastri at round 9 ONLY,
     and the gap closes again at round 10 once both cars have the parts.
  2. Team gap to the fastest team (% of lap time). Captures the team-wide step change that
     a teammate comparison cannot see once both cars are upgraded, at the cost of no
     longer controlling for track characteristics.

Limits of this method (say these out loud in an interview):
  - Tiny samples: one race with asymmetric cars and a handful of races either side.
  - Driver confounders: Piastri was a rookie improving race by race, so the teammate delta
    drifts on its own; Norris was also simply the stronger driver in 2023.
  - Track confounders: some tracks suit a car better (the MCL60 was known to be weak in
    slow corners), so the field-relative gap moves even without upgrades.
  - Race-specific events: strategy, traffic, damage, and wet running (Zandvoort) distort
    median lap times. Only dry-compound, green-flag clean laps are used to limit this.
  - Other teams upgrade too, so "gap to the fastest team" is a moving target.
"""

import numpy as np
import pandas as pd

from analysis.common import clean_laps, load_laps, load_sessions
from analysis.tire_degradation import DRY_COMPOUNDS

TEAM = "McLaren"
UPGRADED_DRIVER, TEAMMATE = "NOR", "PIA"
UPGRADE_ROUND = 9        # Austria 2023: Norris's car only
TEAMMATE_UPGRADE_ROUND = 10  # Britain 2023: Piastri's car too
MIN_CLEAN_LAPS = 10      # below this a driver's race median is not trustworthy


def race_pace_laps(laps):
    """Clean, dry-tyre laps, with each driver's slowest outliers (>107% of median) removed."""
    laps = clean_laps(laps)
    laps = laps[laps["compound"].isin(DRY_COMPOUNDS)]
    driver_median = laps.groupby("driver")["lap_time_s"].transform("median")
    return laps[laps["lap_time_s"] <= 1.07 * driver_median]


def pace_for_race(laps):
    """One race -> teammate delta and team gap to the fastest team."""
    laps = race_pace_laps(laps)

    # 1. Teammate delta
    counts = laps.groupby("driver").size()
    medians = laps.groupby("driver")["lap_time_s"].median()
    if min(counts.get(UPGRADED_DRIVER, 0), counts.get(TEAMMATE, 0)) >= MIN_CLEAN_LAPS:
        delta_s = medians[UPGRADED_DRIVER] - medians[TEAMMATE]
        teammate_delta_pct = 100 * delta_s / medians[TEAMMATE]
    else:
        delta_s = teammate_delta_pct = np.nan

    # 2. Team pace vs field: each team's median over all its drivers' clean laps.
    team_median = laps.groupby("team")["lap_time_s"].median()
    gap_to_fastest_pct = 100 * (team_median[TEAM] / team_median.min() - 1)
    team_rank = int(team_median.rank().loc[TEAM])

    return {
        "nor_median_s": medians.get(UPGRADED_DRIVER, np.nan),
        "pia_median_s": medians.get(TEAMMATE, np.nan),
        "teammate_delta_s": delta_s,             # negative = Norris faster
        "teammate_delta_pct": teammate_delta_pct,
        "team_gap_to_fastest_pct": gap_to_fastest_pct,
        "team_pace_rank": team_rank,             # 1 = fastest team on race pace
    }


def pace_by_race(sessions=None):
    """Run pace_for_race over every race session in the database."""
    sessions = load_sessions() if sessions is None else sessions
    sessions = sessions[sessions["session_type"] == "R"]
    rows = []
    for s in sessions.itertuples(index=False):
        row = {"round": s.round, "event": s.event_name.replace(" Grand Prix", ""),
               "wet": bool(s.had_rainfall)}
        row.update(pace_for_race(load_laps(s.session_id)))
        rows.append(row)
    df = pd.DataFrame(rows).sort_values("round")
    df["phase"] = np.select(
        [df["round"] < UPGRADE_ROUND, df["round"] < TEAMMATE_UPGRADE_ROUND],
        ["before", "NOR only"],
        default="both upgraded",
    )
    return df


def step_change(df, metric):
    """Compare the metric before vs after the upgrade, and against the pre-upgrade trend.

    If the metric was already improving steadily, a before/after difference could just be
    that trend continuing. So we fit a line through the pre-upgrade races and compare its
    prediction for the first fully-upgraded race with what actually happened. We only
    extrapolate one race ahead: a line through 4 points says nothing reliable 6 races out.
    """
    before = df[df["round"] < UPGRADE_ROUND].dropna(subset=[metric])
    after = df[df["round"] >= TEAMMATE_UPGRADE_ROUND].dropna(subset=[metric])
    slope, intercept = np.polyfit(before["round"], before[metric], deg=1)
    first_after = after.iloc[0]
    return {
        "metric": metric,
        "before_mean": float(before[metric].mean()),
        "after_mean": float(after[metric].mean()),
        "step": float(after[metric].mean() - before[metric].mean()),
        "pre_trend_per_race": float(slope),
        "trend_predicted_first_after": float(slope * first_after["round"] + intercept),
        "actual_first_after": float(first_after[metric]),
        "races_before": len(before),
        "races_after": len(after),
    }


if __name__ == "__main__":
    pd.set_option("display.width", 160)
    df = pace_by_race()
    print(df.round(3).to_string(index=False), "\n")
    upgrade_race = df[df["round"] == UPGRADE_ROUND].iloc[0]
    others = df[df["round"] != UPGRADE_ROUND]["teammate_delta_pct"]
    print(f"Teammate delta at the NOR-only race: {upgrade_race['teammate_delta_pct']:.3f}% "
          f"vs mean {others.mean():.3f}% (sd {others.std():.3f}) at the other races\n")
    for metric in ["team_gap_to_fastest_pct", "teammate_delta_pct"]:
        print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in step_change(df, metric).items()})
