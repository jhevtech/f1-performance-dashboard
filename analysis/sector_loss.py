"""Sector loss: where on track each driver is losing time relative to their own best.

For every driver:
  1. personal best in each sector = fastest Sector 1/2/3 time across their clean laps
  2. per-lap delta = that lap's sector time - the personal best for that sector
  3. aggregate the deltas per sector: total seconds lost over the race, and mean per lap

Comparing a driver to *their own* best (not to the leader) removes car performance from
the picture, so the result says "where this driver's pace fades", not "where their car is slow".

Caveats worth stating:
  - Sectors differ in length, so a longer sector naturally accumulates more absolute loss.
    `pct_loss` (mean delta / sector best) normalises for that.
  - Losses include fuel load and tire wear, which affect all three sectors; the useful
    signal is the *relative* split between sectors, e.g. a sector full of traction zones
    losing disproportionately as rear tires wear.
"""

import pandas as pd

from analysis.common import clean_laps, load_laps

SECTORS = ["sector1_s", "sector2_s", "sector3_s"]


def sector_deltas(laps):
    """Per-lap delta from each driver's personal-best sector time (long format)."""
    laps = clean_laps(laps).dropna(subset=SECTORS)
    long = laps.melt(
        id_vars=["driver", "team", "lap_number", "compound", "tyre_life"],
        value_vars=SECTORS,
        var_name="sector",
        value_name="sector_time_s",
    )
    long["sector"] = long["sector"].map({"sector1_s": "S1", "sector2_s": "S2", "sector3_s": "S3"})
    long["personal_best_s"] = long.groupby(["driver", "sector"])["sector_time_s"].transform("min")
    long["delta_s"] = long["sector_time_s"] - long["personal_best_s"]
    return long


def sector_loss_summary(laps):
    """Total and mean loss per driver per sector, plus which sector costs each driver most."""
    deltas = sector_deltas(laps)
    summary = (
        deltas.groupby(["driver", "team", "sector"])
        .agg(
            laps=("delta_s", "size"),
            personal_best_s=("personal_best_s", "first"),
            total_loss_s=("delta_s", "sum"),
            mean_loss_s=("delta_s", "mean"),
        )
        .reset_index()
    )
    summary["pct_loss"] = 100 * summary["mean_loss_s"] / summary["personal_best_s"]
    # Each sector's share of the driver's total time lost: answers "where is the time going".
    summary["share_of_loss"] = summary["total_loss_s"] / summary.groupby("driver")["total_loss_s"].transform("sum")
    return summary


def worst_sector_per_driver(summary):
    """One row per driver: the sector with the largest total time lost."""
    idx = summary.groupby("driver")["total_loss_s"].idxmax()
    worst = summary.loc[idx, ["driver", "team", "sector", "total_loss_s", "share_of_loss", "pct_loss"]]
    return worst.rename(columns={"sector": "worst_sector"}).sort_values("total_loss_s", ascending=False)


if __name__ == "__main__":
    import sys

    session_id = sys.argv[1] if len(sys.argv) > 1 else "2023_07_R"
    summary = sector_loss_summary(load_laps(session_id))
    pd.set_option("display.width", 140)
    print(f"Session {session_id}\n")
    print("Field-wide share of time lost per sector (median across drivers):")
    print(summary.groupby("sector")[["share_of_loss", "pct_loss"]].median().round(3), "\n")
    print(worst_sector_per_driver(summary).round(3).to_string(index=False))
