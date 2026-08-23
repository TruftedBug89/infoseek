"""Offline tests for research.py: query expansion, quality scoring, error
normalization, and version/error snippet focusing. No network needed."""
from infoseek.research import (
    VERSION_RE, auto_focus, expand_queries, focus_snippet, looks_like_error,
    looks_like_version_query, normalize_error_message, quality,
)


def test_version_regex():
    assert VERSION_RE.search("LM Studio 0.3.14 supports it")
    assert VERSION_RE.search("v1.4.2-rc1 released")
    assert not VERSION_RE.search("no numbers here at all")


def test_looks_like_error():
    assert looks_like_error("UnsupportedArchError: bailingmoe3")
    assert looks_like_error("model failed to load: unknown arch")
    assert not looks_like_error("best pasta recipes for dinner")


def test_looks_like_version_query():
    assert looks_like_version_query("which LM Studio version supports bailingmoe3")
    assert looks_like_version_query("LM Studio 0.3.14")
    assert not looks_like_version_query("how to cook pasta")


def test_auto_focus():
    assert auto_focus("Traceback: RuntimeError missing module") == "error"
    assert auto_focus("which version supports GGUF") == "version"
    assert auto_focus("cute cat videos") is None


def test_normalize_error_message():
    msg = ("Error loading model from C:\\Users\\me\\models\\ling.gguf: "
           "UnsupportedArchError 'bailingmoe3' (id 550e8400-e29b-41d4-a716-446655440000)")
    out = normalize_error_message(msg)
    assert "bailingmoe3" in out
    assert "UnsupportedArchError" in out
    assert "C:\\Users" not in out
    assert "550e8400" not in out
    assert len(out) <= 160


def test_expand_queries_variants():
    variants = expand_queries("how do i fix bailingmoe3 unsupported arch error")
    assert variants
    assert all(v != "how do i fix bailingmoe3 unsupported arch error" for v in variants)
    assert any("bailingmoe3" in v for v in variants)


def test_expand_queries_quotes_technical_tokens():
    variants = expand_queries("support for bailingmoe3 architecture")
    assert any('"bailingmoe3"' in v for v in variants)


def test_expand_queries_version_adds_support_terms():
    variants = expand_queries("LM Studio Ling 3.0")
    assert any("version support" in v for v in variants)


def test_quality_empty_is_poor():
    q = quality([], "rust vs go")
    assert not q.ok and q.score == 0.0


def test_quality_good_results():
    results = [{"title": f"rust vs go benchmark {i}", "snippet": "performance comparison"}
               for i in range(6)]
    q = quality(results, "rust vs go")
    assert q.ok


def test_quality_version_query_penalizes_no_versions():
    results = [{"title": "LM Studio discussion", "snippet": "general talk about models"}
               for i in range(6)]
    q = quality(results, "which LM Studio version supports bailingmoe3")
    assert any("no version numbers" in r for r in q.reasons)


def test_focus_snippet_version():
    text = ("This page talks about many things. "
            "Support for the bailingmoe3 architecture was added in LM Studio 0.3.14. "
            "More general commentary follows here.")
    out = focus_snippet(text, "LM Studio bailingmoe3 version", 160, "version")
    assert "0.3.14" in out


def test_focus_snippet_error_prefers_solution():
    text = ("I get UnsupportedArchError when loading the model. "
            "The fix is to upgrade to LM Studio 0.3.14 or newer. "
            "It was very frustrating yesterday.")
    out = focus_snippet(text, "UnsupportedArchError bailingmoe3", 160, "error")
    assert "fix" in out.lower() or "upgrade" in out.lower()
