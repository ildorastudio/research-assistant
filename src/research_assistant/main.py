"""CLI entry point for the Multi-LLM Research Assistant.

Pipeline:
  1. Init db/storage.db and load config. Fail fast if API key is not set.
  2. If data/intermediate/clarifications.md is absent, call the improver with
     just the prompt. If the improver asks questions, write them to
     clarifications.md and exit 0.
  3. If clarifications.md is present, parse it and call the improver with the
     full context. Expect a finalized research brief.
  4. Fan out the brief to every enabled researcher in parallel.
     If fewer than MIN_SUCCESSFUL_RESEARCHERS succeed, abort and explain.
  5. Send all successful responses to the reviewer for synthesis.
  6. Write data/intermediate/output_intermediate.md (raw researcher responses)
     and data/output/output_final.md (reviewer synthesis), then delete
     clarifications.md.

Exit codes:
  0 — success, or "needs clarification, written clarifications.md, exited cleanly"
  1 — configuration or input error (missing db/storage.db key, missing input.txt)
  2 — pipeline abort (too few researchers, malformed model JSON twice, etc.)
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

import httpx

from research_assistant.db import (
    get_enabled_researcher_models,
    get_setting,
    init_db,
)
from research_assistant.improver import (
    ImproverResult,
    parse_clarifications_md,
    run_improver,
    write_clarifications_md,
)
from research_assistant.openrouter_client import ModelCallFailed
from research_assistant.researcher import ResearcherResult, run_researchers
from research_assistant.reviewer import ReviewerResult, run_reviewer


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
PROMPTS_DIR = PROJECT_DIR / "prompts"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    api_key: str
    improver_model: str
    researcher_models: list[str]
    reviewer_model: str
    timeout: float
    max_retries: int
    min_successful_researchers: int


def _load_config() -> Config:
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


def _read_text(path: Path, description: str) -> str:
    if not path.exists():
        print(f"error: {description} not found at {path}", file=sys.stderr)
        sys.exit(1)
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------


def _format_intermediate_output(
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


def _format_final_output(
    original_prompt: str,
    improved_prompt: str,
    config: Config,
    researcher_results: list[ResearcherResult],
    review: ReviewerResult,
) -> str:
    successful = [r for r in researcher_results if r.success]
    failed = [r for r in researcher_results if not r.success]

    lines: list[str] = []
    lines.append("# Research Answer")
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
    lines.append(f"- **Reviewer:** `{config.reviewer_model}`")
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

    lines.append("## Consensus findings")
    lines.append("")
    if review.consensus:
        for entry in review.consensus:
            supporters = (
                ", ".join(f"`{m}`" for m in entry.supporting_models) or "(unspecified)"
            )
            lines.append(f"- {entry.claim}")
            lines.append(f"  - Supported by: {supporters}")
    else:
        lines.append(
            "_The reviewer did not identify any claims that every researcher agreed on._"
        )
    lines.append("")

    lines.append("## Mixed opinions")
    lines.append("")
    if review.mixed_opinions:
        for opinion in review.mixed_opinions:
            lines.append(f"### {opinion.topic}")
            lines.append("")
            for view in opinion.views:
                supporters = (
                    ", ".join(f"`{m}`" for m in view.supporting_models)
                    or "(unspecified)"
                )
                lines.append(f"- **Claim:** {view.claim}")
                lines.append(f"  - **Confidence:** {view.confidence}/100")
                lines.append(f"  - **Supported by:** {supporters}")
                if view.reasoning:
                    lines.append(f"  - **Reasoning:** {view.reasoning}")
            lines.append("")
    else:
        lines.append("_The reviewer did not identify any disputed claims._")
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append(review.notes if review.notes else "_(no reviewer notes)_")
    lines.append("")

    return "\n".join(lines)


def _format_abort_output(
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _run() -> int:
    config = _load_config()

    # Ensure runtime directories exist
    INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    original_prompt = _read_text(INPUT_PATH, "data/input/input.txt").strip()
    if not original_prompt:
        print("error: data/input/input.txt is empty", file=sys.stderr)
        return 1

    # Load all three system prompts up front so a missing file fails fast.
    improver_system = _read_text(PROMPTS_DIR / "improver.txt", "prompts/improver.txt")
    researcher_system = _read_text(
        PROMPTS_DIR / "researcher.txt", "prompts/researcher.txt"
    )
    reviewer_system = _read_text(PROMPTS_DIR / "reviewer.txt", "prompts/reviewer.txt")

    # A single shared client across every stage enables connection pooling.
    async with httpx.AsyncClient() as client:
        # -------------------------------------------------------------------
        # Stage 1: improver
        # -------------------------------------------------------------------
        clarifications_present = CLARIFICATIONS_PATH.exists()
        prior = (
            parse_clarifications_md(CLARIFICATIONS_PATH)
            if clarifications_present
            else None
        )

        print(f"[1/3] Calling improver ({config.improver_model})...")
        try:
            improver_result: ImproverResult = await run_improver(
                client,
                config.improver_model,
                system_prompt=improver_system,
                raw_prompt=original_prompt,
                api_key=config.api_key,
                prior_clarifications=prior,
                timeout=config.timeout,
                max_retries=config.max_retries,
            )
        except ModelCallFailed as exc:
            print(f"error: improver failed: {exc}", file=sys.stderr)
            return 2

        if improver_result.needs_clarification:
            write_clarifications_md(CLARIFICATIONS_PATH, improver_result.questions)
            if clarifications_present:
                print(
                    "The improver still needs more information after your answers. "
                    f"Updated questions written to {CLARIFICATIONS_PATH}. "
                    "Please update your answers and rerun.",
                )
            else:
                print(
                    f"The improver needs clarification. Questions written to "
                    f"{CLARIFICATIONS_PATH}. Fill in your answers and rerun.",
                )
            return 0

        improved_prompt = improver_result.improved_prompt

        # -------------------------------------------------------------------
        # Stage 2: researchers
        # -------------------------------------------------------------------
        print(
            f"[2/3] Running {len(config.researcher_models)} researchers in parallel...",
        )
        researcher_results = await run_researchers(
            client,
            config.researcher_models,
            system_prompt=researcher_system,
            improved_prompt=improved_prompt,
            api_key=config.api_key,
            timeout=config.timeout,
            max_retries=config.max_retries,
        )
        successful = [r for r in researcher_results if r.success]
        failed = [r for r in researcher_results if not r.success]
        for r in successful:
            print(f"    ok   {r.model}")
        for r in failed:
            print(f"    FAIL {r.model} — {r.error}")

        if len(successful) < config.min_successful_researchers:
            reason = (
                f"Only {len(successful)} researcher(s) succeeded, but "
                f"MIN_SUCCESSFUL_RESEARCHERS is {config.min_successful_researchers}. "
                "Not synthesizing a potentially misleading answer."
            )
            print(f"error: {reason}", file=sys.stderr)
            OUTPUT_INTERMEDIATE.write_text(
                _format_abort_output(
                    original_prompt, improved_prompt, config, researcher_results, reason
                ),
                encoding="utf-8",
            )
            return 2

        OUTPUT_INTERMEDIATE.write_text(
            _format_intermediate_output(
                original_prompt, improved_prompt, config, researcher_results
            ),
            encoding="utf-8",
        )
        print(f"Wrote {OUTPUT_INTERMEDIATE.relative_to(PROJECT_DIR)}.")

        # -------------------------------------------------------------------
        # Stage 3: reviewer
        # -------------------------------------------------------------------
        print(f"[3/3] Calling reviewer ({config.reviewer_model})...")
        try:
            review = await run_reviewer(
                client,
                config.reviewer_model,
                system_prompt=reviewer_system,
                improved_prompt=improved_prompt,
                researcher_results=researcher_results,
                preference_ranking=config.researcher_models,
                api_key=config.api_key,
                timeout=config.timeout,
                max_retries=config.max_retries,
            )
        except ModelCallFailed as exc:
            print(f"error: reviewer failed: {exc}", file=sys.stderr)
            return 2

        # -------------------------------------------------------------------
        # Stage 4: output assembly
        # -------------------------------------------------------------------
        OUTPUT_FINAL.write_text(
            _format_final_output(
                original_prompt, improved_prompt, config, researcher_results, review
            ),
            encoding="utf-8",
        )
        print(f"Done. Wrote {OUTPUT_FINAL.relative_to(PROJECT_DIR)}.")

        # Only clean up clarifications.md after a fully successful run.
        if CLARIFICATIONS_PATH.exists():
            try:
                CLARIFICATIONS_PATH.unlink()
            except OSError:
                pass

        return 0


def main_cli() -> None:
    """Entry point for `uv run app`."""
    try:
        code = asyncio.run(_run())
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        code = 1
    except Exception:  # pragma: no cover
        traceback.print_exc()
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main_cli()
