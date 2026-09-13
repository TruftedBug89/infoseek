"""Content extraction: domain-aware fast paths + trafilatura + heuristic fallback."""
import asyncio, os, re
from urllib.parse import urlparse, unquote, quote
import httpx, trafilatura
from bs4 import BeautifulSoup
from .net import PoliteClient
from .research import (auto_focus, VERSION_RE, COMPAT_WORDS, ERROR_WORDS,
                       SOLUTION_WORDS)

def _sanitize_html(html: str) -> str:
    """Strip hidden CSS/DOM elements (honeypots & hidden injection vectors)."""
    try:
        soup = BeautifulSoup(html, "lxml")
        # Remove explicitly hidden elements
        for el in soup.find_all(attrs={"aria-hidden": "true"}):
            el.decompose()
        for el in soup.find_all(attrs={"hidden": True}):
            el.decompose()
        # Remove elements with CSS hiding properties
        _HIDE_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?(?:\s|;|$)|font-size\s*:\s*0(?:px)?|text-indent\s*:\s*-\d{3,}px|position\s*:\s*absolute;\s*(?:left|top)\s*:\s*-\d{3,}px", re.I)
        for el in soup.find_all(style=_HIDE_STYLE):
            el.decompose()
        # Drop image tags to avoid Markdown exfiltration
        for img in soup.find_all("img"):
            img.decompose()
        return str(soup)
    except Exception:
        return html


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
    if max_chars <= 0:
        return text.strip()
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    i = cut.rfind(".")
    if i > max_chars * 0.5:
        return cut[: i + 1]
    j = cut.rfind(" ")
    return cut[:j] + " …" if j > max_chars * 0.5 else cut + " …"


def _gh_api_headers() -> dict:
    """GitHub API headers; optional GITHUB_TOKEN / GH_TOKEN raises rate limits."""
    import os as _os
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = _os.environ.get("GITHUB_TOKEN") or _os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _md_to_text(md: str) -> str:
    """Rough markdown -> plain text for issue/release bodies."""
    s = md or ""
    s = re.sub(r"```.*?```", " [code block] ", s, flags=re.S)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)          # images
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)     # links -> text
    s = re.sub(r"(?m)^#{1,6}\s*", "", s)                   # headings
    s = re.sub(r"(?m)^\s*[-*+]\s+", "- ", s)              # bullets
    s = re.sub(r"(?m)^\s*>\s?", "", s)                    # quotes
    s = re.sub(r"\s+", " ", s)
    return s.strip()


