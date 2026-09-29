-- Schema for the F1 dashboard SQLite snapshot.
-- Every statement is idempotent (IF NOT EXISTS) so the ETL can run it on every start.

-- One row per loaded session (e.g. the 2023 Spanish GP race).
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,     -- e.g. '2023_07_R' = year_round_sessiontype
    year          INTEGER NOT NULL,
    round         INTEGER NOT NULL,
    event_name    TEXT NOT NULL,        -- 'Spanish Grand Prix'
    location      TEXT,                 -- 'Barcelona'
    session_type  TEXT NOT NULL,        -- 'R' (race), 'Q', ...
    session_date  TEXT,                 -- ISO date
    had_rainfall  INTEGER,              -- 1 if the weather feed recorded rain at any point
    loaded_at     TEXT NOT NULL         -- when the ETL last wrote this session
);

-- One row per driver per lap. All laps are kept; data quality is expressed as flags
-- (is_accurate, track_status, pit_in/pit_out) so each analysis decides what "clean" means.
CREATE TABLE IF NOT EXISTS laps (
    session_id      TEXT NOT NULL REFERENCES sessions(session_id),
    driver          TEXT NOT NULL,      -- three-letter code, e.g. 'VER'
    driver_number   TEXT,
    team            TEXT,
    lap_number      INTEGER NOT NULL,
    lap_time_s      REAL,               -- NULL when timing did not capture the lap
    sector1_s       REAL,
    sector2_s       REAL,
    sector3_s       REAL,
    lap_end_time_s  REAL,               -- session clock when the lap ended (used for gaps)
    lap_start_date  TEXT,               -- UTC wall-clock start of the lap (joins to OpenF1)
    stint           INTEGER,
    compound        TEXT,               -- SOFT / MEDIUM / HARD / INTERMEDIATE / WET
    tyre_life       REAL,               -- laps on this set of tyres, including this lap
    fresh_tyre      INTEGER,
    position        INTEGER,
    pit_in          INTEGER NOT NULL,   -- 1 if the driver pitted at the end of this lap
    pit_out         INTEGER NOT NULL,   -- 1 if this lap started in the pit lane
    track_status    TEXT,               -- FastF1 status string; '1' = green flag throughout
    is_accurate     INTEGER NOT NULL,   -- FastF1's own data-quality flag
    deleted         INTEGER,            -- lap time deleted by stewards (track limits)
    PRIMARY KEY (session_id, driver, lap_number)   -- makes re-running the ETL safe
);

-- Official classification per race, from Jolpica (the Ergast replacement).
CREATE TABLE IF NOT EXISTS race_results (
    year           INTEGER NOT NULL,
    round          INTEGER NOT NULL,
    driver         TEXT NOT NULL,       -- three-letter code, joins to laps.driver
    driver_name    TEXT,
    team           TEXT,
    grid           INTEGER,
    position       INTEGER,
    points         REAL,
    status         TEXT,                -- 'Finished', '+1 Lap', 'Collision', ...
    PRIMARY KEY (year, round, driver)
);

-- Raw OpenF1 interval samples (gap to leader, roughly every 4 s), stored so the deployed
-- dashboard never has to call the live API.
CREATE TABLE IF NOT EXISTS openf1_intervals (
    session_id     TEXT NOT NULL REFERENCES sessions(session_id),
    driver_number  TEXT NOT NULL,
    date           TEXT NOT NULL,       -- UTC timestamp of the sample
    gap_to_leader  REAL,                -- seconds; NULL when the car is a lap or more down
    interval_s     REAL,                -- seconds to the car directly ahead
    PRIMARY KEY (session_id, driver_number, date)
);
