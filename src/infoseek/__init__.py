"""infoseek — Tavily-style web research for AI agents. No API keys.

Core API — four primitives (all async except scan):
    await infoseek.search(query, n=6)            -> list[dict] of results
    await infoseek.ask(query, budget=2500)       -> LLM-ready context bundle
    await infoseek.extract(url, max_chars=2000)  -> clean page text
    infoseek.scan(text)                          -> prompt-injection verdict (sync)

Plus: search_many(), smart_search(), suggest(), status(), selfcheck(), and the
research helpers search_error() / search_compat() / changelog().

26 keyless engines (20 direct sources — DuckDuckGo, Hacker News, Stack
Overflow, Reddit, Google News, Wikipedia, Wikidata, arXiv, OpenAlex, PubMed,
Crossref, GitHub, grep.app, lobste.rs, Marginalia, PyPI, npm, crates.io, MDN,
YouTube — plus 6 research modes: gh_issues, prs, gh_releases, changelog,
error, compat). Optional keyed upgrades (Brave, Serper, your own SearXNG)
activate automatically from env vars.

Query routing: prefix the query to pick a source (hn:, so:, news:, wiki:,
arxiv:, gh:, code:, reddit:, pypi:, npm:, crates:, mdn:, error:, compat:,
issues:, prs:, releases:, changelog:, ...); site:<domain> auto-routes.

Token-efficient by design: compact ASCII snippets, near-dup dedupe,
quality-scored merge with recency boost, relevance-sentence extraction, disk
cache, polite rate limiting, robots.txt respected for direct page fetches.

Built-in prompt-injection guard: ask() denies blocked sources, extract()
replaces them with a denial note, search() drops blocked snippets."""

__version__ = "0.9.0"

import asyncio, os, re
from urllib.parse import urlparse

from .cache import info as _cache_info
from .engines import (REGISTRY, KEYLESS, available, resolve_engines, run_engines)
from .extract import extract_many, extract_url
from . import guard
from .guard import scan
from .format import fmt_bundle, fmt_search, fmt_status, no_results_hint
from .net import PoliteClient
from .simple import find, research, read, deep  # sync one-liners for agents
from .rank import Result, merge, to_dicts
from .research import (smart_search, search_error, search_compat, changelog,
                       expand_queries, quality, normalize_error_message,
                       focus_snippet, auto_focus, looks_like_error,
                       looks_like_version_query)
from .social import last30days
from .selfcheck import selfcheck

_clients: dict[tuple[float, bool], PoliteClient] = {}
_last_errors: dict = {}


def _get_client(min_interval: float = 1.0, respect_robots: bool = True) -> PoliteClient:
    """Shared clients, keyed by (interval, robots) so per-call settings are honored."""
    iv = float(os.environ.get("INFOSEEK_INTERVAL", str(min_interval)))
    key = (iv, respect_robots)
    c = _clients.get(key)
    if c is None:
        c = PoliteClient(min_interval=iv, respect_robots=respect_robots)
        _clients[key] = c
    return c


def _apply_site_filter(results: list[Result], query: str) -> list[Result]:
    m = re.search(r"site:\s*([\w.-]+)", query)
    if not m:
        return results
    dom = m.group(1).lower()
    return [r for r in results if dom in (r.url or "").lower()]


_FRESH_ALIASES = {"day": 1, "week": 7, "month": 31, "year": 365}


def _freshness_days(freshness) -> float | None:
    """Accept 'day'/'week'/'month'/'year', '7d', or an int/float number of days."""
    if freshness is None:
        return None
    if isinstance(freshness, (int, float)):
        return max(0.04, float(freshness))
    s = str(freshness).strip().lower()
    if s in _FRESH_ALIASES:
        return float(_FRESH_ALIASES[s])
    m = re.match(r"^(\d+)\s*d$", s)
    if m:
        return float(int(m.group(1)))
    try:
        return max(0.04, float(s))
    except ValueError:
        return None


def _apply_freshness(results: list, days: float | None) -> list:
    """Drop results with a parseable date older than the window; undated results stay."""
    if not days:
        return results
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=days)
    out = []
    for r in results:
        m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", r.date or "")
        if m:
            try:
                if date(int(m.group(1)), int(m.group(2)), int(m.group(3))) < cutoff:
                    continue
            except ValueError:
                pass
        out.append(r)
    return out


