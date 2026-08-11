"""Content extraction: domain-aware fast paths + trafilatura + heuristic fallback."""
import asyncio, re
from urllib.parse import urlparse, unquote, quote
import httpx, trafilatura
from bs4 import BeautifulSoup
from .net import PoliteClient

def _heuristic(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body
    if main is None:
        return ""
    scored = []
    for p in main.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if len(txt) < 80:
            continue
        link_len = sum(len(a.get_text(" ", strip=True)) for a in p.find_all("a"))
        if link_len / max(len(txt), 1) > 0.4:
            continue
        scored.append((len(txt), txt))
    scored.sort(reverse=True)
    return "\n\n".join(t for _, t in scored[:8])

def _meta_desc(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    m = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    return (m.get("content") or "").strip() if m else ""

def _trim(text: str, max_chars: int) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    i = cut.rfind(".")
    if i > max_chars * 0.5:
        return cut[: i + 1]
    j = cut.rfind(" ")
    return cut[:j] + " …" if j > max_chars * 0.5 else cut + " …"

async def _github_readme(client: PoliteClient, url: str, max_chars: int) -> str:
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) < 2:
        return ""
    owner, repo = parts[0], parts[1]
    r = await client.get(f"https://api.github.com/repos/{owner}/{repo}/readme",
                         headers={"Accept": "application/vnd.github.raw+json", "User-Agent": "infoseek"})
    if r.status_code != 200:
        return ""
    txt = r.text
    if r.headers.get("content-type", "").startswith("application/json"):
        try:
            import base64
            txt = base64.b64decode(r.json().get("content", "")).decode("utf-8", "ignore")
        except Exception:
            return ""
    txt = re.sub(r"(?m)^\s*(\.\.|:).*$\n?", "", txt)  # drop RST directive/field lines
    return _trim(txt, max_chars)

async def _wikipedia(client: PoliteClient, url: str, max_chars: int) -> str:
    title = unquote(urlparse(url).path.split("/")[-1]).replace("_", " ")
    r = await client.get("https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title, safe=" ()"))
    if r.status_code != 200:
        return ""
    j = r.json()
    txt = j.get("extract") or ""
    head = f"{j.get('title','')} — {j.get('description','')}\n"
    return _trim(head + txt, max_chars)

async def _hn_item(client: PoliteClient, url: str, max_chars: int) -> str:
    m = re.search(r"item\?id=(\d+)", url)
    if not m:
        return ""
    r = await client.get(f"https://hn.algolia.com/api/v1/items/{m.group(1)}")
    if r.status_code != 200:
        return ""
    j = r.json()
    def plain(s):
        return BeautifulSoup(s or "", "lxml").get_text(" ", strip=True)
    parts = [j.get("title") or "", plain(j.get("text"))]
    for ch in (j.get("children") or [])[:12]:
        t = plain(ch.get("text"))
        if t:
            parts.append(f"· {ch.get('author','')}: {t}")
    return _trim(" | ".join(parts), max_chars)

async def _reddit(client: PoliteClient, url: str, max_chars: int) -> str:
    m = re.search(r"/comments/([a-z0-9]+)", url)
    if not m:
        return ""
    try:
        r = await client.get("https://api.pullpush.io/reddit/search/submission/",
                             params={"ids": m.group(1)}, retries=0)
        if r.status_code != 200:
            return "[reddit: live API blocked; use the search snippets]"
    except Exception:
        return "[reddit: live API unavailable; use the search snippets]"
    data = (r.json().get("data") or [])
    if not data:
        return "[reddit: not found via API]"
    x = data[0]
    return _trim(f"{x.get('title','')} — r/{x.get('subreddit','')}\n\n{x.get('selftext') or '(link post)'}", max_chars)

async def extract_url(client: PoliteClient, url: str, max_chars: int = 2000) -> str:
    """Fetch and extract clean text from one URL (respects robots.txt unless disabled)."""
    host = urlparse(url).netloc.lower()
    # Official-API fast paths: robots.txt governs web pages, not these APIs.
    if "github.com" in host:
        return await _github_readme(client, url, max_chars)
    if "wikipedia.org" in host:
        return await _wikipedia(client, url, max_chars)
    if "news.ycombinator.com" in host:
        return await _hn_item(client, url, max_chars)
    if "reddit.com" in host:
        return await _reddit(client, url, max_chars)
    if not await client.allowed(url):
        return f"[skipped: robots.txt of {host} disallows this fetch]"
    try:
        r = await client.get(url)
    except httpx.HTTPError:
        return ""
    if r.status_code != 200:
        return ""
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        return ""
    html = r.text
    txt = trafilatura.extract(html, url=url, include_comments=False, include_tables=False,
                              include_formatting=False, include_links=False, include_images=False,
                              favor_precision=True)
    if not txt or len(txt) < 120:
        txt = trafilatura.extract(html, url=url, include_comments=False, include_tables=False,
                                  include_formatting=False, include_links=False, include_images=False)
    if not txt:
        txt = _heuristic(html)
    if not txt:
        txt = _meta_desc(html)
    return _trim(txt, max_chars)

async def extract_many(client: PoliteClient, urls: list[str], max_chars: int = 1200,
                       concurrency: int = 3, query: str | None = None) -> list[dict]:
    """Extract several URLs in parallel. If query is given, keep only the sentences
    most relevant to it (see relevant_sentences) — content that really matters."""
    sem = asyncio.Semaphore(concurrency)
    fetch_budget = max(6000, max_chars * 2)

    async def one(url: str) -> dict:
        async with sem:
            try:
                text = await extract_url(client, url, max_chars=fetch_budget)
                if text and query:
                    text = relevant_sentences(text, query, max_chars)
                from . import guard
                verdict = guard.scan(text or "", url=url)
                return {"url": url, "text": text, "ok": bool(text),
                        "guard": {"level": verdict.level, "score": verdict.score,
                                  "reasons": list(verdict.reasons)}}
            except Exception as e:
                return {"url": url, "text": "", "ok": False, "error": str(e)[:80],
                        "guard": {"level": "error", "score": 0, "reasons": [str(e)[:40]]}}

    return list(await asyncio.gather(*[one(u) for u in urls]))


_STOP = set("""a an and are as at be been but by for from has have in is it its not of on or that the their them they this to was were will with you your""".split())


def relevant_sentences(text: str, query: str, max_chars: int = 1200) -> str:
    """Keep only the sentences that matter for the query: term overlap, phrase hits,
    lead-position bonus, then order them as they appear. Token-lean by construction."""
    terms = [t for t in re.split(r"\W+", query.lower()) if t not in _STOP and len(t) > 2]
    text = re.sub(r"\s+", " ", text or "").strip()
    if not terms or not text:
        return _trim(text, max_chars)
    sents = re.split(r"(?<=[.!?])\s+", text)
    scored = []
    for i, s in enumerate(sents):
        low = s.lower()
        hits = sum(low.count(t) for t in terms)
        if hits == 0:
            continue
        score = hits * 2 + (2.0 if len(terms) == 1 else 0) + (1.5 if i < 3 else 0)
        scored.append((score, i, s))
    if not scored:
        return _trim(text, max_chars)
    scored.sort(key=lambda x: -x[0])
    picked = sorted(scored[:4], key=lambda x: x[1])
    return _trim(" ".join(s for _, _, s in picked), max_chars)
