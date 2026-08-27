# infoseek

**Tavily-style web research for AI agents — no API keys, no scraping hacks.**

`infoseek` gives LLM agents the same primitives as paid search services:
multi-engine **search**, LLM-ready context bundles (**ask**), clean page
**extract**, and a built-in **prompt-injection guard** — keyless, polite,
token-efficient.

```bash
pip install git+https://github.com/TruftedBug89/infoseek
```

```python
import asyncio, infoseek

bundle = asyncio.run(infoseek.ask("why is redis faster than postgres", budget=2000))
print(bundle)   # ~500 tokens of curated, guard-screened context for your LLM
```

## The four primitives

| call | returns | use when |
|---|---|---|
| `await infoseek.search(q, n=6)` | `list[dict]` of results | you want links + snippets |
| `await infoseek.ask(q, budget=2500)` | context-bundle string | you want text the model can answer from directly |
| `await infoseek.extract(url, max_chars=2000)` | clean page text | you already have a URL |
| `infoseek.scan(text)` (sync) | verdict: `ok` / `suspect` / `blocked` | you fetched text yourself and want it screened |

**For agents that want zero decisions:** `await infoseek.run(q)` routes by
query shape — bare URL → extract, `ask: ...` → context bundle, error-message
text → fixes-first research, version question → compat research, anything
else → search. `infoseek.help()` returns a 20-line cheat sheet of the whole
surface. `ask()` auto-reformulates and retries once when first-round results
are poor.

**Never a bare empty list.** Real agent sessions show models abandoning a
tool that returns `[]` with no guidance, so empty results are handled in
three steps: (1) auto-retry once with the `wide` mix, (2) if still empty,
return an actionable hint tailored to the engine prefix (`code:` → grep.app
only indexes popular repos, try `gh:` or extract raw files; `gh:` → check
spelling / try `wayback:`; `swarm:` → set `SEARXNG_URL`; plain → try
`engines="wide"` or `suggest()`; …), (3) `extract()` failures return an
explicit `[extract: no content retrieved …]` note instead of `""`.

Helpers: `search_many([q1, q2])` (concurrent fan-out, one merged list),
`smart_search(q)` / `search(q, expand=True)` (auto-reformulates poor
results), `search_error(msg)`, `search_compat(q)`, `changelog(project)`,
`suggest(q)`, `status()`, `selfcheck()`.

All output is ASCII-safe and token-lean (compact snippets, dedupe,
relevance-sentence extraction, budget caps).

## Why not Tavily / Exa / Firecrawl?

| | infoseek | Tavily / Exa / Firecrawl |
|---|---|---|
| API key | never needed | required (paid) |
| Cost | free | metered |
| Sources | 29 keyless engines + archive access ladder | 1 index |
| Prompt-injection guard | built in, µs-fast | not included |

## Engines (26 keyless + 3 optional keyed)

Prefix the query to focus a source; no prefix hits the default mix
(`ddg + hn + so + reddit + news`). `site:<domain>` auto-routes.

| prefix | source | prefix | source |
|---|---|---|---|
| *(none)* | DuckDuckGo + HN + SO + Reddit + News | `arxiv:` | arXiv papers |
| `ddg:` | DuckDuckGo only | `openalex:` / `s2:` | scholarly works (OpenAlex) |
| `hn:` | Hacker News (Algolia API) | `pubmed:` / `pm:` | biomedical (NCBI) |
| `reddit:` | Reddit | `doi:` | DOI / citations (Crossref) |
| `so:` | Stack Overflow / Exchange | `wikidata:` / `wd:` | structured facts (Q-IDs) |
| `news:` | Google News RSS | `gh:` | GitHub repos |
| `wiki:` | Wikipedia | `code:` | code search (grep.app) |
| `pypi:` / `pip:` | Python packages | `npm:` / `node:` | JS/TS packages |
| `crates:` / `rust:` | Rust crates | `mdn:` / `docs:` | MDN Web Docs |
| `yt:` / `youtube:` | YouTube videos | `lobsters:` | lobste.rs |
| `marginalia:` | small / indie web | `issues:` | GitHub issues + PRs |
| `prs:` | GitHub pull requests | `releases:` | GitHub release notes |
| `changelog:` | changelog finder | `error:` | error message → fixes |
| `compat:` | version compatibility | `wayback:`/`wb:` | Wayback Machine snapshots |
| `commoncrawl:`/`cc:` | Common Crawl index | `swarm:` | SearXNG instance swarm |

`engines="wide"` switches `search()` to the maximum-coverage mix
(ddg + swarm + hn + so + news).

Optional keyed engines activate automatically from env vars (never required):
`BRAVE_API_KEY` → `brave:`, `SERPER_API_KEY` → `serper:`,
`SEARXNG_URL` → `searxng:`. Optional `GITHUB_TOKEN` raises GitHub rate
limits.

