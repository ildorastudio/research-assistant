import asyncio
import sys
import threading
import httpx
from research_assistant.common import (
    load_config,
    read_text,
    INPUT_PATH,
    PROMPTS_DIR,
    IMPROVED_PROMPT_PATH,
    PROJECT_DIR,
    OUTPUT_INTERMEDIATE,
    OUTPUT_FINAL,
    INTERMEDIATE_DIR,
    CLARIFICATIONS_PATH,
    format_intermediate_header,
    format_intermediate_footer,
    format_final_output,
    format_abort_output,
)
from research_assistant.researcher import run_researchers
from research_assistant.reviewer import run_reviewer
from research_assistant.openrouter_client import ModelCallFailed

async def main():
    config = load_config()
    
    # Ensure runtime directories exist
    INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FINAL.parent.mkdir(parents=True, exist_ok=True)
    
    original_prompt = read_text(INPUT_PATH, "data/input/input.txt").strip()
    improved_prompt = read_text(IMPROVED_PROMPT_PATH, "data/input/improved_prompt.txt").strip()
    
    if not improved_prompt:
         print(f"error: {IMPROVED_PROMPT_PATH} is empty. Run step1/step2 first.", file=sys.stderr)
         sys.exit(1)

    researcher_system = read_text(PROMPTS_DIR / "researcher.txt", "prompts/researcher.txt")
    reviewer_system = read_text(PROMPTS_DIR / "reviewer.txt", "prompts/reviewer.txt")

    # Write the static header to output_intermediate.md before threads start so
    # partial results are visible on disk as each researcher completes.
    OUTPUT_INTERMEDIATE.write_text(
        format_intermediate_header(original_prompt, improved_prompt),
        encoding="utf-8",
    )

    file_lock = threading.Lock()

    print(f"Running {len(config.researcher_models)} researchers in parallel...", flush=True)
    researcher_results = await run_researchers(
        config.researcher_models,
        system_prompt=researcher_system,
        improved_prompt=improved_prompt,
        api_key=config.api_key,
        timeout=config.timeout,
        max_retries=config.max_retries,
        output_file=OUTPUT_INTERMEDIATE,
        file_lock=file_lock,
    )
    
    successful = [r for r in researcher_results if r.success]
    failed = [r for r in researcher_results if not r.success]
    for r in successful:
        print(f"    ok   {r.model}", flush=True)
    for r in failed:
        print(f"    FAIL {r.model} — {r.error}", flush=True)

    # Save individual researcher results
    for r in successful:
        sanitized_name = r.model.replace("/", "_").replace(":", "_")
        researcher_file = INTERMEDIATE_DIR / f"researcher_{sanitized_name}.md"
        researcher_file.write_text(r.content or "", encoding="utf-8")

    if len(successful) < config.min_successful_researchers:
        reason = (f"Only {len(successful)} researcher(s) succeeded, but "
                  f"MIN_SUCCESSFUL_RESEARCHERS is {config.min_successful_researchers}. "
                  "Not synthesizing a potentially misleading answer.")
        print(f"error: {reason}", file=sys.stderr, flush=True)
        OUTPUT_INTERMEDIATE.write_text(
            format_abort_output(original_prompt, improved_prompt, config, researcher_results, reason),
            encoding="utf-8"
        )
        sys.exit(2)

    # Append the models-summary footer now that all threads have finished and
    # the success/failure status of every researcher is known.
    with file_lock:
        existing = OUTPUT_INTERMEDIATE.read_text(encoding="utf-8")
        OUTPUT_INTERMEDIATE.write_text(
            existing + format_intermediate_footer(config, researcher_results),
            encoding="utf-8",
        )
    print(f"Wrote {OUTPUT_INTERMEDIATE.relative_to(PROJECT_DIR)}.", flush=True)

    async with httpx.AsyncClient() as client:
        print(f"Calling reviewer ({config.reviewer_model})...", flush=True)
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
            print(f"error: reviewer failed: {exc}", file=sys.stderr, flush=True)
            sys.exit(2)

    OUTPUT_FINAL.write_text(format_final_output(review), encoding="utf-8")
    print(f"Done. Wrote {OUTPUT_FINAL.relative_to(PROJECT_DIR)}.", flush=True)

    # Clean up clarifications.md after a fully successful run.
    if CLARIFICATIONS_PATH.exists():
        try:
            CLARIFICATIONS_PATH.unlink()
        except OSError:
            pass

def main_cli():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main_cli()
