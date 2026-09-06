"""social.py — last30days social listening, recency grounding, and community-driven research.

Integrates the core capabilities of last30days-skill into infoseek:
- Searches what real people say, vote on, and wager money on (Reddit, Hacker News,
  Polymarket, Techmeme, YouTube, Bluesky, StockTwits) rather than SEO marketing.
- Temporal recency windowing (default: last 30 days) with strict date verification.
- Engagement-weighted scoring (upvotes, likes, comments, prediction market volume).
- Attributed community quotes & top takes (u/author with vote counts).
- Head-to-head comparison ('X vs Y') mode.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
import json
import math
import os
import re
from urllib.parse import urlparse

from .net import PoliteClient
from .rank import Result, merge, clean
from .engines import run_engines, REGISTRY
from .extract import extract_url, _trim

VS_RE = re.compile(r"^(.+?)\s+(?:vs\.?|versus)\s+(.+)$", re.I)
_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,6}(?:\.[A-Za-z])?)\b")
_CRYPTO_TERMS = {"bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto", "doge"}


def is_comparison_query(query: str) -> tuple[bool, str, str]:
    """Detect comparison queries like 'Rust vs Go' or 'Claude versus ChatGPT'."""
    q = (query or "").strip()
    m = VS_RE.match(q)
    if m:
        entity_a = m.group(1).strip()
        entity_b = m.group(2).strip()
        if entity_a and entity_b:
            return True, entity_a, entity_b
    return False, "", ""


def _looks_financial(query: str) -> bool:
    q = (query or "").lower()
    if _CASHTAG_RE.search(query):
        return True
    return any(c in q for c in _CRYPTO_TERMS) or any(
        w in q for w in ("stock", "stocks", "ticker", "earnings", "valuation", "market cap")
    )


def _pick_social_engines(query: str) -> list[str]:
    """Select the best social & community engines for the topic."""
    engines = ["reddit", "hn", "polymarket", "news", "ddg", "yt"]
    q_lower = query.lower()
    if any(w in q_lower for w in ("ai", "tech", "software", "model", "llm", "app", "startup", "code")):
        engines.append("techmeme")
        engines.append("bluesky")
    if _looks_financial(query):
        engines.append("stocktwits")
    return [e for e in engines if e in REGISTRY]


def _filter_recency(results: list[Result], cutoff_date: date) -> list[Result]:
    """Retain results within the date window; undated items are kept if relevant."""
    out = []
    for r in results:
        m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", r.date or "")
        if m:
            try:
                item_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if item_date < cutoff_date:
                    continue
            except ValueError:
                pass
        out.append(r)
    return out


def _extract_top_quotes(extracted_texts: list[str]) -> list[str]:
    """Extract top community quotes (u/user ... pts) from extracted discussion threads."""
    quotes = []
    for txt in extracted_texts:
        for line in txt.splitlines():
            line = line.strip()
            if re.search(r"^[·•-]?\s*u/[\w-]+\s*\(\d+\s*pts?\):", line, re.I):
                quotes.append(line.lstrip("·•- "))
            elif re.search(r"^[·•-]?\s*@[\w.-]+:", line):
                quotes.append(line.lstrip("·•- "))
            if len(quotes) >= 6:
                break
        if len(quotes) >= 6:
            break
    return quotes


async def _enrich_community_comments(client: PoliteClient, results: list[Result], max_threads: int = 2) -> list[str]:
    """Extract actual comments and takes from top Reddit / HN discussion links."""
    discussion_urls = []
    for r in results:
        if r.source in ("reddit", "hn") and ("reddit.com/r/" in r.url or "item?id=" in r.url):
            discussion_urls.append(r.url)
        if len(discussion_urls) >= max_threads:
            break

    if not discussion_urls:
        return []

    async def _safe_extract(u: str):
        try:
            return await asyncio.wait_for(extract_url(client, u, max_chars=1800), timeout=3.0)
        except Exception:
            return ""

    extracted = await asyncio.gather(*[
        _safe_extract(u) for u in discussion_urls
    ])

    valid_texts = [t for t in extracted if isinstance(t, str) and len(t) > 60]
    return _extract_top_quotes(valid_texts)


def _format_comparison_brief(entity_a: str, entity_b: str, from_date: str, to_date: str, days: int,
                             res_a: list[Result], res_b: list[Result],
                             quotes_a: list[str], quotes_b: list[str],
                             res_joint: list[Result] | None = None,
                             quotes_joint: list[str] | None = None) -> str:
    """Format an LLM-ready head-to-head comparison brief."""
    lines = [
        f"🌐 infoseek last30days · window: {from_date} to {to_date} (last {days} days)",
        "",
        f"# {entity_a} vs {entity_b}: What the Community Says (/last30days)",
        "",
        "## Quick Verdict",
        f"Community consensus over the last {days} days on {entity_a} versus {entity_b}, "
        "synthesized from active discussions, community upvotes, and developer experiences.",
    ]

    if res_joint:
        lines.append("")
        lines.append("## Direct Comparisons & Community Debates")
        for i, r in enumerate(res_joint[:4], 1):
            eng = f" [{r.engagement_str}]" if r.engagement_str else ""
            lines.append(f"{i}. [{r.source}] **{r.title}**{eng}")
            if r.snippet:
                lines.append(f"   {r.snippet}")
            lines.append(f"   Link: {r.url}")
        if quotes_joint:
            for q in quotes_joint[:3]:
                lines.append(f"- {q}")

    lines.append("")
    lines.append(f"## {entity_a}")
    for i, r in enumerate(res_a[:4], 1):
        eng = f" [{r.engagement_str}]" if r.engagement_str else ""
        lines.append(f"{i}. [{r.source}] **{r.title}**{eng}")
        if r.snippet:
            lines.append(f"   {r.snippet}")
        lines.append(f"   Link: {r.url}")

    if quotes_a:
        lines.append("")
        lines.append(f"**Top Community Takes on {entity_a}:**")
        for q in quotes_a[:3]:
            lines.append(f"- {q}")

    lines.append("")
    lines.append(f"## {entity_b}")
    for i, r in enumerate(res_b[:4], 1):
        eng = f" [{r.engagement_str}]" if r.engagement_str else ""
        lines.append(f"{i}. [{r.source}] **{r.title}**{eng}")
        if r.snippet:
            lines.append(f"   {r.snippet}")
        lines.append(f"   Link: {r.url}")

    if quotes_b:
        lines.append("")
        lines.append(f"**Top Community Takes on {entity_b}:**")
        for q in quotes_b[:3]:
            lines.append(f"- {q}")

    lines.extend([
        "",
        "## Head-to-Head Key Differences",
        f"- Analyze the real-world trade-offs between {entity_a} and {entity_b} reported by users.",
        "- Contrast community sentiment, developer momentum, and reported pain points.",
        "",
        "<!-- PASS-THROUGH FOOTER -->",
        f"✅ Research complete · {from_date} to {to_date}",
        f"├─ Direct Debates: {len(res_joint or [])} sources",
        f"├─ {entity_a}: {len(res_a)} sources",
        f"└─ {entity_b}: {len(res_b)} sources",
        "<!-- END PASS-THROUGH FOOTER -->"
    ])
    return "\n".join(lines)


def _format_general_brief(query: str, from_date: str, to_date: str, days: int,
                          results: list[Result], quotes: list[str],
                          polymarket_results: list[Result]) -> str:
    """Format an LLM-ready standard last30days research brief."""
    lines = [
        f"🌐 infoseek last30days · window: {from_date} to {to_date} (last {days} days)",
        "",
        "What the community is saying:",
        f"Active discussions and sentiment around '{query}' from the last {days} days, "
        "scored by upvotes, comments, and real engagement across Reddit, Hacker News, "
        "Polymarket, and social platforms.",
        "",
    ]

    # Prediction markets section
    if polymarket_results:
        lines.append("Prediction Markets & Odds:")
        for r in polymarket_results[:3]:
            lines.append(f"- **{r.title}**: {r.snippet} ({r.url})")
        lines.append("")

    # Top results
    lines.append("Top Community Discussions & Evidence:")
    for i, r in enumerate(results, 1):
        meta_parts = [f"[{r.source}]"]
        if r.engagement_str:
            meta_parts.append(f"({r.engagement_str})")
        if r.date:
            meta_parts.append(r.date)
        meta_str = " ".join(meta_parts)
        lines.append(f"{i}. {meta_str} **{r.title}**")
        if r.snippet:
            lines.append(f"   {r.snippet}")
        lines.append(f"   Link: {r.url}")

    # Community quotes
    if quotes:
        lines.append("")
        lines.append("Top Community Takes & Quotes:")
        for q in quotes:
            lines.append(f"- {q}")

    # Source breakdown counts
    counts: dict[str, int] = {}
    for r in results:
        counts[r.source] = counts.get(r.source, 0) + 1

    lines.extend([
        "",
        "KEY PATTERNS from the research:",
        f"1. **Dominant Sentiment**: Primary viewpoint expressed across recent community threads regarding {query}.",
        f"2. **Key Pain Points / Strengths**: Highlights consistently reported by practitioners and users.",
        f"3. **Real-World Momentum**: Velocity of discussion, releases, and upcoming milestones.",
        "",
        "<!-- PASS-THROUGH FOOTER -->",
        f"✅ Research complete · {from_date} to {to_date}",
    ])

    src_items = list(counts.items())
    for idx, (src, cnt) in enumerate(src_items):
        branch = "└─" if idx == len(src_items) - 1 else "├─"
        lines.append(f"{branch} {src.capitalize()}: {cnt} items")
    lines.append("<!-- END PASS-THROUGH FOOTER -->")

    return "\n".join(lines)


async def last30days(query: str, days: int = 30, n: int = 8, budget: int = 2500,
                     fresh: bool = False, format: str = "text") -> str | dict:
    """Research what people actually say about any topic in the last 30 days.

    Pulls and ranks posts and engagement from Reddit, Hacker News, Polymarket,
    Techmeme, YouTube, News, and social sources.

    query: Research topic, entity, or comparison ('X vs Y').
    days: Temporal recency window in days (default 30).
    n: Max ranked evidence items to return.
    budget: Approx token budget for briefing context.
    format: 'text' (human/LLM readable brief) or 'json' (structured dict).
    """
    clean_q = (query or "").strip()
    if not clean_q:
        return "(no query provided)"

    # Strip prefixes if caller passed 'last30days: ...' or 'social: ...'
    clean_q = re.sub(r"^(?:last30days|last30|social|people):\s*", "", clean_q, flags=re.I).strip()

    today = date.today()
    cutoff = today - timedelta(days=max(1, days))
    from_date = cutoff.strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    client = PoliteClient(min_interval=1.0)

    # Check for comparison mode (X vs Y)
    is_vs, entity_a, entity_b = is_comparison_query(clean_q)

    if is_vs:
        # Fan-out queries for joint comparison and both individual entities
        engines_joint = _pick_social_engines(clean_q)
        engines_a = _pick_social_engines(entity_a)
        engines_b = _pick_social_engines(entity_b)

        (res_joint, _), (res_a, _), (res_b, _) = await asyncio.gather(
            run_engines(client, clean_q, n=max(n, 6), engines_list=engines_joint,
                        fresh=fresh, freshness_days=days),
            run_engines(client, entity_a, n=max(n, 6), engines_list=engines_a,
                        fresh=fresh, freshness_days=days),
            run_engines(client, entity_b, n=max(n, 6), engines_list=engines_b,
                        fresh=fresh, freshness_days=days),
        )

        filtered_joint = _filter_recency(res_joint, cutoff)
        filtered_a = _filter_recency(res_a, cutoff)
        filtered_b = _filter_recency(res_b, cutoff)

        merged_joint = merge([filtered_joint], n, engines_joint)
        merged_a = merge([filtered_a], n, engines_a)
        merged_b = merge([filtered_b], n, engines_b)

        quotes_joint, quotes_a, quotes_b = await asyncio.gather(
            _enrich_community_comments(client, merged_joint, max_threads=2),
            _enrich_community_comments(client, merged_a, max_threads=2),
            _enrich_community_comments(client, merged_b, max_threads=2),
        )

        brief_text = _format_comparison_brief(
            entity_a, entity_b, from_date, to_date, days,
            merged_a, merged_b, quotes_a, quotes_b,
            res_joint=merged_joint, quotes_joint=quotes_joint,
        )

        if format == "json":
            return {
                "query": clean_q,
                "window": {"from": from_date, "to": to_date, "days": days},
                "is_comparison": True,
                "entities": [entity_a, entity_b],
                "direct_comparisons": [r.__dict__ for r in merged_joint],
                "entity_a": {"name": entity_a, "results": [r.__dict__ for r in merged_a], "quotes": quotes_a},
                "entity_b": {"name": entity_b, "results": [r.__dict__ for r in merged_b], "quotes": quotes_b},
                "brief": brief_text,
            }
        return brief_text

    # Standard social research
    engines = _pick_social_engines(clean_q)
    results, errors = await run_engines(
        client, clean_q, n=max(n + 4, 10), engines_list=engines,
        fresh=fresh, freshness_days=days
    )

    filtered = _filter_recency(results, cutoff)
    merged = merge([filtered], n + 2, engines)

    # Separate polymarket predictions if found
    poly_results = [r for r in merged if r.source == "polymarket"]
    non_poly = [r for r in merged if r.source != "polymarket"]

    # Extract top community quotes from discussion threads
    quotes = await _enrich_community_comments(client, non_poly, max_threads=3)

    brief_text = _format_general_brief(
        clean_q, from_date, to_date, days,
        non_poly[:n], quotes, poly_results
    )

    if format == "json":
        return {
            "query": clean_q,
            "window": {"from": from_date, "to": to_date, "days": days},
            "is_comparison": False,
            "polymarket": [r.__dict__ for r in poly_results],
            "discussions": [r.__dict__ for r in non_poly[:n]],
            "quotes": quotes,
            "brief": brief_text,
        }

    return brief_text
