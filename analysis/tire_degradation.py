"""Tire degradation: how much lap time a driver loses per lap of tire age, per compound.

Method: for each (driver, stint, compound) group of clean laps, fit a straight line
    lap_time = slope * tyre_life + intercept
with numpy.polyfit(deg=1). The slope is the degradation rate in seconds per lap.

Fuel correction: a car burns ~1.5-2 kg of fuel per lap and gets faster as it gets lighter,
which hides tire wear (raw slopes are often near zero or negative). We add back a fixed
estimate of the fuel effect before fitting. 0.06 s/lap is a commonly used ballpark
(~0.03 s per kg x ~1.8 kg per lap); it is an assumption, and it is shown next to the raw slope.
"""

import numpy as np
import pandas as pd

from analysis.common import clean_laps, load_laps

FUEL_EFFECT_S_PER_LAP = 0.06
MIN_LAPS_PER_STINT = 6          # fewer points than this gives an unreliable slope
PLAUSIBLE_RANGE = (0.05, 0.30)  # s/lap; outside this, the fit is flagged as suspicious
DRY_COMPOUNDS = ["SOFT", "MEDIUM", "HARD"]
# 2018 used Pirelli's older range, softest first. Its SOFT is a different tire from 2019's
# SOFT (2019+ names are relative to each weekend's selection), so compare 2018 only with itself.
DRY_COMPOUNDS_2018 = ["HYPERSOFT", "ULTRASOFT", "SUPERSOFT", "SOFT", "MEDIUM", "HARD", "SUPERHARD"]
# 2010-2017 races have no compound data; every stint is labelled this.
UNKNOWN_COMPOUND = "UNKNOWN"
COMPOUND_ORDER = DRY_COMPOUNDS_2018 + [UNKNOWN_COMPOUND]


def with_dry_compounds(laps):
    """Laps on dry tires. A race with no compound data at all (2010-2017) keeps every lap,
    labelled UNKNOWN: wet running can't be told apart there, beyond the heavy-rain laps
    that clean_laps already drops as neutralised."""
    if laps["compound"].isna().all():
        return laps.assign(compound=UNKNOWN_COMPOUND)
    return laps[laps["compound"].isin(DRY_COMPOUNDS_2018)]


def _remove_slow_outliers(stint_laps, threshold=1.02):
    """Drop laps more than 2% slower than the stint median: traffic, lock-ups, blue flags.

    These are real laps but not tire-limited ones, and a single 3-second outlier can
    swing a 20-lap regression by more than the degradation we're trying to measure.
    """
    median = stint_laps["lap_time_s"].median()
    return stint_laps[stint_laps["lap_time_s"] <= median * threshold]


def degradation_by_stint(laps):
    """Fit one degradation line per driver/stint/compound. Returns one row per stint."""
    laps = clean_laps(laps)
    # Some feeds miss tire age or stint on a few laps; polyfit fails on NaN, so drop those.
    laps = with_dry_compounds(laps).dropna(subset=["tyre_life", "stint"])
    # Fuel-corrected time = what the lap would have been at the start-of-race fuel load.
    laps["fuel_corrected_s"] = laps["lap_time_s"] + FUEL_EFFECT_S_PER_LAP * (laps["lap_number"] - 1)

    rows = []
    for (driver, stint, compound), group in laps.groupby(["driver", "stint", "compound"]):
        group = _remove_slow_outliers(group)
        if len(group) < MIN_LAPS_PER_STINT:
            continue
        slope, intercept = np.polyfit(group["tyre_life"], group["fuel_corrected_s"], deg=1)
        raw_slope, _ = np.polyfit(group["tyre_life"], group["lap_time_s"], deg=1)
        rows.append({
            "driver": driver,
            "team": group["team"].iloc[0],
            "stint": int(stint),
            # Opening stints degrade suspiciously little across every compound (DRS trains,
            # tire management, track rubbering-in), so they are flagged for separate analysis.
            "opening_stint": int(stint) == 1,
            "compound": compound,
            "laps_used": len(group),
            "tyre_life_start": int(group["tyre_life"].min()),
            "tyre_life_end": int(group["tyre_life"].max()),
            "deg_s_per_lap": slope,
            "intercept_s": intercept,
            "raw_slope_s_per_lap": raw_slope,
            "suspicious": not (PLAUSIBLE_RANGE[0] <= slope <= PLAUSIBLE_RANGE[1]),
        })
    return pd.DataFrame(rows)


def degradation_by_compound(stints, exclude_opening_stints=False):
    """Summarise stint-level rates by compound, using the median to resist outliers."""
    if stints.empty:
        return pd.DataFrame()
    if exclude_opening_stints:
        stints = stints[~stints["opening_stint"]]
    return (
        stints.groupby("compound")
        .agg(
            stints=("deg_s_per_lap", "size"),
            median_deg_s_per_lap=("deg_s_per_lap", "median"),
            mean_deg_s_per_lap=("deg_s_per_lap", "mean"),
            median_raw_slope=("raw_slope_s_per_lap", "median"),
            suspicious_stints=("suspicious", "sum"),
        )
        .reindex([c for c in COMPOUND_ORDER if c in set(stints["compound"])])
    )


def stint_laps_for_plot(laps, driver, stint):
    """Clean, fuel-corrected laps for one stint, for plotting the points behind a fit."""
    laps = clean_laps(laps)
    laps = with_dry_compounds(laps)
    stint_laps = laps[(laps["driver"] == driver) & (laps["stint"] == stint)].dropna(subset=["tyre_life"])
    stint_laps["fuel_corrected_s"] = (
        stint_laps["lap_time_s"] + FUEL_EFFECT_S_PER_LAP * (stint_laps["lap_number"] - 1)
    )
    return _remove_slow_outliers(stint_laps)


if __name__ == "__main__":
    import sys

    session_id = sys.argv[1] if len(sys.argv) > 1 else "2023_07_R"
    stints = degradation_by_stint(load_laps(session_id))
    pd.set_option("display.width", 140)
    print(f"Session {session_id}: {len(stints)} stints fitted\n")
    print(degradation_by_compound(stints).round(3), "\n")
    print(stints[stints["driver"] == "VER"].round(3).to_string(index=False))
