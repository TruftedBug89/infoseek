"""Offline tests for the 0.8.0 access ladder, archive engines, swarm, and router."""
import asyncio
import json

import httpx
import pytest

from infoseek.extract import (_looks_bot_blocked, _wayback_extract, extract_url)
from infoseek.net import PoliteClient
from infoseek.swarm import parse_results

LONG_P = ("Choosing an HTTP client library depends on the workload at hand. Synchronous "
          "libraries are simple for scripts, while asynchronous libraries shine when many "
          "requests run concurrently. Benchmarks show connection pooling and keep-alive reuse "
          "dominate raw throughput numbers in real workloads, far more than parser overhead. "
          "Retries, timeouts, and sensible default headers matter just as much as speed.")
CLEAN_HTML = f"<html><head><title>Archived Article</title></head><body><article><p>{LONG_P}</p></article></body></html>"
BOT_WALL = '<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing...</body></html>'


def _client(handler) -> PoliteClient:
    return PoliteClient(min_interval=0.0, transport=httpx.MockTransport(handler))


def test_bot_wall_detected():
    assert _looks_bot_blocked(BOT_WALL)
    assert not _looks_bot_blocked(CLEAN_HTML)


def test_ladder_falls_back_to_wayback_on_bot_wall():
    """Live page bot-blocked -> Wayback snapshot content is served instead."""
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if host == "archive.org" and path == "/wayback/available":
            return httpx.Response(200, json={"archived_snapshots": {"closest": {
                "available": True, "status": "200",
                "url": "http://web.archive.org/web/20260101000000/https://walled.example/x"}}})
        if host == "web.archive.org":
            assert "id_" in path  # raw original, no archive toolbar
            return httpx.Response(200, text=CLEAN_HTML, headers={"content-type": "text/html"})
        return httpx.Response(200, text=BOT_WALL, headers={"content-type": "text/html"})

    out = asyncio.run(extract_url(_client(handler), "https://walled.example/x", max_chars=1000))
    assert "connection pooling" in out


def test_ladder_no_snapshot_returns_empty_not_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "archive.org":
            return httpx.Response(200, json={"archived_snapshots": {}})
        return httpx.Response(403)

    out = asyncio.run(extract_url(_client(handler), "https://gone.example/x", max_chars=500))
    assert out == ""


