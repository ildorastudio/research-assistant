# Multi-LLM Research Assistant — Build Plan

Bottom-up build order. Each phase produces something independently testable before the next phase depends on it.

## Build order and rationale

The pipeline runs Improver → Researchers → Reviewer → Output, but the right *build* order is not the same. Build the shared HTTP client first, then each stage with its own test harness, then the orchestrator last. Every stage should be provably working before the glue code exists.

---

## Phase 0 — Scaffolding (no Python yet)

1. Create the folder layout exactly as specified in §7 of the requirements.
2. Write `requirements.txt`:
   - `httpx>=0.27`
   - `python-dotenv>=1.0`
3. Write `.env.example` with known-good OpenRouter slugs so the tool runs out of the box:
   - `IMPROVER_MODEL=anthropic/claude-sonnet-4.6`
   - `RESEARCHER_MODELS=anthropic/claude-opus-4.6,anthropic/claude-sonnet-4.6,openai/gpt-4o,google/gemini-pro-1.5,deepseek/deepseek-chat`
   - `REVIEWER_MODEL=anthropic/claude-opus-4.6`
   - `REQUEST_TIMEOUT_SECONDS=180`
   - `MAX_RETRIES=2`
   - `MIN_SUCCESSFUL_RESEARCHERS=2`
4. Stub the three prompt files in `prompts/` with one-line placeholders; real prompts land in Phase 6.
5. Create `input.txt` with a throwaway question for testing.

**Test point:** `pip install -r requirements.txt` succeeds in a clean venv.

---

## Phase 1 — `openrouter_client.py`

The single foundation every stage depends on. Keep it small.

- Public surface: one async function
  `call_model(client, model, system, user, *, response_format=None, timeout, max_retries) -> str` (returns the assistant message content).
- Use a shared `httpx.AsyncClient` passed in from the caller — connection pooling matters when 5+ researchers fire concurrently.
- Endpoint: `POST https://openrouter.ai/api/v1/chat/completions`, OpenAI-compatible body `{model, messages:[{role:"system",…},{role:"user",…}], response_format}`.
- Retries: exponential backoff 1s / 2s / 4s, capped at `max_retries`. Retry on network errors, 429, and 5xx. Do NOT retry on 4xx auth errors — fail fast.
- Raise a typed `ModelCallFailed(model, last_error)` after exhaustion so callers decide whether to drop or abort.

**Test point:** a tiny `scratch_client.py` that fires one call against a cheap model (`openai/gpt-4o-mini`) and prints the response. If this works, every downstream stage will.

---

## Phase 2 — `improver.py`

Handles the conditional two-pass flow. Pure logic, no orchestration decisions.

Build in this order:

1. `parse_clarifications_md(path) -> list[dict]` — reads the file into `[{question, answer}, …]`. Format: `## Question N` header, question text, then `**Answer:**` followed by user-typed text.
2. `write_clarifications_md(path, questions)` — writes the template with empty answer slots.
3. `run_improver(client, model, raw_prompt, prior_clarifications=None) -> dict` — calls the model, forces JSON via `response_format={"type":"json_object"}`, validates the three keys (`needs_clarification`, `questions`, `improved_prompt`). If JSON parsing fails, retry once with a schema reminder; if still malformed, raise.

**Test point:** two scratch runs — a vague prompt ("tell me about AI") should return `needs_clarification=true`; a well-scoped prompt should return a rewritten `improved_prompt`.

---

## Phase 3 — `researcher.py`

Parallel fan-out. Only genuinely concurrent stage.

- Public function:
  `async def run_researchers(client, model_slugs, improved_prompt, system_prompt, config) -> list[ResearcherResult]`
  where each result is `{model, success: bool, content: str | None, error: str | None}`.
- Use `asyncio.gather(..., return_exceptions=True)` so one failing model can't sink the batch.
- Each call goes through `openrouter_client.call_model` which already handles retries. The researcher layer's only job on failure is to convert `ModelCallFailed` into a recorded drop.
- Do NOT enforce `MIN_SUCCESSFUL_RESEARCHERS` here. That's an orchestration-level decision.

**Test point:** run against two real models plus one deliberately bogus slug (`fake/does-not-exist`). Expect two successes and one recorded drop, with wall-clock time ≈ the slowest single call, not the sum.

---

## Phase 4 — `reviewer.py`

