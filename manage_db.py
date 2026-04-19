"""Admin CLI for managing db/storage.db settings and researcher models.

Usage:
  uv run python manage_db.py init
  uv run python manage_db.py show
  uv run python manage_db.py set <key> <value>
  uv run python manage_db.py enable <model-slug>
  uv run python manage_db.py disable <model-slug>
  uv run python manage_db.py add <model-slug> [--order N] [--disabled]
  uv run python manage_db.py reseed

Commands:
  init      Create db/storage.db with default settings and researcher model list.
            Safe to run multiple times — existing data is never overwritten.
  show      Print all settings (API key is masked) and the researcher model list.
  set       Update or insert a scalar setting (e.g. OPENROUTER_API_KEY).
  enable    Set enabled=1 for a researcher model slug.
  disable   Set enabled=0 for a researcher model slug.
  add       Insert a new researcher model slug.
  reseed    Re-insert any missing researcher models from the built-in seed list.
            Useful if the database was partially wiped. Does not overwrite existing rows.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "db" / "storage.db"

# Import after setting up sys.path if running as a script before `uv sync`
try:
    from research_assistant.db import (
        RESEARCHER_MODEL_SEEDS,
        SETTINGS_SEEDS,
        add_researcher_model,
        get_all_researcher_models,
        get_setting,
        init_db,
        set_researcher_model_enabled,
        set_setting,
    )
    import sqlite3
except ImportError as exc:
    print(
        f"error: could not import research_assistant — run `uv sync` first.\n  {exc}",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


def _cmd_init() -> None:
    init_db(DB_PATH)
    print(f"Initialised {DB_PATH}")
    print("Run `uv run python manage_db.py show` to review defaults.")
    print(
        "Set your API key with: uv run python manage_db.py set OPENROUTER_API_KEY sk-or-..."
    )


def _cmd_show() -> None:
    init_db(DB_PATH)

    print("=== Settings ===")
    import sqlite3

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT key, value FROM settings ORDER BY key;").fetchall()
    conn.close()
    for row in rows:
        value = row["value"]
        if row["key"] == "OPENROUTER_API_KEY":
            display = _mask(value) if value else "(not set)"
        else:
            display = value if value else "(empty)"
        print(f"  {row['key']:<30} {display}")

    print()
    print("=== Researcher models ===")
    models = get_all_researcher_models(DB_PATH)
    print(f"  {'#':<4} {'enabled':<8} {'order':<6} slug")
    print(f"  {'-' * 4} {'-' * 8} {'-' * 6} {'-' * 40}")
    for m in models:
        flag = "yes" if m["enabled"] else "no"
        print(f"  {m['id']:<4} {flag:<8} {m['preference_order']:<6} {m['model_slug']}")


def _cmd_set(key: str, value: str) -> None:
    init_db(DB_PATH)
    set_setting(DB_PATH, key, value)
    if key == "OPENROUTER_API_KEY":
        display = _mask(value) if value else "(empty)"
    else:
        display = repr(value)
    print(f"Set {key} = {display}")


def _cmd_enable(slug: str) -> None:
    init_db(DB_PATH)
    try:
        set_researcher_model_enabled(DB_PATH, slug, True)
        print(f"Enabled: {slug}")
    except KeyError:
        print(
            f"error: model {slug!r} not found. Add it first with the 'add' command.",
            file=sys.stderr,
        )
        sys.exit(1)


def _cmd_disable(slug: str) -> None:
    init_db(DB_PATH)
    try:
        set_researcher_model_enabled(DB_PATH, slug, False)
        print(f"Disabled: {slug}")
    except KeyError:
        print(f"error: model {slug!r} not found.", file=sys.stderr)
        sys.exit(1)


def _cmd_add(slug: str, order: int, enabled: bool) -> None:
    init_db(DB_PATH)
    try:
        add_researcher_model(DB_PATH, slug, enabled=enabled, preference_order=order)
        print(f"Added: {slug}  (enabled={enabled}, order={order})")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_reseed() -> None:
    """Re-insert any researcher model seeds that are missing from the table."""
    init_db(DB_PATH)
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(DB_PATH))
    conn.executemany(
        "INSERT OR IGNORE INTO researcher_models "
        "(model_slug, enabled, preference_order) VALUES (?, ?, ?);",
        RESEARCHER_MODEL_SEEDS,
    )
    conn.commit()
    conn.close()
    print("Reseeded researcher_models (existing rows untouched).")
    print("Run `show` to review the current state.")


# ---------------------------------------------------------------------------
# Argument parsing (no external deps)
# ---------------------------------------------------------------------------


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]

    if cmd == "init":
        _cmd_init()

    elif cmd == "show":
        _cmd_show()

    elif cmd == "set":
        if len(args) < 3:
            print("usage: manage_db.py set <key> <value>", file=sys.stderr)
            sys.exit(1)
        _cmd_set(args[1], args[2])

    elif cmd == "enable":
        if len(args) < 2:
            print("usage: manage_db.py enable <model-slug>", file=sys.stderr)
            sys.exit(1)
        _cmd_enable(args[1])

    elif cmd == "disable":
        if len(args) < 2:
            print("usage: manage_db.py disable <model-slug>", file=sys.stderr)
            sys.exit(1)
        _cmd_disable(args[1])

    elif cmd == "add":
        if len(args) < 2:
            print(
                "usage: manage_db.py add <model-slug> [--order N] [--disabled]",
                file=sys.stderr,
            )
            sys.exit(1)
        slug = args[1]
        order = 0
        enabled = True
        rest = args[2:]
        i = 0
        while i < len(rest):
            if rest[i] == "--order" and i + 1 < len(rest):
                try:
                    order = int(rest[i + 1])
                except ValueError:
                    print("error: --order must be an integer", file=sys.stderr)
                    sys.exit(1)
                i += 2
            elif rest[i] == "--disabled":
                enabled = False
                i += 1
            else:
                print(f"error: unknown argument {rest[i]!r}", file=sys.stderr)
                sys.exit(1)
        _cmd_add(slug, order, enabled)

    elif cmd == "reseed":
        _cmd_reseed()

    else:
        print(f"error: unknown command {cmd!r}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
