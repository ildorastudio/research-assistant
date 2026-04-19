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
import time
import threading
from datetime import datetime
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
    duration_seconds: float = 0.0
    start_timestamp: Optional[str] = None
    end_timestamp: Optional[str] = None


def _run_one_sync(
    model: str,
    system_prompt: str,
    improved_prompt: str,
    api_key: str,
    timeout: float,
    max_retries: int,
    output_file: Path,
    intermediate_dir: Path,
    file_lock: threading.Lock,
) -> ResearcherResult:
    """Run a single researcher synchronously (called from a worker thread).

    On success the researcher's output is immediately appended to *output_file*
    under *file_lock* so partial results are visible on disk as each model
    finishes rather than only after all models complete.
    """
    start_time = time.perf_counter()
    start_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        duration = time.perf_counter() - start_time
        end_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result = ResearcherResult(
            model=model,
            success=True,
            content=content,
            error=None,
            duration_seconds=duration,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
    except ModelCallFailed as exc:
        duration = time.perf_counter() - start_time
        end_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result = ResearcherResult(
            model=model,
            success=False,
            content=None,
            error=exc.last_error,
            duration_seconds=duration,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
    except Exception as exc:  # pragma: no cover — defensive catch-all
        duration = time.perf_counter() - start_time
        end_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result = ResearcherResult(
            model=model,
            success=False,
            content=None,
            error=f"unexpected: {exc!r}",
            duration_seconds=duration,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )

    if result.success:
        section = (
            f"### {result.model}\n\n"
            f"*Start time: {result.start_timestamp}*\n"
            f"*End time:   {result.end_timestamp}*\n"
            f"*Time taken: {result.duration_seconds:.2f}s*\n\n"
            f"{(result.content or '').strip()}\n\n"
        )
        with file_lock:
            existing = output_file.read_text(encoding="utf-8")
            output_file.write_text(existing + section, encoding="utf-8")

        # Save individual researcher result immediately for parallel visibility
        sanitized_name = result.model.replace("/", "_").replace(":", "_")
        researcher_file = intermediate_dir / f"researcher_{sanitized_name}.md"
        content_with_time = (
            f"Start time: {result.start_timestamp}\n"
            f"End time:   {result.end_timestamp}\n"
            f"Time taken: {result.duration_seconds:.2f}s\n\n"
            f"{result.content or ''}"
        )
        researcher_file.write_text(content_with_time, encoding="utf-8")

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
    intermediate_dir: Path,
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
                output_file, intermediate_dir, file_lock,
            )
            for slug in model_slugs
        ]
        results = await asyncio.gather(*futures)

    return list(results)
