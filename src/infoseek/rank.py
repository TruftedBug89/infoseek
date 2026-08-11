"""URL normalization, dedup, and quality-scored merging."""
from dataclasses import dataclass, asdict
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from difflib import SequenceMatcher
import re

TRACKING = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid","gclsrc","mc_cid","mc_eid","ref","ref_src","igshid"}
CTA = re.compile(r"\b(?:discover|learn more|read more|click here|sign up|subscribe|get started|see more|view all|read the full|keep reading)\b", re.I)
SUFFIX = re.compile(r"\s*[-|–—:]\s*[A-Z][A-Za-z0-9 .&'()]{2,40}$")

# Source priority (higher = more trust at same rank). Tuned: general web + expert Q&A
# first, forums/news after.
PRIORITY = {"ddg": 10, "so": 9, "hn": 8, "gh": 8, "wiki": 8, "arxiv": 7, "reddit": 7,
            "lobsters": 7, "news": 6, "code": 6, "serper": 10, "brave": 10, "searxng": 10,
            "marginalia": 5}


@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    rank: int = 0          # position within its engine
    date: str = ""
    extra: str = ""        # extra signal (stars, score, tags...) shown inline
    score: float = 0.0     # merged relevance score (filled by merge_scored)


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


def merge_scored(groups: list[list[Result]], n: int, order: list[str]) -> list[Result]:
    """Score-driven merge: priority + rank + recency, with per-source diversity cap."""
    by_src: dict[str, list[Result]] = {}
    for g in groups:
        for r in g:
            by_src.setdefault(r.source, []).append(r)
    scored: list[Result] = []
    for src, lst in by_src.items():
        for r in lst:
            r.score = PRIORITY.get(src, 5) - r.rank * 1.6 + _recency_bonus(r)
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


def merge(groups: list[list[Result]], n: int, order: list[str]) -> list[Result]:
    return merge_scored(groups, n, order)


def to_dicts(results: list[Result]) -> list[dict]:
    return [asdict(r) for r in results]
