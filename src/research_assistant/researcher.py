"""Stage 2: parallel fan-out to N researcher models.

Each model is called concurrently through `openrouter_client.call_model`, which
already handles retries and backoff. A failure of any single model produces a
`ResearcherResult` with success=False rather than raising — the orchestration
layer decides whether enough researchers succeeded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx

from research_assistant.openrouter_client import ModelCallFailed, call_model


@dataclass
class ResearcherResult:
    model: str
    success: bool
    content: Optional[str]
    error: Optional[str]


async def _run_one(
    client: httpx.AsyncClient,
    model: str,
    system_prompt: str,
    improved_prompt: str,
    api_key: str,
    timeout: float,
    max_retries: int,
) -> ResearcherResult:
    try:
        content = await call_model(
            client,
            model,
            system=system_prompt,
            user=improved_prompt,
            api_key=api_key,
            response_format=None,
            timeout=timeout,
            max_retries=max_retries,
        )
        return ResearcherResult(model=model, success=True, content=content, error=None)
    except ModelCallFailed as exc:
        return ResearcherResult(model=model, success=False, content=None, error=exc.last_error)
    except Exception as exc:  # pragma: no cover — defensive catch-all
        # Any unexpected error is recorded as a failure for this model only;
        # we never let one bad model take down the whole fan-out.
        return ResearcherResult(model=model, success=False, content=None, error=f"unexpected: {exc!r}")


async def run_researchers(
    client: httpx.AsyncClient,
    model_slugs: list[str],
    system_prompt: str,
    improved_prompt: str,
    *,
    api_key: str,
    timeout: float = 180.0,
    max_retries: int = 2,
) -> list[ResearcherResult]:
    """Fire every researcher concurrently and return results in the same order as `model_slugs`.

    Never raises — per-model failures are captured in the ResearcherResult objects.
    """
    if not model_slugs:
        return []

    # TODO: if the researcher count ever grows past ~15, wrap this in an
    # asyncio.Semaphore to cap concurrency. 3-7 is well within safe territory
    # for a shared httpx.AsyncClient.
    coros = [
        _run_one(client, slug, system_prompt, improved_prompt, api_key, timeout, max_retries)
        for slug in model_slugs
    ]
    return await asyncio.gather(*coros)
