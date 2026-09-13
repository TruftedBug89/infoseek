"""infoseek.simple — the easy API: one line, no asyncio, no keys, text in / text out.

Built for AI agents, including small and local models that cannot reliably write
`async def` / `await` code. Every function takes a string (or URL) and returns a
string. Nothing to configure, nothing to await, nothing to import twice.

    import infoseek

    infoseek.find("rust vs go 2026")             # search   -> ranked results + urls
    infoseek.research("why is redis fast")        # answer   -> context to answer from
    infoseek.read("https://example.com/article")  # page     -> clean text
    infoseek.deep("llm quantization tradeoffs")   # brief    -> multi-angle research
    infoseek.help()                               # usage card (call it to re-learn)

Pick the tool by what you want back:

| you want                        | call                |
|---------------------------------|---------------------|
| links / sources to cite         | `find()`            |
| facts to answer a question       | `research()`        |
| the text of one known page       | `read()`            |
| a broader, slower, multi-angle brief | `deep()`        |

Errors never raise: they come back as a short `[[...]]` note, so a model can
recover or rephrase instead of crashing the turn.

The full async API (`search`, `ask`, `extract`, `suggest`, `status`,
`selfcheck`) stays unchanged for callers that want structured data.
"""
from __future__ import annotations

import asyncio
from concurrent import futures

from .rank import Result, dedupe, normalize_url

CARD = """infoseek — keyless web research. Simple API (call it directly, no await):

  infoseek.find("query")              search -> ranked results with urls
  infoseek.research("question")        search + read -> context to answer from
  infoseek.read("https://url")         one page -> clean text
  infoseek.deep("topic")               multi-angle research brief (slower)

All return plain text. Errors come back as [[...]] notes, never exceptions.
budget= caps size in tokens (~4 chars each). fresh=True bypasses the cache.

Focus a source with a prefix: hn: reddit: so: news: wiki: arxiv: openalex:
pubmed: doi: gh: code: lobsters: marginalia: ddg:   — or site:domain.com

Async API for structured data: await infoseek.search/ask/extract/suggest/status
CLI: infoseek find|research|read|deep|ask|extract|scan|suggest|status|selfcheck
"""


