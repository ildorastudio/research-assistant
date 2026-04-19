# Multi-LLM Research Assistant — Restructuring Plan

This document supersedes the original build plan. The project is already functional; the goal here is to restructure it for maintainability, proper packaging with **uv**, and replacement of `.env`-based config with a **SQLite** settings database.

---

## 1. Target Directory Structure

```
research-assistant/
├── docs/
│   ├── plan.md              ← this file (moved from root)
│   ├── MODELS.md            ← moved from root
│   └── clarifications.md   ← RUNTIME, moved from root (gitignore)
├── data/
│   ├── input/
│   │   └── input.txt        ← moved from root
│   ├── intermediate/        ← RUNTIME, gitignored
│   │   └── output_intermediate.md
│   └── output/              ← RUNTIME, gitignored
│       └── output_final.md
├── src/
│   └── research_assistant/
│       ├── __init__.py      ← new (empty, marks package)
│       ├── main.py          ← moved + updated
│       ├── improver.py      ← moved + updated
│       ├── researcher.py    ← moved + updated
│       ├── reviewer.py      ← moved + updated
│       ├── openrouter_client.py  ← moved + updated
│       └── db.py            ← NEW — SQLite settings layer
├── prompts/
│   ├── improver.txt         ← stays
│   ├── researcher.txt       ← stays
│   └── reviewer.txt         ← stays
├── pyproject.toml           ← NEW — replaces requirements.txt
├── db/
│   └── storage.db          ← RUNTIME, gitignored — replaces .env
└── .gitignore               ← updated
```

**Files to delete after migration:** `requirements.txt`, `.env`, `.env.example`, `output.md` (root-level artefact).

---

## 2. uv Packaging

### 2a. `pyproject.toml`

Replace `requirements.txt` with a `pyproject.toml`. The `[project.scripts]` entry is what makes `uv run app` work — uv installs the package in editable mode and exposes the script as a console command.

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "research-assistant"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
]

[project.scripts]
app = "research_assistant.main:main_cli"

[tool.hatch.build.targets.wheel]
packages = ["src/research_assistant"]
```

`python-dotenv` is **removed** — config now comes from SQLite, not `.env`.

### 2b. Entry point rename

`main.py` currently has `async def main()` called via `asyncio.run(main())`. Rename the sync wrapper to `main_cli()` so `pyproject.toml` can reference it:

```python
def main_cli() -> None:
    asyncio.run(_async_main())
```

### 2c. How to run

```sh
# First-time setup
uv sync               # creates .venv, installs deps, installs package editable

# Every subsequent run
uv run app            # reads data/input/input.txt, writes to data/output/
```

---

## 3. SQLite Settings Database (`db/storage.db`)

Replaces `.env`. The file lives at the `db/` folder and is gitignored.

### 3a. Schema

```sql
-- Key/value store for scalar settings (replaces all .env vars except researcher models)
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Researcher model roster with an enabled/disabled flag
-- Multiple researchers can run in parallel; improver and reviewer each use
-- exactly one model from settings.
CREATE TABLE IF NOT EXISTS researcher_models (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    model_slug       TEXT    UNIQUE NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,   -- 1 = active, 0 = skipped
    preference_order INTEGER NOT NULL DEFAULT 0    -- ascending: 1 = most preferred
);
```

### 3b. Settings rows (seed data)

| key | default value |
|-----|---------------|
| `OPENROUTER_API_KEY` | *(empty — user must set)* |
| `IMPROVER_MODEL` | `anthropic/claude-sonnet-4-5` |
| `REVIEWER_MODEL` | `anthropic/claude-opus-4-5` |
| `REQUEST_TIMEOUT_SECONDS` | `180` |
| `MAX_RETRIES` | `2` |
| `MIN_SUCCESSFUL_RESEARCHERS` | `2` |

### 3c. Researcher models seed data

The models listed in `docs/MODELS.md` are seeded on first `init_db()`. Each gets an `enabled` flag the user can flip:

| model_slug | enabled | preference_order |
|-----------|---------|-----------------|
| `anthropic/claude-sonnet-4-5` | 1 | 1 |
| `anthropic/claude-opus-4-5` | 1 | 2 |
| `openai/gpt-4o` | 1 | 3 |
| `google/gemini-pro-1.5` | 1 | 4 |
| `deepseek/deepseek-chat` | 1 | 5 |
| `openai/gpt-5.2` | 0 | 6 |
| `google/gemini-3.1-pro-preview` | 0 | 7 |
| `deepseek/deepseek-v3.2` | 0 | 8 |
| `mistralai/devstral-2-2512` | 0 | 9 |

To enable/disable a model, use a SQLite client or the helper script (see §5):

```sh
# Enable a model
sqlite3 db/storage.db "UPDATE researcher_models SET enabled=1 WHERE model_slug='openai/gpt-5.2';"

