# PLAN.md — RAVE (Research And Verify Engine)

Build specification. Execute this plan top to bottom. All code, prompts, and docs
must be written from scratch for this repository. Do not copy code from, or refer
by name to, any existing project, product, or person anywhere in the code,
comments, commits, or documentation.

---

## 0. Mission

RAVE is an original, self-hostable deep-research engine: embedded agents that make
schema-enforced (forced) calls to embedded tools. Given a question, it plans,
searches the live web in parallel, cross-checks facts deterministically, runs an
adversarial verification loop, and outputs a fully cited report with a source
ledger and verification log. It runs locally (Ollama) or against any
OpenAI-compatible / Anthropic API endpoint, and ships as a public GitHub repo.

## 1. Hard principles (never violate)

1. **Grounding over recall.** Factual answers come only from live retrieval during
   the run. No working web access → print a HALT message and exit; never answer
   from model memory.
2. **Forced transitions.** Every phase transition is a forced tool call validated
   against a JSON schema. Agents can never skip a phase or answer directly.
3. **No source, no claim.** Every claim carries a citation or is deleted. Never
   invent a URL, date, quote, or statistic. `[no source found]` and `[unverified]`
   are legal outputs; fabrication is not.
4. **Conflicts are surfaced, never smoothed.** Disagreements between sources are
   preserved and reported in their own section.
5. **Deterministic where possible.** Cross-checking, confidence upgrades, deduping,
   and budget enforcement are plain code, not LLM judgment.
6. **Simplicity.** Plain Python. No agent frameworks. Small, readable modules.

## 2. Architecture

- **Orchestrator** — pure-Python state machine that owns phases P0→P5, budgets,
  and the run log. Not an LLM.
- **Four LLM agent roles** — Planner, Researcher (N parallel instances),
  Critic, Writer. Each role = a prompt + a restricted toolset.
- **Tool registry** — declares every LLM-callable tool with a JSON schema and a
  Python executor; dispatches calls; records everything to the run log.
- **Embedded pipeline tools (pure code)** — fetcher/extractor, chunker, ranker,
  source vetter, cross-checker. Written in-house; no external SaaS required.
- **UI helper** — renders any agent question as numbered options plus a free-text
  escape hatch (works in CLI today; schema is chip/button-ready for a future web UI).

## 3. Runtime flow — exact call order

**P0 — Intake**
1. (code) Startup connectivity check. Offline → HALT message, exit.
2. (Planner, call 1) Classify the query and rewrite it as a standalone question
   using conversation/context if provided.
3. (Planner, forced `ask_user`, only if a critical detail is ambiguous) Ask exactly
   ONE clarifying question: options[] + free-text allowed. Otherwise skip.

**P1 — Decompose**
4. (Planner, forced `create_research_plan`) Return 3–7 mutually independent
   sub-questions, each with its own done-criteria. May not answer directly.
5. (code, only with `--confirm`) Render the plan via `ask_user` with
   [Proceed] / [Edit]. Edit → collect feedback, regenerate plan, loop until Proceed.

**P2 — Swarm research** (asyncio; one Researcher per sub-question, run in parallel)
6. Each Researcher loops, up to the mode's iteration cap:
   a. forced `plan_preamble` — one short paragraph: goal, next action, expected info.
      MUST be the first tool call of every turn.
   b. `web_search` and/or `fetch_page` calls (the embedded pipeline runs inside them).
   c. Emit findings strictly in the Finding schema (see §5).
   d. forced `done` to exit (auto-fires at the iteration cap). Direct final answers
      are rejected.

**P3 — Cross-check (pure code, zero LLM)**
7. `crosscheck.py`: claims confirmed by ≥2 independent sub-passes get confidence
   upgraded one level; contradictions flagged; thin sub-passes flagged; findings
   deduped by URL; a fact→source map is built and kept for the rest of the run.

