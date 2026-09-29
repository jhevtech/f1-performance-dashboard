"""Streamlit dashboard. Presentation only: every number comes from the analysis/ modules,
which read from the committed SQLite snapshot (no live API calls).

Run from the project root:  streamlit run dashboard/app.py
"""

import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

# Make the project root importable (Streamlit only adds this file's own folder to the path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import common, gap_tracking, sector_loss, tire_degradation, upgrade_attribution  # noqa: E402
from etl.db import read_sql  # noqa: E402

# UI colors come from the personal theme in .streamlit/config.toml; charts reuse it here.
THEME_PRIMARY = "#1D2A24"
THEME_ACCENT = "#5F7D6A"
LINE_COLOR = THEME_ACCENT      # single-series lines
REFERENCE_RULE = {"color": THEME_PRIMARY, "opacity": 0.35, "strokeDash": [4, 4]}  # zero line, upgrade markers

# Compounds keep Pirelli's red / yellow convention (hard is blue: white vanishes on a light
# background). The theme is one green hue, so it can't separate three categories on its own.
COMPOUND_COLORS = alt.Scale(domain=["SOFT", "MEDIUM", "HARD"], range=["#e34948", "#eda100", "#2a78d6"])
# Sectors run in order around the lap, so a light->dark ramp of the theme greens fits.
# #9BB29C replaces accent-soft #B7C9B5, which is too faint on white (1.7:1) for a data mark.
SECTOR_COLORS = alt.Scale(domain=["S1", "S2", "S3"], range=["#9BB29C", THEME_ACCENT, THEME_PRIMARY])

st.set_page_config(page_title="F1 Performance Intelligence", layout="wide")


# ---- Cached data access ------------------------------------------------------------------
@st.cache_data
def get_sessions():
    return common.load_sessions()


@st.cache_data
def get_laps(session_id):
    return common.load_laps(session_id)


@st.cache_data
def get_results(year, round_number):
    return read_sql("SELECT * FROM race_results WHERE year = ? AND round = ? ORDER BY position",
                    (year, round_number))


@st.cache_data
def get_upgrade_table():
    return upgrade_attribution.pace_by_race()


# ---- Sidebar -----------------------------------------------------------------------------
sessions = get_sessions()
labels = {row.session_id: f"{row.year} R{row.round} · {row.event_name}"
          + (" 🌧" if row.had_rainfall else "") for row in sessions.itertuples()}

st.sidebar.title("F1 Performance Intelligence")
session_id = st.sidebar.selectbox("Race", list(labels), format_func=labels.get,
                                  index=list(labels).index("2023_07_R"))
session = sessions.set_index("session_id").loc[session_id]
laps = get_laps(session_id)
results = get_results(int(session["year"]), int(session["round"]))

# Default to the podium; drivers ordered by finishing position.
drivers = results["driver"].tolist()
selected = st.sidebar.multiselect("Drivers", drivers, default=drivers[:3])
st.sidebar.caption("🌧 = rain recorded during the race. Data: FastF1, OpenF1, Jolpica-F1.")

st.title(labels[session_id])
tab_tires, tab_sectors, tab_gap, tab_upgrade = st.tabs(
    ["Tire degradation", "Sector loss", "Gap tracking", "Upgrade attribution"]
)

