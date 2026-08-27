"""Content extraction: domain-aware fast paths + trafilatura + heuristic fallback."""
import asyncio, re
from urllib.parse import urlparse, unquote, quote
import httpx, trafilatura
from bs4 import BeautifulSoup
from .net import PoliteClient

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


_RAW_EXTENSIONS = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".md", ".txt", ".rs", ".go", ".sh", ".bash", ".zsh", ".toml", ".ini", ".cfg",
    ".sql", ".c", ".cpp", ".h", ".hpp", ".cs", ".java", ".kt", ".rb", ".php", ".xml",
    ".csv", ".tsv", ".rst", ".proto"
}


async def extract_url(client: PoliteClient, url: str, max_chars: int = 2000) -> str:
    """Fetch and extract clean text from one URL (respects robots.txt unless disabled)."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path

    # Fast path: raw code / raw GitHub / Gists / pastebins
    if "raw.githubusercontent.com" in host or "gist.githubusercontent.com" in host or "pastebin.com/raw" in url:
        try:
            r = await client.get(url)
            if r.status_code == 200:
                return _trim(r.text, max_chars)
        except httpx.HTTPError:
            return ""
        return ""

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
        return await _github_readme(client, url, max_chars)
    if "wikipedia.org" in host:
        return await _wikipedia(client, url, max_chars)
    if "news.ycombinator.com" in host:
        return await _hn_item(client, url, max_chars)
    if "reddit.com" in host:
        return await _reddit(client, url, max_chars)
    if "pypi.org" in host and "/project/" in url:
        return await _pypi_pkg(client, url, max_chars)
    if "crates.io" in host and "/crates/" in url:
        return await _crates_pkg(client, url, max_chars)
    if not await client.allowed(url):
        return f"[skipped: robots.txt of {host} disallows this fetch]"

    # Check for direct file extensions
    ext = os.path.splitext(path.lower())[1]
    if ext in _RAW_EXTENSIONS:
        try:
            r = await client.get(url)
            if r.status_code == 200:
                return _trim(r.text, max_chars)
        except httpx.HTTPError:
            return ""

    try:
        r = await client.get(url)
    except httpx.HTTPError:
        return ""
    if r.status_code != 200:
        return ""
    ctype = r.headers.get("content-type", "").lower()
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        return _pdf_text(r, max_chars)
    if "text/plain" in ctype or "text/markdown" in ctype or "application/json" in ctype or "application/javascript" in ctype:
        return _trim(r.text, max_chars)
    if "html" not in ctype and "text" not in ctype:
        return ""
    html = _sanitize_html(r.text)
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
        from .research import auto_focus
        focus = auto_focus(query)
    if focus in ("version", "error"):
        from .research import VERSION_RE, COMPAT_WORDS, ERROR_WORDS, SOLUTION_WORDS

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
