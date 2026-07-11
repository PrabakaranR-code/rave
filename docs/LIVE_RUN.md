# Running RAVE live

RAVE only answers from live retrieval. A run needs exactly two things you
provide: **an LLM endpoint** and **a search backend**. Without working web
access it prints a HALT message and exits nonzero — by design.

## 1. Provide an LLM

One model serves all four agent roles. Two modes:

- **`mode: local`** — keyless; `base_url` defaults to
  `http://localhost:11434/v1`, so a local Ollama server hosting any model
  (llama, qwen, deepseek-r1, …) works out of the box.
- **`mode: api`** — set `base_url` and export the key in the env var named by
  `api_key_env`.

The wire protocol is auto-detected from `base_url`: `api.anthropic.com`
speaks the Anthropic messages protocol; every other URL speaks
OpenAI-compatible chat completions.

Four copy-paste examples for `llm:` in `config.yaml`:

### Ollama, local (keyless)

```yaml
llm:
  mode: local
  model: llama3.1:8b            # whatever you pulled: qwen3:8b, deepseek-r1:8b, ...
```

### Anthropic

```yaml
llm:
  mode: api
  base_url: https://api.anthropic.com   # with or without /v1 — both work
  model: claude-sonnet-5
  api_key_env: RAVE_LLM_API_KEY         # export RAVE_LLM_API_KEY=sk-ant-...
```

### OpenAI

```yaml
llm:
  mode: api
  base_url: https://api.openai.com/v1
  model: <an OpenAI model with tool calling>
  api_key_env: RAVE_LLM_API_KEY         # export RAVE_LLM_API_KEY=sk-...
```

### Any other OpenAI-compatible provider

DeepSeek, Kimi/Moonshot, Perplexity Sonar, a remote LM Studio box — anything
speaking the `/v1/chat/completions` protocol with tool calling:

```yaml
llm:
  mode: api
  base_url: https://api.deepseek.com/v1   # your provider's /v1 base
  model: deepseek-chat                    # your provider's model id
  api_key_env: RAVE_LLM_API_KEY           # export RAVE_LLM_API_KEY=<key>
```

## 2. Provide a search backend

Pick one option and set it under `search:` in `config.yaml`. The default,
SCOUT, needs nothing installed.

### Option A — SCOUT (built-in, keyless; recommended default)

```yaml
search:
  backend: scout
  scout:
    trusted_outlets: []       # optional: domains given a vetting quality boost
    sources: {}               # optional per-source overrides, e.g. {startpage: false}
    searxng_public: false     # leave off unless you want volunteer public instances
```

SCOUT queries independent search engines, science databases (arXiv, PubMed),
Wikipedia, and news feeds in parallel, directly from your machine — no server,
no Docker, no API key. It deliberately skips Google/Bing (ad- and SEO-driven).
This is what `rave setup` picks by default.

### Option B — SearXNG for extra breadth (self-hosted, needs Docker)

Adds Google + Bing reach on top of SCOUT's sources, at the cost of running a
container.

```yaml
search:
  backend: metasearch
  metasearch_url: http://localhost:8080   # must serve /search?q=...&format=json
```

Any metasearch engine exposing that JSON endpoint works (for SearXNG, enable
the JSON API once: in `settings.yml` set `search: { formats: [html, json] }`
and restart). The wizard's SearXNG option installs Docker and starts this for
you.

### Option C — commercial JSON search API

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

### Option D — crawl only domains you trust

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

# Minimal setup (local LLM + built-in SCOUT search — nothing else to install):
#   install Ollama, then:  ollama pull llama3.1:8b
#   search.backend: scout is the default; no server or key needed

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
| 3 | search backend misconfigured (e.g. `metasearch` with no URL) | set one of the `search:` options above, or use `backend: scout` |
| 4 | LLM failure (unreachable, or schema-invalid tool calls after retries) | check endpoint/key; use a stronger model |
| 5 | budget stop outside a recoverable phase | rerun, or raise the mode ceiling |

The exit-4 caveat matters for small local models: every phase transition is a
forced, schema-validated tool call, retried at most twice before failing
loudly. If an 8B model can't keep the schemas straight, set `llm.model` to a
stronger model (or switch `llm.mode: api` and point `base_url` at a hosted
provider).