## CLI

```bash
infoseek search "retrieval augmented generation" --n 5 [--json] [--freshness week]
infoseek ask "best self-hosted vector db" --budget 2000 [--json]
infoseek extract https://example.com/article [--max-chars 2000]
infoseek scan --text "..." | --url https://...     # exit 2 if blocked
infoseek suggest "python asyn"
infoseek status                                    # engine health + last errors
infoseek selfcheck                                 # unit + live test battery
```

## Reaching pages scrapers can't

`extract()` climbs an **access ladder** until it gets text:

1. **live fetch** — official-API fast paths first (GitHub/Wikipedia/HN/Reddit/
   PyPI/crates), then a robots-respected direct fetch with bot-wall detection
2. **Wayback Machine** — the closest archived snapshot (raw original via the
   `id_` flag; falls back to recent CDX snapshots). The fetch never touches
   the origin, so it also works when robots.txt disallows, the site bot-blocks
   scrapers, or the page/domain no longer exists
3. **Jina Reader** (optional) — renders JS-heavy pages; only used when
   `JINA_API_KEY` is set (the keyless tier is Cloudflare-gated from most
   server IPs)

For search coverage of the bot-walled web, `swarm:` fans out over
community-hosted **SearXNG** instances (github.com/searxng/searxng) in
parallel — the instances do the scraping for you. Instance list = curated
seed + daily refresh from the official searx.space list; per-instance health
is cached so walled/dead instances quickly cost nothing. Public instances
increasingly bot-wall datacenter IPs (degrades gracefully, never blocks);
for a guaranteed private swarm run your own instance:

```bash
docker run -d -p 8888:8080 searxng/searxng   # then set SEARXNG_URL=http://localhost:8888
```

`wayback:` and `commoncrawl:` search the archives themselves (snapshot
discovery without touching origin sites).

## Prompt-injection guard

Retrieved web content is untrusted. `infoseek.scan(text)` runs layered
regex/structural heuristics (~µs per page, no LLM cost) and returns a verdict:

| level | meaning | handling |
|---|---|---|
| `ok` | clean | pass through |
| `suspect` | ambiguous signals | included, flagged as untrusted DATA |
| `blocked` | clear injection attempt | denied: removed from `ask()` bundles; `extract()` returns a denial note |

Detection covers hijack directives, role/framing takeover, prompt
exfiltration, jailbreak phrasing, `<system>`-style markup, homoglyph and
spaced-letter obfuscation, encoded payloads, and directive density. Policy:
`INFOSEEK_GUARD=block` (default) | `warn` | `off` — keep it on `block`.

## Integrations

* **Python library** — `import infoseek` (this README).
* **MCP server** — `pip install "infoseek[mcp]"`, then register stdio
  command `python -m infoseek.mcp`. Exposes `search`, `ask`, `extract`,
  `scan`, `suggest`, `status`, `selfcheck`, `run` as native tools in
  opencode / Claude Code / Cursor / Windsurf / Continue / Goose.
* **Skill** — the repo root is a skill layout (`SKILL.md`); point
  skill-aware harnesses at it (see `AGENTS.md` for per-harness paths).

## Configuration

| env var | default | purpose |
|---|---|---|
| `INFOSEEK_CACHE` | platform cache dir | cache location (falls back to temp dir if not writable) |
| `INFOSEEK_INTERVAL` | `1.0` s | per-host minimum request interval |
| `INFOSEEK_GUARD` | `block` | guard policy: `block` / `warn` / `off` |
| `INFOSEEK_ENGINE_TIMEOUT` | `3.5` s | per-engine timeout |
| `SEARXNG_URL` | – | your own SearXNG instance (private swarm, preferred) |
| `JINA_API_KEY` | – | enables Jina Reader as last-resort page renderer |

Search results cache for 30 min, extractions for 7 days, failures for 90 s.
Warm calls return in ~10 ms.

## Design principles

* **Legal & polite** — official APIs first; HTML parsing only where the site
  server-renders results (DDG, old.reddit); robots.txt gates direct page
  fetches; per-host throttling; no CAPTCHA bypass, ever.
* **Fast** — concurrent engines, per-engine caches, failure markers,
  n-independent cache keys.
* **Cheap** — everything is measured in tokens: snippet caps, CTA trimming,
  relevance-sentence extraction, budget-capped bundles.
* **AI-friendly** — ASCII output, predictable JSON options, explicit failure
  notes instead of silent empty results.

## Testing

```bash
pip install -e ".[dev]"
pytest                 # offline suite (guard battery, routing, ranking, regressions)
pytest -m live         # + live engine probes (network required)
infoseek selfcheck     # unit checks + live probes of all engines + smoke runs
```

## License

MIT © 2026 TruftedBug89
