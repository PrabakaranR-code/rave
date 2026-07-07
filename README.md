# RAVE — Research And Verify Engine

A self-hostable deep-research engine. Given a question, RAVE plans, searches
the live web in parallel, cross-checks facts deterministically, runs an
adversarial verification loop, and outputs a fully cited report with a source
ledger and verification log. It runs locally against Ollama or any
OpenAI-compatible / Anthropic-style API endpoint.

## Install in one line

First, open a terminal (the command window):

- **Windows**: press the Windows key, type `powershell`, press Enter.
- **Mac**: press Cmd+Space, type `terminal`, press Enter.
- **Ubuntu**: press Ctrl+Alt+T.
- **VPS / server**: you're already in a shell after `ssh`.

Paste with Ctrl+V (Windows/Ubuntu) or Cmd+V (Mac) — on some terminals it's
right-click — then press Enter.

**Mac / Ubuntu / VPS:**

```sh
curl -fsSL https://raw.githubusercontent.com/PrabakaranR-code/rave/main/install.sh | sh
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/PrabakaranR-code/rave/main/install.ps1 | iex
```

The installer checks your computer, fetches what's missing, and hands over to
the setup wizard. The wizard asks a few numbered questions — where RAVE's
thinking should happen (a local AI model, an online service with an API key
(your access key), or a provider bundle that also connects RAVE into that
company's AI apps), sets up web search (a private SearXNG search engine via
Docker, or the built-in crawler), runs a test question, and finishes. You
never type model names, addresses, or paths — you only pick numbers.

Afterwards, daily use is just:

```sh
rave            # ask a question, pick a depth, get a cited report
rave setup      # change settings any time
```

## Hard principles

1. **Grounding over recall.** Factual answers come only from live retrieval
   during the run. No working web access → RAVE prints a HALT message and
   exits nonzero. It never answers from model memory.
2. **Forced transitions.** Every phase transition is a forced tool call
   validated against a JSON schema. Agents can never skip a phase or answer
   directly.
3. **No source, no claim.** Every claim carries a citation or is deleted —
   in code, after validation. `[no source found]` and `[unverified]` are
   legal outputs; fabrication is not.
4. **Conflicts are surfaced, never smoothed.** Source disagreements get their
   own report section.
5. **Deterministic where possible.** Cross-checking, confidence upgrades,
   deduping, and budget enforcement are plain code, not LLM judgment.

## Architecture

```
                              ┌────────────────────────────┐
                              │  ORCHESTRATOR (pure code)   │
                              │  phases P0→P5, budgets,     │
                              │  run log — not an LLM       │
                              └─────────────┬──────────────┘
                                            │ forced, schema-validated
                                            │ tool calls only
        ┌───────────────┬───────────────────┼────────────────┬──────────────┐
        ▼               ▼                   ▼                ▼              ▼
   ┌─────────┐    ┌────────────┐      ┌──────────┐     ┌─────────┐    ┌──────────┐
   │ PLANNER │    │ RESEARCHER │ ×N   │ CRITIC   │     │ WRITER  │    │ ask_user │
   │ classify│    │ (parallel) │      │ audit    │     │ report  │    │ options+ │
   │ + plan  │    │ swarm      │      │ checklist│     │ compiler│    │ free text│
   └─────────┘    └─────┬──────┘      └────┬─────┘     └─────────┘    └──────────┘
                        │ web_search /      │ issues[]
                        │ fetch_page        ▼
                        ▼             ┌───────────────────────────┐
   ┌────────────────────────────┐     │ VERIFY (pure code)        │
   │ EMBEDDED PIPELINE (code)   │     │ ≤2 tool calls per issue:  │
   │ fetch → extract → chunk →  │     │ re-search new angle +     │
   │ rank (BM25+cosine) → vet   │     │ re-fetch cited URL        │
   └────────────────────────────┘     └────────────┬──────────────┘
                                                   ▼
                                      ┌───────────────────────────┐
                                      │ CROSS-CHECK (pure code)   │
                                      │ dedupe, ≥2-pass upgrade,  │
                                      │ contradictions, fact→src  │
                                      └───────────────────────────┘
```

Phases: **P0** intake (connectivity check, classify, ≤1 clarifying question)
→ **P1** decompose (3–7 independent sub-questions) → **P2** swarm research
(one researcher per sub-question, in parallel) → **P3** deterministic
cross-check → **P4** critic → verify loop → **P5** report.

## Quickstart (local, with Ollama)

```bash
git clone <this-repo> && cd rave
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 1. an LLM — local mode is keyless and defaults to Ollama's localhost:11434/v1:
#    ollama serve
#    ollama pull llama3.1:8b   # or qwen, deepseek-r1, ... — any tool-calling model
#    then set config.yaml → llm.model to the model you pulled
#    (hosted APIs work too: llm.mode: api + base_url + key; see docs/LIVE_RUN.md)

# 2. a search backend — one of:
#    * a self-hosted metasearch instance (keyless):
#        search.metasearch_url: http://localhost:8080
#    * a commercial search API:
#        search.commercial.endpoint + export RAVE_SEARCH_API_KEY=...
#    * a local crawl of domains you name:
#        search.backend: crawler, search.crawler.domains: [docs.example.com]

python main.py "your question" --mode balanced --out report.md
```

Add `--confirm` to review/edit the research plan before the swarm runs.
Outputs: the report on stdout and in `report.md`, plus `runlog.jsonl` — a
JSONL trace of every tool call (phase, role, args, forced/auto flags, budget).

## For technical users (the expert layer)

Skip the hand-holding entirely:

