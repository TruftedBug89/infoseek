"""Search engines. Every engine is keyless by default; brave/serper/searxng activate
only when the matching env vars are present (checked at call time, never logged)."""
import json, os, re
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, unquote, quote
from bs4 import BeautifulSoup
from .net import PoliteClient, UAS
from .rank import Result, clean

ENGINE_NAMES = [
    "ddg", "marginalia", "hn", "lobsters", "so", "news", "wiki", "arxiv",
    "openalex", "pubmed", "crossref", "wikidata", "gh", "code", "reddit",
    "pypi", "npm", "crates", "mdn", "yt", "wayback", "commoncrawl", "swarm",
    "gh_issues", "prs", "gh_releases", "changelog", "error", "compat",
    "brave", "serper", "searxng"
]

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

def _ddg_df(days: float) -> str:
    return "day" if days <= 1 else "week" if days <= 7 else "month" if days <= 31 else "year"


# Per-engine freshness query hints: map engine -> (query, days) -> adjusted query.
_FRESH_Q = {
    "news": lambda q, d: f"{q} when:{max(1, int(d))}d",
    "ddg": lambda q, d: f"{q} __df__{_ddg_df(d)}",
}


async def _ddg_fetch(c, q, n):
    df = None
    m = re.search(r" __df__(day|week|month|year)$", q)
    if m:
        df = m.group(1)
        q = q[:m.start()]
    data = {"q": q}
    if df:
        data["df"] = df
    r = await c.post("https://html.duckduckgo.com/html/", data=data)
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
    if r.status_code in (429, 503):  # the public instance is often overloaded
        return [], "marginalia: rate-limited (search engine busy, try again in a minute)"
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
                          extra=f"{pts} points, {cm} comments, by {h.get('author','')}", date=d,
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
                          extra=f"{it.get('comment_count',0)} comments, tags: {','.join((it.get('tags') or [])[:2])}",
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
        ans = "answered" if it.get("is_answered") else "unanswered"
        out.append(Result(title=it.get("title", ""), url=it.get("link", ""), source="so", rank=i,
                          extra=f"{ans}, score {it.get('score',0)}, {it.get('answer_count',0)} answers, tags: {tags}",
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
    """GitHub repo search. Smarter forms: 'owner/repo' does an exact repo
    lookup; 'owner/repo#123' fetches that issue/PR directly."""
    q = (q or "").strip()
    m = _REPO_REF_RE.match(q)
    if m:
        owner_repo = m.group(1)
        num = m.group(2)
        if num:
            return await _gh_issue_fetch(c, owner_repo, num)
        if not m.group(3):  # bare owner/repo -> exact lookup, then repo search
            res, err = await _gh_repo_result(c, owner_repo)
            if res:
                return res, None
    r = await c.get("https://api.github.com/search/repositories",
                    params={"q": q, "per_page": n}, headers=_gh_headers())
    if r.status_code in (403, 429):
        return [], _gh_rate_msg(r)
    if r.status_code != 200:
        return [], f"github http {r.status_code}"
    out = []
    for i, it in enumerate(r.json().get("items", [])[:n]):
        desc = it.get("description") or ""
        lang = it.get("language") or ""
        out.append(Result(title=it.get("full_name", ""), url=it.get("html_url", ""), source="gh", rank=i,
                          extra=f"stars {it.get('stargazers_count',0)}, {lang}, updated {it.get('updated_at','')[:10]}",
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
    """old.reddit HTML search (server-rendered, keyless). Falls back to pullpush.io,
    then to DuckDuckGo site-restricted search (reddit walls most direct endpoints
    from datacenter IPs; DDG site: search stays reliable)."""
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
            out = [Result(title=x.get("title", ""),
                          url=f"https://www.reddit.com{x.get('permalink','')}",
                          source="reddit", rank=i,
                          extra=f"r/{x.get('subreddit','')}, score {x.get('score',0)}, {x.get('num_comments',0)} comments, by {x.get('author','')}",
                          snippet=clean((x.get('selftext') or '').strip(), 150))
                   for i, x in enumerate(rows) if x.get("title")]
            if out:
                return out, None
    except Exception:
        pass
    # last resort: DuckDuckGo site-restricted search, re-tagged as reddit
    try:
        out, err = await ddg(c, f"site:reddit.com {q}", n)
        if out:
            for x in out:
                x.source = "reddit"
            return out, None
        return [], err or "reddit: no path returned results (origin walled, pullpush empty)"
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
                          extra=f"citations {it.get('cited_by_count',0)}, {venue}",
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
                          extra=f"citations {it.get('is-referenced-by-count',0)}, {venue}, {auth}",
                          date=year, snippet=""))
    return out, (None if out else "crossref: no items")

async def pypi(c: PoliteClient, q: str, n: int):
    """PyPI Python package search and metadata lookup. Keyless."""
    pkg = q.strip().split()[0]
    out = []
    # 1. Exact package JSON lookup
    r = await c.get(f"https://pypi.org/pypi/{quote(pkg)}/json")
    if r.status_code == 200:
        info = r.json().get("info", {})
        out.append(Result(
            title=f"{info.get('name')} {info.get('version')}",
            url=info.get("project_url") or f"https://pypi.org/project/{pkg}/",
            snippet=clean(info.get("summary") or "", 160),
            source="pypi", rank=0,
            extra=f"license: {info.get('license') or 'N/A'}, author: {info.get('author') or 'N/A'}",
            date=info.get("release_url", "")
        ))
    # 2. General PyPI HTML search for related packages
    sr = await c.get("https://pypi.org/search/", params={"q": q})
    if sr.status_code == 200:
        soup = BeautifulSoup(sr.text, "lxml")
        for i, el in enumerate(soup.select(".package-snippet")[:n]):
            name_el = el.select_one(".package-snippet__name")
            ver_el = el.select_one(".package-snippet__version")
            desc_el = el.select_one(".package-snippet__description")
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            if any(r.title.startswith(name) for r in out):
                continue
            ver = ver_el.get_text(strip=True) if ver_el else ""
            desc = desc_el.get_text(strip=True) if desc_el else ""
            out.append(Result(
                title=f"{name} {ver}".strip(),
                url=f"https://pypi.org/project/{name}/",
                snippet=clean(desc, 150),
                source="pypi", rank=len(out),
                extra="PyPI Package"
            ))
            if len(out) >= n:
                break
    return out, (None if out else "pypi: no packages found")


async def npm(c: PoliteClient, q: str, n: int):
    """npm JavaScript/TypeScript package search via official registry API. Keyless."""
    r = await c.get("https://registry.npmjs.org/-/v1/search", params={"text": q, "size": n})
    if r.status_code != 200:
        return [], f"npm http {r.status_code}"
    out = []
    for i, obj in enumerate((r.json().get("objects") or [])[:n]):
        pkg = obj.get("package", {})
        name = pkg.get("name", "")
        ver = pkg.get("version", "")
        desc = pkg.get("description", "")
        links = pkg.get("links", {})
        pub = (pkg.get("publisher") or {}).get("username", "")
        date = (pkg.get("date") or "")[:10]
        out.append(Result(
            title=f"{name} @{ver}",
            url=links.get("npm") or f"https://www.npmjs.com/package/{name}",
            snippet=clean(desc, 150),
            source="npm", rank=i,
            extra=f"Publisher: {pub}" if pub else "npm package",
            date=date
        ))
    return out, (None if out else "npm: no packages found")


async def crates(c: PoliteClient, q: str, n: int):
    """Rust crates.io package search via official API. Keyless."""
    r = await c.get("https://crates.io/api/v1/crates", params={"q": q, "per_page": n},
                    headers={"User-Agent": "infoseek/0.8.0 (https://github.com/TruftedBug89/infoseek)"})
    if r.status_code != 200:
        return [], f"crates http {r.status_code}"
    out = []
    for i, it in enumerate((r.json().get("crates") or [])[:n]):
        name = it.get("name", "")
        ver = it.get("max_version", "")
        desc = it.get("description") or ""
        dl = it.get("downloads", 0)
        out.append(Result(
            title=f"{name} v{ver}",
            url=f"https://crates.io/crates/{name}",
            snippet=clean(desc, 150),
            source="crates", rank=i,
            extra=f"downloads: {dl:,}, license: {it.get('license', 'N/A')}",
            date=(it.get("updated_at") or "")[:10]
        ))
    return out, (None if out else "crates: no crates found")


async def mdn(c: PoliteClient, q: str, n: int):
    """MDN Web Docs documentation search via official REST API. Keyless."""
    r = await c.get("https://developer.mozilla.org/api/v1/search", params={"q": q, "locale": "en-US"})
    if r.status_code != 200:
        return [], f"mdn http {r.status_code}"
    out = []
    for i, doc in enumerate((r.json().get("documents") or [])[:n]):
        title = doc.get("title", "")
        mdn_url = doc.get("mdn_url", "")
        url = f"https://developer.mozilla.org{mdn_url}" if mdn_url.startswith("/") else mdn_url
        summ = doc.get("summary") or ""
        out.append(Result(
            title=f"{title} - MDN Web Docs",
            url=url,
            snippet=clean(summ, 160),
            source="mdn", rank=i,
            extra="MDN Docs"
        ))
    return out, (None if out else "mdn: no docs found")


async def yt(c: PoliteClient, q: str, n: int):
    """YouTube video search via DuckDuckGo site filter (keyless)."""
    return await ddg(c, f"site:youtube.com/watch {q}", n)



# --------------------------------------------------------------- github deep
# GitHub issue/PR/release engines. Optional GITHUB_TOKEN / GH_TOKEN raises the
# unauthenticated rate limits (search: 10 -> 30 req/min; core: 60 -> 5000/hr).

def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _gh_rate_msg(r) -> str:
    rem = r.headers.get("X-RateLimit-Remaining", "?")
    return (f"github: rate-limited (rate-limit remaining {rem}; "
            "set GITHUB_TOKEN to raise limits)")


async def _gh_issue_fetch(c, owner_repo: str, num: int):
    """Fetch one issue/PR by number (the issues API serves both)."""
    r = await c.get(f"https://api.github.com/repos/{owner_repo}/issues/{num}",
                    headers=_gh_headers())
    if r.status_code in (403, 429):
        return [], _gh_rate_msg(r)
    if r.status_code != 200:
        return [], f"github http {r.status_code}"
    it = r.json()
    kind = "PR" if "pull_request" in it else "issue"
    body = clean(re.sub(r"\s+", " ", it.get("body") or ""), 150)
    return [Result(title=it.get("title", ""), url=it.get("html_url", ""),
                   source="gh_issues", rank=0,
                   extra=f"{kind} #{num}, {it.get('state','')}, {it.get('comments',0)} comments",
                   date=(it.get("created_at") or "")[:10], snippet=body)], None


async def _gh_repo_result(c, owner_repo: str):
    """Exact repo lookup (more reliable than search for 'owner/name')."""
    r = await c.get(f"https://api.github.com/repos/{owner_repo}", headers=_gh_headers())
    if r.status_code in (403, 429):
        return [], _gh_rate_msg(r)
    if r.status_code != 200:
        return [], f"github http {r.status_code}"
    it = r.json()
    return [Result(title=it.get("full_name", ""), url=it.get("html_url", ""),
                   source="gh", rank=0,
                   extra=f"stars {it.get('stargazers_count',0)}, {it.get('language') or 'n/a'}, "
                         f"updated {it.get('updated_at','')[:10]}",
                   snippet=clean(it.get("description") or "", 150))], None


_REPO_REF_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:#(\d+))?(?:\s+(.*))?$")


async def gh_issues(c, q, n, extra_qualifier: str = ""):
    """GitHub issue + PR search (api.github.com/search/issues). Keyless.
    Query forms: 'bailingmoe3', 'repo:owner/name bailingmoe3',
    'owner/name bailingmoe3' (leading owner/name becomes a repo: qualifier)."""
    q = (q or "").strip()
    quals = [extra_qualifier] if extra_qualifier else []
    m = re.match(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s+(.*)$", q)
    if m and "repo:" not in q.lower():
        quals.append(f"repo:{m.group(1)}")
        q = m.group(2)
    qq = " ".join([q] + [x for x in quals if x]).strip()
    if not qq:
        return [], "gh issues: empty query"
    r = await c.get("https://api.github.com/search/issues",
                    params={"q": qq, "per_page": n}, headers=_gh_headers())
    if r.status_code in (403, 429):
        return [], _gh_rate_msg(r)
    if r.status_code != 200:
        return [], f"github http {r.status_code}"
    out = []
    for i, it in enumerate((r.json().get("items") or [])[:n]):
        kind = "PR" if "pull_request" in it else "issue"
        repo = (it.get("repository_url") or "").replace("https://api.github.com/repos/", "")
        body = clean(re.sub(r"\s+", " ", it.get("body") or ""), 150)
        out.append(Result(title=it.get("title", ""), url=it.get("html_url", ""),
                          source="gh_issues", rank=i,
                          extra=f"{kind}, {repo}, {it.get('state','')}, {it.get('comments',0)} comments",
                          date=(it.get("created_at") or "")[:10], snippet=body))
    return out, (None if out else "gh issues: no items")


async def prs(c, q, n):
    """GitHub pull-request search (gh_issues restricted to is:pr)."""
    return await gh_issues(c, q, n, extra_qualifier="is:pr")


async def gh_releases(c, q, n):
    """GitHub release notes. 'owner/repo [version-fragment]' lists releases
    (newest first, optionally filtered); a bare term finds the best-matching
    repo first and then lists its releases."""
    q = (q or "").strip()
    m = re.match(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s*(.*)$", q)
    rest = ""
    if m:
        owner_repo, rest = m.group(1), (m.group(2) or "").strip()
    else:
        res, err = await gh(c, q, 1)
        if not res:
            return [], err or "gh releases: no matching repo found"
        owner_repo = res[0].title
    r = await c.get(f"https://api.github.com/repos/{owner_repo}/releases",
                    params={"per_page": max(n * 2, 10)}, headers=_gh_headers())
    if r.status_code in (403, 429):
        return [], _gh_rate_msg(r)
    if r.status_code != 200:
        return [], f"github http {r.status_code}"
    out = []
    for it in r.json() or []:
        tag = it.get("tag_name", "")
        name = it.get("name") or tag
        if rest and rest.lower() not in f"{tag} {name}".lower():
            continue
        body = clean(re.sub(r"\s+", " ", it.get("body") or ""), 150)
        out.append(Result(title=f"{owner_repo} {tag}" + (f" — {name}" if name and name != tag else ""),
                          url=it.get("html_url", ""), source="gh_releases", rank=len(out),
                          extra="release" + (", prerelease" if it.get("prerelease") else ""),
                          date=(it.get("published_at") or "")[:10], snippet=body))
        if len(out) >= n:
            break
    return out, (None if out else "gh releases: none found")


_CHANGELOG_PATHS = ("CHANGELOG.md", "CHANGELOG.rst", "CHANGES.md", "HISTORY.md")


async def changelog(c, q, n):
    """Changelog / release-notes finder. 'owner/repo' checks the repo for a
    CHANGELOG* file and lists recent releases; anything else web-searches for
    the project's changelog page."""
    q = (q or "").strip()
    if not q:
        return [], "changelog: empty query"
    out = []
    m = re.match(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s*(.*)$", q)
    if m:
        owner_repo = m.group(1)
        for path in _CHANGELOG_PATHS:
            r = await c.get(f"https://api.github.com/repos/{owner_repo}/contents/{path}",
                            headers=_gh_headers())
            if r.status_code == 200:
                j = r.json()
                out.append(Result(title=f"{owner_repo} — {path}", url=j.get("html_url", ""),
                                  source="changelog", rank=0,
                                  extra=f"changelog file, {j.get('size', 0)} bytes",
                                  snippet=f"Changelog file for {owner_repo} (extract to read)"))
                break
            if r.status_code in (403, 429):
                break
        rel, _err = await gh_releases(c, owner_repo, max(n - 1, 2))
        for rr in rel:
            rr.rank = len(out)
            out.append(rr)
        if out:
            return out[:n], None
    res, err = await ddg(c, f'{q} changelog OR "release notes"', max(n, 6))
    for r in res:
        if re.search(r"changelog|releases?|whatsnew|history|release-notes", r.url, re.I):
            r.source, r.rank = "changelog", len(out)
            out.append(r)
        if len(out) >= n:
            break
    if not out:
        out = res[:n]
    return out, (None if out else (err or "changelog: nothing found"))


async def error(c, q, n):
    """Error-message research: normalize the message, then search GitHub
    issues/PRs + Stack Overflow + the general web (with fix/solution terms)
    in parallel; results mentioning fixes/workarounds/patches float up and
    snippets are re-centered on solution lines."""
    import asyncio as _a
    from .research import normalize_error_message, SOLUTION_WORDS, focus_snippet
    core = normalize_error_message(q) or (q or "").strip()
    if not core:
        return [], "error: empty message"
    (res_i, err_i), (res_s, err_s), (res_w, err_w) = await _a.gather(
        gh_issues(c, core, max(n, 4)),
        so(c, core, max(n, 4)),
        ddg(c, f"{core} fix OR solution", max(n, 4)),
    )
    merged, seen = [], set()
    for lst in (res_i, res_s, res_w):
        for r in lst:
            if r.url and r.url not in seen:
                seen.add(r.url)
                merged.append(r)
    for r in merged:
        blob = f"{r.title} {r.snippet}".lower()
        r.score = 2.0 if SOLUTION_WORDS.search(blob) else 0.0
        if r.snippet:
            focused = focus_snippet(r.snippet, core, 160, "error")
            if focused:
                r.snippet = focused
    merged.sort(key=lambda r: -r.score)
    for i, r in enumerate(merged):
        r.rank = i  # carry the solution-first order through merge()
    errs = " · ".join(e for e in (err_i, err_s, err_w) if e)
    return merged[:n], (errs or None)


async def compat(c, q, n):
    """Version-compatibility research ('which tool version supports X'):
    GitHub issues + web variants in parallel, re-ranked for version-bearing
    and compatibility phrasing; snippets re-centered on version lines."""
    import asyncio as _a
    from .research import VERSION_RE, COMPAT_WORDS, focus_snippet
    q = (q or "").strip()
    if not q:
        return [], "compat: empty query"
    tasks = [gh_issues(c, f"{q} support OR version", max(n, 4)),
             ddg(c, f'{q} which version supports OR "added in"', max(n, 4))]
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", q):
        tasks.append(gh_releases(c, q, 3))
    pairs = await _a.gather(*tasks)
    merged, seen = [], set()
    for lst, _e in pairs:
        for r in lst:
            if r.url and r.url not in seen:
                seen.add(r.url)
                merged.append(r)
    for r in merged:
        blob = f"{r.title} {r.snippet}"
        s = 0.0
        if VERSION_RE.search(blob):
            s += 2.0
        if COMPAT_WORDS.search(blob.lower()):
            s += 1.0
        r.score = s
        if r.snippet:
            focused = focus_snippet(r.snippet, q, 160, "version")
            if focused:
                r.snippet = focused
    merged.sort(key=lambda r: -r.score)
    for i, r in enumerate(merged):
        r.rank = i
    errs = " · ".join(e for lst, e in pairs if e)
    return merged[:n], (errs or None)


# ------------------------------------------------------- archive & crawl corps
# Public-good infrastructure: the Wayback Machine (Internet Archive) and the
# Common Crawl index. Both are keyless, stable, and never block polite clients.

async def wayback(c, q, n):
    """Wayback Machine CDX: archived snapshots for a URL, path, or domain
    (wildcards ok: 'example.com/blog/*'). Returns archive.org links — pass
    them to extract(), or extract() the original URL (it falls back to the
    archive automatically when the live page is unreachable)."""
    q = (q or "").strip()
    if not q:
        return [], "wayback: empty query"
    target = re.sub(r"^https?://", "", q)
    r = await c.get("https://web.archive.org/cdx/search/cdx",
                    params={"url": target, "output": "json", "limit": max(n * 3, 10),
                            "collapse": "urlkey", "filter": "statuscode:200"},
                    timeout=20)
    if r.status_code != 200:
        return [], f"wayback http {r.status_code}"
    try:
        rows = r.json()
    except Exception:
        return [], "wayback: bad response"
    if len(rows) < 2:
        return [], "wayback: no snapshots"
    head, body = rows[0], rows[1:]
    out = []
    for i, row in enumerate(body[:n]):
        d = dict(zip(head, row))
        ts = d.get("timestamp", "")
        orig = d.get("original", "")
        date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else ""
        out.append(Result(title=orig, url=f"https://web.archive.org/web/{ts}/{orig}",
                          source="wayback", rank=i, date=date,
                          extra=f"archived {ts}, status {d.get('statuscode', '')}"))
    return out, (None if out else "wayback: no snapshots")


_cc_index: dict = {"id": None, "ts": 0.0}


async def _cc_index_id(c) -> str | None:
    """Newest Common Crawl index id (cached 24 h)."""
    import time as _t
    if _cc_index["id"] and _t.time() - _cc_index["ts"] < 86400:
        return _cc_index["id"]
    try:
        r = await c.get("https://index.commoncrawl.org/collinfo.json", timeout=15)
        if r.status_code == 200:
            _cc_index.update(id=r.json()[0]["id"], ts=_t.time())
            return _cc_index["id"]
    except Exception:
        pass
    return None


async def commoncrawl(c, q, n):
    """Common Crawl index: URLs the latest crawl saw, with snapshot timestamps.
    Discovery without touching the origin site — pair with extract() (which
    falls back to the Wayback Machine if the live page blocks scrapers).
    Wildcards ok: 'example.com/blog/*'."""
    q = (q or "").strip()
    if not q:
        return [], "commoncrawl: empty query"
    idx = await _cc_index_id(c)
    if not idx:
        return [], "commoncrawl: index unavailable"
    target = re.sub(r"^https?://", "", q)
    r = await c.get(f"https://index.commoncrawl.org/{idx}-index",
                    params={"url": target, "output": "json", "limit": n}, timeout=20)
    if r.status_code != 200:
        return [], f"commoncrawl http {r.status_code}"
    out = []
    for i, line in enumerate(r.text.strip().splitlines()[:n]):
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = d.get("timestamp", "")
        date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else ""
        out.append(Result(title=d.get("url", ""), url=d.get("url", ""),
                          source="commoncrawl", rank=i, date=date,
                          extra=f"crawl {idx}, status {d.get('status', '')}, archived {ts}"))
    return out, (None if out else "commoncrawl: no records")


async def swarm(c, q, n):
    """SearXNG instance swarm: fan out over community-hosted SearXNG instances
    (github.com/searxng/searxng) in parallel and merge the first usable
    results. Keyless; bot-walled instances are skipped and health-demoted.
    Set SEARXNG_URL to your own instance for a private, guaranteed swarm."""
    base = os.environ.get("SEARXNG_URL")
    if base:  # own instance first: most reliable + private
        res, _err = await searxng(c, q, n)
        if res:
            for r in res:
                r.source = "swarm"
            return res, None
    from .swarm import swarm_search
    out = await swarm_search(c, q, n)
    return out, (None if out else "swarm: no reachable instance returned results")


REGISTRY = {
    "ddg": ddg, "marginalia": marginalia, "hn": hn, "lobsters": lobsters, "so": so,
    "news": news, "wiki": wiki, "arxiv": arxiv, "openalex": openalex,
    "pubmed": pubmed, "crossref": crossref, "wikidata": wikidata, "gh": gh, "code": code,
    "reddit": reddit, "pypi": pypi, "npm": npm, "crates": crates, "mdn": mdn, "yt": yt,
    "wayback": wayback, "commoncrawl": commoncrawl, "swarm": swarm,
    "gh_issues": gh_issues, "prs": prs, "gh_releases": gh_releases, "changelog": changelog,
    "error": error, "compat": compat,
    "brave": brave, "serper": serper, "searxng": searxng,
}

SITE_MAP = {
    "reddit.com": "reddit", "www.reddit.com": "reddit", "old.reddit.com": "reddit",
    "news.ycombinator.com": "hn", "stackoverflow.com": "so", "stackexchange.com": "so",
    "github.com": "gh", "arxiv.org": "arxiv", "en.wikipedia.org": "wiki", "wikipedia.org": "wiki",
    "old-search.marginalia.nu": "marginalia", "grep.app": "code",
    "web.archive.org": "wayback", "archive.org": "wayback",
    "pypi.org": "pypi", "npmjs.com": "npm", "registry.npmjs.org": "npm",
    "crates.io": "crates", "developer.mozilla.org": "mdn",
    "youtube.com": "yt", "youtu.be": "yt"
}

_ALIAS = {
    "doi": "crossref", "s2": "openalex", "pm": "pubmed", "wd": "wikidata",
    "wb": "wayback", "archive": "wayback", "cc": "commoncrawl",
    "docs": "mdn", "cargo": "crates", "rust": "crates", "node": "npm",
    "python": "pypi", "pip": "pypi", "youtube": "yt",
    "issues": "gh_issues", "issue": "gh_issues", "ghissues": "gh_issues",
    "pr": "prs", "pulls": "prs", "pull": "prs",
    "releases": "gh_releases", "release": "gh_releases",
    "err": "error", "debug": "error",
    "version": "compat", "compatibility": "compat"
}

KEYLESS = {
    "ddg", "marginalia", "hn", "lobsters", "so", "news", "wiki", "arxiv",
    "openalex", "pubmed", "crossref", "wikidata", "gh", "code", "reddit",
    "pypi", "npm", "crates", "mdn", "yt", "wayback", "commoncrawl", "swarm",
    "gh_issues", "prs", "gh_releases", "changelog", "error", "compat"
}


def available() -> list[str]:
    """Keyless engines always listed; keyed engines only when env present."""
    out = [e for e in ENGINE_NAMES if e in KEYLESS]
    for env, eng in [("BRAVE_API_KEY", "brave"), ("SERPER_API_KEY", "serper"), ("SEARXNG_URL", "searxng")]:
        if env in os.environ:
            out.append(eng)
    return out


#: Maximum-coverage mix: general web + SearXNG swarm + forums + news.
WIDE_MIX = ["ddg", "swarm", "hn", "so", "news"]


def resolve_engines(query: str, explicit: str | None) -> tuple[list[str], str]:
    """Return (engine list, cleaned query). Supports prefixes, site: filters,
    and the special mixes 'auto' (default) and 'wide' (maximum coverage).
    A routing prefix is stripped whenever engines are chosen explicitly, so
    'code:o/r t' escalated to the wide mix searches for 'o/r t', not the prefix."""
    m = re.match(r"^([a-z0-9_]+):\s*(.*)$", query, re.S)
    prefixed_q = m.group(2).strip() if m and (m.group(1) in REGISTRY or m.group(1) in _ALIAS) else None
    if explicit == "wide":
        return list(WIDE_MIX), (prefixed_q or query)
    if explicit and explicit != "auto":
        return [e.strip() for e in explicit.split(",") if e.strip() in REGISTRY], (prefixed_q or query)
    if m and m.group(1) in REGISTRY:
        return [m.group(1)], m.group(2).strip()
    if m and m.group(1) in _ALIAS:
        return [_ALIAS[m.group(1)]], m.group(2).strip()
    for dom, eng in SITE_MAP.items():
        if re.search(rf"site:\s*{re.escape(dom)}\b", query):
            return [eng], re.sub(rf"site:\s*{re.escape(dom)}\b", "", query).strip()
    if re.search(r"site:\s*news\.google\.com", query):
        return ["news"], query
    return ["ddg", "hn", "so", "reddit", "news"], query


async def run_engines(client: PoliteClient, query: str, n: int, engines_list: list[str],
                      ttl: float = 1800, fresh: bool = False,
                      freshness_days: float | None = None) -> tuple[list[Result], dict]:
    """Run engines concurrently, cache per-engine JSON, collect errors by engine name."""
    import asyncio as _a, json as _json
    from . import cache as _cache
    results: list[Result] = []
    errors: dict[str, str] = {}

    FETCH = max(min(n, 12), 4)

    async def one(name: str):
        q_use = _FRESH_Q[name](query, freshness_days) if (freshness_days and name in _FRESH_Q) else query
        cached = None if fresh else _cache.get("eng", q_use, name, ttl=ttl)
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
            res, err = await REGISTRY[name](client, q_use, FETCH)
        except Exception as exc:
            res, err = [], f"{type(exc).__name__}: {str(exc)[:90]}"
        if not fresh:
            try:
                if res:
                    _cache.set("eng", q_use, name,
                               value=_json.dumps([r.__dict__ for r in res[:12]], default=str), ttl=ttl)
                else:
                    _cache.set("eng", q_use, name, value='"__err__"', ttl=90)
            except Exception:
                pass
        return name, res[:n], err

    engine_timeout = float(os.environ.get("INFOSEEK_ENGINE_TIMEOUT", "3.5"))

    async def one_timed(nm: str):
        try:
            return await _a.wait_for(one(nm), timeout=engine_timeout)
        except _a.TimeoutError:
            return nm, [], f"{nm}: timeout (> {engine_timeout}s)"
        except Exception as exc:
            return nm, [], f"{type(exc).__name__}: {str(exc)[:90]}"

    tasks = [_a.ensure_future(one_timed(nm)) for nm in engines_list]
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

