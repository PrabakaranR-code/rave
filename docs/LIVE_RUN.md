# Running RAVE live

RAVE only answers from live retrieval. A run needs exactly two things you
provide: **an LLM endpoint** and **a search backend**. Without working web
access it prints a HALT message and exits nonzero — by design.

## 1. Provide an LLM endpoint

Pick one option and set it under `llm:` in `config.yaml`.

### Option A — local Ollama (keyless; the default config shape)

```yaml
llm:
  provider: openai
  base_url: http://localhost:11434/v1
  models: {planner: llama3.1:8b, researcher: llama3.1:8b,
           critic: llama3.1:8b, writer: llama3.1:8b}   # whatever model you pulled
```

### Option B — any hosted OpenAI-compatible endpoint

```yaml
llm:
  provider: openai
  base_url: https://<your-endpoint>/v1
  api_key_env: RAVE_LLM_API_KEY      # then: export RAVE_LLM_API_KEY=<key>
  models: {planner: <model>, researcher: <model>, critic: <model>, writer: <model>}
```

### Option C — Anthropic API

```yaml
llm:
  provider: anthropic
  base_url: https://api.anthropic.com   # no /v1 suffix; the client adds /v1/messages
  api_key_env: RAVE_LLM_API_KEY         # export RAVE_LLM_API_KEY=sk-ant-...
  models: {planner: claude-haiku-4-5-20251001, researcher: claude-haiku-4-5-20251001,
           critic: claude-sonnet-5, writer: claude-sonnet-5}
```

Per-role routing exists so you can put a cheap/fast model on planner and
researcher and the strongest model on critic and writer. A single model
everywhere works too.

## 2. Provide a search backend

Pick one option and set it under `search:` in `config.yaml`.

### Option A — self-hosted metasearch instance (keyless; recommended)

```yaml
search:
  backend: metasearch
  metasearch_url: http://localhost:8080   # must serve /search?q=...&format=json
```

Any metasearch engine exposing that JSON endpoint works (for SearXNG, enable
the JSON API once: in `settings.yml` set `search: { formats: [html, json] }`
and restart).

### Option B — commercial JSON search API

```yaml
search:
  backend: commercial
  commercial:
    endpoint: https://<search-api-endpoint>
    api_key_env: RAVE_SEARCH_API_KEY      # export RAVE_SEARCH_API_KEY=<key>
```

The adapter sends `GET <endpoint>?q=<query>&count=<n>` with the key in both
`X-API-Key` and `Authorization: Bearer` headers, and tolerantly parses
`results` / `items` / `web.results` arrays with `url|link`, `title|name`,
`snippet|description` fields. APIs with a different shape need a small tweak
in `tools/web_search.py` (`CommercialBackend`).

### Option C — no search service: crawl domains you trust

```yaml
search:
  backend: crawler
  crawler: {domains: [www.nih.gov, www.who.int], max_pages_per_domain: 50}
```

Builds a local BM25 index over those domains on first use (respects
robots.txt) and searches it offline afterward. Delete `.rave_index/` to
re-crawl.

## 3. Run

```bash
git clone <this-repo> && cd rave
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Option A prerequisites, as an example:
#   install Ollama, then:  ollama pull llama3.1:8b
#   run a metasearch container on localhost:8080 with the JSON API enabled

python main.py "What are the health benefits of intermittent fasting?" \
  --mode balanced --out report.md
```

What you should see:

- phase progress on stderr: `P0 intake → P1 decompose → P2 swarm research →
  P3 cross-check → P4 verify → P5 report`;
- the cited report on stdout and in `report.md`;
- `runlog.jsonl` — the audit trail of every forced tool call with budget
  positions.

Balanced mode gives each of the 3–7 sub-questions up to 4 research turns, one
critic→verify cycle, and a hard 60-tool-call ceiling; expect a few minutes on
a local 8B model. Add `--confirm` to approve or edit the sub-question plan
before the swarm starts.

## Exit codes and common failures

| exit | meaning | fix |
|------|---------|-----|
| 0 | success | — |
| 2 | HALT: no working web access | check network / search backend URL |
| 3 | no search backend configured | set one of the `search:` options above |
| 4 | LLM failure (unreachable, or schema-invalid tool calls after retries) | check endpoint/key; use a stronger model |
| 5 | budget stop outside a recoverable phase | rerun, or raise the mode ceiling |

The exit-4 caveat matters for small local models: every phase transition is a
forced, schema-validated tool call, retried at most twice before failing
loudly. If an 8B model can't keep the schemas straight, point `critic` and
`writer` (or all roles) at a stronger model — that is exactly what the
per-role `llm.models` routing is for.