async def _github_issue(client: PoliteClient, owner: str, repo: str, num: str,
                        max_chars: int) -> str:
    """Fetch one GitHub issue or PR with its top comments via the REST API."""
    r = await client.get(f"https://api.github.com/repos/{owner}/{repo}/issues/{num}",
                         headers=_gh_api_headers())
    if r.status_code != 200:
        return ""
    it = r.json()
    kind = "pull request" if "pull_request" in it else "issue"
    parts = [f"{owner}/{repo} {kind} #{num}: {it.get('title','')} "
             f"(state: {it.get('state','')}, {it.get('comments',0)} comments)",
             _md_to_text(it.get("body") or "")[:max(600, max_chars // 2)]]
    cr = await client.get(f"https://api.github.com/repos/{owner}/{repo}/issues/{num}/comments",
                          params={"per_page": 10}, headers=_gh_api_headers())
    if cr.status_code == 200:
        for ch in (cr.json() or [])[:8]:
            body = _md_to_text(ch.get("body") or "")
            if body:
                parts.append(f"· {ch.get('user',{}).get('login','')}: {body[:400]}")
    return _trim("\n".join(p for p in parts if p), max_chars)


async def _github_release_page(client: PoliteClient, owner: str, repo: str, tag: str,
                               max_chars: int) -> str:
    """Fetch one GitHub release's notes by tag name."""
    r = await client.get(f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}",
                         headers=_gh_api_headers())
    if r.status_code != 200:
        return ""
    it = r.json()
    head = f"{owner}/{repo} release {it.get('tag_name','')} — {it.get('name','')}\n"
    return _trim(head + _md_to_text(it.get("body") or ""), max_chars)


async def _github_changelog_file(client: PoliteClient, owner: str, repo: str,
                                 max_chars: int) -> str:
    """Fetch the repo's CHANGELOG* file (raw), newest entries first."""
    for path in ("CHANGELOG.md", "CHANGELOG.rst", "CHANGES.md", "HISTORY.md"):
        r = await client.get(f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}",
                             headers=_gh_api_headers())
        if r.status_code == 200 and r.text.strip():
            return _trim(r.text, max_chars)
    return ""


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
    """Fetch one Reddit post with its top community comments.
    Tries Reddit .json endpoint with desktop browser UA, falls back to pullpush."""
    clean_url = url.split("?")[0].rstrip("/")
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    try:
        r = await client.get(f"{clean_url}.json", headers={"User-Agent": ua}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) >= 1:
                post_data = data[0].get("data", {}).get("children", [{}])[0].get("data", {})
                title = post_data.get("title", "")
                sub = post_data.get("subreddit", "")
                author = post_data.get("author", "")
                score = post_data.get("score", 0)
                selftext = post_data.get("selftext", "")
                parts = [f"r/{sub} · {title} (score: {score}, by u/{author})\n\n{selftext}"]
                if len(data) >= 2:
                    parts.append("\nTop Community Comments:")
                    comments = data[1].get("data", {}).get("children", [])
                    for c in comments[:8]:
                        cd = c.get("data", {})
                        c_body = cd.get("body", "")
                        c_auth = cd.get("author", "")
                        c_score = cd.get("score", 0)
                        if c_body and c_body not in ("[deleted]", "[removed]"):
                            parts.append(f"· u/{c_auth} ({c_score} pts): {_trim(c_body, 300)}")
                return _trim("\n".join(parts), max_chars)
    except Exception:
        pass

    m = re.search(r"/comments/([a-z0-9]+)", url)
    if m:
        try:
            r = await client.get("https://api.pullpush.io/reddit/search/submission/",
                                 params={"ids": m.group(1)}, retries=0, timeout=10)
            if r.status_code == 200:
                data = (r.json().get("data") or [])
                if data:
                    x = data[0]
                    return _trim(f"{x.get('title','')} — r/{x.get('subreddit','')} (score: {x.get('score',0)})\n\n{x.get('selftext') or '(link post)'}", max_chars)
        except Exception:
            pass
    return ""


async def _polymarket_event(client: PoliteClient, url: str, max_chars: int) -> str:
    """Extract Polymarket event market data (odds, volume, question)."""
    m = re.search(r"/(?:event|market)/([a-zA-Z0-9_-]+)", url)
    if not m:
        return ""
    slug = m.group(1)
    try:
        import json as _j
        r = await client.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=10)
        if r.status_code == 200:
            events = r.json() or []
            if events and isinstance(events, list):
                ev = events[0]
                title = ev.get("title") or ev.get("question") or ""
                desc = ev.get("description") or ""
                vol = float(ev.get("volume") or 0)
                vol_fmt = f"${vol/1_000_000:.1f}M" if vol >= 1_000_000 else f"${vol/1_000:.1f}K" if vol >= 1_000 else f"${vol:.0f}"
                markets = ev.get("markets") or []
                lines = [f"Polymarket: {title} (Total Volume: {vol_fmt})", ""]
                if desc:
                    lines.append(f"Description: {desc[:400]}")
                    lines.append("")
                lines.append("Markets & Odds:")
                for mkt in markets[:5]:
                    q_text = mkt.get("question") or title
                    try:
                        outcomes = _j.loads(mkt.get("outcomes", "[]")) if isinstance(mkt.get("outcomes"), str) else (mkt.get("outcomes") or [])
                        prices = _j.loads(mkt.get("outcomePrices", "[]")) if isinstance(mkt.get("outcomePrices"), str) else (mkt.get("outcomePrices") or [])
                        odds = []
                        for oc, pr in zip(outcomes, prices):
                            odds.append(f"{oc}: {round(float(pr)*100)}%")
                        lines.append(f"- {q_text}: {' · '.join(odds)}")
                    except Exception:
                        pass
                return _trim("\n".join(lines), max_chars)
    except Exception:
        pass
    return ""


