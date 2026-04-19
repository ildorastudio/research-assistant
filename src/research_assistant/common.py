import sys
from dataclasses import dataclass
from pathlib import Path
from research_assistant.db import (
    get_enabled_researcher_models,
    get_setting,
    init_db,
)
from research_assistant.researcher import ResearcherResult
from research_assistant.reviewer import ReviewerResult

# ---------------------------------------------------------------------------
# Paths — all relative to the project root (three levels above this file)
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DB_PATH = PROJECT_DIR / "db" / "storage.db"
INPUT_PATH = PROJECT_DIR / "data" / "input" / "input.txt"
INTERMEDIATE_DIR = PROJECT_DIR / "data" / "intermediate"
OUTPUT_DIR = PROJECT_DIR / "data" / "output"
CLARIFICATIONS_PATH = INTERMEDIATE_DIR / "clarifications.md"
OUTPUT_INTERMEDIATE = INTERMEDIATE_DIR / "output_intermediate.md"
OUTPUT_FINAL = OUTPUT_DIR / "output_final.md"
IMPROVED_PROMPT_PATH = INPUT_PATH.parent / "improved_prompt.txt"
PROMPTS_DIR = PROJECT_DIR / "prompts"


@dataclass
class Config:
    api_key: str
    improver_model: str
    researcher_models: list[str]
    reviewer_model: str
    timeout: float
    max_retries: int
    min_successful_researchers: int


def load_config() -> Config:
    init_db(DB_PATH)

    def _get(key: str) -> str:
        try:
            return get_setting(DB_PATH, key)
        except KeyError:
            print(f"error: setting {key!r} not found in db/storage.db", file=sys.stderr)
            sys.exit(1)

    api_key = _get("OPENROUTER_API_KEY")
    if not api_key.strip():
        print(
            "error: OPENROUTER_API_KEY is empty.\n"
            "Set it with:  uv run python manage_db.py set OPENROUTER_API_KEY sk-or-...",
            file=sys.stderr,
        )
        sys.exit(1)

    researcher_models = get_enabled_researcher_models(DB_PATH)
    if not researcher_models:
        print(
            "error: no researcher models are enabled in db/storage.db.\n"
            "Enable one with:  uv run python manage_db.py enable <model-slug>",
            file=sys.stderr,
        )
        sys.exit(1)

    def _int(key: str, default: int) -> int:
        raw = _get(key)
        if not raw.strip():
            return default
        try:
            return int(raw)
        except ValueError:
            print(f"error: {key} must be an integer, got {raw!r}", file=sys.stderr)
            sys.exit(1)

    return Config(
        api_key=api_key.strip(),
        improver_model=_get("IMPROVER_MODEL"),
        researcher_models=researcher_models,
        reviewer_model=_get("REVIEWER_MODEL"),
        timeout=float(_int("REQUEST_TIMEOUT_SECONDS", 180)),
        max_retries=_int("MAX_RETRIES", 2),
        min_successful_researchers=_int("MIN_SUCCESSFUL_RESEARCHERS", 2),
    )


def read_text(path: Path, description: str) -> str:
    if not path.exists():
        print(f"error: {description} not found at {path}", file=sys.stderr)
        sys.exit(1)
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------


def format_intermediate_output(
    original_prompt: str,
    improved_prompt: str,
    config: Config,
    researcher_results: list[ResearcherResult],
) -> str:
    successful = [r for r in researcher_results if r.success]
    failed = [r for r in researcher_results if not r.success]

    lines: list[str] = []
    lines.append("# Research — Intermediate Output")
    lines.append("")

    lines.append("## Query")
    lines.append("")
    lines.append("**Original prompt:**")
    lines.append("")
    lines.append("> " + original_prompt.strip().replace("\n", "\n> "))
    lines.append("")
    if improved_prompt.strip() != original_prompt.strip():
        lines.append("**Improved prompt:**")
        lines.append("")
        lines.append("> " + improved_prompt.strip().replace("\n", "\n> "))
        lines.append("")

    lines.append("## Models used")
    lines.append("")
    lines.append(f"- **Improver:** `{config.improver_model}`")
    lines.append("- **Researchers (succeeded):**")
    if successful:
        for r in successful:
            lines.append(f"  - `{r.model}`")
    else:
        lines.append("  - (none)")
    if failed:
        lines.append("- **Researchers (failed and dropped):**")
        for r in failed:
            lines.append(f"  - `{r.model}` — {r.error}")
    lines.append("")

    lines.append("## Researcher responses")
    lines.append("")
    if successful:
        for r in successful:
            lines.append(f"### {r.model}")
            lines.append("")
            lines.append((r.content or "").strip())
            lines.append("")
    else:
        lines.append("_No successful researcher responses._")
        lines.append("")

    return "\n".join(lines)


