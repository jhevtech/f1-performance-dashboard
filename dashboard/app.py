"""Streamlit dashboard. (no live API calls).

Run from the project root:  streamlit run dashboard/app.py
"""

import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

# Make the project root importable (Streamlit only adds this file's own folder to the path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import (  # noqa: E402
    common, gap_tracking, sector_loss, tire_degradation, upgrade_attribution, upgrade_impact,
)
from etl.db import read_sql  # noqa: E402

# UI colors come from the personal theme in .streamlit/config.toml here.
THEME_PRIMARY = "#1D2A24"
THEME_ACCENT = "#5F7D6A"
LINE_COLOR = THEME_ACCENT      # single-series lines
REFERENCE_RULE = {"color": THEME_PRIMARY, "opacity": 0.35, "strokeDash": [4, 4]}  # zero line, upgrade markers

# Compounds keep Pirelli's sidewall colors where they are visible on a light background
# (hard is blue, not white). The theme is one green hue, so it can't separate compounds.
COMPOUND_COLORS = {"SOFT": "#e34948", "MEDIUM": "#eda100", "HARD": "#2a78d6"}
# 2018's range: its SOFT was yellow and MEDIUM white (teal here, since white and gray vanish).
COMPOUND_COLORS_2018 = {"HYPERSOFT": "#d9468f", "ULTRASOFT": "#7b3fbf", "SUPERSOFT": "#e34948",
                        "SOFT": "#eda100", "MEDIUM": "#1b9e8a", "HARD": "#2a78d6", "SUPERHARD": "#c96a12"}
UNKNOWN_COMPOUND_COLOR = THEME_ACCENT   # 2010-2017: compound not recorded


def compound_scale(year, compounds):
    colors = COMPOUND_COLORS_2018 if year == 2018 else COMPOUND_COLORS
    order = [c for c in tire_degradation.COMPOUND_ORDER if c in set(compounds)]
    return alt.Scale(domain=order, range=[colors.get(c, UNKNOWN_COMPOUND_COLOR) for c in order])
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


@st.cache_data(max_entries=4)
def get_season_pace(year):
    return upgrade_impact.season_pace(year)


@st.cache_data
def get_upgrades(year):
    return upgrade_impact.load_upgrades(year)


# ---- Sidebar -----------------------------------------------------------------------------
sessions = get_sessions()
labels = {row.session_id: f"{row.year} R{row.round} · {row.event_name}"
          + (" 🌧" if row.had_rainfall == 1 else "") for row in sessions.itertuples()}

st.sidebar.title("F1 Performance Intelligence")
# Newest season and newest race first, so the default view is the latest race.
years = sorted(sessions["year"].unique(), reverse=True)
year = int(st.sidebar.selectbox("Season", years))   # numpy ints would bind to SQL as BLOBs
season = sessions[sessions["year"] == year].sort_values("round", ascending=False)
session_id = st.sidebar.selectbox(
    "Race", season["session_id"].tolist(),
    format_func=lambda sid: labels[sid].split(" ", 1)[1],   # "R7 · Spanish Grand Prix"
)
session = sessions.set_index("session_id").loc[session_id]
# 2010-2017 races come from Jolpica: lap times, positions and pit stops only.
lap_times_only = session["timing_source"] == "jolpica"
laps = get_laps(session_id)
results = get_results(int(session["year"]), int(session["round"]))

# Drivers ordered by finishing position; default to the podium. If official results aren't
# published yet (a race from the last few hours), order by position on each driver's last lap.
if results.empty:
    last_laps = laps.sort_values("lap_number").groupby("driver").tail(1)
    drivers = last_laps.sort_values(["lap_number", "position"], ascending=[False, True])["driver"].tolist()
else:
    drivers = results["driver"].tolist()
selected = st.sidebar.multiselect("Drivers", drivers, default=drivers[:3])
st.sidebar.caption(
    f"{len(sessions)} races, {sessions['year'].min()}–{sessions['year'].max()}, "
    f"refreshed weekly. 🌧 = rain recorded during the race. "
    "Data: FastF1, OpenF1, Jolpica-F1, FIA."
)
if lap_times_only:
    st.sidebar.info(
        "Before 2018 only lap times, positions and pit stops exist (Jolpica-F1): no sector "
        "times, tire compounds or flag data. Safety-car laps are detected from lap times.",
        icon=":material/info:",
    )

st.title(labels[session_id])
tab_tires, tab_sectors, tab_gap, tab_upgrade = st.tabs(
    ["Tire degradation", "Sector loss", "Gap tracking", "Upgrades"]
)