```sh
rave setup --expert                 # compact checklist + config.yaml field docs
rave setup --expert --mode api --provider anthropic \
           --model claude-sonnet-4-6 --search metasearch   # non-interactive
```

`config.yaml` is fully inline-documented (fields: `mode local|api`,
`base_url`, `model`, `api_key_env`, search backend); the API key lives in
`.env` (chmod 600), never in config. Daily pipeline:

```sh
python main.py "question" --mode speed|balanced|quality [--confirm] [--out report.md]
python mcp_server.py                # stdio tool server for MCP clients
python mcp_server.py --http --port 8765   # HTTP mode for remote connectors
```

## CLI

```
python main.py                      # daily chat mode
python main.py setup [--expert ...] # setup wizard
python main.py "question" [--mode speed|balanced|quality] [--confirm]
               [--out report.md] [--runlog runlog.jsonl]
               [--config config.yaml] [--context "extra context"]
```

Exit codes: `0` success · `2` HALT (no web access) · `3` no search backend
configured · `4` LLM failure · `5` budget stop.

## Modes and budgets (enforced in code, hard stops)

| mode     | researcher iters / sub-q | critic verify cycles              | tool-call ceiling |
|----------|--------------------------|-----------------------------------|-------------------|
| speed    | 2                        | 0 (critic audits once, report-only) | 25              |
| balanced | 4                        | 1                                 | 60                |
| quality  | 8                        | 3                                 | 150               |

Re-search budget: ≤2 tool calls per critic issue. When the ceiling hits,
researchers exit via auto-fired `done`, verification stops, and the report is
still written (that final call is logged with `auto: true`).

## Configuration reference (`config.yaml`)

```yaml
llm:
  mode: local                 # local (keyless) | api (key required)
  base_url: ""                # local: empty = http://localhost:11434/v1
  model: llama3.1:8b          # ONE model serves all four agent roles
  api_key_env: RAVE_LLM_API_KEY   # api mode only: env var NAME holding the key
  timeout_seconds: 120
  max_tokens: 4096
  temperature: 0.2

search:
  backend: auto               # auto | metasearch | crawler | commercial
  metasearch_url: ""          # self-hosted metasearch instance (keyless)
  commercial:
    endpoint: ""              # generic JSON search API
    api_key_env: RAVE_SEARCH_API_KEY
  crawler:
    domains: []               # mini-crawler + local BM25 index over these
    index_dir: .rave_index
    max_pages_per_domain: 50

pipeline:
  chunk_tokens: 400           # chunk size (approximate tokens)
  chunk_overlap: 50
  stale_months: 6             # staleness window for fast-moving topics
  max_fetch_per_search: 5
  embeddings:
    provider: hash            # hash (built-in, offline) | local
    base_url: ""              # OpenAI-compatible /v1 for provider: local
    model: ""

modes:                        # override budgets if you must
  speed:    {researcher_iters: 2, critic_cycles: 0, tool_call_ceiling: 25}
  balanced: {researcher_iters: 4, critic_cycles: 1, tool_call_ceiling: 60}
  quality:  {researcher_iters: 8, critic_cycles: 3, tool_call_ceiling: 150}
```

Backend auto-selection: `metasearch_url` if set, else `commercial` if its key
is present, else exit with a setup hint.

LLM protocol auto-detection: a `base_url` on `api.anthropic.com` speaks the
Anthropic messages protocol; every other URL speaks OpenAI-compatible chat
completions (OpenAI, DeepSeek, Moonshot/Kimi, Perplexity Sonar, LM Studio,
Ollama, …). See `docs/LIVE_RUN.md` for copy-paste configs.

## The Finding schema (used everywhere, no exceptions)

```
{claim, url, date, source_type: primary|official|peer_reviewed|news|analyst|
 blog|forum|ai_generated, confidence: H|M|L, quote (verbatim, ≤25 words)}
```

Findings citing URLs never retrieved during the run are rejected in code.
Fast-moving topics: sources older than 6 months are flagged `[stale]` and
down-weighted; slow topics are exempt.

## Eval harness

```bash
python -m evals.harness --mode speed --desc "tightened researcher prompt"
```

Runs ~10 benchmark questions (`evals/questions.yaml`), scores citation
coverage %, corroboration % (claims with ≥2 sources), unverified count, and
expected-fact hits, then appends one tab-separated row to `evals/results.tsv`:

```
commit  score  mode  status  description
```

`status` is `keep` when the score matches or beats the best previous row,
else `discard`. Workflow: change a prompt or module → run the harness → keep
the commit if scores improve, revert if not. `results.tsv` stays untracked;
the header is created on first run.

## Tool server stub

`mcp_server.py` exposes `deep_research(query, mode)` over JSON-RPC/stdio for
a future remote-connector deployment. It is a stub — not wired into the main
flow.

## Repository layout

```
agents/        orchestrator.py  planner.py  researcher.py  critic.py  writer.py
tools/         registry.py  web_search.py  fetch_page.py  extractor.py
               chunker.py  ranker.py  source_vetter.py  crosscheck.py
llm/           client.py  schemas.py
prompts/       planner.md  researcher.md  critic.md  writer.md
ui/            prompt.py            # options + free-text renderer (chip-ready)
evals/         harness.py  questions.yaml
tests/         offline unit + end-to-end tests (no network, everything mocked)
mcp_server.py  main.py  config.yaml  requirements.txt
```

## Tests

```bash
python -m pytest
```

Everything runs offline: HTML fixtures for the extractor, mock transports for
the fetcher, search backends, and both LLM providers, plus scripted
end-to-end runs asserting forced-call run logs, budget ceilings, the verify
loop, and HALT behavior.

## License

MIT — see [LICENSE](LICENSE).
