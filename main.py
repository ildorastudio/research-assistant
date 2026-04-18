"""CLI entry point for the Multi-LLM Research Assistant.

Pipeline:
  1. Load .env and input.txt. Fail fast if anything is missing.
  2. If clarifications.md is absent, call the improver with just the prompt.
     If the improver asks questions, write them to clarifications.md and exit 0.
  3. If clarifications.md is present, parse it and call the improver with the
     full context. Expect a finalized research brief.
  4. Fan out the brief to every researcher in RESEARCHER_MODELS in parallel.
     If fewer than MIN_SUCCESSFUL_RESEARCHERS succeed, abort and explain.
  5. Send all successful responses to the reviewer for synthesis.
  6. Write output.md and delete clarifications.md.

Exit codes:
  0 — success, or "needs clarification, written clarifications.md, exited cleanly"
  1 — configuration or input error (missing .env, missing input.txt, etc.)
  2 — pipeline abort (too few researchers, malformed model JSON twice, etc.)
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv

from improver import (
    ImproverResult,
    parse_clarifications_md,
    run_improver,
    write_clarifications_md,
)
from openrouter_client import ModelCallFailed
from researcher import ResearcherResult, run_researchers
from reviewer import ReviewerResult, run_reviewer


PROJECT_DIR = Path(__file__).resolve().parent
INPUT_PATH = PROJECT_DIR / "input.txt"
OUTPUT_PATH = PROJECT_DIR / "output.md"
CLARIFICATIONS_PATH = PROJECT_DIR / "clarifications.md"
PROMPTS_DIR = PROJECT_DIR / "prompts"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    improver_model: str
    researcher_models: list[str]
    reviewer_model: str
    timeout: float
    max_retries: int
    min_successful_researchers: int


def _require_env(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        print(f"error: {key} is not set in .env", file=sys.stderr)
        sys.exit(1)
    return value


def _load_config() -> Config:
    load_dotenv(PROJECT_DIR / ".env")

    # OPENROUTER_API_KEY is read inside openrouter_client.call_model, but we
    # validate its presence up front for a better error message.
    _require_env("OPENROUTER_API_KEY")

    improver_model = _require_env("IMPROVER_MODEL")
    reviewer_model = _require_env("REVIEWER_MODEL")
    researcher_raw = _require_env("RESEARCHER_MODELS")
    researcher_models = [s.strip() for s in researcher_raw.split(",") if s.strip()]
    if not researcher_models:
        print("error: RESEARCHER_MODELS is empty", file=sys.stderr)
        sys.exit(1)

    def _int(key: str, default: int) -> int:
        raw = os.environ.get(key, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print(f"error: {key} must be an integer, got {raw!r}", file=sys.stderr)
            sys.exit(1)

    return Config(
        improver_model=improver_model,
        researcher_models=researcher_models,
        reviewer_model=reviewer_model,
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

def _format_output(
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

    # --- Query ---
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

    # --- Models used ---
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

    # --- Consensus ---
    lines.append("## Consensus findings")
    lines.append("")
    if review.consensus:
        for entry in review.consensus:
            supporters = ", ".join(f"`{m}`" for m in entry.supporting_models) or "(unspecified)"
            lines.append(f"- {entry.claim}")
            lines.append(f"  - Supported by: {supporters}")
    else:
        lines.append("_The reviewer did not identify any claims that every researcher agreed on._")
    lines.append("")

    # --- Mixed opinions ---
    lines.append("## Mixed opinions")
    lines.append("")
    if review.mixed_opinions:
        for opinion in review.mixed_opinions:
            lines.append(f"### {opinion.topic}")
            lines.append("")
            for view in opinion.views:
                supporters = ", ".join(f"`{m}`" for m in view.supporting_models) or "(unspecified)"
                lines.append(f"- **Claim:** {view.claim}")
                lines.append(f"  - **Confidence:** {view.confidence}/100")
                lines.append(f"  - **Supported by:** {supporters}")
                if view.reasoning:
                    lines.append(f"  - **Reasoning:** {view.reasoning}")
            lines.append("")
    else:
        lines.append("_The reviewer did not identify any disputed claims._")
        lines.append("")

    # --- Notes ---
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
    original_prompt = _read_text(INPUT_PATH, "input.txt").strip()
    if not original_prompt:
        print("error: input.txt is empty", file=sys.stderr)
        return 1

    # Load all three system prompts up front so a missing file fails fast.
    improver_system = _read_text(PROMPTS_DIR / "improver.txt", "prompts/improver.txt")
    researcher_system = _read_text(PROMPTS_DIR / "researcher.txt", "prompts/researcher.txt")
    reviewer_system = _read_text(PROMPTS_DIR / "reviewer.txt", "prompts/reviewer.txt")

    # A single shared client across every stage enables connection pooling.
    async with httpx.AsyncClient() as client:

        # -------------------------------------------------------------------
        # Stage 1: improver
        # -------------------------------------------------------------------
        clarifications_present = CLARIFICATIONS_PATH.exists()
        prior = parse_clarifications_md(CLARIFICATIONS_PATH) if clarifications_present else None

        print(f"[1/3] Calling improver ({config.improver_model})...")
        try:
            improver_result: ImproverResult = await run_improver(
                client,
                config.improver_model,
                system_prompt=improver_system,
                raw_prompt=original_prompt,
                prior_clarifications=prior,
                timeout=config.timeout,
                max_retries=config.max_retries,
            )
        except ModelCallFailed as exc:
            print(f"error: improver failed: {exc}", file=sys.stderr)
            return 2

        if improver_result.needs_clarification:
            # Write the questions and exit. On the next run this branch will be
            # skipped because clarifications.md exists.
            write_clarifications_md(CLARIFICATIONS_PATH, improver_result.questions)
            if clarifications_present:
                print(
                    "The improver still needs more information after your answers. "
                    f"Updated questions written to {CLARIFICATIONS_PATH.name}. "
                    "Please update your answers and rerun.",
                )
            else:
                print(
                    f"The improver needs clarification. Questions written to "
                    f"{CLARIFICATIONS_PATH.name}. Fill in your answers and rerun.",
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
            OUTPUT_PATH.write_text(
                _format_abort_output(
                    original_prompt, improved_prompt, config, researcher_results, reason
                ),
                encoding="utf-8",
            )
            return 2

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
                timeout=config.timeout,
                max_retries=config.max_retries,
            )
        except ModelCallFailed as exc:
            print(f"error: reviewer failed: {exc}", file=sys.stderr)
            reason = f"The reviewer model could not produce a valid synthesis: {exc}"
            OUTPUT_PATH.write_text(
                _format_abort_output(
                    original_prompt, improved_prompt, config, researcher_results, reason
                ),
                encoding="utf-8",
            )
            return 2

        # -------------------------------------------------------------------
        # Stage 4: output assembly
        # -------------------------------------------------------------------
        OUTPUT_PATH.write_text(
            _format_output(original_prompt, improved_prompt, config, researcher_results, review),
            encoding="utf-8",
        )
        print(f"Done. Wrote {OUTPUT_PATH.name}.")

        # Only clean up clarifications.md after a fully successful run.
        if CLARIFICATIONS_PATH.exists():
            try:
                CLARIFICATIONS_PATH.unlink()
            except OSError:
                pass

        return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        return 1
    except Exception:  # pragma: no cover
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