async def _pypi_pkg(client: PoliteClient, url: str, max_chars: int) -> str:
    m = re.search(r"/project/([A-Za-z0-9_.-]+)", url)
    if not m:
        return ""
    pkg = m.group(1)
    r = await client.get(f"https://pypi.org/pypi/{pkg}/json")
    if r.status_code != 200:
        return ""
    info = r.json().get("info", {})
    desc = info.get("description") or info.get("summary") or ""
    header = f"{info.get('name')} {info.get('version')} — {info.get('summary','')}\nLicense: {info.get('license','')}\n\n"
    return _trim(header + desc, max_chars)


async def _crates_pkg(client: PoliteClient, url: str, max_chars: int) -> str:
    m = re.search(r"/crates/([A-Za-z0-9_.-]+)", url)
    if not m:
        return ""
    pkg = m.group(1)
    r = await client.get(f"https://crates.io/api/v1/crates/{pkg}")
    if r.status_code != 200:
        return ""
    crate = r.json().get("crate", {})
    header = f"{crate.get('name')} v{crate.get('max_version')} — {crate.get('description','')}\nDownloads: {crate.get('downloads')} · License: {crate.get('license','')}\n"
    readme_r = await client.get(f"https://crates.io/api/v1/crates/{pkg}/readme")
    body = readme_r.text if readme_r.status_code == 200 else (crate.get("description") or "")
    return _trim(header + "\n" + body, max_chars)


def _pdf_text(r, max_chars: int) -> str:
    """Extract text from a downloaded PDF response. Needs the optional pypdf extra."""
    try:
        import io
        from pypdf import PdfReader
    except ImportError:
        return "[pdf: install the 'pdf' extra (pip install \"infoseek[pdf]\") to extract PDF text]"
    try:
        reader = PdfReader(io.BytesIO(r.content))
        pages = [(p.extract_text() or "") for p in reader.pages[:20]]
        txt = re.sub(r"[ \t]+", " ", "\n".join(pages)).strip()
    except Exception:
        return ""
    return _trim(txt, max_chars)


_BOT_WALL = re.compile(
    r"just a moment|verifying your browser|making sure you.?re not a bot|"
    r"attention required|cf-browser-verification|are you a robot|"
    r"please enable (?:cookies|javascript) to continue|captcha", re.I)


def _looks_bot_blocked(html: str) -> bool:
    """Cheap anti-bot challenge detector (Cloudflare & friends)."""
    return bool(_BOT_WALL.search(html[:4000])) and len(html) < 60000


def _html_to_text(html: str, url: str, max_chars: int, markdown: bool = True) -> str:
    """Shared HTML -> clean text/markdown pipeline (trafilatura -> heuristic -> meta)."""
    html = _sanitize_html(html)
    txt = None
    if markdown:
        try:
            txt = trafilatura.extract(
                html, url=url, include_comments=False, include_tables=True,
                include_formatting=True, include_links=True, include_images=False,
                output_format="markdown", favor_precision=True
            )
            if not txt or len(txt) < 100:
                txt = trafilatura.extract(
                    html, url=url, include_comments=False, include_tables=True,
                    include_formatting=True, include_links=True, include_images=False,
                    output_format="markdown"
                )
        except Exception:
            txt = None

    if not txt or len(txt) < 80:
        txt = trafilatura.extract(html, url=url, include_comments=False, include_tables=True,
                                  include_formatting=False, include_links=False, include_images=False,
                                  favor_precision=True)
        if not txt or len(txt) < 120:
            txt = trafilatura.extract(html, url=url, include_comments=False, include_tables=True,
                                      include_formatting=False, include_links=False, include_images=False)
    if not txt:
        txt = _heuristic(html)
    if not txt:
        txt = _meta_desc(html)
    return _trim(txt or "", max_chars)


