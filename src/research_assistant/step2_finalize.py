import asyncio
import sys
import httpx
from research_assistant.common import (
    load_config,
    read_text,
    INPUT_PATH,
    PROMPTS_DIR,
    CLARIFICATIONS_PATH,
    IMPROVED_PROMPT_PATH,
    PROJECT_DIR,
)
from research_assistant.improver import run_improver, parse_clarifications_md
from research_assistant.openrouter_client import ModelCallFailed

async def main():
    config = load_config()
    
    if not CLARIFICATIONS_PATH.exists():
        print(f"error: {CLARIFICATIONS_PATH} not found. Run step1_clarify.py first.", file=sys.stderr)
        sys.exit(1)

    original_prompt = read_text(INPUT_PATH, "data/input/input.txt").strip()
    prior = parse_clarifications_md(CLARIFICATIONS_PATH)
    improver_system = read_text(PROMPTS_DIR / "improver.txt", "prompts/improver.txt")

    async with httpx.AsyncClient() as client:
        print(f"Calling improver ({config.improver_model}) to finalize prompt...", flush=True)
        try:
            result = await run_improver(
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
            print(f"error: improver failed: {exc}", file=sys.stderr, flush=True)
            sys.exit(2)

        if result.needs_clarification:
            print("The improver still needs more clarification. Please update clarifications.md and run this script again.", flush=True)
            # We don't overwrite the existing file here to avoid losing user answers if they made a mistake, 
            # though in a more robust app we might append new questions.
        else:
            IMPROVED_PROMPT_PATH.write_text(result.improved_prompt, encoding="utf-8")
            print(f"Improved prompt written to {IMPROVED_PROMPT_PATH.relative_to(PROJECT_DIR)}.", flush=True)
            print("Please review it and then run step3_research.py.", flush=True)

def main_cli():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr, flush=True)
        sys.exit(1)

if __name__ == "__main__":
    main_cli()
