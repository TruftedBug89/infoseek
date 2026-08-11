"""Live engine probes — run with: pytest -m live

These hit real public endpoints and are skipped by default so the offline
suite stays fast and CI-friendly.
"""
import asyncio
import pytest

import infoseek

PROBES = {
    "ddg": "python http client",
    "hn": "rust async",
    "so": "python asyncio",
    "news": "openai",
    "wiki": "python programming language",
    "arxiv": "large language models",
    "openalex": "transformer",
    "wikidata": "Tim Berners-Lee",
    "pubmed": "cancer immunotherapy",
    "crossref": "transformer",
    "gh": "searxng",
    "code": "AsyncClient",
    "reddit": "tavily",
    "lobsters": "the",
    "marginalia": "knowledge management",
}


@pytest.mark.live
@pytest.mark.parametrize("engine,query", list(PROBES.items()))
def test_engine_live(engine, query):
    async def go():
        client = infoseek.net.PoliteClient(min_interval=0.8)
        try:
            res, err = await infoseek.engines.REGISTRY[engine](client, query, 2)
        finally:
            await client.close()
        if err and "rate-limited" in err:
            pytest.skip(err)
        assert res, f"{engine}: no results ({err})"

    asyncio.run(go())


@pytest.mark.live
def test_ask_live():
    out = asyncio.run(infoseek.ask("python httpx vs requests", n=4, extract_top=1, budget=1200))
    assert "QUERY:" in out and len(out) > 300