- Public function: `run_reviewer(client, model, improved_prompt, researcher_results, preference_ranking) -> dict`.
- The user message to the reviewer must include: the improved prompt, each successful researcher's output labeled with its slug, and the preference ranking as a tiebreaker hint (pass it in from `RESEARCHER_MODELS` order — don't hardcode in the prompt file).
- Force JSON output and validate the schema shape (`consensus`, `mixed_opinions`, `notes`). Same retry-once-on-malformed policy as the improver.

**Test point:** feed synthetic researcher outputs where two models agree on claim A and diverge on claim B. Verify the reviewer puts A in `consensus` and B in `mixed_opinions` with reasonable confidence scores.

---

## Phase 5 — `main.py` orchestration

Only built after every stage above is independently verified.

1. Load `.env`, validate required keys, fail fast if any missing.
2. Read `input.txt`.
3. Branch on `clarifications.md` existence:
   - Missing → call improver with prompt only. If `needs_clarification=true`, write `clarifications.md`, print instructions, exit 0. If false, continue with `improved_prompt`.
   - Present → parse it, call improver with prompt + Q&A pairs, expect `needs_clarification=false` and continue. Edge case: if it still asks questions, overwrite `clarifications.md` and exit.
4. Run researchers. If fewer than `MIN_SUCCESSFUL_RESEARCHERS` succeed, skip the reviewer and write an explanatory `output.md` listing the failures.
5. Run reviewer.
6. Assemble `output.md` (see Phase 6).
7. **Only on full success**, delete `clarifications.md`. If any later stage errors after a successful clarifications parse, leave the file alone so the user can rerun.

Open a single `httpx.AsyncClient` at the top of `main()`, pass it into every stage, close it in a `finally`.

---

## Phase 6 — Output assembly and final prompts

The markdown builder is ~50 lines of string concatenation; can live in `main.py` or a small `output.py`. Five sections in the order the requirements specify.

Write the real system prompts in `prompts/` now that you know exactly what inputs each role receives:

- **Improver:** JSON-only response, strict schema, err on the side of `needs_clarification=true` only when genuinely ambiguous (not just terse).
- **Researcher:** answer thoroughly, show reasoning, state uncertainty inline, don't refuse or over-hedge.
- **Reviewer:** JSON-only, scoring rubric spelled out (support count, reasoning consistency, verifiability), preference ranking is a tiebreaker only.

---

## Phase 7 — End-to-end tests (manual, no test framework)

Four runs against the real stack:

1. Clear prompt, 2 researchers → full `output.md` produced.
2. Vague prompt → `clarifications.md` written; fill it; rerun → full `output.md`.
3. Deliberately bogus slug mixed into `RESEARCHER_MODELS` → that model listed as dropped; run still completes.
4. Set `MIN_SUCCESSFUL_RESEARCHERS=10` with only 5 researchers → pipeline aborts with an explanatory `output.md`.

---

## Gotchas to resolve before coding

1. **`MAX_RETRIES` semantics.** Is `2` "two total attempts" or "two retries after the first"? The word "retries" implies the latter (3 total calls). Pick one and document it in `.env.example`.
2. **`clarifications.md` format.** The spec says the user fills in answers "inline" but doesn't define the format. Pin it down now (suggest: `## Question N` + `**Answer:**` blocks) so the parser stays trivial.
3. **JSON mode reliability.** Not every OpenRouter provider honors `response_format={"type":"json_object"}`. Belt-and-suspenders: instruct JSON in the system prompt *and* defensively extract the first balanced `{…}` block from the response before parsing.
4. **Preference ranking source of truth.** Reviewer uses it as a tiebreaker — is it the literal order of `RESEARCHER_MODELS`, or a separate var? Reusing `RESEARCHER_MODELS` order is simpler and matches how depth is tuned.
5. **Deletion of `clarifications.md`.** Only after `output.md` is fully written. A crash mid-review should preserve it so the user can retry without re-typing answers.
6. **Concurrency ceiling.** `asyncio.gather` with 5–7 coroutines is fine, but at 20+ you want a `Semaphore`. Leave a TODO.
7. **Exit codes.** Not specified. Suggest: `0` on success (including "needs clarification, exiting cleanly"), `1` on configuration/input errors, `2` on pipeline-abort (too few researchers, malformed JSON).