def format_intermediate_header(original_prompt: str, improved_prompt: str) -> str:
    """Return the static header written to output_intermediate.md before threads start.

    Each researcher thread appends its own section via ``format_intermediate_footer``
    once it has finished.
    """
    lines: list[str] = [
        "# Research — Intermediate Output",
        "",
        "## Query",
        "",
        "**Original prompt:**",
        "",
        "> " + original_prompt.strip().replace("\n", "\n> "),
        "",
    ]
    if improved_prompt.strip() != original_prompt.strip():
        lines += [
            "**Improved prompt:**",
            "",
            "> " + improved_prompt.strip().replace("\n", "\n> "),
            "",
        ]
    lines += [
        "## Researcher responses",
        "",
    ]
    return "\n".join(lines)


def format_intermediate_footer(config: Config, researcher_results: list[ResearcherResult]) -> str:
    """Return the models-summary section appended after all researcher threads complete."""
    successful = [r for r in researcher_results if r.success]
    failed = [r for r in researcher_results if not r.success]

    lines: list[str] = [
        "## Models used",
        "",
        f"- **Improver:** `{config.improver_model}`",
        "- **Researchers (succeeded):**",
    ]
    if successful:
        for r in successful:
            lines.append(f"  - `{r.model}`")
    else:
        lines.append("  - (none)")
    if failed:
        lines.append("- **Researchers (failed and dropped):**")
        for r in failed:
            lines.append(f"  - `{r.model}` — {r.error}")
    lines.append("")
    return "\n".join(lines)


def format_final_output(
    review: ReviewerResult,
) -> str:
    def _esc(t: str) -> str:
        return t.replace("\n", " ").replace("|", "\\|")

    lines: list[str] = []
    lines.append("# Research Answer")
    lines.append("")

    lines.append("## Consensus findings")
    lines.append("")
    if review.consensus:
        lines.append("| Claim | Supported by |")
        lines.append("| :--- | :--- |")
        for entry in review.consensus:
            supporters = (
                ", ".join(f"`{m}`" for m in entry.supporting_models) or "(unspecified)"
            )
            lines.append(f"| {_esc(entry.claim)} | {supporters} |")
    else:
        lines.append(
            "_The reviewer did not identify any claims that every researcher agreed on._"
        )
    lines.append("")

    if review.mixed_opinions:
        lines.append("## Mixed opinions")
        lines.append("")
        
        # Flatten all views to sort them by confidence
        all_views = []
        for opinion in review.mixed_opinions:
            for view in opinion.views:
                all_views.append({
                    "topic": opinion.topic,
                    "claim": view.claim,
                    "confidence": view.confidence,
                    "supporting_models": view.supporting_models,
                    "reasoning": view.reasoning
                })
        
        # Sort by confidence descending
        all_views.sort(key=lambda x: x["confidence"], reverse=True)

        lines.append("| Topic | Claim | Confidence | Supported by | Reasoning |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for v in all_views:
            supporters = (
                ", ".join(f"`{m}`" for m in v["supporting_models"])
                or "(unspecified)"
            )
            lines.append(
                f"| {_esc(v['topic'])} | {_esc(v['claim'])} | {v['confidence']}/100 | "
                f"{supporters} | {_esc(v['reasoning'])} |"
            )
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append(review.notes if review.notes else "_(no reviewer notes)_")
    lines.append("")

    return "\n".join(lines)


def format_abort_output(
    original_prompt: str,
    improved_prompt: str,
    config: Config,
    researcher_results: list[ResearcherResult],
    reason: str,
) -> str:
    successful = [r for r in researcher_results if r.success]
    failed = [r for r in researcher_results if not r.success]

    lines = [
        "# Research Answer — Aborted",
        "",
        "## Reason",
        "",
        reason,
        "",
        "## Query",
        "",
        "**Original prompt:**",
        "",
        "> " + original_prompt.strip().replace("\n", "\n> "),
        "",
    ]
    if improved_prompt and improved_prompt.strip() != original_prompt.strip():
        lines += [
            "**Improved prompt:**",
            "",
            "> " + improved_prompt.strip().replace("\n", "\n> "),
            "",
        ]
    lines += [
        "## Models used",
        "",
        f"- **Improver:** `{config.improver_model}`",
        f"- **Reviewer:** `{config.reviewer_model}` (not invoked)",
        f"- **MIN_SUCCESSFUL_RESEARCHERS:** {config.min_successful_researchers}",
        "",
        "**Researcher results:**",
        "",
    ]
    if successful:
        for r in successful:
            lines.append(f"- `{r.model}` — succeeded")
    if failed:
        for r in failed:
            lines.append(f"- `{r.model}` — FAILED: {r.error}")
    if not successful and not failed:
        lines.append("- (no researchers ran)")
    lines.append("")
    return "\n".join(lines)
