"""Search engines. Every engine is keyless by default; brave/serper/searxng activate
only when the matching env vars are present (checked at call time, never logged)."""
import os, re
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, unquote, quote
from bs4 import BeautifulSoup
from .net import PoliteClient, UAS
from .rank import Result, clean

ENGINE_NAMES = ["ddg","marginalia","hn","lobsters","so","news","wiki","arxiv","openalex","pubmed","crossref","wikidata","gh","code","reddit","brave","serper","searxng"]

async def ddg(c, q, n):
    out = await _ddg_fetch(c, q, n)
    if out:
        return out, None
    # Possible UA challenge: retry once with a different browser UA on a fresh client.
    try:
        c2 = PoliteClient(min_interval=c.min_interval, ua=next(u for u in UAS if u != c.ua))
        try:
            out = await _ddg_fetch(c2, q, n)
            return out, (None if out else "ddg: no results parsed")
        finally:
            await c2.close()
    except Exception:
        return [], "ddg: no results parsed"

async def _ddg_fetch(c, q, n):
    r = await c.post("https://html.duckduckgo.com/html/", data={"q": q})
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    if not soup.select_one(".result") and ("anomaly" in r.text.lower() or "challenge" in r.text.lower()):
        return []
    out = []
    for i, x in enumerate(soup.select(".result")[:n]):
        if "result--ad" in (x.get("class") or []):
            continue
        a = x.select_one(".result__a")
        if not a:
            continue
        href = a.get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
        if "duckduckgo.com/y.js" in href or "ad_domain=" in href or "ad_provider=" in href:
            continue  # sponsored result
        sn = x.select_one(".result__snippet")
        out.append(Result(title=a.get_text(" ", strip=True), url=href,
                          snippet=clean(sn.get_text(" ", strip=True)) if sn else "", source="ddg", rank=i))
    return out

async def marginalia(c, q, n):
    r = await c.get("https://old-search.marginalia.nu/search", params={"query": q})
    if r.status_code != 200:
        return [], f"marginalia http {r.status_code}"
    if "barraged by queries" in r.text or "<title>Error</title>" in r.text:
        return [], "marginalia: rate-limited (search engine busy, try again in a minute)"
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for i, sec in enumerate(soup.select("section.search-result")[:n]):
        a = sec.select_one("a.title")
        url_el = sec.select_one("div.url")
        desc = sec.select_one("p.description")
        if not a:
            continue
        out.append(Result(title=a.get_text(" ", strip=True), url=url_el.get_text(" ", strip=True) if url_el else a.get("href", ""),
                          snippet=clean(desc.get_text(" ", strip=True)) if desc else "", source="marginalia", rank=i))
    return out, (None if out else "marginalia: no results parsed")

async def hn(c, q, n):
    r = await c.get("https://hn.algolia.com/api/v1/search",
                    params={"query": q, "hitsPerPage": n, "tags": "story"})
    if r.status_code != 200:
        return [], f"hn http {r.status_code}"
    out = []
    for i, h in enumerate(r.json().get("hits", [])[:n]):
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID','')}"
        pts = h.get("points") or 0
        cm = h.get("num_comments") or 0
        d = (h.get("created_at") or "")[:10]
        out.append(Result(title=h.get("title") or "", url=url, source="hn", rank=i,
                          extra=f"▲{pts} · {cm}💬 · {h.get('author','')}", date=d,
                          snippet=clean(BeautifulSoup(h.get("story_text") or "", "lxml").get_text(" ", strip=True) or f"Discuss on Hacker News ({cm} comments)", 140)))
    return out, (None if out else "hn: no hits")

async def lobsters(c, q, n):
    r = await c.get("https://lobste.rs/newest.json", params={"count": min(max(n * 4, 10), 100)})
    if r.status_code != 200:
        return [], f"lobsters http {r.status_code}"
    tokens = [t for t in re.split(r"\W+", q.lower()) if len(t) > 2]
    out = []
    for i, it in enumerate(r.json() or []):
        title = it.get("title", "")
        if tokens and not any(t in title.lower() for t in tokens[:3]):
            continue
        out.append(Result(title=title, url=it.get("url") or f"https://lobste.rs{it.get('short_id_url','')}",
                          source="lobsters", rank=i, date=(it.get("created_at") or "")[:10],
                          extra=f"{it.get('comment_count',0)}💬 · {it.get('tags',[])[:2]}",
                          snippet=clean(it.get("description") or "", 140)))
        if len(out) >= n:
            break
    return out, (None if out else "lobsters: no title match")

async def so(c, q, n, site="stackoverflow"):
    r = await c.get("https://api.stackexchange.com/2.3/search/advanced",
                    params={"site": site, "q": q, "pagesize": n, "order": "desc", "sort": "relevance"})
    if r.status_code != 200:
        return [], f"so http {r.status_code}"
    out = []
    for i, it in enumerate(r.json().get("items", [])[:n]):
        tags = ",".join((it.get("tags") or [])[:3])
        ans = "✓" if it.get("is_answered") else ""
        out.append(Result(title=it.get("title", ""), url=it.get("link", ""), source="so", rank=i,
                          extra=f"{ans} {it.get('score',0)} · {it.get('answer_count',0)} answers · [{tags}]",
                          date=datetime.fromtimestamp(it.get("creation_date", 0), tz=timezone.utc).strftime("%Y-%m-%d")))
    return out, (None if out else "so: no items")

