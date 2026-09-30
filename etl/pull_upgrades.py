"""Pull every team's declared car upgrades from the FIA's "Car Presentation Submissions".

Before each Grand Prix, every team must tell the FIA which parts of the car are new and
why. The FIA publishes these as a PDF on its documents site from the 2024 season onward
(none are published for 2022-2023 or earlier), one table per team:

    # | Updated component | Primary reason for update | Geometric differences | How it works

Idempotent: rows are keyed on (year, round, team, item) and events already stored are
skipped, so the weekly run only downloads the newest documents.

Usage (from the project root):
    python -m etl.pull_upgrades          # every season the FIA publishes, missing events only
    python -m etl.pull_upgrades 2025     # one season (re-parses events already stored)
"""

import collections
import io
import re
import sys
import time
from urllib.parse import quote, unquote

import fastf1
import pdfplumber
import requests

from etl.db import PROJECT_ROOT, get_connection, read_sql
from etl.teams import team_key

FIRST_YEAR = 2024        # first season with Car Presentation Submissions on fia.com
FIA = "https://www.fia.com"
F1_DOCUMENTS = "/documents/championships/fia-formula-one-world-championship-14"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) f1-performance-dashboard"}
COLUMNS = ["item", "component", "reason", "geometric_change", "description"]


def _get(url):
    """GET with retries: the FIA site answers 5xx under load fairly often."""
    for attempt in range(4):
        try:
            response = requests.get(url, headers=HEADERS, timeout=60)
            if response.status_code == 200:
                return response
        except requests.RequestException:
            pass
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"could not fetch {url}")


def season_paths():
    """{year: '/documents/.../season/season-2025-2071'} for every season listed by the FIA."""
    html = _get(FIA + "/documents").text
    return {int(year): f"{F1_DOCUMENTS}/season/season-{year}-{sid}"
            for year, sid in re.findall(rf"{F1_DOCUMENTS}/season/season-(\d{{4}})-(\d+)", html)}


def event_documents(season_path):
    """[(event name, PDF url)] for every event in a season that has a Car Presentation PDF."""
    html = _get(FIA + season_path).text
    events = sorted(set(re.findall(rf'value="({re.escape(season_path)}/event/[^"]+)"', html)))
    found = []
    for event_path in events:
        name = unquote(event_path.rsplit("/", 1)[1])
        if "grand prix" not in name.lower():      # skips pre-season tests, appeals, ...
            continue
        page = _get(FIA + event_path).text
        pdf = re.search(r'href="([^"]*car[ _]presentation[ _]submissions[^"]*\.pdf)"', page, re.I)
        if pdf:
            found.append((name, FIA + quote(pdf.group(1))))
        time.sleep(0.5)
    return found


def _cluster(values, tolerance):
    """Merge nearly equal coordinates (a ruled line is drawn as several touching segments)."""
    groups = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= tolerance:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _row_bounds(page, col_x):
    """[(top, bottom)] of each table row on the page."""
    # Most documents draw each cell as a filled rectangle: take the ones spanning column 1.
    bounds = sorted({
        (round(r["top"], 1), round(r["bottom"], 1)) for r in page.rects
        if abs(r["x0"] - col_x[0]) < 3 and abs(r["x1"] - col_x[1]) < 3 and r["height"] > 8
    })
    if bounds:
        return bounds
    # Some draw the grid with plain lines: a row border is a height where line segments
    # cover (almost) the full table width.
    covered = collections.defaultdict(float)
    for e in page.horizontal_edges:
        covered[round(e["top"])] += e["x1"] - e["x0"]
    ys = _cluster([y for y, width in covered.items() if width > 0.9 * (col_x[-1] - col_x[0])], 2)
    return list(zip(ys, ys[1:]))


def parse_document(pdf_bytes):
    """Parse one Car Presentation PDF into rows of (team as filed, item, component, ...).

    pdfplumber's generic table finder splits these cells badly, so the grid is rebuilt from
    the drawing: column borders are clusters of vertical ruling edges, and rows come from
    _row_bounds. Each team's table sits
    under a heading with the team's entry name; a table that continues onto the next page has
    no heading, so the last team seen carries over.
    """
    rows, team = [], None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[1:]:                         # page 1 is the FIA cover sheet
            col_x = _cluster([e["x0"] for e in page.vertical_edges], 8)
            if len(col_x) != len(COLUMNS) + 1:
                continue
            row_bounds = _row_bounds(page, col_x)
            if not row_bounds:
                continue

            for line in page.extract_text_lines():
                if line["top"] < row_bounds[0][0] and team_key(line["text"]):
                    team = line["text"].strip(" *.")

            for y0, y1 in row_bounds:
                cells = [
                    " ".join((page.crop((x0, y0, x1, y1)).extract_text() or "").split())
                    for x0, x1 in zip(col_x, col_x[1:])
                ]
                if cells[0].isdigit() and team:            # skips the header row
                    rows.append((team, int(cells[0]), *cells[1:]))
    # A few documents draw some tables twice; keep the first copy of each identical row.
    return list(dict.fromkeys(rows))


def round_for_event(year, event_name):
    """Map the FIA's event name to the championship round, using FastF1's calendar."""
    fastf1.Cache.enable_cache(str(PROJECT_ROOT / "cache"))
    schedule = fastf1.get_event_schedule(year, include_testing=False)
    exact = schedule[schedule["EventName"].str.lower() == event_name.lower()]
    event = exact.iloc[0] if len(exact) else schedule.get_event_by_name(event_name)
    return int(event["RoundNumber"]), event["EventName"]


def pull_season(year, season_path, skip_stored=True):
    stored = set(read_sql("SELECT DISTINCT round FROM upgrades WHERE year = ?", (year,))["round"])
    for fia_name, url in event_documents(season_path):
        round_number, event_name = round_for_event(year, fia_name)
        if skip_stored and round_number in stored:
            continue
        rows = parse_document(_get(url).content)
        with get_connection() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO upgrades
                   (year, round, event_name, team, team_as_filed, item, component, reason,
                    geometric_change, description, source_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(year, round_number, event_name, team_key(team), team, *cells, url)
                 for team, *cells in rows],
            )
        teams = len({r[0] for r in rows})
        print(f"{year} R{round_number:02d} {event_name}: {len(rows)} upgrades from {teams} teams")


def update(years=None):
    """Default: every published season, downloading only events not stored yet.
    With explicit years, those seasons are re-parsed in full."""
    seasons = season_paths()
    for year in years or [y for y in sorted(seasons) if y >= FIRST_YEAR]:
        pull_season(year, seasons[year], skip_stored=years is None)


if __name__ == "__main__":
    fastf1.set_log_level("WARNING")
    update([int(sys.argv[1])] if len(sys.argv) > 1 else None)
