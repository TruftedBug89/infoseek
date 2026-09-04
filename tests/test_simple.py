"""Offline tests for the simple sync API: find / research / read / deep / help."""
import asyncio
import sys

import infoseek

# `infoseek.extract` is the public *function* (it shadows the submodule name),
# so patch the extract helpers through sys.modules instead.
extract_mod = sys.modules["infoseek.extract"]

ROWS = [
 {"title": "Alpha", "url": "https://alpha.example/a", "snippet": "first hit",
 "source": "ddg", "rank": 0, "date": "", "extra": "", "score": 5.0},
 {"title": "Beta", "url": "https://beta.example/b", "snippet": "second hit",
 "source": "hn", "rank": 1, "date": "", "extra": "12 pts", "score": 4.0},
]


def test_help_card_lists_every_simple_call():
 card = infoseek.help()
 for name in ("find", "research", "read", "deep"):
 assert f"infoseek.{name}(" in card


def test_find_formats_results(monkeypatch):
 async def fake_search(q, n=6, engines="auto", fresh=False):
 assert q == "rust vs go"
 return ROWS

 monkeypatch.setattr(infoseek, "search", fake_search)
 out = infoseek.find("rust vs go")
 assert "Alpha" in out and "https://alpha.example/a" in out
 assert "first hit" in out and "Beta" in out


def test_find_handles_empty(monkeypatch):
 async def fake_search(q, n=6, engines="auto", fresh=False):
 return []

 monkeypatch.setattr(infoseek, "search", fake_search)
 assert infoseek.find("x").startswith("[[no results")


def test_find_never_raises(monkeypatch):
 async def boom(q, n=6, engines="auto", fresh=False):
 raise RuntimeError("network down")

 monkeypatch.setattr(infoseek, "search", boom)
 out = infoseek.find("x")
 assert out.startswith("[[find error:") and "network down" in out


def test_research_uses_ask(monkeypatch):
 async def fake_ask(q, n=5, extract_top=2, budget=2500, fresh=False, respect_robots=True):
 return f"QUERY: {q}\nBUDGET:{budget}"

 monkeypatch.setattr(infoseek, "ask", fake_ask)
 assert "BUDGET:1200" in infoseek.research("why redis fast", budget=1200)


def test_read_uses_extract(monkeypatch):
 async def fake_extract(url, max_chars=2000, fresh=False, respect_robots=True, guard=True):
 return "clean text"

 monkeypatch.setattr(infoseek, "extract", fake_extract)
 assert infoseek.read("https://example.com/a") == "clean text"


def test_read_never_raises(monkeypatch):
 async def boom(url, max_chars=2000, fresh=False, respect_robots=True, guard=True):
 raise ValueError("404")

 monkeypatch.setattr(infoseek, "extract", boom)
 assert infoseek.read("https://example.com/a").startswith("[[read error:")


def test_sync_call_inside_running_loop(monkeypatch):
 """Async agents must be able to call the sync API without 'loop already running'."""
 async def fake_search(q, n=6, engines="auto", fresh=False):
 return ROWS

 monkeypatch.setattr(infoseek, "search", fake_search)

 async def main():
 return infoseek.find("rust vs go")

 assert "Alpha" in asyncio.run(main())


def test_deep_merges_angles_and_caps_budget(monkeypatch):
 doms: dict[str, str] = {}

 async def fake_search(q, n=6, engines="auto", fresh=False):
 dom = doms.setdefault(q, f"site{len(doms)}.example")
 return [{"title": f"Hit: {q}", "url": f"https://{dom}/a", "snippet": "snip",
 "source": "ddg", "rank": 0, "score": 3.0}]

 async def fake_suggest(q):
 return "- rust vs go performance\n- go vs rust 2026\n- rust go comparison"

 async def fake_extract_many(client, urls, max_chars=1200, concurrency=3, query=""):
 return [{"ok": True, "url": u, "text": f"body of {u}", "guard": {"level": "ok"}}
 for u in urls]

 monkeypatch.setattr(infoseek, "search", fake_search)
 monkeypatch.setattr(infoseek, "suggest", fake_suggest)
 monkeypatch.setattr(extract_mod, "extract_many", fake_extract_many)

 out = infoseek.deep("rust vs go", budget=800, angles=3)
 assert out.startswith("QUERY: rust vs go")
 assert "performance" in out.splitlines()[1] # ANGLES line lists variants
 assert "## SOURCES" in out and "## BRIEF" in out
 assert "body of https://" in out # pages were read
 assert len(out) <= 800 * 4 # budget respected


def test_deep_falls_back_to_lenses_when_suggest_is_empty(monkeypatch):
 """Long questions get no autocomplete hits - deep() must still fan out."""
 doms: dict[str, str] = {}

 async def fake_search(q, n=6, engines="auto", fresh=False):
 dom = doms.setdefault(q, f"site{len(doms)}.example")
 return [{"title": f"Hit: {q}", "url": f"https://{dom}/a", "snippet": "snip",
 "source": "ddg", "rank": 0, "score": 3.0}]

 async def fake_suggest(q):
 return "_no suggestions_"

 async def fake_extract_many(client, urls, max_chars=1200, concurrency=3, query=""):
 return []

 monkeypatch.setattr(infoseek, "search", fake_search)
 monkeypatch.setattr(infoseek, "suggest", fake_suggest)
 monkeypatch.setattr(extract_mod, "extract_many", fake_extract_many)

 out = infoseek.deep("how does quantum error correction scale", budget=400, angles=3)
 angles_line = out.splitlines()[1]
 assert "explained" in angles_line and "pros and cons" in angles_line


def test_deep_survives_total_failure(monkeypatch):
 async def boom(q, n=6, engines="auto", fresh=False):
 raise RuntimeError("all engines down")

 async def boom_suggest(q):
 raise RuntimeError("suggest down")

 monkeypatch.setattr(infoseek, "search", boom)
 monkeypatch.setattr(infoseek, "suggest", boom_suggest)
 assert infoseek.deep("x", budget=500) == "[[no results]]"
