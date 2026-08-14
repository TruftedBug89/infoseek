---
name: infoseek
description: >-
  Web research without Tavily: keyless multi-engine search, page extraction, and
  token-efficient LLM-ready context bundles. Use when you need to research a topic
  online, search the web, forums/social (Hacker News, Reddit, Stack Overflow,
  lobste.rs), news, Wikipedia, arXiv papers, GitHub repos, or code; or to extract
  clean text from a URL ("search for", "look up", "research", "find sources", "news
  about", "what does X do", "ask the web"). No API keys needed; optional
  Brave/Serper/SearXNG keys make it stronger when present.
version: 0.3.0
author: TruftedBug89
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [search, web-research, rag, keyless, prompt-injection, extraction, tavily]
    related_skills: [arxiv]
---

# infoseek

Direct, brief, fast information seeking. One tool, many sources, tiny token footprint.

## Call from kernel

    await infoseek.search("rust vs go 2025", n=6)          # -> list of result dicts
    await infoseek.ask("how does searxng work", n=5, extract_top=2, budget=2000)
                                                           # -> context bundle for you
    await infoseek.extract("https://...", max_chars=1500)  # -> clean page text
    await infoseek.suggest("local llm")                    # -> autocomplete ideas
    await infoseek.status()                                # -> engine availability, errors
    await infoseek.selfcheck(verbose=True)  # -> run the test battery (unit + live engines)

## CLI

    infoseek --query "reddit: tavily alternatives" --n 4
    infoseek --query "ask: best self-hosted vector db" --budget 2000

`ask:` prefix = search + extract top pages, trimmed to a token budget. Feed its output
to the LLM to write the final brief answer (this is the Tavily "context" equivalent).

## Query routing

Prefixes pick focused sources; everything else hits the mixed default
(ddg + hn + stackoverflow + reddit + news):

| prefix / filter | source |
|---|---|
| `hn:` | Hacker News (Algolia API) |
| `reddit:` | Reddit (old.reddit HTML search) |
| `so:` | Stack Overflow / Stack Exchange API |
| `news:` | Google News RSS (current) |
| `wiki:` | Wikipedia API |
| `arxiv:` | arXiv papers |
| `openalex:` / `s2:` | scholarly works across all venues (OpenAlex API) |
| `wikidata:` / `wd:` | structured facts entities (Wikidata API) |
| `pubmed:` / `pm:` | biomedical literature (NCBI E-utilities) |
| `doi:` | resolve DOIs / cite counts (Crossref API) |
| `gh:` | GitHub repos (stars, lang) |
| `code:` | code search (grep.app) |
| `lobsters:` | lobste.rs recent stories |
| `marginalia:` | indie/old web |
| `ddg:` | DuckDuckGo only |
| `site:github.com` etc. | auto-routes to matching engine |

## v0.3: prompt-injection guard + scholarly engines
- **`infoseek.scan(text)` prompt-injection guard** — layered heuristic detection
  (hijack directives, role/framing takeover, prompt-exfiltration, jailbreak
  phrasing, system/instruction markup, obfuscation like spaced-out or collapsed
  letters, encoded payloads, instruction-shaped openings). Pure regex, ~1-4 µs
  per page, LRU-cached, no LLM cost.
- **Verdicts:** `ok` / `suspect` / `blocked`. `ask()` **denies** blocked sources
  (removed from the context bundle + a note is appended), flags suspect sources
  as untrusted DATA inline. `extract(url)` replaces blocked content with a denial
  note. Policy: `INFOSEEK_GUARD=block|warn|off` (default `block`).
- **15 keyless engines**: added Wikidata (facts), PubMed (biomedicine),
  Crossref (DOI/citations) alongside the v0.2 set.

## v0.2 quality upgrades

- **Scored merge** — results ranked by source priority + engine rank + recency bonus
  (news/wikis < 30 days old get a boost), with a per-source diversity cap so one
  engine can't dominate. Near-duplicate titles ("Same Article - SiteA" vs "| SiteB")
  are collapsed; `www.`/`m.` variants dedup.
- **Relevance-scored extraction** — `ask()` keeps only the sentences that match the
  query (term overlap, phrase hits, lead bonus), then reorders them as they appear.
  No more dumping boilerplate intros.
- **Smart extraction targets** — picks the highest-scored results, prefers pages
  whose snippet/title contains query terms, avoids Google-News redirect wrappers,
  one page per domain.
- **New OpenAlex engine** (`openalex:`) — keyless scholarly search across journals,
  preprints, and books with citation counts.
- **New v0.3 engines** — `wikidata:` entity facts, `pubmed:` biomedicine,
  `doi:`/`crossref:` citation lookup (all keyless, all in selfcheck).
- **DDG resilience** — if the primary UA gets challenged, one automatic retry with a
  different browser UA.
- **Smarter retries** — 429/503 retried; 502 only when Retry-After is present (so a
  dead upstream doesn't cost 2s per search).
- **n-independent cache** — `search(n=4)` and `ask(n=5)` share the same cached engine
  payloads; ~12 results cached per engine per query.

## Design (why it's fast, cheap, legal)

- **Keyless by default** — DDG HTML POST, old.reddit HTML, and official public APIs
  (HN Algolia, Stack Exchange, Google News RSS, Wikipedia, arXiv, OpenAlex, GitHub, grep.app).
  Optional keys are honored if env vars exist: `BRAVE_API_KEY`, `SERPER_API_KEY`,
  `SEARXNG_URL`. Keys are read at call time, never logged.
- **Token-lean** — snippets ≤160 chars; results deduped by normalized URL and title;
  search ≈ 500 tokens, ask bundle ≈ 1-2k tokens; `budget` caps it (tokens = chars/4).
- **Fast** — engines run concurrently; disk cache (SQLite): search 30 min, extraction
  7 days. Cold first search ~8s, cached ~0.01s. `fresh=True` bypasses cache.
  `INFOSEEK_CACHE`, `INFOSEEK_INTERVAL` env vars tune cache dir and rate limit.
- **Legal/polite** — no Google scraping, no CAPTCHA bypassing, ever. Per-host rate
  limiting (default 1.2s), Retry-After honored, retries on 429/502/503, failures
  cached 90 s so flaky endpoints don't slow repeats. `robots.txt` respected for
  direct page fetches; official APIs (Wikipedia REST, GitHub API, HN Algolia) are
  used instead of scraping those sites. Pass `respect_robots=False` only for pages
  you own/are allowed to fetch.
- **Resilient** — every engine is isolated; one failing source never blocks others.
  `status()` shows what's healthy and last errors.

## Notes

- Google News URLs are redirect wrappers; pass them to `extract()` and the redirect
  resolves automatically.
- Reddit deep extraction depends on pullpush.io (flaky); search snippets are the
  reliable path.
- For code answers prefer `code:` or `gh:`; for current events `news:`; for
  discussions, `hn:` / `reddit:` / `so:`.
