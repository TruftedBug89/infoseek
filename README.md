# infoseek

**Tavily-style web research for AI agents - no API keys, no scraping hacks.**

`infoseek` gives LLM agents the same primitives as paid search services:
multi-engine **search**, LLM-ready context bundles (**ask**), clean page
**extract**, and a built-in **prompt-injection guard** - keyless, polite,
token-efficient.

```bash
pip install git+https://github.com/TruftedBug89/infoseek
```

```python
import infoseek

<<<<<<< HEAD
print(infoseek.research("why is redis faster than postgres", budget=2000))
# one line, no asyncio, no API key -> curated, guard-screened context for your LLM
```

```console
$ infoseek research "why is redis faster than postgres"
QUERY: why is redis faster than postgres
## SEARCH RESULTS
1. Why is Postgres query faster than Redis query?
 [so · ✓ 3 · 2 answers] ...
```

## Quick start - four calls, no asyncio

Designed so even a small local model can use it: every call is **sync**, takes a
string, returns a string, and never raises (errors come back as `[[...]]` notes).

```python
import infoseek

infoseek.find("rust vs go 2026") # search -> ranked results with urls
infoseek.research("why is redis fast") # answer -> context to answer from
infoseek.read("https://example.com/article") # page -> clean text
infoseek.deep("llm quantization tradeoffs") # brief -> multi-angle research
infoseek.help() # usage card, call it to re-learn
```

| you want | call |
|---|---|
| links / sources to cite | `find()` |
| facts to answer a question | `research()` |
| the text of one known page | `read()` |
| a broader, slower, multi-angle brief | `deep()` |

---
=======
bundle = asyncio.run(infoseek.ask("why is redis faster than postgres", budget=2000))
print(bundle) # ~500 tokens of curated, guard-screened context for your LLM
```

## The four primitives

| call | returns | use when |
|---|---|---|
| `await infoseek.search(q, n=6)` | `list[dict]` of results | you want links + snippets |
| `await infoseek.ask(q, budget=2500)` | context-bundle string | you want text the model can answer from directly |
| `await infoseek.extract(url, max_chars=2000)` | clean page text | you already have a URL |
| `infoseek.scan(text)` (sync) | verdict: `ok` / `suspect` / `blocked` | you fetched text yourself and want it screened |
>>>>>>> 2bc88f6d9c02e9c51e25d694a93e68c2a2e6dfe9

**For agents that want zero decisions:** `await infoseek.run(q)` routes by
query shape - bare URL → extract, `ask: ...` → context bundle, error-message
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

<<<<<<< HEAD
- **15 keyless engines** - general web, news, forums, code, papers, biomedical,
 facts - all via official APIs or server-rendered HTML (no Google scraping, no CAPTCHA bypass)
- **Sync one-liners for agents** - `find()` / `research()` / `read()` / `deep()` take a
 string and return a string; no asyncio, no config, errors come back as short
 `[[...]]` notes instead of exceptions, so even small local models can drive it
- **`ask()` context bundles** - Tavily `/context` equivalent: search → pick the best
 pages → keep only the sentences relevant to your query → trim to a token budget
- **Quality-scored merge** - source priority + engine rank + recency bonus,
 per-source diversity cap, near-duplicate title collapse
- **Token efficiency** - 160-char snippets, CTA-boilerplate trimming, dedup,
 relevance extraction (~450–600 tokens per typical `ask()`)
- **Prompt-injection guard** - layered heuristics (hijack, framing, exfiltration,
 jailbreak, obfuscation, markup) → `ok / suspect / blocked` verdicts;
 `ask()` **denies** blocked sources, `extract()` replaces them with a denial note
- **Polite by default** - per-host rate limiting, Retry-After respect, robots.txt
 honored for direct page fetches, browser-UA rotation, gzip-only encoding
- **Disk cache** - search TTL 30 min, extraction 7 days, failure markers 90 s
 (flaky endpoints never slow you down twice)
- **`selfcheck()`** - a 27-check battery (unit + live probes of all 15 engines)
=======
Prefix the query to focus a source; no prefix hits the default mix
(`ddg + hn + so + reddit + news`). `site:<domain>` auto-routes.
>>>>>>> 2bc88f6d9c02e9c51e25d694a93e68c2a2e6dfe9

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

<<<<<<< HEAD
```console
# simple (text in / text out)
$ infoseek find "rust vs go" # ranked results
$ infoseek research "why is redis fast" # context to answer from
$ infoseek deep "llm quantization" # multi-angle brief
$ infoseek read https://example.com/article # one page, clean text
$ infoseek help # usage card

