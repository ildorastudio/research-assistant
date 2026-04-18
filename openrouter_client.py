"""Thin async wrapper around OpenRouter's OpenAI-compatible chat completions endpoint.

The whole module exposes one async function (`call_model`) and one exception
(`ModelCallFailed`). Callers pass in a shared `httpx.AsyncClient` so connection
pooling works across the parallel researcher fan-out.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# HTTP status codes we will retry. 4xx other than 429 are not retried — they
# almost always indicate auth or request-shape problems that won't fix themselves.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class ModelCallFailed(Exception):
    """Raised after all retries for a single model call have been exhausted."""

    def __init__(self, model: str, last_error: str) -> None:
        super().__init__(f"{model}: {last_error}")
        self.model = model
        self.last_error = last_error


async def call_model(
    client: httpx.AsyncClient,
    model: str,
    system: str,
    user: str,
    *,
    response_format: Optional[dict] = None,
    timeout: float = 180.0,
    max_retries: int = 2,
) -> str:
    """Call a single model on OpenRouter and return the assistant message text.

    `max_retries` is the number of retries AFTER the first attempt, so
    `max_retries=2` means up to 3 total HTTP requests.

    Backoff between retries is exponential: 1s, 2s, 4s, ...

    Raises `ModelCallFailed` if every attempt fails. Auth-style 4xx errors
    fail immediately without retry.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ModelCallFailed(model, "OPENROUTER_API_KEY is not set")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenRouter likes these headers for attribution. Harmless if absent.
        "HTTP-Referer": "https://github.com/local/multi-llm-research-assistant",
        "X-Title": "Multi-LLM Research Assistant",
    }

    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if response_format is not None:
        payload["response_format"] = response_format

    last_error: str = "no attempt was made"
    total_attempts = max_retries + 1

    for attempt in range(total_attempts):
        try:
            response = await client.post(
                OPENROUTER_URL,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"network error: {exc!r}"
        else:
            if response.status_code == 200:
                try:
                    data = response.json()
                    content = data["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise ModelCallFailed(
                        model,
                        f"unexpected response shape: {exc!r} body={response.text[:500]!r}",
                    )
                if not isinstance(content, str) or not content.strip():
                    raise ModelCallFailed(model, "empty content in response")
                return content

            # Auth and request-shape errors are not retryable.
            if response.status_code not in _RETRYABLE_STATUSES:
                raise ModelCallFailed(
                    model,
                    f"HTTP {response.status_code}: {response.text[:500]}",
                )

            last_error = f"HTTP {response.status_code}: {response.text[:200]}"

        # If we get here, this attempt failed in a retryable way.
        if attempt < total_attempts - 1:
            await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s, ...

    raise ModelCallFailed(model, f"after {total_attempts} attempts: {last_error}")