**P4 — Critic → Verify loop**
8. (Critic, forced `critique_findings`) Audit against the checklist: unsourced,
   single-sourced, stale, conflicting, weak-source, coverage-gap. Returns issues[].
9. (code + Researcher) For each issue: targeted re-search with a different query
   angle (≤2 tool calls per issue) and re-fetch of cited URLs to confirm the page
   still supports the claim. Then return to step 8.
10. Stop when the Critic returns zero issues OR the mode's cycle cap is reached.
    Anything still open is marked `[unverified]` — reported, never buried.

**P5 — Report**
11. (Writer, forced `write_report`) Produce the final report (schema in §4).
    (code) Render to markdown → stdout + `report.md`, plus a JSONL run log of every
    tool call for the eval harness.

## 4. LLM-callable tool contracts (JSON schemas in `llm/schemas.py`)

- `ask_user {question, options[], allow_free_text: true}` → user's choice or text.
- `create_research_plan {standalone_query, sub_questions[3..7]: {id, question,
  done_criteria}}`
- `plan_preamble {current_goal, next_action, expected_info}`
- `web_search {query, top_k}` → vetted, ranked chunks with metadata.
- `fetch_page {url}` → cleaned text + title + best-effort publication date.
- `done {coverage_summary, gaps[]}`
- `critique_findings {issues[]: {id, type: unsourced|single_source|stale|conflict|
  weak_source|coverage_gap, claim_ref, instruction}}`
- `write_report {title, exec_summary[<=5], findings[]: {claim, citations[],
  confidence, date}, comparison_table?, disagreements[]: {claim, position_a,
  position_b, likely_reason}, gaps[], recommendations[], source_ledger[],
  verification_log: {cycles_run, changes[], unverified[]}}`

Forcing mechanism: the LLM client sets `tool_choice` to the required tool
(Anthropic `{type:"tool", name:...}` / OpenAI `"required"` + single tool) and
validates the returned arguments with pydantic. Invalid → retry with the
validation error appended, max 2 retries, then fail the run loudly.

## 5. Finding schema (used everywhere, no exceptions)

```
{claim, url, date (publication or retrieval), source_type: primary | official |
 peer_reviewed | news | analyst | blog | forum | ai_generated,
 confidence: H | M | L, quote (verbatim, <=25 words)}
```

Staleness rule: fast-moving topics (prices, policy, models, product specs,
people-in-roles) → sources older than 6 months are flagged `[stale]` and
down-weighted; slow topics (history, fundamentals) exempt.

## 6. Embedded retrieval pipeline (pure code, runs inside `web_search`/`fetch_page`)

fetch (httpx, polite headers, timeouts, robots.txt respected)
→ extract (own readability heuristic: text-density scoring, boilerplate stripping)
→ chunk (sentence/paragraph splitter, ~300–500 tokens, small overlap)
→ rank (own BM25 implementation + cosine similarity over embeddings; hybrid score.
  Embeddings via a local model if configured, else a hashing-vector fallback so the
  pipeline never requires an external API)
→ vet (`source_vetter.py`: rank primary/official/peer-reviewed up; downrank or drop
  SEO farms, undated pages, anonymous blogs; apply staleness rule)
→ return top chunks with full metadata.

**Search backends** (pluggable adapters behind one interface, chosen in
`config.yaml`):
(a) self-hosted metasearch instance URL (keyless, private),
(b) own mini-crawler + local index for a user-supplied domain list,
(c) optional commercial search API adapter (key in env var).
Default: (a) if URL configured, else (c) if key present, else exit with a setup hint.

## 7. Modes and budgets (enforced by the orchestrator, not the LLM)

| mode     | researcher iters / sub-q | critic verify cycles | global tool-call ceiling |
|----------|--------------------------|----------------------|--------------------------|
| speed    | 2                        | 0 (critic runs once, report-only) | 25       |
| balanced | 4                        | 1                    | 60                       |
| quality  | 8                        | 3                    | 150                      |