async def _live_extract(client: PoliteClient, url: str, path: str, max_chars: int,
                        raw: bool = False, markdown: bool = True) -> str:
    """Fetch from the origin site and extract. Returns '' on any failure
    (network, non-200, bot wall, unparseable) so the caller can climb the
    access ladder (Wayback -> Jina)."""
    ext = os.path.splitext(path.lower())[1]
    try:
        r = await client.get(url)
    except httpx.HTTPError:
        return ""
    if r.status_code != 200:
        return ""
    if raw:
        return _trim(r.text, max_chars)
    if ext in _RAW_EXTENSIONS:
        return _trim(r.text, max_chars)
    if _looks_bot_blocked(r.text):
        return ""
    ctype = r.headers.get("content-type", "").lower()
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        return _pdf_text(r, max_chars)
    if "text/plain" in ctype or "text/markdown" in ctype or "application/json" in ctype or "application/javascript" in ctype:
        return _trim(r.text, max_chars)
    if "html" not in ctype and "text" not in ctype:
        return ""
    text = _html_to_text(r.text, url, max_chars, markdown=markdown)
    if not text and r.text:
        soup = BeautifulSoup(r.text, "lxml")
        raw_t = soup.get_text("\n", strip=True)
        if raw_t:
            text = _trim(raw_t, max_chars)
    return text


async def _wayback_snapshot(client: PoliteClient, url: str) -> str:
    """Nearest archived snapshot URL (Wayback availability API). '' if none."""
    try:
        r = await client.get("https://archive.org/wayback/available",
                             params={"url": url}, timeout=10)
        if r.status_code == 200:
            snap = ((r.json().get("archived_snapshots") or {}).get("closest") or {})
            if snap.get("available"):
                return snap.get("url") or ""
    except Exception:
        pass
    return ""


async def _wayback_cdx_snapshots(client: PoliteClient, url: str, limit: int = 2) -> list:
    """Most recent archived snapshot URLs via CDX (fallback candidate list)."""
    try:
        target = re.sub(r"^https?://", "", url)
        r = await client.get("https://web.archive.org/cdx/search/cdx",
                             params={"url": target, "output": "json", "limit": limit + 2,
                                     "fastLatest": "true", "filter": "statuscode:200"},
                             timeout=15)
        if r.status_code != 200:
            return []
        rows = r.json()
        if len(rows) < 2:
            return []
        head, body = rows[0], rows[1:]
        idx = {k: i for i, k in enumerate(head)}
        out = []
        for row in body:
            ts = row[idx.get("timestamp", 1)]
            orig = row[idx.get("original", 2)]
            out.append(f"https://web.archive.org/web/{ts}/{orig}")
        return out[:limit]
    except Exception:
        return []


async def _fetch_snapshot(client: PoliteClient, snap: str, url: str, max_chars: int,
                          markdown: bool = True) -> str:
    """Fetch one snapshot URL as the raw original ('id_' flag, no toolbar)."""
    if "id_" not in snap:
        snap = re.sub(r"(/web/[^/]+)", r"\1id_", snap, count=1)
    try:
        r = await client.get(snap, timeout=25)
    except httpx.HTTPError:
        return ""
    if r.status_code != 200:
        return ""
    ctype = r.headers.get("content-type", "").lower()
    if "html" not in ctype and "text" not in ctype:
        return ""
    if "text/plain" in ctype or "text/markdown" in ctype:
        return _trim(r.text, max_chars)
    return _html_to_text(r.text, url, max_chars, markdown=markdown)


async def _wayback_extract(client: PoliteClient, url: str, max_chars: int,
                           markdown: bool = True) -> str:
    """Extract a page from the Wayback Machine. Works for pages that block
    scrapers or no longer exist: the fetch never touches the origin site.
    Tries the closest snapshot first, then the most recent ones via CDX."""
    snap = await _wayback_snapshot(client, url)
    if snap:
        txt = await _fetch_snapshot(client, snap, url, max_chars, markdown=markdown)
        if txt:
            return txt
    for snap in await _wayback_cdx_snapshots(client, url, limit=2):
        txt = await _fetch_snapshot(client, snap, url, max_chars, markdown=markdown)
        if txt:
            return txt
    return ""


