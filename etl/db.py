"""Shared SQLite helpers: one place that knows where the database lives."""

import sqlite3
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "f1_dashboard.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "db_schema.sql"


def get_connection(db_path=DB_PATH):
    """Open the database and make sure every table exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=60)
    conn.executescript(SCHEMA_PATH.read_text())
    _migrate(conn)
    return conn


def _migrate(conn):
    """Add columns introduced after a database was first created (CREATE TABLE IF NOT
    EXISTS leaves existing tables unchanged)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "timing_source" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN timing_source TEXT NOT NULL DEFAULT 'fastf1'")


def read_sql(query, params=(), db_path=DB_PATH):
    """Run a SELECT and return a DataFrame. Used by the analyses and the dashboard."""
    with get_connection(db_path) as conn:
        return pd.read_sql_query(query, conn, params=params)