# Disable a model
sqlite3 db/storage.db "UPDATE researcher_models SET enabled=0 WHERE model_slug='openai/gpt-4o';"

# Set API key
sqlite3 db/storage.db "UPDATE settings SET value='sk-or-...' WHERE key='OPENROUTER_API_KEY';"
```

### 3d. `db.py` public API

```python
def init_db(db_path: Path) -> None
    """Create tables + seed defaults if they do not exist. Safe to call on every start."""

def get_setting(db_path: Path, key: str) -> str
    """Return value or raise KeyError if key not found."""

def set_setting(db_path: Path, key: str, value: str) -> None

def get_enabled_researcher_models(db_path: Path) -> list[str]
    """Return slugs of all enabled models ordered by preference_order ascending."""

def get_all_researcher_models(db_path: Path) -> list[dict]
    """Return all rows as dicts for display/debugging."""

def set_researcher_model_enabled(db_path: Path, slug: str, enabled: bool) -> None
```

The functions open and close a connection per call (no persistent connection object) — the call frequency is too low to warrant pooling.

---

## 4. Code Changes per File

### `src/research_assistant/main.py`

- **Remove** `from dotenv import load_dotenv` and all `os.environ.get` config reads.
- **Import** `from research_assistant.db import init_db, get_setting, get_enabled_researcher_models`.
- **Add** `DB_PATH = PROJECT_DIR / "db" / "storage.db"` (project root — `__file__` is now two levels deeper, so `parent.parent.parent`).
- **Update** all `Path` constants:
  ```python
  PROJECT_DIR           = Path(__file__).resolve().parent.parent.parent
  DB_PATH               = PROJECT_DIR / "db" / "storage.db"
  INPUT_PATH            = PROJECT_DIR / "data" / "input" / "input.txt"
  INTERMEDIATE_DIR      = PROJECT_DIR / "data" / "intermediate"
  OUTPUT_DIR            = PROJECT_DIR / "data" / "output"
  CLARIFICATIONS_PATH   = INTERMEDIATE_DIR / "clarifications.md"
  OUTPUT_INTERMEDIATE   = INTERMEDIATE_DIR / "output_intermediate.md"
  OUTPUT_FINAL          = OUTPUT_DIR / "output_final.md"
  PROMPTS_DIR           = PROJECT_DIR / "prompts"
  ```
- **Update** `_load_config()` to call `init_db(DB_PATH)` then read all settings from db.
- **Rename** sync wrapper to `main_cli()`.
- **Create** `INTERMEDIATE_DIR` and `OUTPUT_DIR` at startup with `mkdir(parents=True, exist_ok=True)`.

### `src/research_assistant/improver.py`, `researcher.py`, `reviewer.py`, `openrouter_client.py`

- Only change: update relative imports to absolute package imports:
  ```python
  # before
  from openrouter_client import ...
  # after
  from research_assistant.openrouter_client import ...
  ```
- `improver.py` also references `_extract_json_object` which `reviewer.py` imports — no change needed there since both are in the same package.
- No logic changes required.

### `.gitignore` additions

```
db/storage.db
data/intermediate/
data/output/
docs/clarifications.md
.venv/
__pycache__/
*.pyc
```

---

## 5. Optional: `manage_db.py` Helper Script

A small CLI helper so the user doesn't need to type raw SQL to configure the database. Located at project root (not inside the package — it's a dev/admin tool).

```
usage: uv run python manage_db.py [command]

Commands:
  init                  Create db/storage.db with defaults (run once)
  show                  Print all settings and researcher model list
  set <key> <value>     Update a setting (e.g., set OPENROUTER_API_KEY sk-or-...)
  enable <slug>         Enable a researcher model
  disable <slug>        Disable a researcher model
  add <slug> [order]    Add a new researcher model slug
