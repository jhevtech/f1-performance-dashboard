# F1 Performance Intelligence Dashboard

A data pipeline and Streamlit dashboard that turns raw Formula 1 timing data into four
analyses of race performance: how fast tires wear out, where on track a driver loses
time, how the gap between two drivers evolves, and whether a car upgrade actually made
the car faster.

It covers **every Grand Prix from 2010 to the most recent race**, and every car upgrade
the teams have declared to the FIA since 2024. A scheduled job adds each new race and its
upgrade list the Monday after it happens. Pick a season and a race in the dashboard to run
the analyses on it. The in-depth write-ups below use ten 2023 races (Miami → Monza,
rounds 5–14) as the reference set.

What each era supports:

| Seasons | Lap timing source | Tire degradation | Sector loss | Gap tracking | Upgrades |
|---|---|---|---|---|---|
| 2010–2017 | Jolpica-F1 (lap times, positions, pit stops) | per stint, compound unknown | – | ✓ | not published by the FIA |
| 2018 | FastF1 | ✓ (Pirelli's older hypersoft→superhard range) | ✓ | ✓ | not published by the FIA |
| 2019–2023 | FastF1 | ✓ | ✓ | ✓ | not published by the FIA (2023: McLaren case study) |
| 2024 → today | FastF1 | ✓ | ✓ | ✓ | ✓ FIA Car Presentation Submissions |

---

## How it works

```
FastF1 (lap timing 2018+)        ─┐
Jolpica dump (laps 2010–2017)    ─┤
OpenF1 (live gaps)               ─┼─► etl/ ──► data/f1_dashboard.db ──► analysis/ ──► dashboard/app.py
Jolpica API (results)            ─┤          (SQLite snapshot)        (pure pandas)    (Streamlit)
FIA documents (upgrades 2024+)   ─┘
```

- **ETL (`etl/`)** pulls each race once and **upserts** it into SQLite. Laps are keyed on
  `(session_id, driver, lap_number)`, so re-running the pipeline overwrites rows instead
  of duplicating them. `etl/update.py` compares FastF1's race calendar with what's already
  stored and pulls only the missing races, so the same command does the initial back-fill
  and the weekly top-up.
- **2010–2017 from Jolpica.** FastF1's lap timing starts in 2018. For earlier seasons
  `etl/pull_history.py` reads Jolpica's full database, dump every lap time and position, pit stops from 2011, and the
  official results. There are no sector times, tire compounds or flag data, so those races
  are stored with `timing_source = 'jolpica'` and some columns are derived: stints are split
  at pit stops, and a lap where the whole field was more than 7% slower than normal is
  treated as neutralised (safety car, VSC, red flag). 2010 has no pit stop records, so a
  stop is inferred from an out-lap more than 12 s slower than that lap's field median
  (recorded out-laps in 2011–2017 are a median 18.5 s slow).
- **Upgrades from the FIA.** Before every Grand Prix each team must declare its new parts,
  why they were brought, and how they work. The FIA publishes these "Car Presentation
  Submissions" as PDFs from 2024 onward. `etl/pull_upgrades.py` finds them on fia.com and
  rebuilds each team's table from the PDF's drawn grid; `pdfplumber`'s generic table
  finder splits those cells badly. Team names are normalised (`etl/teams.py`) so the FIA's
  "Visa Cash App RB F1 Team" joins to FastF1's "RB".
- **Weekly refresh.** A GitHub Actions workflow (`.github/workflows/update-data.yml`)
  runs the update every Monday and commits the new database. Streamlit Cloud redeploys on
  every push, so the live app stays current without ever calling an F1 API itself.
- **Nothing is thrown away at load time.** FastF1 marks some laps as unreliable (pit
  laps, lap 1, timing glitches). Those laps are stored with an `is_accurate` flag, and
  each analysis decides what "clean" means for it. That makes the filtering choice
  visible and reversible.
- **Analysis (`analysis/`)** is plain functions that take a DataFrame and return a
  DataFrame. Each module runs on its own from the command line and prints its results.
- **Dashboard (`dashboard/`)** only displays results. It calls the analysis functions
  and makes no API calls, so the deployed app runs entirely off the committed database.

A "clean" lap (`analysis/common.py`) passes FastF1's accuracy check **and** was run
entirely under green flags. FastF1's flag already drops safety-car laps, but it keeps
yellow-flag laps, where drivers must slow down in the flagged zone. The green-flag
filter removes another 328 of those (about 3% of accurate laps).

---

## The four analyses and what they found

### 1. Tire degradation: how much slower does each lap get as tires wear?

For each driver's stint, fit a straight line of lap time against tire age
(`numpy.polyfit`, degree 1). The slope is the degradation rate in seconds per lap.

One complication is fuel. Cars burn ~1.8 kg per lap and get faster as they get
lighter, which hides tire wear: raw slopes are often *negative*. Each lap gets
+0.06 s per lap already run added back, which is an assumed, commonly used
ballpark. Laps more than 2% slower than the stint median (traffic, mistakes) are dropped
before fitting.

**Result: race data does not cleanly show "soft wears fastest".**

| Compound | Median deg, all stints (10 races) | Excluding opening stints | Spain 2023, excl. opening |
|---|---|---|---|
| Soft   | 0.045 s/lap (94 stints)  | 0.057 s/lap | **0.093 s/lap** |
| Medium | 0.052 s/lap (191 stints) | 0.073 s/lap | 0.070 s/lap |
| Hard   | 0.066 s/lap (170 stints) | 0.073 s/lap | 0.075 s/lap |

Stint position turned out to matter more than compound. **Opening stints look about 3×
flatter than later stints for every compound** (≈0.02 vs ≈0.07 s/lap). At the start of a
race, cars sit in DRS trains and drivers deliberately save their tires, so lap time is
set by the car in front, not by tire wear. The deeper reason compound ordering is hard to
see is **selection bias**: teams choose each compound for the stint length it can handle,
then drive to a target lap time. Race data measures *managed* degradation, not the tire's
intrinsic wear. With opening stints removed, Spain shows the expected soft > medium/hard
ordering. Some races show it clearly even with every stint included: the **2026
Barcelona GP** gives soft 0.25 > medium 0.18 > hard 0.13 s/lap, with almost no stints
flagged.

About 40% of stints fall outside the "plausible" 0.05–0.30 s/lap range and are flagged
`suspicious`. Almost all of them are *below* 0.05, which says that threshold is too high
for race conditions rather than that the data is dirty.

### 2. Sector loss: where on track is a driver losing time?

Each driver is compared **against their own best** in each sector (S1/S2/S3), not
against other drivers. That takes the car out of the picture, so the result shows where
*this driver's* pace fades over a race. Per-lap losses are summed per sector.

**Spain 2023:** for most front-runners (Verstappen, Pérez, Leclerc, Russell, Piastri) the
largest loss is **Sector 2**, the long high-load corners where tire wear hurts most.
Across the field the split is S1 35% / S2 37% / S3 31%.
*Caveat:* longer sectors naturally accumulate more loss, so the dashboard also shows a
per-sector % loss that normalizes for length.

### 3. Gap tracking: how does the gap between two drivers evolve?

Two independent sources:
- **FastF1**: the gap at the end of lap *n* is the difference between the two cars'
  line-crossing times (equivalent to summing lap times, but not broken by one missing lap).
- **OpenF1**: the official live-timing "gap to leader", differenced between the two
  drivers and read at the moment each one crosses the line.

**Validation across five races:** the two sources agree to a **median of 0.000 s per
lap**. FastF1 reproduces the official winning margins exactly (Spain VER→HAM **24.090 s**,
Monza VER→PER **6.064 s**). OpenF1's final-lap value was off at Monza, so FastF1 is the
plotted series and OpenF1 is the cross-check. The remaining disagreements (up to a few
seconds) all fall on pit-stop laps, where the two systems measure at different points.
The spikes in the chart are pit cycles: one car has pitted and the other hasn't yet.

Two bugs came up along the way that are worth mentioning:
1. OpenF1 only sends a new sample when a value **changes**, so the leader has almost
   no data points (the gap stays at 0). The fix was to carry the last value forward.
2. Reading both drivers' gaps at the same instant gave a stale value for the car
   behind. The fix was to read each driver as *they* cross the line.

### 4. Upgrades: what each team brought, and did it make the car faster?

For every Grand Prix from 2024, the dashboard lists each part every team declared, with the
team's own reason and description. To judge whether a package worked
(`analysis/upgrade_impact.py`), each team's race pace is the median of its drivers' clean
dry laps, as **% slower than the fastest team** that day. The upgraded car's gap over the
three races from the upgrade onward is compared with the previous version's gap over the
three races before. As in the case study below, a line through the races before is
extended to the upgrade race, to show whether the gap was already closing. Only packages
with at least one part declared as a *performance* upgrade are assessed. Circuit-specific
parts, such as a low-drag Monza rear wing, aren't meant to carry over.

Seasons before 2024 show "not provided": the FIA didn't publish upgrade declarations
then, and the dashboard doesn't substitute press reports for them. The one exception is
the original 2023 McLaren study below, which is labelled as coming from press reports.

#### Case study: did McLaren's 2023 Austria upgrade work?

**The upgrade:** at the 2023 Austrian GP (round 9, 2 July) McLaren brought new
sidepods, bodywork and floor, but **only Lando Norris's car got it**. Oscar Piastri
received it one race later at Silverstone (round 10). Sources:
[McLaren MCL60, Wikipedia](https://en.wikipedia.org/wiki/McLaren_MCL60),
[RacingNews365](https://racingnews365.com/piastri-misses-out-on-mclaren-upgrade).

The comparison needs to control for the fact that some tracks suit some cars. Comparing
a driver to their **teammate** does that: same car, same track, same weather, same day.
But if both cars get an upgrade on the same weekend, the teammate comparison cancels it
out. Austria is useful because the cars were **different for exactly one race**. So there
are two measures:

1. **Teammate delta**: Norris's median clean lap minus Piastri's, as % of lap time.
   *Prediction if the upgrade worked:* Norris gains at Austria only, and the gap closes
   at Silverstone.
2. **Gap to the fastest team**: McLaren's median lap vs the fastest team's, as %.
   This picks up the team-wide change that a teammate comparison can't see.

**Results:**

| | Before (R5–8) | Austria (R9, NOR only) | After (R10–14) |
|---|---|---|---|
| Norris vs Piastri | −0.28% avg | **−0.80%** | −0.12% avg (Silverstone: −0.02%) |
| McLaren gap to fastest team | 1.37% avg | 1.44% | **0.79% avg** |
| McLaren race-pace rank | 5th–9th | 5th | 2nd–6th |

- The teammate result matches the prediction. At Austria Norris was 0.80% faster
  than Piastri, about 2 standard deviations beyond the other races (mean −0.20%, sd 0.29).
  The gap closed to almost nothing at Silverstone once both cars had the parts. In the
  official results, Norris finished **P4** at Austria and Piastri **P16**. McLaren's
  best finish in the four races before was P9.
- The team-level result is **less convincing than it looks**. The gap to the front
  roughly halved, but it was already shrinking before the upgrade (Canada, round 8,
  was 0.79%). A straight line through the four pre-upgrade races predicts 0.32% at
  Silverstone, and the actual was 0.25%. On this metric alone, the upgrade can't be
  told apart from a trend that was already under way.

**Limits of this method:**
- **Tiny samples:** one race with different cars and 4–6 races either side. That isn't
  enough for a formal statistical test.
- **Driver effects:** Piastri was a rookie improving race by race, so the teammate delta
  drifts on its own. Spain and Hungary show Norris-favoring gaps nearly as large as
  Austria with identical cars.
- **Track effects:** the MCL60 was known to be weak in slow corners, so its position
  relative to the field moves with the circuit.
- **One-off events:** strategy, traffic, damage and rain. Only dry-tire, green-flag
  laps are used, but Monaco, Austria, Spa and Zandvoort all had some rain recorded.
- **Moving target:** other teams upgrade too, so the fastest team isn't a fixed benchmark.

The honest conclusion: *the evidence is consistent with a real upgrade effect, and the
teammate natural experiment at Austria is the strongest part of it. It isn't proof.*

---

## Running it locally

Requires Python 3.11+ (developed on 3.13 in WSL2/Ubuntu).

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# The committed database already contains every race, so this is enough:
streamlit run dashboard/app.py
```

To add new races, or rebuild the database from scratch:

```bash
python -m etl.pull_history           # 2010-2017 from the Jolpica dump (one download)
python -m etl.update                 # every completed race since 2018 that isn't stored yet,
                                     # plus new FIA upgrade documents
python -m etl.pull_upgrades 2025     # re-parse one season's FIA upgrade documents
python -m etl.pull_sessions          # the ten 2023 reference races, with telemetry timestamps
python -m etl.pull_sessions 2023 Japan R   # ...or any single session
python -m etl.pull_standings         # Jolpica race results for every loaded race
python -m etl.pull_openf1            # OpenF1 interval data for every loaded race (large!)
```

Each analysis also runs on its own and prints its numbers:

```bash
python -m analysis.tire_degradation 2023_07_R
python -m analysis.sector_loss 2023_07_R
python -m analysis.gap_tracking 2023_07_R VER HAM
python -m analysis.upgrade_attribution
python -m analysis.upgrade_impact 2025
```

A full back-fill takes a few hours: FastF1 allows 500 API calls per hour (about 50 races),
and `etl.update` waits and resumes when it hits that limit.

## Project layout

```
etl/
  db_schema.sql         tables: sessions, laps, race_results, openf1_intervals, upgrades
  db.py                 database path + connection helper
  pull_sessions.py      FastF1 -> laps/sessions (idempotent upsert)
  pull_standings.py     Jolpica-F1 -> race_results
  pull_openf1.py        OpenF1 /intervals -> openf1_intervals
  pull_history.py       Jolpica dump -> 2010-2017 sessions/laps/race_results
  pull_upgrades.py      FIA Car Presentation Submissions (PDF) -> upgrades
  teams.py              one canonical name per team across sources
  update.py             pull every completed race not yet stored (back-fill + weekly)
analysis/
  common.py             loading + the shared "clean lap" definition
  tire_degradation.py   per-stint regression, fuel correction, compound summary
  sector_loss.py        per-sector loss vs personal best
  gap_tracking.py       two-source gap series + cross-validation
  upgrade_attribution.py  McLaren 2023 case study: teammate + field-relative comparison
  upgrade_impact.py     any FIA-declared upgrade: gap to fastest team, before vs after
dashboard/app.py        Streamlit UI (display only)
data/f1_dashboard.db    committed SQLite snapshot used by the deployed app
.github/workflows/update-data.yml   Monday refresh: run etl.update (races + upgrades), commit the snapshot
```

### Coverage decisions

- **From 2010.** 2010 is the first season without refuelling, so the fixed fuel correction
  in the tire analysis applies throughout. 2018 uses Pirelli's old compound names
  (hypersoft → superhard); its "soft" is a different tire from 2019's, so the dashboard
  gives 2018 its own compound order and colours.
- **OpenF1 only for the ten 2023 reference races.** Raw OpenF1 samples take ~1.8 MB per
  race and would push the database past GitHub's 100 MB file limit. FastF1 timing is the
  primary gap source anyway, and those ten races are enough to validate it.
- **Races only**, not sprints or qualifying. The schema supports other session types
  (`session_type`), so adding them is a small change.

## Data sources

- [FastF1](https://docs.fastf1.dev/): lap and sector timing, tire data, track status.
- [OpenF1](https://openf1.org/): live-timing intervals (historical data is free).
- [Jolpica-F1](https://github.com/jolpica/jolpica-f1): official classifications, accessed via
  `fastf1.ergast`, and its [database dump](https://api.jolpi.ca/data/dumps/download/delayed/?dump_type=csv)
  for 2010–2017 lap times and pit stops. It's the community replacement for the Ergast API,
  which shut down in early 2025.
- [FIA documents](https://www.fia.com/documents/championships/fia-formula-one-world-championship-14):
  the teams' Car Presentation Submissions (declared upgrades), 2024 onward.

## Possible extensions

- Replace the fixed fuel-correction constant with one estimated from the data.
- Fit a non-linear model to catch the tire "cliff" at the end of long stints.
- Repeat the upgrade analysis for other asymmetric upgrades to build a larger sample.
- Move storage to Delta Lake / Databricks. Each weekly commit stores a new copy of the
  whole database in git history, which is fine for a season or two; after that, Git LFS or
  a table format that appends files per race would scale better.