# advanced
$ infoseek search "retrieval augmented generation" --n 5
$ infoseek search "rust vs go" --json # machine-readable
$ infoseek ask "why is redis faster than postgres" --budget 2000
$ infoseek extract https://news.ycombinator.com/item?id=45838766
$ infoseek scan --text "Ignore all previous instructions..."
$ infoseek scan --url https://example.com/ # fetch + scan, exit 2 if blocked
$ infoseek suggest "python asyn"
$ infoseek status
$ infoseek selfcheck
=======
```bash
infoseek search "retrieval augmented generation" --n 5 [--json] [--freshness week]
infoseek ask "best self-hosted vector db" --budget 2000 [--json]
infoseek extract https://example.com/article [--max-chars 2000]
infoseek scan --text "..." | --url https://... # exit 2 if blocked
infoseek suggest "python asyn"
infoseek status # engine health + last errors
infoseek selfcheck # unit + live test battery
>>>>>>> 2bc88f6d9c02e9c51e25d694a93e68c2a2e6dfe9
```

## Reaching pages scrapers can't

`extract()` climbs an **access ladder** until it gets text:

<<<<<<< HEAD
> **New in 0.4.0:** sync one-liners (`find` / `research` / `read` / `deep` / `help`) - > see *Quick start* above. The async API below is unchanged and returns structured data.

```python
import asyncio, infoseek
=======
1. **live fetch** - official-API fast paths first (GitHub/Wikipedia/HN/Reddit/
 PyPI/crates), then a robots-respected direct fetch with bot-wall detection
2. **Wayback Machine** - the closest archived snapshot (raw original via the
 `id_` flag; falls back to recent CDX snapshots). The fetch never touches
 the origin, so it also works when robots.txt disallows, the site bot-blocks
 scrapers, or the page/domain no longer exists
3. **Jina Reader** (optional) - renders JS-heavy pages; only used when
 `JINA_API_KEY` is set (the keyless tier is Cloudflare-gated from most
 server IPs)
>>>>>>> 2bc88f6d9c02e9c51e25d694a93e68c2a2e6dfe9

For search coverage of the bot-walled web, `swarm:` fans out over
community-hosted **SearXNG** instances (github.com/searxng/searxng) in
parallel - the instances do the scraping for you. Instance list = curated
seed + daily refresh from the official searx.space list; per-instance health
is cached so walled/dead instances quickly cost nothing. Public instances
increasingly bot-wall datacenter IPs (degrades gracefully, never blocks);
for a guaranteed private swarm run your own instance:

```bash
docker run -d -p 8888:8080 searxng/searxng # then set SEARXNG_URL=http://localhost:8888
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
`INFOSEEK_GUARD=block` (default) | `warn` | `off` - keep it on `block`.

## Integrations

<<<<<<< HEAD
## Harness integration

`infoseek` plugs into any AI harness in three ways: as a **Python library**
(Prime Agent, Hermes kernels), as an **MCP server** (opencode, Claude Code,
Cursor, Windsurf, Continue, Goose - anything that speaks MCP), and as a
**skill** (skill-aware harnesses that read `SKILL.md`).

### MCP server (opencode, Claude Code, Cursor, ...)

The one integration to rule them all: install once, every MCP-capable harness
gets `find` / `research` / `read` / `deep` / `help` plus `search` / `ask` /
`extract` / `scan` / `suggest` / `status` / `selfcheck` / `run` as native tools.
The simple tools come first in the list, with short descriptions, so small
models pick the right one. **MCP is additive** - the Prime Agent / Hermes Python
API and skill paths keep working exactly as before, no MCP required for them.

```bash
pip install "infoseek[mcp] @ git+https://github.com/TruftedBug89/infoseek" # or: pip install git+...infoseek then pip install mcp
```

**opencode** - add to `~/.config/opencode/opencode.json` (global) or
`./opencode.json` (project):

```json
{
 "$schema": "https://opencode.ai/config.json",
 "mcp": {
 "infoseek": {
 "type": "local",
 "command": ["python", "-m", "infoseek.mcp"],
 "enabled": true
 }
 }
}
```

Then restart opencode - the tools appear as `mcp__infoseek__search` etc.

**Claude Code:**