```

---

## 6. Migration Steps (Ordered)

1. **Create folders:** `docs/`, `data/input/`, `data/intermediate/`, `data/output/`, `src/research_assistant/`.
2. **Move docs:** `plan.md` → `docs/plan.md`, `MODELS.md` → `docs/MODELS.md`.
3. **Move input:** `input.txt` → `data/input/input.txt`.
4. **Move Python files** into `src/research_assistant/`; create `__init__.py`.
5. **Write `src/research_assistant/db.py`** with schema + seed data.
6. **Update imports** in all moved Python files (absolute package imports).
7. **Update `Path` constants** in `main.py` and rename `main()` → `main_cli()`.
8. **Write `pyproject.toml`**.
9. **Delete** `requirements.txt`, `.env`, `.env.example`.
10. **Run `uv sync`** — creates `.venv`, installs httpx, installs the package editable.
11. **Run `uv run python manage_db.py init`** to create `db/storage.db` with seed data.
12. **Set API key:** `uv run python manage_db.py set OPENROUTER_API_KEY sk-or-...`
13. **Verify:** `uv run app` — pipeline should run end-to-end reading from `data/input/input.txt`.
14. **Clean up** root-level artefacts: `output.md`, `clarifications.md` (if present).

---

## 7. What Does NOT Change

- The pipeline logic in all four stage modules (`improver`, `researcher`, `reviewer`, `openrouter_client`) — zero functional changes.
- The `prompts/` directory location (stays at project root, referenced by `PROMPTS_DIR`).
- The three-stage pipeline flow (Improver → Researchers → Reviewer).
- The output format of `output_intermediate.md` and `output_final.md`.

---

*Original build plan (Phases 0–5) is preserved in git history.*
4. Stub the three prompt files in `prompts/` with one-line placeholders; real prompts land in Phase 6.
5. Create `input.txt` with a throwaway question for testing.

**Test point:** `pip install -r requirements.txt` succeeds in a clean venv.

---

## Phase 1 — `openrouter_client.py`

The single foundation every stage depends on. Keep it small.

- Public surface: one async function
  `call_model(client, model, system, user, *, response_format=None, timeout, max_retries) -> str` (returns the assistant message content).
- Use a shared `httpx.AsyncClient` passed in from the caller — connection pooling matters when 5+ researchers fire concurrently.
- Endpoint: `POST https://openrouter.ai/api/v1/chat/completions`, OpenAI-compatible body `{model, messages:[{role:"system",…},{role:"user",…}], response_format}`.
- Retries: exponential backoff 1s / 2s / 4s, capped at `max_retries`. Retry on network errors, 429, and 5xx. Do NOT retry on 4xx auth errors — fail fast.
- Raise a typed `ModelCallFailed(model, last_error)` after exhaustion so callers decide whether to drop or abort.

**Test point:** a tiny `scratch_client.py` that fires one call against a cheap model (`openai/gpt-4o-mini`) and prints the response. If this works, every downstream stage will.

---

## Phase 2 — `improver.py`

Handles the conditional two-pass flow. Pure logic, no orchestration decisions.

Build in this order:

1. `parse_clarifications_md(path) -> list[dict]` — reads the file into `[{question, answer}, …]`. Format: `## Question N` header, question text, then `**Answer:**` followed by user-typed text.
2. `write_clarifications_md(path, questions)` — writes the template with empty answer slots.
3. `run_improver(client, model, raw_prompt, prior_clarifications=None) -> dict` — calls the model, forces JSON via `response_format={"type":"json_object"}`, validates the three keys (`needs_clarification`, `questions`, `improved_prompt`). If JSON parsing fails, retry once with a schema reminder; if still malformed, raise.

**Test point:** two scratch runs — a vague prompt ("tell me about AI") should return `needs_clarification=true`; a well-scoped prompt should return a rewritten `improved_prompt`.

---

## Phase 3 — `researcher.py`

Parallel fan-out. Only genuinely concurrent stage.

- Public function:
  `async def run_researchers(client, model_slugs, improved_prompt, system_prompt, config) -> list[ResearcherResult]`
  where each result is `{model, success: bool, content: str | None, error: str | None}`.
- Use `asyncio.gather(..., return_exceptions=True)` so one failing model can't sink the batch.
- Each call goes through `openrouter_client.call_model` which already handles retries. The researcher layer's only job on failure is to convert `ModelCallFailed` into a recorded drop.
- Do NOT enforce `MIN_SUCCESSFUL_RESEARCHERS` here. That's an orchestration-level decision.

**Test point:** run against two real models plus one deliberately bogus slug (`fake/does-not-exist`). Expect two successes and one recorded drop, with wall-clock time ≈ the slowest single call, not the sum.

---

## Phase 4 — `reviewer.py`

