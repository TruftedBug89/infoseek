"""URL normalization, dedup, and quality-scored merging."""
from dataclasses import dataclass, asdict
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from difflib import SequenceMatcher
import math
import re

TRACKING = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid","gclsrc","mc_cid","mc_eid","ref","ref_src","igshid"}
CTA = re.compile(r"\b(?:discover|learn more|read more|click here|sign up|subscribe|get started|see more|view all|read the full|keep reading)\b", re.I)
SUFFIX = re.compile(r"\s*[-|–—:]\s*[A-Z][A-Za-z0-9 .&'()]{2,40}$")

# Source priority (higher = more trust at same rank). Tuned: general web + expert Q&A
# first, forums/news after.
PRIORITY = {
    "ddg": 10, "pypi": 10, "npm": 10, "crates": 10, "mdn": 10,
    "so": 9, "hn": 8, "gh": 8, "wiki": 8, "arxiv": 8, "openalex": 8,
    "polymarket": 9, "techmeme": 8, "bluesky": 7, "stocktwits": 7,
    "pubmed": 8, "crossref": 7, "wikidata": 7, "reddit": 7,
    "lobsters": 7, "wayback": 7, "commoncrawl": 6, "news": 6, "code": 6, "yt": 6,
    "serper": 10, "brave": 10, "searxng": 10, "swarm": 9, "marginalia": 5
}


@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    rank: int = 0          # position within its engine
    date: str = ""
    extra: str = ""        # extra signal (stars, score, tags...) shown inline
    score: float = 0.0     # merged relevance score (filled by merge)
    upvotes: int = 0       # community upvotes / likes / score
    comments: int = 0      # comment / discussion reply count
    engagement_str: str = "" # formatted human-readable signal (e.g. '$1.2M vol · 84% Yes')


def normalize_url(u: str) -> str:
    try:
        p = urlparse(u)
        host = p.netloc.lower()
        for prefix in ("www.", "m."):
            if host.startswith(prefix) and host[len(prefix):].count(".") >= 1:
                host = host[len(prefix):]
                break
        q = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACKING]
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme, host, path, "", urlencode(q), ""))
    except Exception:
        return u


def clean_title(t: str) -> str:
    """Drop suffix boilerplate like ' - SiteName' / '| SiteName' for near-dup detection."""
    t = " ".join(t.split()).lower()
    t = SUFFIX.sub("", t)
    return t[:80]


def clean(s: str, limit: int = 160) -> str:
    s = " ".join(s.split())
    # cut CTA boilerplate at the trail
    m = CTA.search(s, 30)
    if m:
        s = s[:m.start()].rstrip(" .,;:-–—|")
    if len(s) <= limit:
        return s
    cut = s[:limit]
    i = cut.rfind(" ")
    return (cut[:i] + " …") if i > 40 else cut + " …"


def dedupe(results: list[Result]) -> list[Result]:
    out: list[Result] = []
    seen_url: set[str] = set()
    by_host: dict[str, list[str]] = {}
    for r in results:
        nu = normalize_url(r.url)
        if nu in seen_url:
            continue
        host = urlparse(nu).netloc
        ct = clean_title(r.title)
        if any(SequenceMatcher(None, ct, t).ratio() > 0.86 for t in by_host.get(host, [])):
            continue
        seen_url.add(nu)
        by_host.setdefault(host, []).append(ct)
        out.append(r)
    return out


def _recency_bonus(r: Result) -> float:
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", r.date or "")
    if not m:
        return 0.0
    from datetime import date
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        age = (date.today() - d).days
    except ValueError:
        return 0.0
    if age < 7:
        return 3.0
    if age < 30:
        return 1.5
    if age < 180:
        return 0.5
    return 0.0


def _engagement_bonus(r: Result) -> float:
    """Bonus for social and community engagement (upvotes, comments, volume)."""
    up = r.upvotes or 0
    cm = r.comments or 0
    if not up and not cm and r.extra:
        m_pts = re.search(r"(\d+)\s*(?:points|pts|upvotes|score|likes)", r.extra, re.I)
        if m_pts:
            up = int(m_pts.group(1))
        m_cm = re.search(r"(\d+)\s*(?:comments|cmt|answers|replies)", r.extra, re.I)
        if m_cm:
            cm = int(m_cm.group(1))
        if re.search(r"\$[0-9.,]+[kKmMbB]?\s*vol", r.extra, re.I):
            return 2.5  # high-liquidity prediction market
    total = up + cm * 2
    if total <= 0:
        return 0.0
    return min(3.5, math.log10(total + 1) * 0.9)


def merge(groups: list[list[Result]], n: int, order: list[str]) -> list[Result]:
    """Score-driven merge: priority + rank + recency + engagement, with per-source diversity cap."""
    by_src: dict[str, list[Result]] = {}
    for g in groups:
        for r in g:
            by_src.setdefault(r.source, []).append(r)
    scored: list[Result] = []
    for src, lst in by_src.items():
        for r in lst:
            r.score = PRIORITY.get(src, 5) - r.rank * 1.6 + _recency_bonus(r) + _engagement_bonus(r)
            if not r.snippet and src not in ("code", "gh"):
                r.score -= 2.0
            scored.append(r)
    scored.sort(key=lambda r: (-r.score, order.index(r.source) if r.source in order else 99, r.rank))
    cap = max(1, (n + 1) // 2)
    counts: dict[str, int] = {}
    merged: list[Result] = []
    for r in scored:
        if len(merged) >= n:
            break
        if counts.get(r.source, 0) >= cap:
            continue
        merged.append(r)
        counts[r.source] = counts.get(r.source, 0) + 1
    return dedupe(merged)[:n]


def to_dicts(results: list[Result]) -> list[dict]:
    return [asdict(r) for r in results]
