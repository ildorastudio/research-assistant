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
    INTERMEDIATE_DIR,
)
from research_assistant.improver import run_improver, write_clarifications_md
from research_assistant.openrouter_client import ModelCallFailed

async def main():
    config = load_config()
    INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
    
    original_prompt = read_text(INPUT_PATH, "data/input/input.txt").strip()
    if not original_prompt:
        print("error: data/input/input.txt is empty", file=sys.stderr, flush=True)
        sys.exit(1)

    improver_system = read_text(PROMPTS_DIR / "improver.txt", "prompts/improver.txt")

    async with httpx.AsyncClient() as client:
        print(f"Calling improver ({config.improver_model}) for clarification check...", flush=True)
        try:
            result = await run_improver(
                client,
                config.improver_model,
                system_prompt=improver_system,
                raw_prompt=original_prompt,
                api_key=config.api_key,
                timeout=config.timeout,
                max_retries=config.max_retries,
            )
        except ModelCallFailed as exc:
            print(f"error: improver failed: {exc}", file=sys.stderr, flush=True)
            sys.exit(2)

        if result.needs_clarification:
            write_clarifications_md(CLARIFICATIONS_PATH, result.questions)
            print(f"Clarification questions written to {CLARIFICATIONS_PATH.relative_to(PROJECT_DIR)}.", flush=True)
            print("Please fill in your answers and then run step2_finalize.py.", flush=True)
        else:
            IMPROVED_PROMPT_PATH.write_text(result.improved_prompt, encoding="utf-8")
            print(f"No clarification needed. Improved prompt written to {IMPROVED_PROMPT_PATH.relative_to(PROJECT_DIR)}.", flush=True)
            print("Please review it and then run step3_research.py.", flush=True)

def main_cli():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr, flush=True)
        sys.exit(1)

if __name__ == "__main__":
    main_cli()
