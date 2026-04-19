"""SQLite-backed settings store for the research assistant.

Replaces .env. Tables:
  settings          — scalar key/value pairs (API key, model slugs, timeouts)
  researcher_models — one row per model with an enabled flag the user flips

Public API
----------
init_db(db_path)                        — create tables + seed if absent
get_setting(db_path, key)               — return str value or raise KeyError
set_setting(db_path, key, value)        — upsert a setting
get_enabled_researcher_models(db_path)  — list of enabled slugs, ordered
get_all_researcher_models(db_path)      — all rows as dicts (for display)
set_researcher_model_enabled(db_path, slug, enabled)
add_researcher_model(db_path, slug, enabled, preference_order)

The OPENROUTER_API_KEY is stored in the `settings` table but is never seeded
with a real value — the user must set it manually after init.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Seed data — authoritative list of researcher models.
# If the researcher_models table is empty (e.g. database was wiped) this list
# is used to re-populate it. Add new slugs here to make them available.
# ---------------------------------------------------------------------------

RESEARCHER_MODEL_SEEDS: list[tuple[str, int, int]] = [
    # (slug, enabled, preference_order)
    ("anthropic/claude-sonnet-4-5",      1, 1),
    ("anthropic/claude-opus-4-5",        1, 2),
    ("openai/gpt-4o",                    1, 3),
    ("google/gemini-pro-1.5",            1, 4),
    ("deepseek/deepseek-chat",           1, 5),
    ("openai/gpt-5.2",                   0, 6),
    ("google/gemini-3.1-pro-preview",    0, 7),
    ("deepseek/deepseek-v3.2",           0, 8),
    ("mistralai/devstral-2-2512",        0, 9),
]

# Default scalar settings (no secret values here)
SETTINGS_SEEDS: list[tuple[str, str]] = [
    ("OPENROUTER_API_KEY",          ""),   # must be set by user — never hardcoded
    ("IMPROVER_MODEL",              "anthropic/claude-sonnet-4-5"),
    ("REVIEWER_MODEL",              "anthropic/claude-opus-4-5"),
    ("REQUEST_TIMEOUT_SECONDS",     "180"),
    ("MAX_RETRIES",                 "2"),
    ("MIN_SUCCESSFUL_RESEARCHERS",  "2"),
]

_CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CREATE_RESEARCHER_MODELS = """
CREATE TABLE IF NOT EXISTS researcher_models (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    model_slug       TEXT    UNIQUE NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    preference_order INTEGER NOT NULL DEFAULT 0
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(db_path: Path) -> None:
    """Create tables and seed defaults where rows are missing.

    Safe to call on every application start — existing data is never
    overwritten. The OPENROUTER_API_KEY seed row has an empty value;
    the user sets the real key via manage_db.py or sqlite3 directly.
    """
    with _connect(db_path) as conn:
        conn.execute(_CREATE_SETTINGS)
        conn.execute(_CREATE_RESEARCHER_MODELS)

        # Seed scalar settings (INSERT OR IGNORE keeps existing values intact)
        conn.executemany(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);",
            SETTINGS_SEEDS,
        )

        # Seed researcher models the same way
        conn.executemany(
            "INSERT OR IGNORE INTO researcher_models "
            "(model_slug, enabled, preference_order) VALUES (?, ?, ?);",
            RESEARCHER_MODEL_SEEDS,
        )

        conn.commit()


def get_setting(db_path: Path, key: str) -> str:
    """Return the value for *key* or raise KeyError if absent."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?;", (key,)
        ).fetchone()
    if row is None:
        raise KeyError(f"setting {key!r} not found in {db_path}")
    return row["value"]


def set_setting(db_path: Path, key: str, value: str) -> None:
    """Upsert a setting row."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
            (key, value),
        )
        conn.commit()


def get_enabled_researcher_models(db_path: Path) -> list[str]:
    """Return slugs of all enabled researcher models, ordered by preference_order."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT model_slug FROM researcher_models "
            "WHERE enabled = 1 ORDER BY preference_order ASC;",
        ).fetchall()
    return [row["model_slug"] for row in rows]


def get_all_researcher_models(db_path: Path) -> list[dict[str, Any]]:
    """Return all researcher model rows as dicts (for display / manage_db)."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, model_slug, enabled, preference_order "
            "FROM researcher_models ORDER BY preference_order ASC;",
        ).fetchall()
    return [dict(row) for row in rows]


def set_researcher_model_enabled(db_path: Path, slug: str, enabled: bool) -> None:
    """Enable (True) or disable (False) a researcher model by slug.

    Raises KeyError if the slug does not exist in the table.
    """
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE researcher_models SET enabled = ? WHERE model_slug = ?;",
            (1 if enabled else 0, slug),
        )
        conn.commit()
    if cur.rowcount == 0:
        raise KeyError(f"researcher model {slug!r} not found in {db_path}")


def add_researcher_model(
    db_path: Path,
    slug: str,
    enabled: bool = True,
    preference_order: int = 0,
) -> None:
    """Insert a new researcher model. Raises ValueError if slug already exists."""
    with _connect(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO researcher_models (model_slug, enabled, preference_order)"
                " VALUES (?, ?, ?);",
                (slug, 1 if enabled else 0, preference_order),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError(f"researcher model {slug!r} already exists in {db_path}")
