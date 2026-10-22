# agent-search-kit

**Small, uniform search/read CLIs for research agents: Exa, Tavily, Jina, Gemini grounding, any
OpenAI-compatible Responses `web_search` (incl. xAI `x_search`), DeepSeek Search/Fetch, and a
rate-limited, cached GitHub search.**

Coding and research agents are good at running shell commands and reading JSON. They are bad at
remembering five different search APIs, their auth headers, their rate limits and their failure
modes. This kit gives them one convention:

```bash
python3 <tool>.py "query" [options]   ->   one JSON object on stdout
{"channel": "...", "ok": true, "results"|"answer"|"sources": ..., "latency_s": 1.4}
{"channel": "...", "ok": false, "error": "redacted message"}            # exit code 1
```

Standard library only (Python 3.9+). No SDKs, no daemon. Keys come from env vars or key files
and are never printed: error text is redacted.

| Tool | What it does | Needs |
| --- | --- | --- |
| `exa_search.py` | neural web search with snippets | `EXA_API_KEY` |
| `tavily_search.py` | web search, optional synthesized answer (`--answer`) | `TAVILY_API_KEY` |
| `jina_search.py` | web search with page markdown (`--max 0` = links only) | `JINA_API_KEY` |
| `jina_read.py` | URL -> clean markdown; falls back to the free keyless reader on 401/402/429, optional `--proxy` | optional `JINA_API_KEY` |
| `site_search.py` | search restricted to one domain (Exa, then Tavily) | Exa or Tavily key |
| `gemini_search.py` | Gemini with Google Search grounding: answer + issued queries + grounded sources | `GEMINI_API_KEY` |
| `web_search.py` | any **Responses API** model with hosted tools (`web_search`, `x_search`, ...): answer + cited URLs + number of searches. Streams, so long reasoning does not hit proxy timeouts | `RESPONSES_API_KEY` / `OPENAI_API_KEY` |
| `deepseek_search.py` | DeepSeek Search / Fetch tool API (+ `--balance`); cross-process QPS limiter, unescapes Fetch markdown | `DEEPSEEK_API_KEY` |
| `github_search.py` | `repos`, `similar` (find related projects by topics/keywords/language, scored), `readme`, `code`, `issues`, `rate` | optional `GITHUB_TOKEN` / `gh` |

### Why the GitHub tool is more than a wrapper

Agents love to fire ten GitHub searches in a row and get rate-limited for an hour.
`github_search.py` adds a **cross-process throttle** (file lock shared by every agent on the
machine: 2.2 s between authenticated searches, 6.5 s unauthenticated), **backoff on 403/429**
using `Retry-After`/`x-ratelimit-reset`, a **10-minute response cache**, and token handling that
never puts the token in argv or output (`$GITHUB_TOKEN` -> `$GH_TOKEN` -> `gh auth token`, read in-process).
`similar owner/repo` builds several queries from the repo's topics, description keywords and
language, merges them, and ranks the results by shared rare topics, keyword hits and stars. This
is handy for "find prior art / competitors" tasks.

## Install

```bash
git clone https://github.com/sdpwh7-ux/agent-search-kit.git
cd agent-search-kit
cp examples/keys.env.example ~/.config/agent-search.env   # edit, chmod 600, and source it
```

## Usage

```bash
python3 exa_search.py "rust async runtime comparison" -n 5
python3 tavily_search.py "CVE-2026-xxxx nginx" --answer
python3 jina_read.py https://example.com/blog/post --max 8000
python3 site_search.py news.ycombinator.com "self-hosted LLM gateway"
python3 gemini_search.py "What changed in Python 3.14 free-threading?" --thinking high
python3 web_search.py "latest stable Kubernetes release and date" --model "$WEB_SEARCH_MODEL" --effort high
RESPONSES_BASE_URL=https://api.x.ai/v1 RESPONSES_API_KEY=... \
  python3 web_search.py "reactions to the new release" --model grok-4 --tools web_search,x_search
python3 deepseek_search.py search "向量数据库 选型" --n 10 --json
python3 deepseek_search.py fetch https://example.com/article
python3 github_search.py repos "mcp server postgres" --lang python --min-stars 50 --pushed-after 90d
python3 github_search.py similar owner/repo -n 10
python3 github_search.py rate
```

Give your agent a short instruction like: *"For web research run the tools in
`/path/to/agent-search-kit`; each prints JSON. Prefer two independent channels for factual
claims and cite the `url`s you used."*

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_SEARCH_KEY_DIR` | `~/.config/agent-search` | directory of key files (`exa_key`, `tavily_key`, `jina_key`, `gemini_key`, `deepseek_key`, `responses_key`, `openai_key`) used when the env var is unset |
| `RESPONSES_BASE_URL` | `$OPENAI_BASE_URL` or `https://api.openai.com/v1` | Responses API endpoint for `web_search.py` |
| `WEB_SEARCH_MODEL` | - | default model for `web_search.py` |
| `GEMINI_BASE_URL` / `GEMINI_SEARCH_MODEL` | Google API / `gemini-flash-latest` | Gemini endpoint and model |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek endpoint |
| `AGENT_SEARCH_PROXY` | - | proxy for the keyless Jina reader (`http://...` or `socks5h://...`) |
| `AGENT_SEARCH_STATE` / `GITHUB_SEARCH_STATE` | `~/.cache/agent-search` / `~/.cache/github_search` | rate-limit locks and cache |
| `AGENT_SEARCH_UA` | `agent-search-kit/0.1` | User-Agent |

Optional usage accounting: put [agent-usage-ledger](https://github.com/sdpwh7-ux/agent-usage-ledger)'s
`usage_log.py` on `PYTHONPATH` and every HTTP call is recorded per agent (`AGENT_NAME`).

## Security notes for agent tooling

- **Network.** Each tool talks only to its provider's API (or the base URL you configure) and,
  for `jina_read.py`, to Jina's reader, which fetches the URL you pass. Queries and URLs are sent
  to third parties: do not search for secrets or private URLs.
- **Credentials.** Keys come from env vars or `AGENT_SEARCH_KEY_DIR` files (make them `chmod 600`).
  They are sent only as request headers to their own provider and never printed; error messages
  are redacted. `github_search.py` may call `gh auth token` in-process; the token is not
  written anywhere. Running an agent with these env vars gives it the ability to spend your
  API credit: set provider-side spending limits.
- **Shell.** No tool runs a shell. `jina_read.py --proxy` and `github_search.py --proxy` invoke
  `curl` with an argument list (no shell) and pass headers through stdin, so tokens never show up in
  `ps`.
- **Files.** Only rate-limit lock files and the GitHub response cache are written (under
  `~/.cache`). The cache holds public API responses only.
- **Prompt injection.** Search results and fetched pages are untrusted content. Agents should
  treat them as data, never as instructions.

## Tests

```bash
python3 -m unittest discover -s tests -v   # local fake server; no internet, no real keys
```

## License

MIT