async def _jina_extract(client: PoliteClient, url: str, max_chars: int) -> str:
    """Jina Reader (r.jina.ai): renders JS-heavy and challenging pages into clean markdown.
    Uses JINA_API_KEY if present, or falls back to public keyless tier."""
    headers = {
        "Accept": "text/plain, text/markdown",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    }
    key = os.environ.get("JINA_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        r = await client.get("https://r.jina.ai/" + url, timeout=25, headers=headers)
        if r.status_code == 200 and r.text.strip():
            if not _looks_bot_blocked(r.text):
                return _trim(r.text, max_chars)
    except Exception:
        pass
    return ""


_RAW_EXTENSIONS = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".md", ".txt", ".rs", ".go", ".sh", ".bash", ".zsh", ".toml", ".ini", ".cfg",
    ".sql", ".c", ".cpp", ".h", ".hpp", ".cs", ".java", ".kt", ".rb", ".php", ".xml",
    ".csv", ".tsv", ".rst", ".proto"
}


async def _access_ladder(client: PoliteClient, url: str, path: str, max_chars: int,
                         raw: bool = False, markdown: bool = True) -> str:
    """Generic-page access ladder: origin first (robots-respected if configured),
    then public archives and keyless Jina Reader. Archive and Jina fetches never touch
    the origin site, allowing bot-blocked or JS-rendered pages to be read cleanly."""
    if not client.respect_robots or await client.allowed(url):
        txt = await _live_extract(client, url, path, max_chars, raw=raw, markdown=markdown)
        if txt:
            return txt
    txt = await _wayback_extract(client, url, max_chars, markdown=markdown)
    if not txt:
        txt = await _jina_extract(client, url, max_chars)
    return txt