# ---- 1. Tire degradation -----------------------------------------------------------------
with tab_tires:
    st.markdown(
        "Lap time lost per lap of tire age, from a straight-line fit per driver stint "
        f"(fuel-corrected by +{tire_degradation.FUEL_EFFECT_S_PER_LAP} s/lap). "
        "Opening stints are usually flat for every compound (DRS trains, tire management), "
        "so they can be excluded."
    )
    stints = tire_degradation.degradation_by_stint(laps)
    exclude_opening = st.checkbox("Exclude opening stints", value=False)
    by_compound = tire_degradation.degradation_by_compound(stints, exclude_opening).reset_index()

    col_chart, col_table = st.columns([1, 1])
    col_chart.altair_chart(
        alt.Chart(by_compound).mark_bar(cornerRadiusEnd=4, size=40).encode(
            x=alt.X("compound:N", sort=["SOFT", "MEDIUM", "HARD"], title=None),
            y=alt.Y("median_deg_s_per_lap:Q", title="Median degradation (s/lap)"),
            color=alt.Color("compound:N", scale=COMPOUND_COLORS, legend=None),
            tooltip=["compound", alt.Tooltip("median_deg_s_per_lap:Q", format=".3f"), "stints"],
        ).properties(height=280),
        width="stretch",
    )
    col_table.dataframe(by_compound.round(3), hide_index=True)

    shown = stints[stints["driver"].isin(selected)]
    if not shown.empty:
        # Points = the actual laps; lines = the fitted model from the analysis module.
        points = pd.concat(
            tire_degradation.stint_laps_for_plot(laps, s.driver, s.stint).assign(label=f"{s.driver} stint {s.stint}")
            for s in shown.itertuples()
        )
        lines = pd.concat(
            pd.DataFrame({
                "label": f"{s.driver} stint {s.stint}", "compound": s.compound,
                "tyre_life": [s.tyre_life_start, s.tyre_life_end],
                "fuel_corrected_s": [s.intercept_s + s.deg_s_per_lap * t for t in (s.tyre_life_start, s.tyre_life_end)],
            })
            for s in shown.itertuples()
        )
        base = alt.Chart().encode(
            x=alt.X("tyre_life:Q", title="Tire age (laps)"),
            y=alt.Y("fuel_corrected_s:Q", title="Fuel-corrected lap time (s)", scale=alt.Scale(zero=False)),
            color=alt.Color("compound:N", scale=COMPOUND_COLORS),
            detail="label:N",
        )
        st.altair_chart(
            alt.layer(
                base.mark_circle(size=40, opacity=0.6).encode(
                    tooltip=["label", "lap_number", alt.Tooltip("lap_time_s:Q", format=".3f")]
                ).properties(data=points),
                base.mark_line(strokeWidth=2).properties(data=lines),
            ).properties(height=380),
            width="stretch",
        )
        st.dataframe(
            shown.drop(columns=["team", "intercept_s"]).round(3), hide_index=True,
            column_config={"suspicious": st.column_config.CheckboxColumn(
                help="Outside 0.05–0.30 s/lap: check for traffic or tire management")},
        )

# ---- 2. Sector loss ----------------------------------------------------------------------
with tab_sectors:
    st.markdown(
        "Time lost in each sector versus the driver's **own** best sector time, summed over "
        "every clean lap. Comparing drivers to themselves removes car performance, so this "
        "shows where each driver's pace fades."
    )
    summary = sector_loss.sector_loss_summary(laps)
    shown = summary[summary["driver"].isin(selected)]
    st.altair_chart(
        alt.Chart(shown).mark_bar(cornerRadiusEnd=4).encode(
            y=alt.Y("driver:N", sort=selected, title=None),
            x=alt.X("total_loss_s:Q", title="Total time lost vs personal best (s)"),
            color=alt.Color("sector:N", scale=SECTOR_COLORS, title="Sector"),
            yOffset="sector:N",
            tooltip=["driver", "sector", alt.Tooltip("total_loss_s:Q", format=".1f"),
                     alt.Tooltip("share_of_loss:Q", format=".0%"), alt.Tooltip("pct_loss:Q", format=".2f")],
        ).properties(height=max(160, 70 * len(selected))),
        width="stretch",
    )
    st.caption("Longer sectors accumulate more absolute loss; `pct_loss` normalises by sector length.")
    st.dataframe(sector_loss.worst_sector_per_driver(summary).round(3), hide_index=True)