- Public function: `run_reviewer(client, model, improved_prompt, researcher_results, preference_ranking) -> dict`.
- The user message to the reviewer must include: the improved prompt, each successful researcher's output labeled with its slug, and the preference ranking as a tiebreaker hint (pass it in from `RESEARCHER_MODELS` order — don't hardcode in the prompt file).
- Force JSON output and validate the schema shape (`consensus`, `mixed_opinions`, `notes`). Same retry-once-on-malformed policy as the improver.

**Test point:** feed synthetic researcher outputs where two models agree on claim A and diverge on claim B. Verify the reviewer puts A in `consensus` and B in `mixed_opinions` with reasonable confidence scores.

---

## Phase 5 — `main.py` orchestration

Only built after every stage above is independently verified.

1. Load `.env`, validate required keys, fail fast if any missing.
2. Read `input.txt`.
3. Branch on `clarifications.md` existence or improver result:
   - Starting from a fresh run: The improver is called. If `needs_clarification=true`, the app enters an **interactive terminal loop**. It prompts the user for answers one-by-one using `input()`, then re-calls the improver with the full context.
   - Resuming/Pre-filling: If `clarifications.md` exists at startup (e.g. from a past run or manual pre-fill), it is loaded as initial context for the first improver call.
   - Once the improver returns `needs_clarification=false`, the pipeline continues with the `improved_prompt`.
4. Run researchers. If fewer than `MIN_SUCCESSFUL_RESEARCHERS` succeed, skip the reviewer and write an explanatory `output.md` listing the failures.
5. Run reviewer.
6. Assemble `output.md` (see Phase 6).
7. **Only on full success**, delete `clarifications.md`. If any later stage errors after a successful clarifications parse, leave the file alone so the user can rerun.


Open a single `httpx.AsyncClient` at the top of `main()`, pass it into every stage, close it in a `finally`.

---

## Phase 6 — Output assembly and final prompts

The markdown builder is ~50 lines of string concatenation; can live in `main.py` or a small `output.py`. Five sections in the order the requirements specify.

Write the real system prompts in `prompts/` now that you know exactly what inputs each role receives:

- **Improver:** JSON-only response, strict schema, err on the side of `needs_clarification=true` only when genuinely ambiguous (not just terse).
- **Researcher:** answer thoroughly, show reasoning, state uncertainty inline, don't refuse or over-hedge.
- **Reviewer:** JSON-only, scoring rubric spelled out (support count, reasoning consistency, verifiability), preference ranking is a tiebreaker only.

---

## Phase 7 — End-to-end tests (manual, no test framework)

Four runs against the real stack:

1. Clear prompt, 2 researchers → full `output.md` produced.
2. Vague prompt → Interactive loop starts in terminal; provide answers; verify → full `output_final.md`.
3. Deliberately bogus slug mixed into `RESEARCHER_MODELS` → that model listed as dropped; run still completes.
4. Set `MIN_SUCCESSFUL_RESEARCHERS=10` with only 5 researchers → pipeline aborts with an explanatory `output.md`.

---

## Gotchas to resolve before coding

1. **`MAX_RETRIES` semantics.** Is `2` "two total attempts" or "two retries after the first"? The word "retries" implies the latter (3 total calls). Pick one and document it in `.env.example`.
2. **`clarifications.md` format.** The spec says the user fills in answers "inline" but doesn't define the format. Pin it down now (suggest: `## Question N` + `**Answer:**` blocks) so the parser stays trivial.
3. **JSON mode reliability.** Not every OpenRouter provider honors `response_format={"type":"json_object"}`. Belt-and-suspenders: instruct JSON in the system prompt *and* defensively extract the first balanced `{…}` block from the response before parsing.
4. **Preference ranking source of truth.** Reviewer uses it as a tiebreaker — is it the literal order of `RESEARCHER_MODELS`, or a separate var? Reusing `RESEARCHER_MODELS` order is simpler and matches how depth is tuned.
5. **Deletion of `clarifications.md`.** Only after `output.md` is fully written. A crash mid-review should preserve it so the user can retry without re-typing answers.
6. **Concurrency ceiling.** `asyncio.gather` with 5–7 coroutines is fine, but at 20+ you want a `Semaphore`. Leave a TODO.
7. **Exit codes.** Not specified. Suggest: `0` on success (including "needs clarification, exiting cleanly"), `1` on configuration/input errors, `2` on pipeline-abort (too few researchers, malformed JSON).
