"""research.py — smart research helpers: query expansion, error/compat modes,
version-aware snippet focusing.

Powers the `error:` and `compat:` query prefixes, `smart_search()` (automatic
query reformulation when initial results are poor), and the version/compat/
error focusing used by extraction.

Design notes
------------
* Pure functions (normalize/expand/quality/focus) are offline and unit-testable.
* Async entry points (smart_search/search_error/search_compat) import the
  package lazily inside the function body to avoid circular imports.
* Everything here is keyless; an optional GITHUB_TOKEN raises GitHub limits.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

# ------------------------------------------------------------------ versions
#: Matches version numbers like 1.4, v0.5.1, 2.10.3-rc1, 1.2.3+build.
VERSION_RE = re.compile(r"\bv?(\d+\.\d+(?:\.\d+){0,3}(?:[-+][0-9A-Za-z.\-]+)?)\b")

#: Compatibility / support-matrix phrasing.
COMPAT_WORDS = re.compile(
    r"\b(?:support(?:s|ed)?|requires?|required|introduc(?:ed|es)|added in|"
    r"available (?:in|since|from)|since version|fixed in|minimum|at least|"
    r"compatible|compatibility|ships? (?:with|in)|bundled|upgrade(?:d)? to|"
    r"from version|lands? in|included (?:in|since))\b", re.I)

#: Error / failure phrasing.
ERROR_WORDS = re.compile(
    r"\b(?:error|exception|traceback|failed|failure|fatal|panic|"
    r"crash(?:ed|es|ing)?|segfault|abort(?:ed)?|denied|refused|not found|"
    r"missing|unsupported|invalid|cannot|can't|unable|no such|"
    r"unrecognized|unknown (?:arch|type|option))\b", re.I)

#: Solution / resolution phrasing (what debugging research actually wants).
SOLUTION_WORDS = re.compile(
    r"\b(?:fix(?:ed|es)?|solution|solved|resolve[d]?|workaround|"
    r"patch(?:ed)?|upgrade(?:d)?|downgrad(?:e|ed)|update(?:d)? to|"
    r"closed as completed|merged|shipped in|released in|will be (?:fixed|included))\b",
    re.I)

_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]{2,}\b")
_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s:\"']+")
_UNIX_PATH_RE = re.compile(r"(?<![\w/])(/(?:[\w.\-]+/){2,}[\w.\-]+)")
_LONGNUM_RE = re.compile(r"\b\d{6,}\b")
_FILLER_RE = re.compile(
    r"\b(?:how do i|how to|how can i|what is|what's|why does|why is|why do|"
    r"where can i find|is there a way to|the best way to|does anyone know)\b",
    re.I)


# ------------------------------------------------------------------ detection
def looks_like_error(query: str) -> bool:
    """True when the query reads like an error message / failure report."""
    q = query or ""
    if ERROR_WORDS.search(q):
        return True
    if re.search(r"\btraceback\b", q, re.I):
        return True
    # quoted exception identifiers, e.g. "UnsupportedArchError: bailingmoe3"
    if re.search(r"[A-Za-z]+(Error|Exception|Fault)\b", q):
        return True
    return False


def looks_like_version_query(query: str) -> bool:
    """True when the query is really asking about versions / compatibility."""
    q = query or ""
    if re.search(r"\b(?:version|versions|support|supports|supported|compat(?:ible|ibility)?|"
                 r"requires?|introduc|added in|which .* (?:version|release))\b", q, re.I):
        return True
    return bool(VERSION_RE.search(q))


def auto_focus(query: str) -> str | None:
    """Pick an extraction focus for a query: 'error', 'version', or None."""
    if looks_like_error(query):
        return "error"
    if looks_like_version_query(query):
        return "version"
    return None


# ------------------------------------------------------------------ cleaning
def normalize_error_message(msg: str) -> str:
    """Clean an error message for searching.

    Drops machine-specific noise (file paths, UUIDs, hex addresses, long
    numbers), unwraps quotes around identifiers, keeps error codes and
    distinctive identifiers (e.g. 'bailingmoe3'). Capped at ~160 chars.
    """
    s = (msg or "").strip()
    s = _UUID_RE.sub(" ", s)
    s = _HEX_RE.sub(" ", s)
    s = _WIN_PATH_RE.sub(" ", s)
    s = _UNIX_PATH_RE.sub(" ", s)
    s = _LONGNUM_RE.sub(" ", s)
    s = re.sub(r"['‘’]([^'‘’]{1,40})['‘’]", r"\1", s)  # unwrap quotes
    s = re.sub(r"\"([^\"]{1,40})\"", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip(" \t\n\r:,-|")
    if len(s) > 160:
        s = s[:160].rsplit(" ", 1)[0]
    return s


# ------------------------------------------------------------------ expansion
def expand_queries(query: str, max_variants: int = 3) -> list[str]:
    """Deterministic query reformulations for a second search round.

    Strategies: strip question filler to a keyword core; quote distinctive
    technical tokens (digits/camelCase/snake_case — e.g. bailingmoe3);
    add version/support terms for compatibility questions; add fix/workaround
    terms for error reports. Never returns the original query.
    """
    q = (query or "").strip()
    if not q:
        return []
    out: list[str] = []

    core = re.sub(r"\s+", " ", _FILLER_RE.sub(" ", q)).strip(" ?.")
    if core and core.lower() != q.lower() and len(core.split()) >= 2:
        out.append(core)

    tech = [t for t in re.split(r"\s+", q)
            if len(t) > 3 and re.search(r"\d|[a-z][A-Z]|_|-", t.strip('"‘’()'))]
    if tech:
        qv = q
        for t in tech[:2]:
            bare = t.strip('"‘’()')
            if bare and f'"{bare}"' not in qv:
                qv = qv.replace(bare, f'"{bare}"', 1)
        if qv.strip() != q:
            out.append(qv.strip())

    base = core or q
    if looks_like_version_query(q) and not re.search(r"\bversion\b", q, re.I):
        out.append(f"{base} version support")
    if looks_like_error(q):
        out.append(f"{base} fix OR workaround github issue")
    if not re.search(r"\b(github|issue)\b", q, re.I):
        out.append(f"{base} github issue")

    seen = {q.lower()}
    uniq: list[str] = []
    for v in out:
        key = v.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(v)
    return uniq[:max_variants]


# ------------------------------------------------------------------ quality
@dataclass
class Quality:
    """How well a result list answers the query (drives smart_search retries)."""
    ok: bool
    score: float
    reasons: list = field(default_factory=list)


def _result_field(r, name: str) -> str:
    v = r.get(name, "") if isinstance(r, dict) else getattr(r, name, "")
    return v or ""


def quality(results, query: str) -> Quality:
    """Score a result list: coverage (>=4 results), term overlap, and — for
    version-flavored queries — whether any snippet actually contains a version
    number. ok=True means 'good enough, no reformulation needed'."""
    results = list(results or [])
    if not results:
        return Quality(False, 0.0, ["no results"])
    terms = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2]
    hits = 0
    ver_hits = 0
    for r in results:
        blob = (_result_field(r, "title") + " " + _result_field(r, "snippet")).lower()
        if terms and any(t in blob for t in terms):
            hits += 1
        if VERSION_RE.search(blob):
            ver_hits += 1
    overlap = hits / len(results)
    score = min(len(results) / 4.0, 1.0) * 0.5 + overlap * 0.5
    reasons = [f"{len(results)} results", f"term overlap {overlap:.0%}"]
    if looks_like_version_query(query):
        if ver_hits:
            score += 0.1
            reasons.append(f"{ver_hits} version-bearing snippets")
        else:
            score -= 0.25
            reasons.append("no version numbers in snippets")
    return Quality(score >= 0.55, round(score, 3), reasons)


# ------------------------------------------------------------------ focusing
def focus_snippet(text: str, query: str, max_len: int = 160, focus: str | None = None) -> str:
    """Re-center a snippet on its most decision-relevant line: version numbers
    and compatibility statements for version queries; fix/solution lines for
    error queries. Falls back to the original text when nothing scores."""
    from .rank import clean
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]
    if not sents:
        return (text or "")[:max_len]
    focus = focus or auto_focus(query)
    if focus not in ("version", "error"):
        return (text or "")[:max_len]
    terms = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2]
    best, best_score = None, 0.0
    for s in sents:
        low = s.lower()
        score = sum(1.0 for t in terms if t in low)
        if focus == "version":
            if VERSION_RE.search(s):
                score += 3.0 + (1.0 if score else 0.0)
            if COMPAT_WORDS.search(s):
                score += 2.0
        else:  # error
            if SOLUTION_WORDS.search(s):
                score += 3.0 + (1.0 if score else 0.0)
            if ERROR_WORDS.search(s):
                score += 1.5
        if score > best_score:
            best, best_score = s, score
    if best is None or best_score < 2.0:
        return (text or "")[:max_len]
    return clean(best, max_len)


# ------------------------------------------------------------------ async API
async def smart_search(query: str, n: int = 6, fresh: bool = False,
                       max_expansions: int = 3) -> list[dict]:
    """Search with automatic reformulation: run the query, score the results
    (coverage + term overlap + version coverage), and if they are poor, fan out
    over expand_queries() variants and merge the better candidates in.
    Returns the same dict shape as infoseek.search()."""
    from . import search as _search, search_many as _search_many
    results = await _search(query, n=n, fresh=fresh)
    q = quality(results, query)
    if q.ok or max_expansions <= 0:
        return results
    variants = expand_queries(query, max_expansions)
    if not variants:
        return results
    more = await _search_many(variants[:max_expansions], n=n, fresh=fresh)
    seen = {r.get("url") for r in results if r.get("url")}
    merged = list(results)
    for r in more:
        u = r.get("url")
        if u and u not in seen:
            seen.add(u)
            merged.append(r)
    merged.sort(key=lambda r: -(r.get("score") or 0.0))
    return merged[:n]


async def search_error(message: str, n: int = 6, fresh: bool = False) -> list[dict]:
    """Search an error message + solutions: GitHub issues, Stack Overflow, and
    the general web in parallel, merged and re-ranked so results mentioning
    fixes/workarounds/patches float to the top. The message is normalized
    first (paths/UUIDs/hex noise dropped, identifiers kept)."""
    from . import search as _search
    from .rank import Result as _R, merge as _merge, to_dicts
    core = normalize_error_message(message)
    if not core:
        return []
    l1, l2, l3 = await asyncio.gather(
        _search(f"issues: {core}", n=max(n, 4), fresh=fresh),
        _search(f"so: {core}", n=max(n, 4), fresh=fresh),
        _search(f"{core} fix OR solution", n=max(n, 4), fresh=fresh),
    )
    groups = [[_R(**d) for d in lst] for lst in (l1, l2, l3)]
    order = ["gh_issues", "so", "ddg", "hn", "reddit", "news"]
    merged = _merge(groups, n, order)
    for r in merged:
        if SOLUTION_WORDS.search(f"{r.title} {r.snippet}".lower()):
            r.score += 2.0
        if r.snippet:
            focused = focus_snippet(r.snippet, core, 160, "error")
            if focused:
                r.snippet = focused
    merged.sort(key=lambda r: -r.score)
    return to_dicts(merged[:n])


async def search_compat(query: str, n: int = 6, fresh: bool = False) -> list[dict]:
    """Version/compatibility lookup — 'which tool version supports X'.
    Fans out over GitHub issues, web variants ('which version supports',
    'added in release'), then re-ranks for version-bearing snippets and
    re-centers each snippet on its version/compatibility line."""
    from . import search as _search
    from .rank import Result as _R, merge as _merge, to_dicts
    q = (query or "").strip()
    if not q:
        return []
    variants = [
        f"issues: {q} support OR version",
        f"{q} which version supports",
        f"{q} added in release changelog",
    ]
    lists = await asyncio.gather(*[_search(v, n=max(n, 4), fresh=fresh) for v in variants])
    groups = [[_R(**d) for d in lst] for lst in lists]
    order = ["gh_issues", "gh_releases", "changelog", "ddg", "hn", "so", "reddit", "news"]
    merged = _merge(groups, n, order)
    for r in merged:
        blob = f"{r.title} {r.snippet}"
        if VERSION_RE.search(blob):
            r.score += 1.5
        if COMPAT_WORDS.search(blob):
            r.score += 1.0
        if r.snippet:
            focused = focus_snippet(r.snippet, q, 160, "version")
            if focused:
                r.snippet = focused
    merged.sort(key=lambda r: -r.score)
    return to_dicts(merged[:n])


async def changelog(project: str, n: int = 6, fresh: bool = False) -> list[dict]:
    """Find changelog / release-notes entries for a project.
    'owner/repo' fetches CHANGELOG* + releases via the GitHub API; anything
    else web-searches for the project's changelog page."""
    from . import search as _search
    return await _search(f"changelog: {project}", n=n, fresh=fresh)