# ---- 3. Gap tracking ---------------------------------------------------------------------
with tab_gap:
    col_a, col_b = st.columns(2)
    driver_a = col_a.selectbox("Driver A", drivers, index=0)
    driver_b = col_b.selectbox("Driver B", drivers, index=1)
    if driver_a == driver_b:
        st.info("Pick two different drivers.")
    else:
        gaps = gap_tracking.gap_series(session_id, driver_a, driver_b, laps=laps)
        st.markdown(f"Positive = **{driver_a}** ahead. Spikes are pit-stop cycles "
                    "(one car has pitted, the other has not yet).")
        st.altair_chart(
            alt.layer(
                alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(**REFERENCE_RULE).encode(y="y:Q"),
                alt.Chart(gaps).mark_line(color=LINE_COLOR, strokeWidth=2, point=alt.OverlayMarkDef(size=20)).encode(
                    x=alt.X("lap_number:Q", title="Lap"),
                    y=alt.Y("gap_s:Q", title=f"Gap {driver_a} → {driver_b} (s)"),
                    tooltip=["lap_number", alt.Tooltip("gap_s:Q", format=".3f"), "source",
                             alt.Tooltip("gap_fastf1_s:Q", format=".3f"), alt.Tooltip("gap_openf1_s:Q", format=".3f")],
                ),
            ).properties(height=380),
            width="stretch",
        )
        agreement = gap_tracking.source_agreement(gaps)
        m1, m2, m3 = st.columns(3)
        m1.metric("Final gap", f"{gaps['gap_s'].iloc[-1]:.3f} s")
        m2.metric("Laps where OpenF1 & FastF1 both report", agreement["laps_compared"])
        m3.metric("Median source disagreement", f"{agreement['median_abs_diff_s']:.3f} s")

# ---- 4. Upgrade attribution --------------------------------------------------------------
with tab_upgrade:
    st.markdown(
        "**McLaren's 2023 Austria upgrade.** At the Austrian GP (round 9) only Norris's car "
        "had the new sidepods and floor; Piastri got them at Silverstone (round 10). "
        "If the upgrade worked, Norris should gain on Piastri at Austria **only**, while the "
        "team as a whole closes on the fastest car from round 10 onward. "
        "This tab uses all races in the database, not the race selected in the sidebar."
    )
    upgrade = get_upgrade_table()
    rules = alt.Chart(pd.DataFrame({"round": [upgrade_attribution.UPGRADE_ROUND - 0.5,
                                              upgrade_attribution.TEAMMATE_UPGRADE_ROUND - 0.5]})
                      ).mark_rule(**REFERENCE_RULE).encode(x="round:Q")

    def metric_chart(column, title):
        return alt.layer(
            rules,
            alt.Chart(upgrade).mark_line(color=LINE_COLOR, strokeWidth=2, point=alt.OverlayMarkDef(size=60)).encode(
                x=alt.X("round:Q", title="Round", scale=alt.Scale(domain=[4.5, 14.5])),
                y=alt.Y(f"{column}:Q", title=title),
                tooltip=["event", "phase", alt.Tooltip(f"{column}:Q", format=".3f")],
            ),
        ).properties(height=300)

    col_1, col_2 = st.columns(2)
    col_1.markdown("**Norris vs Piastri** (median lap, % — negative = Norris faster)")
    col_1.altair_chart(metric_chart("teammate_delta_pct", "Teammate delta (%)"), width="stretch")
    col_2.markdown("**McLaren gap to fastest team** (median lap, %)")
    col_2.altair_chart(metric_chart("team_gap_to_fastest_pct", "Gap to fastest team (%)"), width="stretch")
    st.caption("Dashed lines: Norris-only upgrade (round 9), both cars upgraded (round 10).")

    steps = pd.DataFrame([upgrade_attribution.step_change(upgrade, m)
                          for m in ["teammate_delta_pct", "team_gap_to_fastest_pct"]])
    st.dataframe(steps.round(3), hide_index=True)
    st.dataframe(upgrade.round(3), hide_index=True)
    st.warning(
        "Limits: one race with asymmetric cars, only 4–6 races either side; Piastri was an "
        "improving rookie; track layouts favour different cars; other teams upgrade too. "
        "Treat this as evidence consistent with an upgrade effect, not proof."
    )
