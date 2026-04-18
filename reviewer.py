"""Stage 3: synthesis and confidence scoring.

The reviewer sees the improved prompt, every successful researcher's output
labeled by slug, and the user's preference ranking. It returns a structured
JSON object that the orchestrator turns into output.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import httpx

from improver import _extract_json_object  # re-use the same tolerant parser
from openrouter_client import ModelCallFailed, call_model
from researcher import ResearcherResult


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ConsensusClaim:
    claim: str
    supporting_models: list[str]


@dataclass
class MixedView:
    claim: str
    supporting_models: list[str]
    confidence: int
    reasoning: str


@dataclass
class MixedOpinion:
    topic: str
    views: list[MixedView]


@dataclass
class ReviewerResult:
    consensus: list[ConsensusClaim]
    mixed_opinions: list[MixedOpinion]
    notes: str


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------

def _build_user_message(
    improved_prompt: str,
    successful: list[ResearcherResult],
    preference_ranking: list[str],
) -> str:
    parts: list[str] = []
    parts.append("RESEARCH BRIEF:")
    parts.append(improved_prompt.strip())
    parts.append("")
    parts.append(
        "USER PREFERENCE RANKING (trust order, tiebreaker only — NOT a dominating factor):"
    )
    if preference_ranking:
        for i, slug in enumerate(preference_ranking, start=1):
            parts.append(f"  {i}. {slug}")
    else:
        parts.append("  (none provided)")
    parts.append("")
    parts.append(f"RESEARCHER RESPONSES ({len(successful)} successful):")
    parts.append("")
    for result in successful:
        parts.append(f"=== Researcher: {result.model} ===")
        parts.append((result.content or "").strip())
        parts.append("")
    parts.append(
        "Now produce the synthesis as a single JSON object matching the required schema. "
        "Return JSON only, no prose, no code fences."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(data: dict, valid_slugs: set[str]) -> ReviewerResult:
    if not isinstance(data, dict):
        raise ValueError("reviewer returned a non-object")

    consensus_raw = data.get("consensus", [])
    mixed_raw = data.get("mixed_opinions", [])
    notes = data.get("notes", "")

    if not isinstance(consensus_raw, list):
        raise ValueError("consensus must be a list")
    if not isinstance(mixed_raw, list):
        raise ValueError("mixed_opinions must be a list")
    if not isinstance(notes, str):
        raise ValueError("notes must be a string")

    consensus: list[ConsensusClaim] = []
    for entry in consensus_raw:
        if not isinstance(entry, dict):
            raise ValueError("consensus entry is not an object")
        claim = entry.get("claim", "")
        supporting = entry.get("supporting_models", [])
        if not isinstance(claim, str) or not claim.strip():
            raise ValueError("consensus claim must be a non-empty string")
        if not isinstance(supporting, list) or not all(isinstance(m, str) for m in supporting):
            raise ValueError("consensus supporting_models must be a list of strings")
        consensus.append(
            ConsensusClaim(
                claim=claim.strip(),
                supporting_models=[m for m in supporting if m in valid_slugs] or list(supporting),
            )
        )

    mixed: list[MixedOpinion] = []
    for entry in mixed_raw:
        if not isinstance(entry, dict):
            raise ValueError("mixed_opinions entry is not an object")
        topic = entry.get("topic", "")
        views_raw = entry.get("views", [])
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("mixed_opinions topic must be a non-empty string")
        if not isinstance(views_raw, list):
            raise ValueError("mixed_opinions views must be a list")

        views: list[MixedView] = []
        for v in views_raw:
            if not isinstance(v, dict):
                raise ValueError("view is not an object")
            v_claim = v.get("claim", "")
            v_supporting = v.get("supporting_models", [])
            v_conf = v.get("confidence", -1)
            v_reasoning = v.get("reasoning", "")
            if not isinstance(v_claim, str) or not v_claim.strip():
                raise ValueError("view claim must be a non-empty string")
            if not isinstance(v_supporting, list) or not all(isinstance(m, str) for m in v_supporting):
                raise ValueError("view supporting_models must be a list of strings")
            if not isinstance(v_conf, int) or not (0 <= v_conf <= 100):
                raise ValueError("view confidence must be an integer 0-100")
            if not isinstance(v_reasoning, str):
                raise ValueError("view reasoning must be a string")
            views.append(
                MixedView(
                    claim=v_claim.strip(),
                    supporting_models=[m for m in v_supporting if m in valid_slugs] or list(v_supporting),
                    confidence=v_conf,
                    reasoning=v_reasoning.strip(),
                )
            )
        mixed.append(MixedOpinion(topic=topic.strip(), views=views))

    return ReviewerResult(consensus=consensus, mixed_opinions=mixed, notes=notes.strip())


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_reviewer(
    client: httpx.AsyncClient,
    model: str,
    system_prompt: str,
    improved_prompt: str,
    researcher_results: list[ResearcherResult],
    preference_ranking: list[str],
    *,
    timeout: float = 180.0,
    max_retries: int = 2,
) -> ReviewerResult:
    """Run the reviewer over successful researcher responses. Retries once on malformed JSON."""
    successful = [r for r in researcher_results if r.success and r.content]
    if not successful:
        raise ValueError("run_reviewer called with no successful researchers")

    valid_slugs = {r.model for r in successful}
    user_message = _build_user_message(improved_prompt, successful, preference_ranking)

    last_parse_error: Optional[str] = None
    for attempt in range(2):
        if attempt == 1 and last_parse_error is not None:
            user_message = (
                user_message
                + "\n\nYour previous response could not be parsed: "
                + last_parse_error
                + "\nReturn ONLY a valid JSON object matching the required schema. "
                + "No code fences, no commentary."
            )

        raw = await call_model(
            client,
            model,
            system=system_prompt,
            user=user_message,
            response_format={"type": "json_object"},
            timeout=timeout,
            max_retries=max_retries,
        )

        try:
            data = _extract_json_object(raw)
            return _validate(data, valid_slugs)
        except (ValueError, json.JSONDecodeError) as exc:
            last_parse_error = str(exc)

    raise ModelCallFailed(
        model,
        f"reviewer returned malformed JSON twice; last error: {last_parse_error}",
    )