def _run(coro):
    """Run a coroutine from sync code — works even inside a running event loop
    (e.g. an async agent calling the sync API by mistake)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def help() -> str:
    """Return the usage card. Call this first if you are unsure how to use infoseek."""
    return CARD


def find(query: str, n: int = 6, engines: str = "auto", fresh: bool = False) -> str:
    """Search the web. Returns formatted results (title, url, snippet) as text.

    query: what to look for. n: max results. engines: 'auto' or a comma list.
    fresh: bypass the 30-minute cache."""
    from . import search as _search
    from .format import fmt_search

    try:
        rows = _run(_search(query, n=n, engines=engines, fresh=fresh))
        if not rows:
            return "[[no results — try fewer words or a prefix like news: or hn:]]"
        return fmt_search([Result(**d) for d in rows])
    except Exception as e:  # never raise at the model
        return f"[[find error: {type(e).__name__}: {e}]]"


def research(query: str, budget: int = 1500, fresh: bool = False) -> str:
    """Search, read the best pages, and return the context needed to answer.

    This is the one to use when you need facts, not links. Feed the result to
    the model and let it write the answer. budget: approx output tokens."""
    from . import ask as _ask

    try:
        out = _run(_ask(query, n=5, extract_top=2, budget=budget, fresh=fresh))
        return out or "[[no results — try fewer words or a prefix like news: or hn:]]"
    except Exception as e:
        return f"[[research error: {type(e).__name__}: {e}]]"


def read(url: str, max_chars: int = 10000, fresh: bool = False,
         markdown: bool = True, raw: bool = False) -> str:
    """Fetch one URL and return its clean text/markdown.

    Prompt-injection attempts are denied: hostile pages come back as
    [[denied: ...]] instead of the injected instructions."""
    from . import extract as _extract

    try:
        try:
            coro = _extract(url, max_chars=max_chars, fresh=fresh, markdown=markdown, raw=raw)
        except TypeError:
            coro = _extract(url, max_chars=max_chars, fresh=fresh)
        out = _run(coro)
        return out or "[[empty page]]"
    except Exception as e:
        return f"[[read error: {type(e).__name__}: {e}]]"


def deep(query: str, budget: int = 3000, angles: int = 3, fresh: bool = False) -> str:
    """Multi-angle research brief: expands the query, searches each angle, reads the
    best pages, and returns one citable brief. Slower than research(), broader.

    budget: approx output tokens. angles: how many query variants (1-5)."""
    try:
        out = _run(_deep(query, budget=budget, angles=max(1, min(angles, 5)), fresh=fresh))
        return out or "[[no results — try fewer words or a prefix like news: or hn:]]"
    except Exception as e:
        return f"[[deep error: {type(e).__name__}: {e}]]"


# --------------------------------------------------------------------------- #
# deep() internals
# --------------------------------------------------------------------------- #

def _pick(results: list[Result], query: str, k: int) -> list[Result]:
    """Best extraction targets: query terms in title/snippet, one per domain."""
    terms = [t for t in query.lower().split() if len(t) > 2]
    scored = []
    for r in results:
        s = r.score
        blob = f"{r.title} {r.snippet or ''}".lower()
        if terms and any(t in blob for t in terms):
            s += 2.0
        if "news.google.com" in r.url:
            s -= 3.0
        scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _, r in scored:
        dom = r.url.split("/")[2] if "://" in r.url else r.url
        if dom in seen:
            continue
        seen.add(dom)
        out.append(r)
        if len(out) >= k:
            break
    return out


async def _deep(query: str, budget: int = 3000, angles: int = 3, fresh: bool = False) -> str:
    from . import search as _search, suggest as _suggest
    from .extract import extract_many
    from .format import fmt_search
    from .net import PoliteClient

    # 1. expand into angles: the query itself + cheap autocomplete variants.
    #    Autocomplete is thin for long questions, so fall back to generic lenses —
    #    deep() must always look at a topic from more than one side.
    queries = [query]
    if angles > 1:
        try:
            ideas = [ln[2:].strip() for ln in (await _suggest(query)).splitlines()
                     if ln.startswith("- ")]
        except Exception:
            ideas = []
        if len(ideas) < 2:
            ideas = [f"{query} {lens}" for lens in ("explained", "pros and cons",
                                                    "best practices")]
        for idea in ideas:
            if len(queries) >= angles:
                break
            if idea and idea.lower() != query.lower():
                queries.append(idea)

    # 2. search every angle at once, then collapse duplicates across angles
    batches = await asyncio.gather(
        *[_search(q, n=4, fresh=fresh) for q in queries], return_exceptions=True
    )
    seen: set[str] = set()
    pooled: list[Result] = []
    for rows in batches:
        if isinstance(rows, BaseException) or not rows:
            continue
        for d in rows:
            r = Result(**d)
            key = normalize_url(r.url)
            if key in seen:
                continue
            seen.add(key)
            pooled.append(r)
    if not pooled:
        return "[[no results]]"
    merged = dedupe(pooled)[: max(6, angles * 2)]

    # 3. read the strongest pages (one per domain)
    targets = _pick(merged, query, min(3, len(merged)))
    client = PoliteClient(min_interval=1.4)
    try:
        per_page = max(500, budget * 4 // max(len(targets), 1) - 300)
        extr = await extract_many(client, [r.url for r in targets],
                                  max_chars=per_page, concurrency=3, query=query)
    finally:
        await client.close()

    # 4. brief: angles, citable sources, then the guard-screened excerpts
    parts = [f"QUERY: {query}", f"ANGLES: {' | '.join(queries)}", "", "## SOURCES",
             fmt_search(merged)]
    ok = [x for x in extr if x.get("ok") and x.get("text")]
    if ok:
        parts += ["", "## BRIEF (verbatim excerpts, trimmed, injection-screened)"]
        for x in ok:
            parts += ["", f"### {x['url']}", x["text"]]
    out = "\n".join(parts)
    return out[: budget * 4]


__all__ = ["find", "research", "read", "deep", "help"]