def _screen_snippets(results: list) -> list:
    """Guard-screen titles+snippets: drop blocked, flag suspect. µs-fast, cached."""
    out = []
    for r in results:
        v = scan((r.title or "") + "\n" + (r.snippet or ""), url=r.url)
        if v.level == "blocked":
            continue
        if v.level == "suspect":
            r.extra = ((r.extra + " · ") if r.extra else "") + "[guard:suspect]"
        out.append(r)
    return out


async def search(query: str, n: int = 10, engines: str = "auto", fresh: bool = False,
                 min_interval: float = 1.0, freshness=None, expand: bool = False,
                 domain: str | None = None) -> list[dict]:
    """Run a multi-engine search. Returns deduped, merged result dicts
    (title, url, snippet, source, extra, date, rank).

    domain: optional domain filter to restrict search (e.g. 'github.com', 'docs.python.org').
    freshness: limit recency — 'day'/'week'/'month'/'year', '7d', or days (int).
    expand=True: if the first round scores poorly (few results / low term
    overlap / no version numbers for version questions), automatically
    reformulate and merge a second round (see smart_search).
    Titles/snippets are guard-screened: blocked snippets dropped, suspect flagged."""
    if expand:
        return await smart_search(query, n=n, fresh=fresh)
    days = _freshness_days(freshness)
    if domain:
        dom_clean = domain.strip().lower()
        if f"site:{dom_clean}" not in query.lower():
            query = f"site:{dom_clean} {query.strip()}"
    engines_list, q = resolve_engines(query, engines)
    if not engines_list:
        return []
    client = _get_client(min_interval)
    results, errors = await run_engines(client, q, n=max(n, 4), engines_list=engines_list,
                                        fresh=fresh, freshness_days=days)
    _last_errors.update(errors)
    results = _apply_freshness(_apply_site_filter(results, query), days)
    merged = merge([results], n, engines_list + [e for e in KEYLESS if e not in engines_list])
    res_dicts = to_dicts(_screen_snippets(merged))

    # Automatic query relaxation fallback:
    # If 0 results on a strictly quoted / boolean query, relax operators and retry once.
    if not res_dicts and ('"' in q or "'" in q or " OR " in q or " AND " in q):
        relaxed_q = re.sub(r'["\']', ' ', q)
        relaxed_q = re.sub(r'\b(OR|AND)\b', ' ', relaxed_q)
        relaxed_q = re.sub(r'\s+', ' ', relaxed_q).strip()
        if relaxed_q and relaxed_q != q:
            rel_results, _ = await run_engines(client, relaxed_q, n=max(n, 4), engines_list=engines_list,
                                               fresh=True, freshness_days=days)
            if rel_results:
                rel_results = _apply_freshness(_apply_site_filter(rel_results, query), days)
                merged = merge([rel_results], n, engines_list + [e for e in KEYLESS if e not in engines_list])
                res_dicts = to_dicts(_screen_snippets(merged))

    return res_dicts


async def search_many(queries, n: int = 6, engines: str = "auto", fresh: bool = False,
                      freshness=None) -> list[dict]:
    """Run several queries concurrently and return one merged, deduped, ranked list.
    Fan-out for iterative agent research: variant phrasings in one round-trip."""
    queries = [q for q in (queries if isinstance(queries, (list, tuple)) else [queries]) if q and q.strip()]
    if not queries:
        return []
    lists = await asyncio.gather(*[search(q, n=n, engines=engines, fresh=fresh,
                                          freshness=freshness) for q in queries])
    groups = [[Result(**d) for d in lst] for lst in lists]
    order: list[str] = []
    for g in groups:
        for r in g:
            if r.source not in order:
                order.append(r.source)
    merged = merge(groups, min(n * len(groups), 24), order)
    return to_dicts(_screen_snippets(merged))