def test_wayback_engine_cdx_offline():
    from infoseek.engines import wayback
    rows = [["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
            ["com,example)/a", "20260101000000", "https://example.com/a", "text/html", "200", "d1", "1000"],
            ["com,example)/b", "20260201000000", "https://example.com/b", "text/html", "200", "d2", "900"]]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "web.archive.org"
        return httpx.Response(200, json=rows)

    res, err = asyncio.run(wayback(_client(handler), "example.com/*", 5))
    assert err is None and len(res) == 2
    assert res[0].url == "https://web.archive.org/web/20260101000000/https://example.com/a"
    assert res[0].date == "2026-01-01" and res[0].source == "wayback"


def test_commoncrawl_engine_offline(monkeypatch):
    import infoseek.engines as eng
    eng._cc_index.update(id=None, ts=0.0)  # force collinfo fetch
    line = json.dumps({"url": "https://example.com/blog/post", "timestamp": "20260808200549",
                       "status": "200", "filename": "x.warc.gz"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/collinfo.json":
            return httpx.Response(200, json=[{"id": "CC-MAIN-2026-34", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-34-index"}])
        return httpx.Response(200, text=line + "\n" + line)

    res, err = asyncio.run(eng.commoncrawl(_client(handler), "example.com/blog/*", 5))
    assert err is None and len(res) == 2
    assert res[0].url == "https://example.com/blog/post"
    assert "CC-MAIN-2026-34" in res[0].extra


def test_swarm_parse_results_and_challenge():
    html = '''<html><body>
    <article class="result"><h3><a href="https://a.example/1">First Result</a></h3>
      <p class="content">Snippet about python http clients.</p></article>
    <article class="result"><h3><a href="https://b.example/2">Second Result</a></h3>
      <p class="content">Another snippet.</p></article>
    </body></html>'''
    out = parse_results(html, 5)
    assert len(out) == 2 and out[0].url == "https://a.example/1" and out[0].source == "swarm"
    assert parse_results(BOT_WALL, 5) == []  # challenge pages yield nothing


def test_run_router_dispatch(monkeypatch):
    import infoseek

    async def fake_extract(url, **kw):
        return f"EXTRACTED:{url}"

    async def fake_ask(q, **kw):
        return f"ASKED:{q}"

    async def fake_search(q, **kw):
        return [{"title": "t", "url": "https://x", "snippet": "", "source": "ddg",
                 "rank": 0, "date": "", "extra": "", "score": 1.0}]

    monkeypatch.setattr(infoseek, "extract", fake_extract)
    monkeypatch.setattr(infoseek, "ask", fake_ask)
    monkeypatch.setattr(infoseek, "search", fake_search)

    assert asyncio.run(infoseek.run("https://example.com/a")).startswith("EXTRACTED:")
    assert asyncio.run(infoseek.run("ask: why is redis fast")).startswith("ASKED:")
    assert "t" in asyncio.run(infoseek.run("UnsupportedArchError: bailingmoe3"))
    assert "(no query provided)" in asyncio.run(infoseek.run("   "))


def test_help_cheat_sheet():
    import infoseek
    h = infoseek.help()
    for token in ("run(", "search(", "ask(", "extract(", "scan(", "wayback:", "swarm:"):
        assert token in h


def test_wide_mix_routing():
    from infoseek.engines import resolve_engines, WIDE_MIX
    engines, q = resolve_engines("rust vs go", "wide")
    assert engines == WIDE_MIX and "swarm" in engines


def test_new_prefix_routing():
    from infoseek import resolve_engines
    assert resolve_engines("wayback: example.com/*", "auto")[0] == ["wayback"]
    assert resolve_engines("wb: example.com/*", "auto")[0] == ["wayback"]
    assert resolve_engines("archive: example.com/*", "auto")[0] == ["wayback"]
    assert resolve_engines("cc: example.com/*", "auto")[0] == ["commoncrawl"]
    assert resolve_engines("commoncrawl: example.com/*", "auto")[0] == ["commoncrawl"]
    assert resolve_engines("swarm: python http", "auto")[0] == ["swarm"]


# --- never-empty tailoring (derived from real Antigravity sessions) ---------

def test_wide_escalation_strips_prefix():
    from infoseek.engines import resolve_engines, WIDE_MIX
    engines, q = resolve_engines("code:owner/repo deepseek", "wide")
    assert engines == WIDE_MIX and q == "owner/repo deepseek"
    engines, q = resolve_engines("plain query", "wide")
    assert engines == WIDE_MIX and q == "plain query"


def test_no_results_hint_shapes():
    from infoseek.format import no_results_hint
    h = no_results_hint("code:owner/tiny-repo deepseek")
    assert "grep.app" in h and "raw.githubusercontent.com" in h
    h = no_results_hint("gh:owner/missing-repo")
    assert "renamed" in h or "spelling" in h
    h = no_results_hint("swarm: anything")
    assert "SEARXNG_URL" in h
    h = no_results_hint("some plain query")
    assert "wide" in h and "suggest" in h
    h = no_results_hint("anything", engines="hn,so")
    assert "hn,so" in h
    h = no_results_hint("old news", freshness="week")
    assert "freshness" in h


def test_run_escalates_to_wide_on_empty(monkeypatch):
    import infoseek

    calls = []

    async def fake_search(q, n=6, engines="auto", fresh=False, **kw):
        calls.append(engines)
        if engines == "wide":
            return [{"title": "Wide Hit", "url": "https://x.test/", "snippet": "s",
                     "source": "ddg", "extra": "", "date": "", "rank": 0, "score": 1.0}]
        return []

    monkeypatch.setattr(infoseek, "search", fake_search)
    out = asyncio.run(infoseek.run("obscure query"))
    assert calls == ["auto", "wide"]
    assert "Wide Hit" in out


def test_run_returns_hint_when_all_empty(monkeypatch):
    import infoseek

    async def fake_search(q, n=6, engines="auto", fresh=False, **kw):
        return []

    monkeypatch.setattr(infoseek, "search", fake_search)
    out = asyncio.run(infoseek.run("zzz nothing here"))
    assert "no results for" in out and "hint:" in out


def test_mcp_search_empty_returns_hint(monkeypatch):
    mcp = pytest.importorskip("infoseek.mcp", reason="mcp package not installed")
    import infoseek

    async def fake_search(q, n=6, engines="auto", fresh=False, **kw):
        return []

    monkeypatch.setattr(infoseek, "search", fake_search)
    out = asyncio.run(mcp.search("code:owner/tiny-repo deepseek"))
    data = json.loads(out)
    assert data["results"] == []
    assert "grep.app" in data["hint"]
