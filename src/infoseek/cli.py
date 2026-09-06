"""infoseek - command-line interface (stdlib argparse, no extra deps).

Subcommands:
    search     multi-engine web search (default)
    ask        context bundle for LLM answering
    last30days research what people say in the last 30 days (Reddit, HN, Polymarket)
    extract    clean text from one URL (with prompt-injection guard)
    scan       run the prompt-injection guard on a text/URL
    suggest    DuckDuckGo autocomplete
    status     engine availability + last errors
    selfcheck  run the full test battery (unit + live engines)

Compatibility: `infoseek --query "..."` is equivalent to `infoseek search "..."`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys


def _run(coro):
 try:
 asyncio.run(coro)
 except KeyboardInterrupt:
 sys.exit(130)


def cmd_search(a: argparse.Namespace) -> None:
 import infoseek

 async def go():
 results = await infoseek.search(a.query, n=a.n, engines=a.engines, fresh=a.fresh,
 freshness=a.freshness)
 if not results and a.engines == "auto":
 results = await infoseek.search(a.query, n=a.n, engines="wide", fresh=True,
 freshness=a.freshness)
 if a.json:
 _FIELDS = ("title", "url", "snippet", "source", "rank", "date", "extra", "score")
 print(json.dumps([{k: r.get(k) for k in _FIELDS} for r in results], indent=2))
 return
 for i, r in enumerate(results, 1):
 meta = " | ".join(x for x in [r.get("source"), r.get("extra"), r.get("date")] if x)
 print(f"{i}. {r.get('title')}")
 if meta:
 print(f" [{meta}]")
 print(f" {r.get('url')}")
 if r.get("snippet"):
 print(f" {r.get('snippet')}")
 if not results:
 print(infoseek.no_results_hint(a.query, a.engines, a.freshness), file=sys.stderr)

 _run(go())


def cmd_ask(a: argparse.Namespace) -> None:
 import infoseek

 async def go():
 out = await infoseek.ask(a.query, n=a.n, extract_top=a.extract_top,
 budget=a.budget, fresh=a.fresh,
 freshness=a.freshness, format="json" if a.json else "text")
 print(out if isinstance(out, str) else json.dumps(out, indent=2))

 _run(go())


def cmd_last30days(a: argparse.Namespace) -> None:
    import infoseek

    async def go():
        out = await infoseek.last30days(a.query, days=a.days, n=a.n, budget=a.budget,
                                        fresh=a.fresh, format="json" if a.json else "text")
        print(out if isinstance(out, str) else json.dumps(out, indent=2))

    _run(go())


def cmd_extract(a: argparse.Namespace) -> None:
 import infoseek

 async def go():
 print(await infoseek.extract(a.url, max_chars=a.max_chars, fresh=a.fresh,
 guard=not a.no_guard))

 _run(go())


def cmd_scan(a: argparse.Namespace) -> None:
 import infoseek
 if a.text is None and not a.url:
 print("Error: provide either --text <string> or --url <url>", file=sys.stderr)
 sys.exit(1)

 async def go():
 text = a.text if a.text is not None else await infoseek.extract(a.url, max_chars=4000, guard=False)
 v = infoseek.scan(text, url=a.url or "")
 out = {"level": v.level, "score": v.score, "reasons": list(v.reasons)}
 print(json.dumps(out, indent=2))
 sys.exit(0 if v.level != "blocked" else 2)

 _run(go())


def cmd_suggest(a: argparse.Namespace) -> None:
 import infoseek

 async def go():
 print(await infoseek.suggest(a.query))

 _run(go())


def cmd_status(a: argparse.Namespace) -> None:
 import infoseek

 async def go():
 print(await infoseek.status())

 _run(go())


def cmd_selfcheck(a: argparse.Namespace) -> None:
 import infoseek

 async def go():
 print(await infoseek.selfcheck(verbose=not a.quiet))

 _run(go())


def cmd_help(a: argparse.Namespace) -> None:
 import infoseek
 print(infoseek.help())


def cmd_find(a: argparse.Namespace) -> None:
 import infoseek
 print(infoseek.find(a.query, n=a.n, engines=a.engines, fresh=a.fresh))


def cmd_research(a: argparse.Namespace) -> None:
 import infoseek
 print(infoseek.research(a.query, budget=a.budget, fresh=a.fresh))


def cmd_deep(a: argparse.Namespace) -> None:
 import infoseek
 print(infoseek.deep(a.query, budget=a.budget, angles=a.angles, fresh=a.fresh))


def cmd_read(a: argparse.Namespace) -> None:
 import infoseek
 print(infoseek.read(a.url, max_chars=a.max_chars, fresh=a.fresh))


def cmd_help(a: argparse.Namespace) -> None:
 import infoseek
 print(infoseek.help())


def build_parser() -> argparse.ArgumentParser:
 p = argparse.ArgumentParser(prog="infoseek", description=__doc__.split("\n\n")[0])
 sub = p.add_subparsers(dest="command")

 # simple one-liners (sync, text in / text out)
 f = sub.add_parser("find", help="search the web (simple, formatted for reading)")
 f.add_argument("query", nargs="?")
 _add_common(f)
 f.set_defaults(func=cmd_find)

 r = sub.add_parser("research", help="search + read the best pages -> context to answer from")
 r.add_argument("query")
 r.add_argument("--budget", type=int, default=1500, help="approx output token budget")
 r.add_argument("--fresh", action="store_true")
 r.set_defaults(func=cmd_research)

    l30 = sub.add_parser("last30days", help="research what people say in the last 30 days (Reddit, HN, Polymarket, Techmeme)")
    l30.add_argument("query", help="topic or comparison (e.g. 'Rust vs Go')")
    l30.add_argument("--days", type=int, default=30, help="recency window in days (default 30)")
    l30.add_argument("-n", type=int, default=8, help="max discussion items")
    l30.add_argument("--budget", type=int, default=2500, help="approx output token budget")
    l30.add_argument("--fresh", action="store_true", help="bypass cache")
    l30.add_argument("--json", action="store_true", help="structured JSON output")
    l30.set_defaults(func=cmd_last30days)

    e = sub.add_parser("extract", help="clean text from a URL (guard denies injection content)")
    e.add_argument("url")
    e.add_argument("--max-chars", type=int, default=2000)
    e.add_argument("--fresh", action="store_true")
    e.add_argument("--no-guard", action="store_true", help="disable prompt-injection denial")
    e.set_defaults(func=cmd_extract)

 rd = sub.add_parser("read", help="clean text from one URL (guard denies injection content)")
 rd.add_argument("url")
 rd.add_argument("--max-chars", type=int, default=2000)
 rd.add_argument("--fresh", action="store_true")
 rd.set_defaults(func=cmd_read)

 h = sub.add_parser("help", help="print the usage card")
 h.set_defaults(func=cmd_help)

 # search (also the default for --query)
 s = sub.add_parser("search", help="multi-engine web search")
 s.add_argument("query", nargs="?", help="search query (or use --query)")
 _add_common(s)
 s.add_argument("--freshness", help="recency limit: day|week|month|year|7d")
 s.set_defaults(func=cmd_search)

 a = sub.add_parser("ask", help="search + extract top pages into an LLM-ready context bundle")
 a.add_argument("query")
 a.add_argument("-n", type=int, default=5)
 a.add_argument("--extract-top", type=int, default=2)
 a.add_argument("--budget", type=int, default=2500, help="approx output token budget")
 a.add_argument("--fresh", action="store_true", help="bypass cache")
 a.add_argument("--freshness", help="recency limit: day|week|month|year|7d")
 a.add_argument("--json", action="store_true", help="structured bundle with source/guard metadata")
 a.set_defaults(func=cmd_ask)

 e = sub.add_parser("extract", help="clean text from a URL (guard denies injection content)")
 e.add_argument("url")
 e.add_argument("--max-chars", type=int, default=2000)
 e.add_argument("--fresh", action="store_true")
 e.add_argument("--no-guard", action="store_true", help="disable prompt-injection denial")
 e.set_defaults(func=cmd_extract)

 sc = sub.add_parser("scan", help="prompt-injection guard verdict on text or URL content")
 sc.add_argument("--text", help="text to scan")
 sc.add_argument("--url", help="URL to fetch and scan")
 sc.set_defaults(func=cmd_scan)

 su = sub.add_parser("suggest", help="search-suggestion autocomplete")
 su.add_argument("query")
 su.set_defaults(func=cmd_suggest)

 st = sub.add_parser("status", help="engine availability and last errors")
 st.set_defaults(func=cmd_status)

 chk = sub.add_parser("selfcheck", help="run the full test battery (unit checks + live engines)")
 chk.add_argument("--quiet", action="store_true")
 chk.set_defaults(func=cmd_selfcheck)

 h = sub.add_parser("help", help="usage cheat sheet for agents")
 h.set_defaults(func=cmd_help)

 # compatibility: top-level --query behaves like `search`
 p.add_argument("--query", help="search query (compat shorthand)")
 p.add_argument("-n", "--n", type=int, default=6)
 p.add_argument("--engines", default="auto", help="comma-separated engine list or auto")
 p.add_argument("--fresh", action="store_true")
 p.add_argument("--freshness", help="recency limit: day|week|month|year|7d")
 p.add_argument("--json", action="store_true", help="search output as JSON")
 return p


def _add_common(s: argparse.ArgumentParser) -> None:
 s.add_argument("-n", "--n", type=int, default=6)
 s.add_argument("--engines", default="auto")
 s.add_argument("--fresh", action="store_true")
 s.add_argument("--json", action="store_true")


def main(argv: list[str] | None = None) -> None:
 if hasattr(sys.stdout, "reconfigure"):
 try:
 sys.stdout.reconfigure(encoding="utf-8", errors="replace")
 sys.stderr.reconfigure(encoding="utf-8", errors="replace")
 except Exception:
 pass
 p = build_parser()
 a = p.parse_args(argv)
 if a.command is None:
 if a.query:
 a.command, a.func = "search", cmd_search
 a.n = a.n or 6
 else:
 p.print_help()
 sys.exit(0)
 a.func(a)


if __name__ == "__main__":
 main()
