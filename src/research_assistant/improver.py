"""Stage 1: clarification check and prompt rewrite.

Two-pass design:
  - First pass: improver sees only the raw prompt. It either rewrites it into a
    research brief (needs_clarification=False) or asks questions
    (needs_clarification=True) which get written to clarifications.md.
  - Second pass (only if clarifications.md exists): improver sees the raw prompt
    plus the parsed Q&A pairs and is expected to return a final brief.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from research_assistant.openrouter_client import ModelCallFailed, call_model


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ClarificationItem:
    question: str
    answer: str


@dataclass
class ImproverResult:
    needs_clarification: bool
    questions: list[str]
    improved_prompt: str


# ---------------------------------------------------------------------------
# clarifications.md format
# ---------------------------------------------------------------------------
#
# ## Question 1
# <question text, may span multiple lines>
#
# **Answer:**
# <user-typed answer, may span multiple lines>
#
# ## Question 2
# ...
#
# Header line is the only structural anchor. Anything before "## Question 1"
# is treated as preamble and ignored.

_HEADER_PREFIX = "## Question "
_ANSWER_MARKER = "**Answer:**"


def write_clarifications_md(path: Path, questions: list[str]) -> None:
    """Write a fresh clarifications.md with empty answer slots."""
    lines: list[str] = [
        "# Clarifications",
        "",
        "The improver needs more information before it can produce a research brief.",
        "Please type your answer under each question, then rerun the tool.",
        "Leave the `## Question N` headers and `**Answer:**` markers exactly as written.",
        "",
    ]
    for i, question in enumerate(questions, start=1):
        lines.append(f"{_HEADER_PREFIX}{i}")
        lines.append(question.strip())
        lines.append("")
        lines.append(_ANSWER_MARKER)
        lines.append("")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_clarifications_md(path: Path) -> list[ClarificationItem]:
    """Parse a filled-in clarifications.md back into Q&A pairs."""
    text = path.read_text(encoding="utf-8")
    items: list[ClarificationItem] = []

    # Split on the question header. The first chunk is preamble.
    chunks = text.split(f"\n{_HEADER_PREFIX}")
    if not chunks or len(chunks) < 2:
        return items

    for chunk in chunks[1:]:
        # `chunk` looks like: "1\n<question>\n\n**Answer:**\n<answer>\n\n"
        # Drop the leading number-and-newline.
        newline_idx = chunk.find("\n")
        if newline_idx == -1:
            continue
        body = chunk[newline_idx + 1 :]

        if _ANSWER_MARKER not in body:
            # Malformed chunk — skip rather than raise; the improver will likely
            # ask again on the next pass and the user can fix it.
            continue
        question_part, answer_part = body.split(_ANSWER_MARKER, 1)
        items.append(
            ClarificationItem(
                question=question_part.strip(),
                answer=answer_part.strip(),
            )
        )
    return items


# ---------------------------------------------------------------------------
# Improver call
# ---------------------------------------------------------------------------

def _build_user_message(
    raw_prompt: str,
    prior_clarifications: Optional[list[ClarificationItem]],
) -> str:
    parts = ["The user's original prompt is below, between <prompt> tags.", "", "<prompt>", raw_prompt.strip(), "</prompt>"]
    if prior_clarifications:
        parts.append("")
        parts.append("You previously asked the following clarifying questions and the user answered them:")
        parts.append("")
        for i, item in enumerate(prior_clarifications, start=1):
            ans = item.answer.strip()
            parts.append(f"Q{i}: {item.question}")
            parts.append(f"A{i}: {ans if ans else '[User provided no answer]'}")
            parts.append("")
        parts.append(
            "Use the provided answers to produce a final improved_prompt. "
            "Prioritize finalizing the brief now that you have more context. "
            "Only set needs_clarification to true if a critical ambiguity still prevents "
            "any reasonable research from starting."
        )
    return "\n".join(parts)


def _extract_json_object(text: str) -> dict:
    """Pull the first balanced top-level JSON object out of a string and parse it.

    Models sometimes wrap JSON in ```json fences or add stray prose despite
    being told not to. This finds the outermost {...} and parses it.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("no '{' found in response")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = text[start : i + 1]
                return json.loads(blob)
    raise ValueError("no balanced JSON object found in response")


def _validate_improver_payload(data: dict) -> ImproverResult:
    if not isinstance(data, dict):
        raise ValueError("improver returned a non-object")

    needs = data.get("needs_clarification")
    questions = data.get("questions", [])
    improved = data.get("improved_prompt", "")

    if not isinstance(needs, bool):
        raise ValueError("needs_clarification must be a boolean")
    if not isinstance(questions, list) or not all(isinstance(q, str) for q in questions):
        raise ValueError("questions must be a list of strings")
    if not isinstance(improved, str):
        raise ValueError("improved_prompt must be a string")

    if needs and not questions:
        raise ValueError("needs_clarification=true but questions list is empty")
    if not needs and not improved.strip():
        raise ValueError("needs_clarification=false but improved_prompt is empty")

    return ImproverResult(
        needs_clarification=needs,
        questions=[q.strip() for q in questions],
        improved_prompt=improved.strip(),
    )


async def run_improver(
    client: httpx.AsyncClient,
    model: str,
    system_prompt: str,
    raw_prompt: str,
    *,
    api_key: str,
    prior_clarifications: Optional[list[ClarificationItem]] = None,
    timeout: float = 180.0,
    max_retries: int = 2,
) -> ImproverResult:
    """Run the improver. Retries once on malformed JSON, then gives up."""
    user_message = _build_user_message(raw_prompt, prior_clarifications)

    last_parse_error: Optional[str] = None
    for attempt in range(2):
        if attempt == 1 and last_parse_error is not None:
            user_message = (
                user_message
                + "\n\nYour previous response could not be parsed: "
                + last_parse_error
                + "\nReturn ONLY a valid JSON object matching the schema. "
                + "No code fences, no commentary."
            )

        try:
            raw = await call_model(
                client,
                model,
                system=system_prompt,
                user=user_message,
                api_key=api_key,
                response_format={"type": "json_object"},
                timeout=timeout,
                max_retries=max_retries,
            )
        except ModelCallFailed:
            raise

        try:
            data = _extract_json_object(raw)
            return _validate_improver_payload(data)
        except (ValueError, json.JSONDecodeError) as exc:
            last_parse_error = str(exc)

    raise ModelCallFailed(
        model,
        f"improver returned malformed JSON twice; last error: {last_parse_error}",
    )