Re-search budget: ≤2 tool calls per issue. Ceilings are hard stops.

## 8. LLM layer (`llm/client.py`)

- OpenAI-compatible chat-completions client (works with Ollama's `/v1`) plus an
  optional Anthropic adapter. Endpoint, keys, and model names in `config.yaml`.
- Per-role model routing: cheap/fast model for Planner + Researcher, strongest
  model for Critic + Writer (all configurable; single-model setups fine).
- `forced_tool(role, tool_name, messages)` helper implements §4 forcing + retries.

## 9. Repository layout

```
rave/
├── agents/        orchestrator.py  planner.py  researcher.py  critic.py  writer.py
├── tools/         registry.py  web_search.py  fetch_page.py  extractor.py
│                  chunker.py  ranker.py  source_vetter.py  crosscheck.py
├── llm/           client.py  schemas.py
├── prompts/       planner.md  researcher.md  critic.md  writer.md   (original text)
├── ui/            prompt.py        # options + free-text renderer (chip-ready schema)
├── evals/         harness.py  questions.yaml  results.tsv (header row only)
├── tests/         unit tests (extractor fixtures, ranker sanity, schema forcing,
│                  crosscheck rules, HALT behavior)
├── mcp_server.py  # stub exposing deep_research(query, mode) for a future
│                  # remote-connector deployment; not wired into main flow yet
├── main.py        # CLI entry
├── config.yaml    requirements.txt   README.md   LICENSE (MIT)
```

## 10. CLI

`rave "your question" --mode balanced [--confirm] [--out report.md]`
Prints phase-by-phase progress, saves `report.md` + `runlog.jsonl`.

## 11. Eval harness (`evals/`)

- `questions.yaml`: ~10 benchmark questions with expected key facts.
- `harness.py`: runs the pipeline per question; scores citation coverage %,
  corroboration % (claims with ≥2 sources), and unverified count; appends one row
  to `results.tsv` (tab-separated): `commit  score  mode  status  description`
  with status `keep` or `discard`.
- Documented workflow in README: change prompt/code → run harness → keep the
  commit if scores improve, revert if not. `results.tsv` stays untracked by git.

## 12. Build order (execute as phases; commit after each)

- **A. Scaffold**: repo tree, config loading, LLM client with forced-tool
  validation, tests for forcing/retry. `git init`, first commit.
- **B. Pipeline tools**: fetcher, extractor, chunker, own BM25, ranker, vetter,
  crosscheck + unit tests on local HTML fixtures (no network in tests).
- **C. Agents happy path**: prompts (original), Planner → single Researcher →
  Writer, speed mode, end-to-end on one sub-question.
- **D. Full engine**: swarm parallelism, cross-check integration, Critic→Verify
  loop, budgets and ceilings, HALT behavior.
- **E. UX**: `ask_user` renderer, `--confirm` Proceed/Edit gate, progress output,
  report rendering.
- **F. Ship**: eval harness, `mcp_server.py` stub, README (quickstart with Ollama,
  config reference, architecture diagram in ASCII), license, final polish, push
  to GitHub.

## 13. Acceptance criteria (definition of done)

1. Offline run prints the HALT message and exits nonzero.
2. `rave "<question>" --mode speed` completes online and writes `report.md`.
3. `runlog.jsonl` shows every phase transition as a forced tool call.
4. Report contains all §4 `write_report` sections; every claim has ≥1 citation;
   corroborated claims are marked; conflicts and `[unverified]` items listed.
5. Budgets respected (verifiable from the run log). Tests pass.
6. No external project or person names anywhere in the repo.

## 14. Constraints

Python 3.11+. Dependencies kept minimal: httpx, pydantic, pyyaml, rich (or
questionary) for the CLI, pytest for tests. BM25 implemented in-house. Optional
extras (local embedding model) stay behind config flags. License: MIT.