async def extract_url(client: PoliteClient, url: str, max_chars: int = 10000,
                      raw: bool = False, markdown: bool = True) -> str:
    """Fetch and extract clean text or markdown from one URL.

    Domain fast paths (GitHub/Wikipedia/HN/Reddit/PyPI/crates/raw files) use
    official APIs first; anything that comes back empty climbs the access
    ladder (live fetch -> Wayback Machine -> keyless Jina Reader)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    host = parsed.netloc.lower()
    path = parsed.path

    # Fast path: Gists
    if "gist.github.com" in host:
        seg = path.strip("/").split("/")
        if len(seg) >= 2:
            raw_gist = f"https://gist.githubusercontent.com/{seg[0]}/{seg[1]}/raw"
            try:
                r = await client.get(raw_gist)
                if r.status_code == 200:
                    return _trim(r.text, max_chars)
            except Exception:
                pass
        return await _access_ladder(client, url, path, max_chars, raw=raw, markdown=markdown)

    # Fast path: raw code / raw GitHub / pastebins
    if "raw.githubusercontent.com" in host or "gist.githubusercontent.com" in host or "pastebin.com/raw" in url:
        try:
            r = await client.get(url)
            if r.status_code == 200:
                return _trim(r.text, max_chars)
        except httpx.HTTPError:
            pass
        return await _access_ladder(client, url, path, max_chars, raw=raw, markdown=markdown)

    # Fast path: Hugging Face model cards
    if "huggingface.co" in host or "hf.co" in host:
        seg = path.strip("/").split("/")
        if len(seg) >= 2 and seg[0] not in ("spaces", "datasets", "docs", "blog", "posts", "join", "login", "api"):
            owner, model = seg[0], seg[1]
            raw_readme = f"https://huggingface.co/{owner}/{model}/raw/main/README.md"
            try:
                r = await client.get(raw_readme)
                if r.status_code == 200 and r.text.strip():
                    return _trim(r.text, max_chars)
            except Exception:
                pass
            try:
                r = await client.get(f"https://huggingface.co/api/models/{owner}/{model}", timeout=8)
                if r.status_code == 200:
                    info = r.json()
                    desc = f"Hugging Face Model: {info.get('id', '')}\nPipeline: {info.get('pipeline_tag','')}\nDownloads: {info.get('downloads',0)} · Likes: {info.get('likes',0)}\nTags: {', '.join(info.get('tags',[]))}"
                    return _trim(desc, max_chars)
            except Exception:
                pass

    # Fast path: Google News article redirects
    if "news.google.com" in host:
        p_parts = path.strip("/").split("/")
        if len(p_parts) >= 2 and p_parts[-2] in ("articles", "read"):
            base64_str = p_parts[-1]
            import base64
            try:
                pad = len(base64_str) % 4
                decoded_bytes = base64.urlsafe_b64decode(base64_str + ("=" * (4 - pad) if pad else ""))
                decoded_str = decoded_bytes.decode("latin1", "ignore")
                prefix = bytes([0x08, 0x13, 0x22]).decode("latin1")
                if decoded_str.startswith(prefix):
                    decoded_str = decoded_str[len(prefix):]
                    suffix = bytes([0xD2, 0x01, 0x00]).decode("latin1")
                    if decoded_str.endswith(suffix):
                        decoded_str = decoded_str[:-len(suffix)]
                    bytes_array = bytearray(decoded_str, "latin1")
                    length = bytes_array[0]
                    target_url = decoded_str[2:length+1] if length >= 0x80 else decoded_str[1:length+1]
                    if target_url.startswith("http"):
                        return await extract_url(client, target_url, max_chars=max_chars, raw=raw, markdown=markdown)
            except Exception:
                pass

    # Official-API and GitHub fast paths
    if "github.com" in host:
        p = path.strip("/")
        seg = p.split("/")
        # owner/repo/blob/branch/filepath -> fetch raw file directly
        if len(seg) >= 4 and seg[2] == "blob":
            owner, repo = seg[0], seg[1]
            branch_and_file = "/".join(seg[3:])
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch_and_file}"
            try:
                r = await client.get(raw_url)
                if r.status_code == 200:
                    return _trim(r.text, max_chars)
            except Exception:
                pass
        # owner/repo/issues/123 or owner/repo/pull/123 -> issue + comments
        if len(seg) >= 4 and seg[2] in ("issues", "pull") and seg[3].isdigit():
            txt = await _github_issue(client, seg[0], seg[1], seg[3], max_chars)
            if txt:
                return txt
        # owner/repo/releases/tag/v1.2.3 -> release notes
        if len(seg) >= 5 and seg[2] == "releases" and seg[3] == "tag":
            txt = await _github_release_page(client, seg[0], seg[1], unquote(seg[4]), max_chars)
            if txt:
                return txt
        # owner/repo/blob/.../CHANGELOG.md -> raw changelog file
        if len(seg) >= 4 and seg[2] == "blob" and re.search(
                r"(?i)^(changelog|changes|history|whatsnew|release[-_]?notes)\.(md|rst|txt)$",
                seg[-1]):
            txt = await _github_changelog_file(client, seg[0], seg[1], max_chars)
            if txt:
                return txt
        txt = await _github_readme(client, url, max_chars)
        return txt or await _access_ladder(client, url, path, max_chars, raw=raw, markdown=markdown)
    if "wikipedia.org" in host:
        txt = await _wikipedia(client, url, max_chars)
        return txt or await _access_ladder(client, url, path, max_chars, raw=raw, markdown=markdown)
    if "news.ycombinator.com" in host:
        txt = await _hn_item(client, url, max_chars)
        return txt or await _access_ladder(client, url, path, max_chars, raw=raw, markdown=markdown)
    if "reddit.com" in host:
        txt = await _reddit(client, url, max_chars)
        return txt or await _access_ladder(client, url, path, max_chars, raw=raw, markdown=markdown)
    if "polymarket.com" in host and ("/event/" in url or "/market/" in url):
        txt = await _polymarket_event(client, url, max_chars)
        return txt or await _access_ladder(client, url, path, max_chars, raw=raw, markdown=markdown)
    if "pypi.org" in host and "/project/" in url:
        txt = await _pypi_pkg(client, url, max_chars)
        return txt or await _access_ladder(client, url, path, max_chars, raw=raw, markdown=markdown)
    if "crates.io" in host and "/crates/" in url:
        txt = await _crates_pkg(client, url, max_chars)
        return txt or await _access_ladder(client, url, path, max_chars, raw=raw, markdown=markdown)
    return await _access_ladder(client, url, path, max_chars, raw=raw, markdown=markdown)

async def extract_many(client: PoliteClient, urls: list[str], max_chars: int = 1200,
                       concurrency: int = 3, query: str | None = None,
                       markdown: bool = True) -> list[dict]:
    """Extract several URLs in parallel. If query is given, keep only the sentences
    most relevant to it (see relevant_sentences) — content that really matters."""
    sem = asyncio.Semaphore(concurrency)
    fetch_budget = max(6000, max_chars * 2)

    async def one(url: str) -> dict:
        async with sem:
            try:
                text = await extract_url(client, url, max_chars=fetch_budget, markdown=markdown)
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


def relevant_sentences(text: str, query: str, max_chars: int = 1200,
                       focus: str | None = None) -> str:
    """Keep the blocks and sentences that matter for the query: term overlap, phrase hits,
    lead-position bonus, and code blocks, ordered as they appear. Token-lean by construction.

    focus='version' additionally boosts sentences with version numbers and
    compatibility phrasing; focus='error' boosts fix/solution/error lines
    (auto-detected from the query when focus is None)."""
    text = (text or "").strip()
    if not text:
        return ""
    terms = [t for t in re.split(r"\W+", query.lower()) if t not in _STOP and len(t) > 2]
    if not terms:
        return _trim(text, max_chars)
    if focus is None:
        focus = auto_focus(query)

    # Preserve fenced code blocks as atomic chunks, split prose on paragraphs or sentence boundaries
    raw_chunks = []
    code_pattern = re.compile(r"```[\s\S]*?```")
    last_idx = 0
    for match in code_pattern.finditer(text):
        pre = text[last_idx:match.start()].strip()
        if pre:
            for p in re.split(r"(?<=[.!?])\s+|\n{2,}", pre):
                p_clean = p.strip()
                if p_clean:
                    raw_chunks.append(p_clean)
        raw_chunks.append(match.group(0).strip())
        last_idx = match.end()
    rest = text[last_idx:].strip()
    if rest:
        for p in re.split(r"(?<=[.!?])\s+|\n{2,}", rest):
            p_clean = p.strip()
            if p_clean:
                raw_chunks.append(p_clean)

    if not raw_chunks:
        return _trim(text, max_chars)

    scored = []
    for i, s in enumerate(raw_chunks):
        low = s.lower()
        hits = sum(low.count(t) for t in terms)
        if hits == 0 and not s.startswith("```"):
            continue
        score = hits * 2.0 + (2.0 if len(terms) == 1 else 0) + (1.5 if i < 3 else 0)
        if s.startswith("```"):
            score += 2.0
        if focus == "version":
            if VERSION_RE.search(s):
                score += 3.0
            if COMPAT_WORDS.search(s):
                score += 2.0
        elif focus == "error":
            if SOLUTION_WORDS.search(s):
                score += 3.0
            if ERROR_WORDS.search(s):
                score += 1.5
        scored.append((score, i, s))
    if not scored:
        return _trim(text, max_chars)
    scored.sort(key=lambda x: -x[0])

    # Fill character budget
    picked = []
    curr_len = 0
    for score, idx, chunk in scored:
        if curr_len + len(chunk) + 2 <= max_chars * 1.15 or not picked:
            picked.append((idx, chunk))
            curr_len += len(chunk) + 2
        if curr_len >= max_chars:
            break

    picked.sort(key=lambda x: x[0])
    separator = "\n\n" if any("\n" in s for _, s in picked) else " "
    return _trim(separator.join(s for _, s in picked), max_chars)