async def news(c, q, n):
    r = await c.get("https://news.google.com/rss/search",
                    params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    if r.status_code != 200:
        return [], f"news http {r.status_code}"
    soup = BeautifulSoup(r.text, "lxml-xml")
    out = []
    for i, it in enumerate(soup.find_all("item")[:n]):
        title = it.find("title").get_text(" ", strip=True) if it.find("title") else ""
        src = it.find("source")
        src_name = src.get_text(" ", strip=True) if src else ""
        if src_name and title.endswith("- " + src_name):
            title = title[: -(len(src_name) + 2)].strip()
        link = it.find("link")
        desc = it.find("description")
        desc_txt = BeautifulSoup(desc.get_text(" ", strip=True) if desc else "", "lxml").get_text(" ", strip=True)
        if title and desc_txt.startswith(title):
            desc_txt = desc_txt[len(title):].strip()
        out.append(Result(title=title, url=link.get_text(strip=True) if link else "",
                          source="news", rank=i, extra=f"[{src_name}]",
                          date=(it.find("pubDate").get_text(strip=True) if it.find("pubDate") else "")[:16],
                          snippet=clean(desc_txt, 150)))
    return out, (None if out else "news: no items")

async def wiki(c, q, n):
    r = await c.get("https://en.wikipedia.org/w/api.php",
                    params={"action": "query", "list": "search", "srsearch": q, "srlimit": n, "format": "json"})
    if r.status_code != 200:
        return [], f"wiki http {r.status_code}"
    out = []
    for i, it in enumerate(r.json().get("query", {}).get("search", [])[:n]):
        title = it.get("title", "")
        sn = BeautifulSoup(it.get("snippet", ""), "lxml").get_text(" ", strip=True)
        out.append(Result(title=title, url=f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                          source="wiki", rank=i, extra="Wikipedia", snippet=clean(sn, 160)))
    return out, (None if out else "wiki: no hits")

async def arxiv(c, q, n):
    r = await c.get("https://export.arxiv.org/api/query",
                    params={"search_query": f'all:"{q}"', "max_results": n, "sortBy": "relevance"}, timeout=30)
    if r.status_code != 200:
        return [], f"arxiv http {r.status_code}"
    soup = BeautifulSoup(r.text, "lxml-xml")
    out = []
    for i, e in enumerate(soup.find_all("entry")[:n]):
        title = e.find("title").get_text(" ", strip=True) if e.find("title") else ""
        authors = ", ".join(a.get_text(strip=True) for a in e.find_all("name")[:3])
        if e.find_all("name") and len(e.find_all("name")) > 3: authors += " et al."
        summ = e.find("summary").get_text(" ", strip=True) if e.find("summary") else ""
        out.append(Result(title=title, url=e.find("id").get_text(strip=True) if e.find("id") else "",
                          source="arxiv", rank=i, extra=f"{authors}", date=(e.find("published").get_text(strip=True)[:10] if e.find("published") else ""),
                          snippet=clean(summ, 150)))
    return out, (None if out else "arxiv: no entries")

async def gh(c, q, n):
    r = await c.get("https://api.github.com/search/repositories",
                    params={"q": q, "per_page": n},
                    headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    if r.status_code != 200:
        return [], f"github http {r.status_code}"
    out = []
    for i, it in enumerate(r.json().get("items", [])[:n]):
        desc = it.get("description") or ""
        lang = it.get("language") or ""
        out.append(Result(title=it.get("full_name", ""), url=it.get("html_url", ""), source="gh", rank=i,
                          extra=f"★{it.get('stargazers_count',0)} · {lang} · updated {it.get('updated_at','')[:10]}",
                          snippet=clean(desc, 150)))
    return out, (None if out else "gh: no items")

async def code(c, q, n):
    r = await c.get("https://grep.app/api/search", params={"q": q})
    if r.status_code != 200:
        return [], f"grep.app http {r.status_code}"
    hits = ((r.json().get("hits") or {}).get("hits")) or []
    out = []
    for i, h in enumerate(hits[:n]):
        repo, path = h.get("repo", ""), h.get("path", "")
        raw = h.get("content")
        if isinstance(raw, dict):
            raw = " ".join(str(v) if isinstance(v, str) else " ".join(v) for v in raw.values())
        elif isinstance(raw, list):
            raw = " ".join(str(v) for v in raw)
        content = re.sub(r"<[^>]+>", "", str(raw or "")).strip()
        content = re.sub(r"(?:^|\s)\d+(?=\s*[a-zA-Z])", " ", content)  # drop line numbers
        out.append(Result(title=f"{repo} · {path}", url=f"https://grep.app/search?q={quote(q)}&filter[repo][0]={quote(repo)}",
                          source="code", rank=i, snippet=clean(content, 150), extra=repo))
    return out, (None if out else "grep.app: no hits")

async def reddit(c, q, n):
    """old.reddit HTML search (server-rendered, keyless). Falls back to pullpush.io API."""
    try:
        r = await c.get("https://old.reddit.com/search", params={"q": q, "sort": "relevance"})
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            out = []
            for i, item in enumerate(soup.select("div.search-result-link")[:n]):
                a = item.select_one("a.search-title")
                if not a:
                    continue
                url = a.get("href", "")
                if url.startswith("/"):
                    url = "https://www.reddit.com" + url
                elif "old.reddit.com" in url:
                    url = url.replace("old.reddit.com", "www.reddit.com")
                meta = item.select_one(".search-result-meta")
                md = item.select_one(".md")
                snip = md.get_text(" ", strip=True) if md else ""
                out.append(Result(title=a.get_text(" ", strip=True), url=url, source="reddit", rank=i,
                                  extra=meta.get_text(" ", strip=True) if meta else "",
                                  snippet=clean(snip, 150)))
            if out:
                return out, None
        pp = await c.get("https://api.pullpush.io/reddit/search/submission/",
                         params={"q": q, "size": n}, retries=0)
        if pp.status_code == 200:
            rows = (pp.json().get("data") or [])[:n]
            return [Result(title=x.get("title", ""),
                           url=f"https://www.reddit.com{x.get('permalink','')}",
                           source="reddit", rank=i,
                           extra=f"r/{x.get('subreddit','')} · ▲{x.get('score',0)} · {x.get('num_comments',0)}💬 · {x.get('author','')}",
                           snippet=clean((x.get('selftext') or '').strip(), 150))
                    for i, x in enumerate(rows) if x.get("title")], None if rows else "pullpush: no data"
        return [], f"reddit http {r.status_code}"
    except Exception as e:
        return [], f"reddit: {type(e).__name__}: {str(e)[:80]}"

async def brave(c, q, n):
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return [], "no BRAVE_API_KEY"
    r = await c.get("https://api.search.brave.com/res/v1/web/search",
                    params={"q": q, "count": n}, headers={"X-Subscription-Token": key, "Accept": "application/json"})
    if r.status_code != 200:
        return [], f"brave http {r.status_code}"
    out = []
    for i, it in enumerate((r.json().get("web") or {}).get("results", [])[:n]):
        out.append(Result(title=it.get("title", ""), url=it.get("url", ""), source="brave", rank=i,
                          extra=(it.get("age") or ""), snippet=clean(it.get("description") or "", 150)))
    return out, (None if out else "brave: no results")

async def serper(c, q, n):
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        return [], "no SERPER_API_KEY"
    r = await c.post("https://google.serper.dev/search", json={"q": q, "num": n},
                     headers={"X-API-KEY": key})
    if r.status_code != 200:
        return [], f"serper http {r.status_code}"
    out = []
    for i, it in enumerate((r.json().get("organic") or [])[:n]):
        out.append(Result(title=it.get("title", ""), url=it.get("link", ""), source="serper", rank=i,
                          snippet=clean(it.get("snippet") or "", 150)))
    return out, (None if out else "serper: no results")

async def searxng(c, q, n):
    base = os.environ.get("SEARXNG_URL")
    if not base:
        return [], "no SEARXNG_URL"
    r = await c.get(base.rstrip("/") + "/search", params={"q": q, "format": "json"})
    if r.status_code != 200:
        return [], f"searxng http {r.status_code}"
    out = []
    for i, it in enumerate((r.json().get("results") or [])[:n]):
        out.append(Result(title=it.get("title", ""), url=it.get("url", ""), source="searxng", rank=i,
                          snippet=clean(it.get("content") or "", 150)))
    return out, (None if out else "searxng: no results")

async def openalex(c, q, n):
    """OpenAlex works search (scholarly corpus: journals, preprints, books). Keyless."""
    r = await c.get("https://api.openalex.org/works",
                    params={"search": q, "per-page": n, "sort": "relevance_score:desc",
                            "select": "title,doi,publication_year,cited_by_count,primary_location,open_access,abstract_inverted_index"})
    if r.status_code != 200:
        return [], f"openalex http {r.status_code}"
    out = []
    j = r.json()
    for i, it in enumerate((j.get("results") or [])[:n]):
        title = it.get("title") or ""
        doi = it.get("doi") or ""
        loc = (it.get("primary_location") or {}) or {}
        src = (loc.get("source") or {}) or {}
        venue = src.get("display_name") or ""
        url = doi or it.get("id") or ""
        inv = it.get("abstract_inverted_index")
        snippet = ""
        if inv:  # reconstruct abstract from inverted index
            pos = []
            for word, idxs in inv.items():
                for ix in idxs:
                    pos.append((ix, word))
            pos.sort()
            snippet = " ".join(w for _, w in pos[:60])
        out.append(Result(title=title, url=f"https://doi.org/{doi.replace('https://doi.org/','')}" if doi else url,
                          source="openalex", rank=i,
                          extra=f"citations:{it.get('cited_by_count',0)} · {venue}",
                          date=str(it.get("publication_year") or ""),
                          snippet=clean(snippet, 150)))
    return out, (None if out else "openalex: no hits")

async def wikidata(c, q, n):
    """Wikidata entity search: structured facts with stable Q-IDs. Keyless."""
    r = await c.get("https://www.wikidata.org/w/api.php",
                    params={"action": "wbsearchentities", "search": q, "language": "en",
                            "format": "json", "limit": n})
    if r.status_code != 200:
        return [], f"wikidata http {r.status_code}"
    out = []
    for i, it in enumerate((r.json().get("search") or [])[:n]):
        label = it.get("label") or it.get("id") or ""
        desc = it.get("description") or ""
        out.append(Result(title=label, url=it.get("concepturi") or f"https://www.wikidata.org/wiki/{it.get('id','')}",
                          source="wikidata", rank=i, extra=f"wikidata:{it.get('id','')}",
                          snippet=clean(desc, 150)))
    return out, (None if out else "wikidata: no entities")

async def pubmed(c, q, n):
    """PubMed biomedical literature via NCBI E-utilities. Keyless (3 req/s is fine)."""
    r = await c.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                    params={"db": "pubmed", "term": q, "retmax": n, "retmode": "json"})
    if r.status_code != 200:
        return [], f"pubmed http {r.status_code}"
    ids = (r.json().get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return [], "pubmed: no hits"
    r2 = await c.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                     params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"})
    if r2.status_code != 200:
        return [], f"pubmed summary http {r2.status_code}"
    res = (r2.json().get("result") or {})
    out = []
    for i, uid in enumerate(ids[:n]):
        item = res.get(uid) or {}
        if not item:
            continue
        authors = ", ".join(a["name"] for a in (item.get("authors") or [])[:3])
        if len(item.get("authors") or []) > 3:
            authors += " et al."
        doi = ""
        for aid in item.get("articleids") or []:
            if aid.get("idtype") == "doi":
                doi = aid.get("value", "")
                break
        out.append(Result(title=item.get("title") or "", url=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                          source="pubmed", rank=i, extra=authors,
                          date=str(item.get("pubdate") or "")[:10],
                          snippet=clean(f"[{item.get('fulljournalname','')}] {doi}", 150)))
    return out, (None if out else "pubmed: no summaries")

async def crossref(c, q, n):
    """Crossref DOI registry: scholarly works incl. preprints, with citation counts."""
    r = await c.get("https://api.crossref.org/works",
                    params={"query": q, "rows": n,
                            "select": "title,DOI,URL,container-title,published,is-referenced-by-count,author"})
    if r.status_code != 200:
        return [], f"crossref http {r.status_code}"
    out = []
    for i, it in enumerate(((r.json().get("message") or {}).get("items") or [])[:n]):
        title = (it.get("title") or [""])[0]
        doi = it.get("DOI") or ""
        venue = (it.get("container-title") or [""])[0]
        year = ""
        pub = it.get("published") or {}
        dp = pub.get("date-parts") or [[None]]
        if dp and dp[0]:
            year = str(dp[0][0] or "")
        auth = ""
        au = it.get("author") or []
        if au:
            auth = au[0].get("family", "") + (" et al." if len(au) > 1 else "")
        out.append(Result(title=title, url=f"https://doi.org/{doi}" if doi else (it.get("URL") or ""),
                          source="crossref", rank=i,
                          extra=f"citations:{it.get('is-referenced-by-count',0)} · {venue} · {auth}",
                          date=year, snippet=""))
    return out, (None if out else "crossref: no items")

REGISTRY = {
    "ddg": ddg, "marginalia": marginalia, "hn": hn, "lobsters": lobsters, "so": so,
    "news": news, "wiki": wiki, "arxiv": arxiv, "openalex": openalex,
    "pubmed": pubmed, "crossref": crossref, "wikidata": wikidata, "gh": gh, "code": code,
    "reddit": reddit, "brave": brave, "serper": serper, "searxng": searxng,
}

SITE_MAP = {
    "reddit.com": "reddit", "www.reddit.com": "reddit", "old.reddit.com": "reddit",
    "news.ycombinator.com": "hn", "stackoverflow.com": "so", "stackexchange.com": "so",
    "github.com": "gh", "arxiv.org": "arxiv", "en.wikipedia.org": "wiki", "wikipedia.org": "wiki",
    "old-search.marginalia.nu": "marginalia", "grep.app": "code",
}

KEYLESS = {"ddg","marginalia","hn","lobsters","so","news","wiki","arxiv","openalex","pubmed","crossref","wikidata","gh","code","reddit"}

def available() -> list[str]:
    """Keyless engines always listed; keyed engines only when env present."""
    out = [e for e in ENGINE_NAMES if e in KEYLESS]
    for env, eng in [("BRAVE_API_KEY","brave"), ("SERPER_API_KEY","serper"), ("SEARXNG_URL","searxng")]:
        if env in os.environ:
            out.append(eng)
    return out

async def run_engines(client: PoliteClient, query: str, n: int, engines_list: list[str],
                      ttl: float = 1800, fresh: bool = False) -> tuple[list[Result], dict]:
    """Run engines concurrently, cache per-engine JSON, collect errors by engine name."""
    import asyncio as _a, json as _json
    from . import cache as _cache
    results: list[Result] = []
    errors: dict[str, str] = {}

    FETCH = max(min(n, 12), 4)   # fetch a bit extra; cache key is n-independent

    async def one(name: str):
        cached = None if fresh else _cache.get("eng", query, name, ttl=ttl)
        if cached is not None:
            try:
                rows = _json.loads(cached)
            except Exception:
                rows = None
            if rows == "__err__":
                return name, [], "cached (recent failure, retry in ~90s)"
            if isinstance(rows, list):
                return name, [Result(**row) for row in rows][:n], None
        try:
            res, err = await REGISTRY[name](client, query, FETCH)
        except Exception as exc:
            res, err = [], f"{type(exc).__name__}: {str(exc)[:90]}"
        if not fresh:
            try:
                if res:
                    _cache.set("eng", query, name,
                               value=_json.dumps([r.__dict__ for r in res[:12]], default=str), ttl=ttl)
                else:
                    _cache.set("eng", query, name, value='"__err__"', ttl=90)
            except Exception:
                pass
        return name, res[:n], err

    tasks = [_a.ensure_future(one(nm)) for nm in engines_list]
    for fut in _a.as_completed(tasks):
        try:
            name, res, err = await fut
        except Exception as exc:
            name, res, err = "?", [], f"{type(exc).__name__}: {str(exc)[:90]}"
        if res:
            results.extend(res)
        if err:
            errors[name] = err
    return results, errors

ENGINE_NAMES = ["ddg","marginalia","hn","lobsters","so","news","wiki","arxiv","openalex","pubmed","crossref","wikidata","gh","code","reddit","brave","serper","searxng"]

async def ddg(c, q, n):
    out = await _ddg_fetch(c, q, n)
    if out:
        return out, None
    # Possible UA challenge: retry once with a different browser UA on a fresh client.
    try:
        c2 = PoliteClient(min_interval=c.min_interval, ua=next(u for u in UAS if u != c.ua))
        try:
            out = await _ddg_fetch(c2, q, n)
            return out, (None if out else "ddg: no results parsed")
        finally:
            await c2.close()
    except Exception:
        return [], "ddg: no results parsed"

async def _ddg_fetch(c, q, n):
    r = await c.post("https://html.duckduckgo.com/html/", data={"q": q})
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    if not soup.select_one(".result") and ("anomaly" in r.text.lower() or "challenge" in r.text.lower()):
        return []
    out = []
    for i, x in enumerate(soup.select(".result")[:n]):
        if "result--ad" in (x.get("class") or []):
            continue
        a = x.select_one(".result__a")
        if not a:
            continue
        href = a.get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
        if "duckduckgo.com/y.js" in href or "ad_domain=" in href or "ad_provider=" in href:
            continue  # sponsored result
        sn = x.select_one(".result__snippet")
        out.append(Result(title=a.get_text(" ", strip=True), url=href,
                          snippet=clean(sn.get_text(" ", strip=True)) if sn else "", source="ddg", rank=i))
    return out

async def marginalia(c, q, n):
    r = await c.get("https://old-search.marginalia.nu/search", params={"query": q})
    if r.status_code != 200:
        return [], f"marginalia http {r.status_code}"
    if "barraged by queries" in r.text or "<title>Error</title>" in r.text:
        return [], "marginalia: rate-limited (search engine busy, try again in a minute)"
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for i, sec in enumerate(soup.select("section.search-result")[:n]):
        a = sec.select_one("a.title")
        url_el = sec.select_one("div.url")
        desc = sec.select_one("p.description")
        if not a:
            continue
        out.append(Result(title=a.get_text(" ", strip=True), url=url_el.get_text(" ", strip=True) if url_el else a.get("href", ""),
                          snippet=clean(desc.get_text(" ", strip=True)) if desc else "", source="marginalia", rank=i))
    return out, (None if out else "marginalia: no results parsed")

async def hn(c, q, n):
    r = await c.get("https://hn.algolia.com/api/v1/search",
                    params={"query": q, "hitsPerPage": n, "tags": "story"})
    if r.status_code != 200:
        return [], f"hn http {r.status_code}"
    out = []
    for i, h in enumerate(r.json().get("hits", [])[:n]):
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID','')}"
        pts = h.get("points") or 0
        cm = h.get("num_comments") or 0
        d = (h.get("created_at") or "")[:10]
        out.append(Result(title=h.get("title") or "", url=url, source="hn", rank=i,
                          extra=f"▲{pts} · {cm}💬 · {h.get('author','')}", date=d,
                          snippet=clean(BeautifulSoup(h.get("story_text") or "", "lxml").get_text(" ", strip=True) or f"Discuss on Hacker News ({cm} comments)", 140)))
    return out, (None if out else "hn: no hits")

async def lobsters(c, q, n):
    r = await c.get("https://lobste.rs/newest.json", params={"count": min(max(n * 4, 10), 100)})
    if r.status_code != 200:
        return [], f"lobsters http {r.status_code}"
    tokens = [t for t in re.split(r"\W+", q.lower()) if len(t) > 2]
    out = []
    for i, it in enumerate(r.json() or []):
        title = it.get("title", "")
        if tokens and not any(t in title.lower() for t in tokens[:3]):
            continue
        out.append(Result(title=title, url=it.get("url") or f"https://lobste.rs{it.get('short_id_url','')}",
                          source="lobsters", rank=i, date=(it.get("created_at") or "")[:10],
                          extra=f"{it.get('comment_count',0)}💬 · {it.get('tags',[])[:2]}",
                          snippet=clean(it.get("description") or "", 140)))
        if len(out) >= n:
            break
    return out, (None if out else "lobsters: no title match")

async def so(c, q, n, site="stackoverflow"):
    r = await c.get("https://api.stackexchange.com/2.3/search/advanced",
                    params={"site": site, "q": q, "pagesize": n, "order": "desc", "sort": "relevance"})
    if r.status_code != 200:
        return [], f"so http {r.status_code}"
    out = []
    for i, it in enumerate(r.json().get("items", [])[:n]):
        tags = ",".join((it.get("tags") or [])[:3])
        ans = "✓" if it.get("is_answered") else ""
        out.append(Result(title=it.get("title", ""), url=it.get("link", ""), source="so", rank=i,
                          extra=f"{ans} {it.get('score',0)} · {it.get('answer_count',0)} answers · [{tags}]",
                          date=datetime.fromtimestamp(it.get("creation_date", 0), tz=timezone.utc).strftime("%Y-%m-%d")))
    return out, (None if out else "so: no items")

async def news(c, q, n):
    r = await c.get("https://news.google.com/rss/search",
                    params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    if r.status_code != 200:
        return [], f"news http {r.status_code}"
    soup = BeautifulSoup(r.text, "lxml-xml")
    out = []
    for i, it in enumerate(soup.find_all("item")[:n]):
        title = it.find("title").get_text(" ", strip=True) if it.find("title") else ""
        src = it.find("source")
        src_name = src.get_text(" ", strip=True) if src else ""
        if src_name and title.endswith("- " + src_name):
            title = title[: -(len(src_name) + 2)].strip()
        link = it.find("link")
        desc = it.find("description")
        desc_txt = BeautifulSoup(desc.get_text(" ", strip=True) if desc else "", "lxml").get_text(" ", strip=True)
        if title and desc_txt.startswith(title):
            desc_txt = desc_txt[len(title):].strip()
        out.append(Result(title=title, url=link.get_text(strip=True) if link else "",
                          source="news", rank=i, extra=f"[{src_name}]",
                          date=(it.find("pubDate").get_text(strip=True) if it.find("pubDate") else "")[:16],
                          snippet=clean(desc_txt, 150)))
    return out, (None if out else "news: no items")

async def wiki(c, q, n):
    r = await c.get("https://en.wikipedia.org/w/api.php",
                    params={"action": "query", "list": "search", "srsearch": q, "srlimit": n, "format": "json"})
    if r.status_code != 200:
        return [], f"wiki http {r.status_code}"
    out = []
    for i, it in enumerate(r.json().get("query", {}).get("search", [])[:n]):
        title = it.get("title", "")
        sn = BeautifulSoup(it.get("snippet", ""), "lxml").get_text(" ", strip=True)
        out.append(Result(title=title, url=f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                          source="wiki", rank=i, extra="Wikipedia", snippet=clean(sn, 160)))
    return out, (None if out else "wiki: no hits")

async def arxiv(c, q, n):
    r = await c.get("https://export.arxiv.org/api/query",
                    params={"search_query": f'all:"{q}"', "max_results": n, "sortBy": "relevance"}, timeout=30)
    if r.status_code != 200:
        return [], f"arxiv http {r.status_code}"
    soup = BeautifulSoup(r.text, "lxml-xml")
    out = []
    for i, e in enumerate(soup.find_all("entry")[:n]):
        title = e.find("title").get_text(" ", strip=True) if e.find("title") else ""
        authors = ", ".join(a.get_text(strip=True) for a in e.find_all("name")[:3])
        if e.find_all("name") and len(e.find_all("name")) > 3: authors += " et al."
        summ = e.find("summary").get_text(" ", strip=True) if e.find("summary") else ""
        out.append(Result(title=title, url=e.find("id").get_text(strip=True) if e.find("id") else "",
                          source="arxiv", rank=i, extra=f"{authors}", date=(e.find("published").get_text(strip=True)[:10] if e.find("published") else ""),
                          snippet=clean(summ, 150)))
    return out, (None if out else "arxiv: no entries")

async def gh(c, q, n):
    r = await c.get("https://api.github.com/search/repositories",
                    params={"q": q, "per_page": n},
                    headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    if r.status_code != 200:
        return [], f"github http {r.status_code}"
    out = []
    for i, it in enumerate(r.json().get("items", [])[:n]):
        desc = it.get("description") or ""
        lang = it.get("language") or ""
        out.append(Result(title=it.get("full_name", ""), url=it.get("html_url", ""), source="gh", rank=i,
                          extra=f"★{it.get('stargazers_count',0)} · {lang} · updated {it.get('updated_at','')[:10]}",
                          snippet=clean(desc, 150)))
    return out, (None if out else "gh: no items")

async def code(c, q, n):
    r = await c.get("https://grep.app/api/search", params={"q": q})
    if r.status_code != 200:
        return [], f"grep.app http {r.status_code}"
    hits = ((r.json().get("hits") or {}).get("hits")) or []
    out = []
    for i, h in enumerate(hits[:n]):
        repo, path = h.get("repo", ""), h.get("path", "")
        raw = h.get("content")
        if isinstance(raw, dict):
            raw = " ".join(str(v) if isinstance(v, str) else " ".join(v) for v in raw.values())
        elif isinstance(raw, list):
            raw = " ".join(str(v) for v in raw)
        content = re.sub(r"<[^>]+>", "", str(raw or "")).strip()
        content = re.sub(r"(?:^|\s)\d+(?=\s*[a-zA-Z])", " ", content)  # drop line numbers
        out.append(Result(title=f"{repo} · {path}", url=f"https://grep.app/search?q={quote(q)}&filter[repo][0]={quote(repo)}",
                          source="code", rank=i, snippet=clean(content, 150), extra=repo))
    return out, (None if out else "grep.app: no hits")

async def reddit(c, q, n):
    """old.reddit HTML search (server-rendered, keyless). Falls back to pullpush.io API."""
    try:
        r = await c.get("https://old.reddit.com/search", params={"q": q, "sort": "relevance"})
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            out = []
            for i, item in enumerate(soup.select("div.search-result-link")[:n]):
                a = item.select_one("a.search-title")
                if not a:
                    continue
                url = a.get("href", "")
                if url.startswith("/"):
                    url = "https://www.reddit.com" + url
                elif "old.reddit.com" in url:
                    url = url.replace("old.reddit.com", "www.reddit.com")
                meta = item.select_one(".search-result-meta")
                md = item.select_one(".md")
                snip = md.get_text(" ", strip=True) if md else ""
                out.append(Result(title=a.get_text(" ", strip=True), url=url, source="reddit", rank=i,
                                  extra=meta.get_text(" ", strip=True) if meta else "",
                                  snippet=clean(snip, 150)))
            if out:
                return out, None
        pp = await c.get("https://api.pullpush.io/reddit/search/submission/",
                         params={"q": q, "size": n}, retries=0)
        if pp.status_code == 200:
            rows = (pp.json().get("data") or [])[:n]
            return [Result(title=x.get("title", ""),
                           url=f"https://www.reddit.com{x.get('permalink','')}",
                           source="reddit", rank=i,
                           extra=f"r/{x.get('subreddit','')} · ▲{x.get('score',0)} · {x.get('num_comments',0)}💬 · {x.get('author','')}",
                           snippet=clean((x.get('selftext') or '').strip(), 150))
                    for i, x in enumerate(rows) if x.get("title")], None if rows else "pullpush: no data"
        return [], f"reddit http {r.status_code}"
    except Exception as e:
        return [], f"reddit: {type(e).__name__}: {str(e)[:80]}"

async def brave(c, q, n):
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return [], "no BRAVE_API_KEY"
    r = await c.get("https://api.search.brave.com/res/v1/web/search",
                    params={"q": q, "count": n}, headers={"X-Subscription-Token": key, "Accept": "application/json"})
    if r.status_code != 200:
        return [], f"brave http {r.status_code}"
    out = []
    for i, it in enumerate((r.json().get("web") or {}).get("results", [])[:n]):
        out.append(Result(title=it.get("title", ""), url=it.get("url", ""), source="brave", rank=i,
                          extra=(it.get("age") or ""), snippet=clean(it.get("description") or "", 150)))
    return out, (None if out else "brave: no results")

async def serper(c, q, n):
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        return [], "no SERPER_API_KEY"
    r = await c.post("https://google.serper.dev/search", json={"q": q, "num": n},
                     headers={"X-API-KEY": key})
    if r.status_code != 200:
        return [], f"serper http {r.status_code}"
    out = []
    for i, it in enumerate((r.json().get("organic") or [])[:n]):
        out.append(Result(title=it.get("title", ""), url=it.get("link", ""), source="serper", rank=i,
                          snippet=clean(it.get("snippet") or "", 150)))
    return out, (None if out else "serper: no results")

async def searxng(c, q, n):
    base = os.environ.get("SEARXNG_URL")
    if not base:
        return [], "no SEARXNG_URL"
    r = await c.get(base.rstrip("/") + "/search", params={"q": q, "format": "json"})
    if r.status_code != 200:
        return [], f"searxng http {r.status_code}"
    out = []
    for i, it in enumerate((r.json().get("results") or [])[:n]):
        out.append(Result(title=it.get("title", ""), url=it.get("url", ""), source="searxng", rank=i,
                          snippet=clean(it.get("content") or "", 150)))
    return out, (None if out else "searxng: no results")

REGISTRY = {
    "ddg": ddg, "marginalia": marginalia, "hn": hn, "lobsters": lobsters, "so": so,
    "news": news, "wiki": wiki, "arxiv": arxiv, "openalex": openalex,
    "pubmed": pubmed, "crossref": crossref, "wikidata": wikidata, "gh": gh, "code": code,
    "reddit": reddit, "brave": brave, "serper": serper, "searxng": searxng,
}

SITE_MAP = {
    "reddit.com": "reddit", "www.reddit.com": "reddit", "old.reddit.com": "reddit",
    "news.ycombinator.com": "hn", "stackoverflow.com": "so", "stackexchange.com": "so",
    "github.com": "gh", "arxiv.org": "arxiv", "en.wikipedia.org": "wiki", "wikipedia.org": "wiki",
    "old-search.marginalia.nu": "marginalia", "grep.app": "code",
}

def resolve_engines(query: str, explicit: str | None):
    """Return (engine list, cleaned query). Supports prefixes (hn:, so:, news:, wiki:,
    arxiv:, gh:, code:, reddit:, lobsters:, marginalia:, ddg:, brave:, serper:, searxng:)
    and site: filters."""
    if explicit and explicit != "auto":
        return [e.strip() for e in explicit.split(",") if e.strip() in REGISTRY], query
    m = re.match(r"^([a-z0-9]+):\s*(.*)$", query, re.S)
    if m and m.group(1) in REGISTRY:
        return [m.group(1)], m.group(2).strip()
    _ALIAS = {"doi": "crossref", "s2": "openalex", "pm": "pubmed", "wd": "wikidata"}
    if m and m.group(1) in _ALIAS:
        return [_ALIAS[m.group(1)]], m.group(2).strip()
    for dom, eng in SITE_MAP.items():
        if re.search(rf"site:\s*{re.escape(dom)}\b", query):
            return [eng], re.sub(rf"site:\s*{re.escape(dom)}\b", "", query).strip()
    if re.search(r"site:\s*news\.google\.com", query):
        return ["news"], query
    return ["ddg", "hn", "so", "reddit", "news"], query

KEYLESS = {"ddg","marginalia","hn","lobsters","so","news","wiki","arxiv","openalex","pubmed","crossref","wikidata","gh","code","reddit"}

def available() -> list[str]:
    """Keyless engines always listed; keyed engines only when env present."""
    out = [e for e in ENGINE_NAMES if e in KEYLESS]
    for env, eng in [("BRAVE_API_KEY","brave"), ("SERPER_API_KEY","serper"), ("SEARXNG_URL","searxng")]:
        if env in os.environ:
            out.append(eng)
    return out

async def run_engines(client: PoliteClient, query: str, n: int, engines_list: list[str],
                      ttl: float = 1800, fresh: bool = False) -> tuple[list[Result], dict]:
    """Run engines concurrently, cache per-engine JSON, collect errors by engine name."""
    import asyncio as _a, json as _json
    from . import cache as _cache
    results: list[Result] = []
    errors: dict[str, str] = {}

    FETCH = max(min(n, 12), 4)   # fetch a bit extra; cache key is n-independent

    async def one(name: str):
        cached = None if fresh else _cache.get("eng", query, name, ttl=ttl)
        if cached is not None:
            try:
                rows = _json.loads(cached)
            except Exception:
                rows = None
            if rows == "__err__":
                return name, [], "cached (recent failure, retry in ~90s)"
            if isinstance(rows, list):
                return name, [Result(**row) for row in rows][:n], None
        try:
            res, err = await REGISTRY[name](client, query, FETCH)
        except Exception as exc:
            res, err = [], f"{type(exc).__name__}: {str(exc)[:90]}"
        if not fresh:
            try:
                if res:
                    _cache.set("eng", query, name,
                               value=_json.dumps([r.__dict__ for r in res[:12]], default=str), ttl=ttl)
                else:
                    _cache.set("eng", query, name, value='"__err__"', ttl=90)
            except Exception:
                pass
        return name, res[:n], err

    tasks = [_a.ensure_future(one(nm)) for nm in engines_list]
    for fut in _a.as_completed(tasks):
        try:
            name, res, err = await fut
        except Exception as exc:
            name, res, err = "?", [], f"{type(exc).__name__}: {str(exc)[:90]}"
        if res:
            results.extend(res)
        if err:
            errors[name] = err
    return results, errors

