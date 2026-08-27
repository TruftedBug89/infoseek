"""Regression tests for bugs found in the 0.6.x line (all offline).

Covers:
* extract_url() on generic (non-fast-path) URLs — used to raise NameError('os')
* raw code-file extraction via the extension fast path
* engine extras are pure ASCII (safe on cp1252 consoles / cheap for LLMs)
* cache roundtrip + writable-dir fallback
"""
import asyncio
import tempfile

import httpx

from infoseek.extract import extract_url
from infoseek.net import PoliteClient


def _client(handler) -> PoliteClient:
    return PoliteClient(min_interval=0.0, transport=httpx.MockTransport(handler))


LONG_HTML = """<html><head><title>Sample Article About Http Clients</title></head>
<body><article>
<p>Choosing an HTTP client library depends on the workload at hand. Synchronous
libraries are simple to use for scripts and small tools, while asynchronous
libraries shine when many requests run concurrently. Benchmarks consistently
show that connection pooling and keep-alive reuse dominate raw throughput
numbers in real workloads, far more than the parser overhead itself.</p>
<p>Retries, timeouts, and sensible default headers matter just as much as
speed. A library that exposes clear error types and supports streaming
responses makes production code easier to reason about and to debug.</p>
</article></body></html>"""


def _html_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    return httpx.Response(200, text=LONG_HTML,
                          headers={"content-type": "text/html; charset=utf-8"})


def test_extract_generic_html_url_offline():
    """Regression: any URL not on a domain fast path raised NameError('os')."""
    out = asyncio.run(extract_url(_client(_html_handler),
                                  "https://example.org/article", max_chars=2000))
    assert "HTTP client" in out or "connection pooling" in out
    assert len(out) > 100


def test_extract_raw_code_file_offline():
    """The extension fast path exercises the os.path.splitext branch directly."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="def hello():\n    return 42\n",
                              headers={"content-type": "text/plain"})
    out = asyncio.run(extract_url(_client(handler),
                                  "https://example.org/src/mod.py", max_chars=500))
    assert "def hello" in out


def test_extract_robots_disallowed_uses_archive_not_origin_offline():
    """robots.txt disallow -> the origin is never fetched; the Wayback copy is
    served instead (archive fetches do not touch the origin site)."""
    fetched: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append((request.url.host, request.url.path))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        if request.url.host == "archive.org":
            return httpx.Response(200, json={"archived_snapshots": {"closest": {
                "available": True, "status": "200",
                "url": "http://web.archive.org/web/20260101000000/https://example.org/private/x"}}})
        if request.url.host == "web.archive.org":
            return httpx.Response(200, text=LONG_HTML,
                                  headers={"content-type": "text/html"})
        return httpx.Response(200, text="ORIGIN-CONTENT-SHOULD-NOT-BE-USED",
                              headers={"content-type": "text/html"})

    out = asyncio.run(extract_url(_client(handler),
                                  "https://example.org/private/x", max_chars=500))
    assert "ORIGIN-CONTENT" not in out
    assert "connection pooling" in out or "HTTP client" in out
    assert not any(h == "example.org" and p == "/private/x" for h, p in fetched)


def test_hn_extras_are_ascii_offline():
    """Engine metadata must stay ASCII: safe on cp1252 pipes, cheap in tokens."""
    payload = {"hits": [{"title": "Show HN: Thing", "url": "https://example.com",
                         "points": 123, "num_comments": 45, "author": "alice",
                         "created_at": "2025-01-02T00:00:00Z", "objectID": "1",
                         "story_text": "<p>hello world</p>"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    from infoseek.engines import hn
    res, err = asyncio.run(hn(_client(handler), "thing", 5))
    assert res and err is None
    for r in res:
        blob = r.title + r.snippet + r.extra
        assert all(ord(ch) < 128 for ch in blob), blob


def test_cache_roundtrip_offline():
    import infoseek.cache as c
    c.set("t", "pytest-roundtrip", value="hello", ttl=60)
    assert c.get("t", "pytest-roundtrip", ttl=60) == "hello"


def test_cache_info_reports_health_offline():
    import infoseek.cache as c
    info = c.info()
    assert "entries" in info and "unavailable" not in info


def test_cache_dir_falls_back_when_not_writable(monkeypatch):
    import infoseek.cache as c
    monkeypatch.delenv("INFOSEEK_CACHE", raising=False)
    monkeypatch.setattr(c, "_writable", lambda d: False)
    assert str(c._cache_dir()).startswith(tempfile.gettempdir())
