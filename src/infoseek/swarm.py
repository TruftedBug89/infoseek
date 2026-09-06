"""swarm.py — SearXNG instance swarm: keyless meta-search across
community-hosted SearXNG instances (github.com/searxng/searxng).

Public instances increasingly bot-wall datacenter IPs, so this module is
defensive by design:

* instance list = curated seed + periodic refresh from the official
  searx.space instance list (cached 24 h)
* per-instance health is cached: failures demote an instance for a while,
  so walled/dead instances quickly cost nothing
* each search fans out to a bounded number of instances concurrently and
  keeps the first usable results (as_completed, hard deadline)
* never raises, never blocks the rest of the engine mix

For a guaranteed private + reliable swarm, point SEARXNG_URL at your own
instance (one docker command — see README); it short-circuits the public
swarm automatically.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time

from bs4 import BeautifulSoup

from .rank import Result, clean

SPACE_URL = "https://searx.space/data/instances.json"

#: Curated seed list (stable, long-running community instances).
SEED_INSTANCES = [
    "https://searx.be",
    "https://baresearch.org",
    "https://search.hbubli.cc",
    "https://search.projectsegfau.lt",
    "https://searx.tiekoetter.com",
    "https://paulgo.io",
    "https://opnxng.com",
    "https://priv.au",
    "https://search.mdosch.de",
    "https://searxng.site",
    "https://etsi.me",
    "https://searx.dresden.network",
]

_CHALLENGE = re.compile(
    r"verifying your browser|making sure you.?re not a bot|just a moment|"
    r"challenge-platform|cf-challenge|attention required", re.I)


# ------------------------------------------------------------ instance list

def _parse_space_list(doc: dict) -> list[str]:
    """Extract usable instance URLs from the searx.space instances.json doc."""
    scored: list[tuple[float, str]] = []
    for url, meta in (doc.get("instances") or {}).items():
        if not isinstance(meta, dict) or not str(url).startswith("http"):
            continue
        http = meta.get("http") or {}
        if not isinstance(http, dict) or http.get("status_code") != 200:
            continue
        up = meta.get("uptime") or {}
        week = up.get("uptimeWeek") if isinstance(up, dict) else None
        scored.append((float(week) if isinstance(week, (int, float)) else 50.0,
                       str(url).rstrip("/")))
    scored.sort(key=lambda x: -x[0])
    return [u for _, u in scored[:60]]


async def _discover(c) -> list[str]:
    """Instance list: cache hit, else refresh from searx.space, else seed."""
    from . import cache as _cache
    hit = _cache.get("sxl", "instances", ttl=86400)
    if hit:
        try:
            lst = json.loads(hit)
            if isinstance(lst, list) and lst:
                return lst
        except Exception:
            pass
    out = list(SEED_INSTANCES)
    try:
        r = await c.get(SPACE_URL, timeout=20)
        if r.status_code == 200:
            found = _parse_space_list(r.json())
            for u in found:
                if u not in out:
                    out.append(u)
            _cache.set("sxl", "instances", value=json.dumps(out[:72]), ttl=86400)
    except Exception:
        pass
    return out


# ------------------------------------------------------------ health cache

def _health(base: str) -> tuple[int, float]:
    """(score, last_failure_ts) for an instance, from the cache."""
    from . import cache as _cache
    hit = _cache.get("sxh", base, ttl=604800)
    if hit:
        try:
            d = json.loads(hit)
            return int(d.get("score", 0)), float(d.get("last_fail", 0.0))
        except Exception:
            pass
    return 0, 0.0


def _record(base: str, ok: bool) -> None:
    from . import cache as _cache
    score, last_fail = _health(base)
    if ok:
        score = min(score + 1, 8)
    else:
        score = max(score - 2, -8)
        last_fail = time.time()
    _cache.set("sxh", base, value=json.dumps({"score": score, "last_fail": last_fail}),
               ttl=604800)


def _rank(instances: list[str]) -> list[str]:
    """Healthy first; recently failed (<30 min) or badly scored last."""
    now = time.time()

    def key(b: str) -> tuple:
        score, last_fail = _health(b)
        recent_fail = 1 if now - last_fail < 1800 else 0
        return (recent_fail, -score)

    return sorted(instances, key=key)


# ------------------------------------------------------------ parsing

def parse_results(html: str, n: int) -> list[Result]:
    """Lenient SearXNG HTML result parsing (themes vary)."""
    if _CHALLENGE.search(html[:4000]):
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return []
    out: list[Result] = []
    for el in soup.select("article.result, div.result, li.result"):
        a = el.select_one("h3 a, h4 a, a.url_header") or el.select_one("a[href^='http']")
        if not a:
            continue
        href = a.get("href", "")
        title = a.get_text(" ", strip=True)
        if not href.startswith("http") or not title or title.startswith("http"):
            continue
        sn = el.select_one("p.content, p.snippet, div.content, .result-content")
        out.append(Result(title=title[:160], url=href,
                          snippet=clean(sn.get_text(" ", strip=True)) if sn else "",
                          source="swarm", rank=len(out)))
        if len(out) >= n:
            break
    return out


async def _fetch_one(c, base: str, q: str, n: int) -> tuple[str, list[Result]]:
    try:
        r = await c.get(base + "/search", params={"q": q}, timeout=7)
    except Exception:
        return base, []
    if r.status_code != 200:
        return base, []
    return base, parse_results(r.text, n)


# ------------------------------------------------------------ swarm search

async def swarm_search(c, q: str, n: int = 6, max_instances: int = 6,
                       deadline: float = 8.0) -> list[Result]:
    """Fan out over healthy instances; return the first usable merged results."""
    instances = _rank(await _discover(c))[:max_instances]
    if not instances:
        return []
    results: list[Result] = []
    seen: set[str] = set()
    tasks = [asyncio.ensure_future(_fetch_one(c, b, q, n)) for b in instances]
    stop_at = time.monotonic() + deadline
    try:
        for fut in asyncio.as_completed(tasks, timeout=deadline):
            try:
                base, res = await fut
            except Exception:
                continue
            _record(base, bool(res))
            for r in res:
                if r.url not in seen:
                    seen.add(r.url)
                    results.append(r)
            if len(results) >= n or time.monotonic() > stop_at:
                break
    except asyncio.TimeoutError:
        pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    return results[:n]