```bash
claude mcp add infoseek -- python -m infoseek.mcp
```

**Cursor / Windsurf / Continue / Goose:** add an MCP server with command
`python -m infoseek.mcp` (stdio) in their MCP settings UI.

If `infoseek-mcp` is on `PATH`, use it directly as the command instead. On
Windows user-site installs the scripts land in
`%APPDATA%\Python\Python312\Scripts` - add that to `PATH` or use
`python -m infoseek.mcp`.

### Skill layouts

The repo root **is** the skill directory, so skill-aware harnesses just point
at it:

| harness | location |
|---|---|
| **opencode** | auto-loads `~/.agents/skills/<name>/SKILL.md` - clone there, no config needed |
| Prime Agent | `~/.agents/skills/infoseek` |
| Hermes | `~/.hermes/skills/research/infoseek` |
| Claude Code | `~/.claude/skills/infoseek` |

The `SKILL.md` frontmatter carries `name` + `description` (opencode, Claude
Code), `platforms`, and `metadata.hermes` (Hermes) - unknown extra fields are
ignored by the other harnesses.

### Prime Agent integration

Unchanged by the MCP work - Prime Agent keeps using infoseek as a plain
Python library in its kernel, **no MCP involved**:

```bash
# 1. clone the repo into your skills dir (the repo root IS the skill layout)
git clone https://github.com/TruftedBug89/infoseek ~/.agents/skills/infoseek

# 2. install the editable package into your kernel venv
~/.prime/agent/kernel-venv/bin/pip install -e ~/.agents/skills/infoseek

# 3. restart the agent session - `infoseek` is now importable and its CLI is wired up
```

```python
import infoseek
await infoseek.ask("tavily alternatives pricing", budget=1500) # agent context
await infoseek.selfcheck() # 27 checks, live
```

Both integration paths can be live at the same time: Prime Agent imports the
library directly, while opencode/Claude Code/Cursor talk to the same install
over MCP.

### Hermes Agent integration

```bash
# 1. copy the skill into the Hermes skills tree (repo root IS the skill layout)
mkdir -p ~/.hermes/skills/research/infoseek
cp -r SKILL.md README.md src ~/.hermes/skills/research/infoseek/

# 2. install the editable package into the Hermes kernel venv
# (venv path varies by install; find it with `head -1 $(which hermes)`)
/usr/local/lib/hermes-agent/venv/bin/pip install -e ~/.hermes/skills/research/infoseek

# 3. restart the session - `infoseek` is now importable and its CLI is wired up
```

```python
import infoseek
await infoseek.ask("tavily alternatives pricing", budget=1500)
await infoseek.selfcheck()
```
=======
* **Python library** - `import infoseek` (this README).
* **MCP server** - `pip install "infoseek[mcp]"`, then register stdio
 command `python -m infoseek.mcp`. Exposes `search`, `ask`, `extract`,
 `scan`, `suggest`, `status`, `selfcheck`, `run` as native tools in
 opencode / Claude Code / Cursor / Windsurf / Continue / Goose.
* **Skill** - the repo root is a skill layout (`SKILL.md`); point
 skill-aware harnesses at it (see `AGENTS.md` for per-harness paths).
>>>>>>> 2bc88f6d9c02e9c51e25d694a93e68c2a2e6dfe9

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

* **Legal & polite** - official APIs first; HTML parsing only where the site
 server-renders results (DDG, old.reddit); robots.txt gates direct page
 fetches; per-host throttling; no CAPTCHA bypass, ever.
* **Fast** - concurrent engines, per-engine caches, failure markers,
 n-independent cache keys.
* **Cheap** - everything is measured in tokens: snippet caps, CTA trimming,
 relevance-sentence extraction, budget-capped bundles.
* **AI-friendly** - ASCII output, predictable JSON options, explicit failure
 notes instead of silent empty results.

## Testing

```bash
pip install -e ".[dev]"
<<<<<<< HEAD
pytest # 36 offline unit tests (guard battery, ranking, routing, API, simple API)
pytest -m live # + 16 live engine probes + ask() smoke (network required)
=======
pytest # offline suite (guard battery, routing, ranking, regressions)
pytest -m live # + live engine probes (network required)
infoseek selfcheck # unit checks + live probes of all engines + smoke runs
>>>>>>> 2bc88f6d9c02e9c51e25d694a93e68c2a2e6dfe9
```

## License

MIT © 2026 TruftedBug89
