"""Stage 2: parallel fan-out to N researcher models.

Each model is called in its own OS thread via ``ThreadPoolExecutor``, giving
true parallel execution (Python releases the GIL during I/O waits).  Results
are gathered back through the asyncio event loop so the rest of the pipeline
can remain async.

A failure of any single model produces a ``ResearcherResult`` with
``success=False`` rather than raising — the orchestration layer decides whether
enough researchers succeeded.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from research_assistant.openrouter_client import ModelCallFailed, call_model_sync


@dataclass
class ResearcherResult:
    model: str
    success: bool
    content: Optional[str]
    error: Optional[str]


def _run_one_sync(
    model: str,
    system_prompt: str,
    improved_prompt: str,
    api_key: str,
    timeout: float,
    max_retries: int,
    output_file: Path,
    file_lock: threading.Lock,
) -> ResearcherResult:
    """Run a single researcher synchronously (called from a worker thread).

    On success the researcher's output is immediately appended to *output_file*
    under *file_lock* so partial results are visible on disk as each model
    finishes rather than only after all models complete.
    """
    try:
        content = call_model_sync(
            model,
            system=system_prompt,
            user=improved_prompt,
            api_key=api_key,
            response_format=None,
            timeout=timeout,
            max_retries=max_retries,
        )
        result = ResearcherResult(model=model, success=True, content=content, error=None)
    except ModelCallFailed as exc:
        result = ResearcherResult(model=model, success=False, content=None, error=exc.last_error)
    except Exception as exc:  # pragma: no cover — defensive catch-all
        result = ResearcherResult(model=model, success=False, content=None, error=f"unexpected: {exc!r}")

    if result.success:
        section = f"### {result.model}\n\n{(result.content or '').strip()}\n\n"
        with file_lock:
            existing = output_file.read_text(encoding="utf-8")
            output_file.write_text(existing + section, encoding="utf-8")

    return result


async def run_researchers(
    model_slugs: list[str],
    system_prompt: str,
    improved_prompt: str,
    *,
    api_key: str,
    timeout: float = 180.0,
    max_retries: int = 2,
    output_file: Path,
    file_lock: threading.Lock,
) -> list[ResearcherResult]:
    """Fire every researcher concurrently in separate threads and return results
    in the same order as *model_slugs*.

    Each thread writes its result to *output_file* under *file_lock* as soon as
    it finishes, so the file grows incrementally rather than being written in a
    single batch at the end.

    Never raises — per-model failures are captured in the ResearcherResult objects.
    """
    if not model_slugs:
        return []

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=len(model_slugs)) as pool:
        futures = [
            loop.run_in_executor(
                pool,
                _run_one_sync,
                slug, system_prompt, improved_prompt, api_key, timeout, max_retries,
                output_file, file_lock,
            )
            for slug in model_slugs
        ]
        results = await asyncio.gather(*futures)

    return list(results)