async def ask(query: str, n: int = 5, extract_top: int = 2, budget: int = 2500,
              fresh: bool = False, respect_robots: bool = True, freshness=None,
              format: str = "text") -> str | dict:
    """Tavily-style context bundle: search + extract the top pages, trimmed to a
    token budget (approx tokens ~= budget, chars = budget*4). Feed the result to
    an LLM to synthesize the final brief answer.

    freshness: 'day'/'week'/'month'/'year', '7d', or days (int).
    format='json': returns a dict {query, context, budget_tokens, sources:[...]}
    with per-source guard verdicts so agents can trace citations."""
    days = _freshness_days(freshness)
    engines_list, q = resolve_engines(query, "auto")
    client = _get_client(1.0, respect_robots=respect_robots)
    results, errors = await run_engines(client, q, n=max(n + 2, 6), engines_list=engines_list,
                                        fresh=fresh, freshness_days=days)
    results = _apply_freshness(_apply_site_filter(results, query), days)
    merged = merge([results], n, engines_list + [e for e in KEYLESS if e not in engines_list])
    targets = _pick_targets(merged, q, extract_top)
    per_page = max(500, budget * 4 // max(extract_top, 1) - 250)
    extr = await extract_many(client, [r.url for r in targets], max_chars=per_page,
                              concurrency=4, query=q)
    _last_errors.update(errors)
    bundle = fmt_bundle(q, merged, extr, budget_chars=budget * 4)
    if format == "json":
        guard_by_url = {x["url"]: x.get("guard") or {} for x in extr}
        target_urls = {r.url for r in targets}
        sources = [{"title": r.title, "url": r.url, "source": r.source,
                    "date": r.date, "score": round(r.score, 2),
                    "extracted": r.url in target_urls,
                    "guard": (guard_by_url.get(r.url) or {}).get("level")
                             if r.url in target_urls else None}
                   for r in merged]
        return {"query": q, "context": bundle, "budget_tokens": budget,
                "sources": sources}
    return bundle


async def extract(url: str, max_chars: int = 10000, fresh: bool = False,
                  respect_robots: bool = True, guard: bool = True) -> str:
    """Fetch one URL and return clean trimmed text (robots.txt respected by default).

    With guard=True (default), prompt-injection attempts are denied: blocked content
    is replaced by a short denial note instead of the hostile text."""
    client = _get_client(1.0, respect_robots=respect_robots)
    from . import cache as _c
    if not fresh:
        hit = _c.get("ext", url, ttl=604800)
        if hit:
            return hit
    txt = await extract_url(client, url, max_chars=max_chars)
    if txt and guard:
        v = scan(txt, url=url)
        if v.level == "blocked" and guard.POLICY != "off":
            txt = f"[[denied: {v.short()}]]"
    if txt and not fresh:
        _c.set("ext", url, value=txt, ttl=604800)
    if not txt:
        return f"[extract: no content retrieved for {url} (network error, robots.txt, or empty page)]"
    return txt


async def suggest(query: str) -> str:
    """DuckDuckGo autocomplete suggestions (keyless)."""
    client = _get_client(1.0)
    try:
        r = await client.get("https://duckduckgo.com/ac/", params={"q": query, "type": "list"})
        if r.status_code == 200:
            j = r.json()
            return "\n".join(f"- {s}" for s in j[1][:10]) if len(j) > 1 and j[1] else "_no suggestions_"
        return "_suggest unavailable_"
    except Exception as e:
        return f"_suggest error: {type(e).__name__}_"


async def status() -> str:
    """Engine availability, cache size, and last errors."""
    return fmt_status(available(), _last_errors, _cache_info(), [str(os.environ.get("INFOSEEK_INTERVAL", "1.0"))])


async def run(query: str, n: int = 6, engines: str = "auto", fresh: bool = False,
              budget: int = 0, min_interval: float = 1.0) -> str:
    """The one-call entry point - routes by query shape so callers never have
    to pick a function:

    * bare URL                -> extract() the page (archive fallback included)
    * 'ask: ...'              -> ask() context bundle (budget tokens cap it)
    * looks like an error     -> error: research mode (fixes float to the top)
    * looks like a version q. -> compat: research mode
    * anything else           -> search(), formatted
    """
    q = (query or "").strip()
    if not q:
        return "(no query provided)"
    if q.lower().startswith("ask:"):
        return await ask(q[4:].strip(), n=max(3, n), budget=budget or 2500, fresh=fresh)
    if re.match(r"^(?:last30days|last30|social|people):\s*", q, re.I):
        return await last30days(q, budget=budget or 2500, fresh=fresh)
    if re.match(r"^(?:last\s+(?:30\s+days|month)|what\s+(?:are\s+people\s+saying|do\s+people\s+think|are\s+users\s+saying)\s+about)\b", q, re.I):
        return await last30days(q, budget=budget or 2500, fresh=fresh)
    if re.match(r"^https?://\S+$", q):
        return await extract(q, max_chars=budget * 4 if budget else 2000, fresh=fresh)
    if looks_like_error(q):
        res = await search(f"error: {q}", n=n, fresh=fresh)
        return fmt_search([Result(**d) for d in res]) if res else no_results_hint(q, "error:")
    if looks_like_version_query(q):
        res = await search(f"compat: {q}", n=n, fresh=fresh)
        return fmt_search([Result(**d) for d in res]) if res else no_results_hint(q, "compat:")
    res = await search(q, n=n, engines=engines, fresh=fresh, min_interval=min_interval)
    if not res and engines == "auto":
        # real sessions show agents abandon the tool on a bare empty list:
        # escalate once to the wide mix before giving up
        res = await search(q, n=n, engines="wide", fresh=True, min_interval=min_interval)
    return fmt_search([Result(**d) for d in res]) if res else no_results_hint(q, engines)


def help() -> str:
    """Usage cheat sheet - the whole tool surface in ~25 lines. Feed this to
    any agent that is unsure how to call infoseek."""
    return """infoseek - keyless web research (no API keys). One call does the right thing:

  await infoseek.run("rust vs go")          -> formatted search results
  await infoseek.run("https://...")         -> clean page text (archive fallback)
  await infoseek.run("ask: why is X fast")  -> LLM-ready context bundle
  await infoseek.run("last30days: nvidia")  -> social listening & recency brief
  await infoseek.run("UnsupportedArchError: foo")  -> fixes-first error research

Simple sync API (no asyncio needed, returns plain text):
  infoseek.find("q")                        -> search results with urls
  infoseek.research("q", budget=2000)       -> search + extract context bundle
  infoseek.read("https://...")              -> single page clean text
  infoseek.deep("q", budget=2000)           -> multi-angle research brief

Focused async tools:
  await infoseek.search("q", n=10)          -> list of {title,url,snippet,source,...}
  await infoseek.ask("q", budget=2500)      -> context bundle string (format="json" for structured)
  await infoseek.last30days("q", days=30)   -> research what people say (Reddit/HN/Polymarket)
  await infoseek.extract("https://...")     -> page text; blocked pages fall back to the
                                               Wayback Machine; [[denied: ...]] = injection blocked
  infoseek.scan(text)                       -> {ok|suspect|blocked} prompt-injection verdict (sync)

Route to a source by prefix: hn: reddit: so: news: wiki: arxiv: gh: code: pypi:
npm: crates: mdn: yt: polymarket: techmeme: bluesky: stocktwits: wayback: commoncrawl:
swarm: issues: prs: releases: changelog: error: compat:  |  site:<domain> auto-routes
engines="wide" = max coverage.

Diagnostics: await infoseek.status(); await infoseek.selfcheck()
Rules: keep the guard on; pass budget= to ask(); cached calls are ~10 ms (fresh=True bypasses).
Empty results never come back bare: run()/search CLI/MCP auto-retry once with the wide
mix and then return an actionable hint telling you exactly what to try next."""


__all__ = ["run", "help", "no_results_hint", "search", "search_many", "smart_search", "ask", "extract",
           "suggest", "status", "selfcheck", "Result", "last30days",
           "find", "research", "read", "deep",
           "search_error", "search_compat", "changelog",
           "expand_queries", "quality", "normalize_error_message",
           "focus_snippet", "auto_focus"]


def _pick_targets(merged: list, query: str, k: int) -> list:
    """Choose extraction targets: highest merged score, boosted by query-term presence
    in snippet/title, penalized for redirect wrappers; one per domain."""
    terms = [x for x in re.split(r"\W+", query.lower()) if len(x) > 2]
    scored = []
    for r in merged:
        s = r.score
        if "news.google.com" in r.url:
            s -= 3.0
        blob = ((r.snippet or "") + " " + r.title).lower()
        if terms and any(x in blob for x in terms):
            s += 2.0
        scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    seen: set[str] = set()
    out = []
    for _, r in scored:
        dom = urlparse(r.url).netloc
        if dom in seen:
            continue
        seen.add(dom)
        out.append(r)
        if len(out) >= k:
            break
    return out