# ---- 1. Tire degradation -----------------------------------------------------------------
with tab_tires:
    st.markdown(
        "Lap time lost per lap of tire age, from a straight-line fit per driver stint "
        f"(fuel-corrected by +{tire_degradation.FUEL_EFFECT_S_PER_LAP} s/lap). "
        "Opening stints are usually flat for every compound (DRS trains, tire management), "
        "so they can be excluded."
    )
    if lap_times_only:
        st.caption("Tire compounds weren't recorded before 2018, so every stint is shown as "
                   "UNKNOWN; stints are split at pit stops and tire age counts laps since the stop.")
    elif int(session["year"]) == 2018:
        st.caption("2018 used Pirelli's older range (hypersoft → superhard). Its names don't "
                   "match 2019 onward, so compare 2018 compounds only with each other.")
    stints = tire_degradation.degradation_by_stint(laps)
    exclude_opening = st.checkbox("Exclude opening stints", value=False)
    by_compound = tire_degradation.degradation_by_compound(stints, exclude_opening).reset_index()
    compound_colors = compound_scale(int(session["year"]), by_compound.get("compound", []))

    col_chart, col_table = st.columns([1, 1])
    col_chart.altair_chart(
        alt.Chart(by_compound).mark_bar(cornerRadiusEnd=4, size=40).encode(
            x=alt.X("compound:N", sort=tire_degradation.COMPOUND_ORDER, title=None),
            y=alt.Y("median_deg_s_per_lap:Q", title="Median degradation (s/lap)"),
            color=alt.Color("compound:N", scale=compound_colors, legend=None),
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
            color=alt.Color("compound:N", scale=compound_colors),
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
    if lap_times_only:
        st.info("Sector times aren't available for this race: they are only recorded from "
                "2018 onward.", icon=":material/info:")
    else:
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
        if lap_times_only:   # same method (summed lap times), different timing source
            gaps["source"] = "Jolpica"
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
        if agreement["laps_compared"]:
            m2.metric("Laps where OpenF1 & FastF1 both report", agreement["laps_compared"])
            m3.metric("Median source disagreement", f"{agreement['median_abs_diff_s']:.3f} s")
        else:
            m2.caption("OpenF1 cross-check is stored for the 2023 reference races (R5–R14) "
                       "only, to keep the database small. The gap shown is "
                       + ("summed Jolpica lap times." if lap_times_only else "FastF1 timing."))

# ---- 4. Upgrades -------------------------------------------------------------------------
def mclaren_2023_case_study():
    """The original analysis: McLaren's 2023 Austria upgrade, from press reports."""
    st.markdown(
        "At the Austrian GP (round 9) only Norris's car had the new sidepods and floor; "
        "Piastri got them at Silverstone (round 10). If the upgrade worked, Norris should gain "
        "on Piastri at Austria **only**, while the team as a whole closes on the fastest car "
        "from round 10 onward."
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


PARTS_COLUMNS = {
    "team": st.column_config.TextColumn("Team"),
    "component": st.column_config.TextColumn("Component"),
    "reason": st.column_config.TextColumn("Declared reason"),
    "geometric_change": st.column_config.TextColumn("What changed", width="large"),
    "description": st.column_config.TextColumn("How it works (team's description)", width="large"),
}

with tab_upgrade:
    race_round = int(session["round"])
    if year < upgrade_impact.FIRST_UPGRADE_YEAR:
        st.info(
            f"Upgrade data is not provided for {year}. The FIA publishes the upgrades each team "
            "declares before a Grand Prix (Car Presentation Submissions) from the "
            f"{upgrade_impact.FIRST_UPGRADE_YEAR} season onward.",
            icon=":material/info:",
        )
        if year == upgrade_attribution.YEAR:
            with st.expander("Case study: McLaren's 2023 Austria upgrade (from press reports)"):
                mclaren_2023_case_study()
    else:
        upgrades = get_upgrades(year)
        packages = upgrade_impact.upgrade_packages(upgrades)

        # -- What was brought to the selected Grand Prix
        at_race = upgrades[upgrades["round"] == race_round]
        st.subheader(f"Upgrades declared for the {session['event_name']}")
        if at_race.empty:
            st.caption("No team declared new parts for this Grand Prix, or the FIA hasn't "
                       "published the document yet.")
        else:
            st.caption(
                f"{len(at_race)} updated parts from {at_race['team'].nunique()} teams, as each team "
                f"declared them to the FIA. [Source document]({at_race['source_url'].iloc[0]})"
            )
            st.dataframe(at_race[list(PARTS_COLUMNS)], hide_index=True, column_config=PARTS_COLUMNS)

        # -- Before vs after for one package
        st.subheader("Did the upgrade make the car faster?")
        st.markdown(
            "Each race, a team's pace is the median of its drivers' clean laps, measured as "
            "**% slower than the fastest team** that day (which removes most of the "
            f"track-to-track difference). The upgraded car is compared over the "
            f"{upgrade_impact.WINDOW} races from the upgrade onward against the previous version "
            f"of the car in the {upgrade_impact.WINDOW} races before it. Only packages with at "
            "least one part declared as a performance upgrade are listed."
        )
        perf = packages[packages["performance_parts"] > 0].reset_index(drop=True)
        if perf.empty:
            st.caption(f"No performance upgrades stored for {year} yet.")
        else:
            here = perf.index[perf["round"] == race_round]
            choice = st.selectbox(
                "Upgrade package", perf.index,
                # Default to the biggest package at the selected race (packages are sorted by
                # round, then size), else the first of the season.
                index=int(here[0]) if len(here) else 0,
                format_func=lambda i: (f"R{perf.at[i, 'round']} {perf.at[i, 'event_name']} · "
                                       f"{perf.at[i, 'team']} ({perf.at[i, 'performance_parts']} "
                                       f"performance part{'s' if perf.at[i, 'performance_parts'] > 1 else ''})"),
            )
            package = perf.loc[choice]
            pace = get_season_pace(year)
            # Races with too few clean laps for this team (e.g. wet ones) have no gap and are skipped.
            team_pace = pace[pace["team"] == package["team"]].dropna(
                subset=["gap_to_fastest_pct"]).sort_values("round")
            effect = upgrade_impact.upgrade_effect(pace, package["team"], package["round"])

            if effect["races_before"] == 0:
                st.info("This package arrived at the first race of the season, so there is no "
                        "earlier version of the car to compare it with.", icon=":material/info:")
            elif effect["races_after"] == 0:
                st.info("Lap data for this race hasn't been loaded yet.", icon=":material/info:")
            else:
                m1, m2, m3 = st.columns(3)
                def races(n):
                    return f"{n} race{'s' if n != 1 else ''}"

                m1.metric(f"Gap to fastest, {races(effect['races_before'])} before",
                          f"{effect['gap_before_pct']:.2f}%")
                m2.metric(f"Gap to fastest, {races(effect['races_after'])} from upgrade",
                          f"{effect['gap_after_pct']:.2f}%",
                          delta=f"{effect['change_pct']:+.2f} pts", delta_color="inverse")
                m3.metric("Average pace rank", f"{effect['rank_after']:.1f}",
                          delta=f"{effect['rank_after'] - effect['rank_before']:+.1f} vs before",
                          delta_color="inverse")
                at_upgrade = team_pace.loc[team_pace["round"] == package["round"], "gap_to_fastest_pct"]
                if pd.notna(effect["trend_predicted_pct"]) and len(at_upgrade):
                    st.caption(
                        f"The trend of the races before predicted a {effect['trend_predicted_pct']:.2f}% "
                        f"gap at the upgrade race; the actual gap was "
                        f"{at_upgrade.iloc[0]:.2f}%. "
                        "If those are close, the improvement may be a trend that was already under way."
                    )

            if not team_pace.empty:
                window = team_pace.assign(phase=team_pace["round"].map(
                    lambda r: "from upgrade" if r >= package["round"] else "before"))
                upgrade_rule = alt.Chart(pd.DataFrame({"round": [package["round"] - 0.5]})).mark_rule(
                    **REFERENCE_RULE).encode(x="round:Q")
                st.altair_chart(
                    alt.layer(
                        upgrade_rule,
                        alt.Chart(window).mark_line(color=LINE_COLOR, strokeWidth=2,
                                                    point=alt.OverlayMarkDef(size=60)).encode(
                            x=alt.X("round:Q", title="Round", axis=alt.Axis(tickMinStep=1)),
                            y=alt.Y("gap_to_fastest_pct:Q", title=f"{package['team']} gap to fastest team (%)"),
                            tooltip=["event", "phase", alt.Tooltip("gap_to_fastest_pct:Q", format=".2f"),
                                     "pace_rank", "wet"],
                        ),
                    ).properties(height=320),
                    width="stretch",
                )
                st.caption(f"Dashed line: {package['event_name']} (round {package['round']}), where "
                           "the upgrade arrived. Lower is closer to the front. The whole season is shown "
                           "for context; the numbers above use only the races either side. Races where "
                           f"the team had fewer than {upgrade_impact.MIN_CLEAN_LAPS} clean dry laps "
                           "(usually wet ones) are left out.")

            with st.expander(f"Parts in this package ({package['parts']})"):
                parts = upgrades[(upgrades["round"] == package["round"]) & (upgrades["team"] == package["team"])]
                st.dataframe(parts[list(PARTS_COLUMNS)[1:]], hide_index=True, column_config=PARTS_COLUMNS)

            with st.expander(f"Every {year} performance upgrade, before vs after"):
                effects = upgrade_impact.season_upgrade_effects(pace, packages)
                st.dataframe(
                    effects[["round", "event_name", "team", "performance_parts", "gap_before_pct",
                             "gap_after_pct", "change_pct"]],
                    hide_index=True,
                    column_config={
                        "event_name": "Grand Prix", "team": "Team",
                        "performance_parts": "Performance parts",
                        "gap_before_pct": st.column_config.NumberColumn("Gap before (%)", format="%.2f"),
                        "gap_after_pct": st.column_config.NumberColumn("Gap after (%)", format="%.2f"),
                        "change_pct": st.column_config.NumberColumn(
                            "Change (pts)", format="%+.2f", help="Negative = closer to the fastest team"),
                    },
                )

            st.warning(
                "Limits: every team upgrades, so the fastest team is a moving benchmark; tracks "
                "suit cars differently and three races is a small sample; teams often bring "
                "several packages in a row, and this can't separate them. Read a change as "
                "consistent with an effect, not proof of one.",
                icon=":material/warning:",
            )
