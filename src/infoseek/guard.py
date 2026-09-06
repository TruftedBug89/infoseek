"""guard.py — prompt-injection guard for retrieved web content.

Layered, dependency-free detection of instruction-hijacking, jailbreak, and
prompt-exfiltration attacks in extracted page text. Pure regex/structural
heuristics over a single normalized pass — no LLM calls, ~µs per page.

Verdicts
--------
  ok          content is clean, safe to pass to an LLM as data
  suspect     ambiguous signals ("you must…" styles, single framing hit);
              include but flag the source so it is never treated as instructions
  blocked     clear injection attempt; callers must deny the content

Usage
-----
  verdict = scan(text, url=..., title=...)
  verdict.level  -> "ok" | "suspect" | "blocked"
  verdict.score  -> numeric confidence
  verdict.reasons-> human-readable evidence list
  bool(verdict)  -> True when safe (ok) — handy for filters

Policy
------
  INFOSEEK_GUARD = block (default) | warn | off
    block: blocked sources are removed from context bundles
    warn:  blocked sources kept but always quoted/flagged as untrusted
"""
from __future__ import annotations

import hashlib
import os
import re

import unicodedata

# Homoglyphs commonly used to evade keyword filters (Cyrillic, Greek, fullwidth Latin)
_HOMOGLYPH_DICT = {
    '\u0430': 'a', '\u0435': 'e', '\u043e': 'o', '\u0440': 'p', '\u0441': 'c',
    '\u0443': 'y', '\u0445': 'x', '\u0456': 'i', '\u0458': 'j', '\u0455': 's',
    '\u0410': 'A', '\u0415': 'E', '\u041e': 'O', '\u0420': 'P', '\u0421': 'C',
    '\u0423': 'Y', '\u0425': 'X', '\u0406': 'I', '\u0408': 'J', '\u0405': 'S',
    '\u03b1': 'a', '\u03bf': 'o', '\u03c1': 'p', '\u03bd': 'v',
    '\u2010': '-', '\u2011': '-', '\u2012': '-', '\u2013': '-', '\u2014': '-',
}
# Add fullwidth Latin \uFF01-\uFF5E
for code in range(0xFF01, 0xFF5F):
    _HOMOGLYPH_DICT[chr(code)] = chr(code - 0xFEE0)
_HOMOGLYPHS = str.maketrans(_HOMOGLYPH_DICT)

# ---------------------------------------------------------------- policy
POLICY = os.environ.get("INFOSEEK_GUARD", "block").strip().lower()
if POLICY not in ("block", "warn", "off"):
    POLICY = "block"

# ------------------------------------------------------------- patterns
# Group weights: a single hit contributes w points; blocking needs >= BLOCK_SCORE.
_HIJACK = re.compile(
    r"ignore (?:all |any )?(?:previous |prior |above |earlier )?instructions?"
    r"|ignore (?:everything |all )?(?:above|before|prior|previous)"
    r"|(?:disregard|forget|dismiss) (?:all )?(?:previous|prior|above|earlier) instructions?"
    r"|forget (?:everything|all (?:the |your )?(?:above|previous))"
    r"|disregard everything (?:above|below|before)",
    re.I)
_FRAMING = re.compile(
    r"you are (?:now |not |no longer )?(?:an? |the )?(?:assistant|agent|chatbot|ai|model|gpt|claude|system)"
    r"|from now on (?:you|you will|you must)"
    r"|(?:your|you have) (?:new |updated |real )?(?:instructions?|role|directives?|guidelines?|system prompt)"
    r"|(?:this is|these are) (?:your|my|the) (?:new )?(?:instructions?|system prompt)"
    r"|(?:pretend|act) (?:as if|like)? you (?:are|were)"
    r"|override (?:your|the|previous) instructions?"
    r"|system prompt:",
    re.I)
_EXFIL = re.compile(
    r"(?:print|output|repeat|display|show|reveal|copy|paste)(?: the| our| my| your)?"
    r" (?:text|instructions?|system prompt|prompt)s?(?: (?:from )?(?:above|below|verbatim|exactly))?"
    r"|(?:what are|what is|tell me|reply with) (?:your|the) (?:system prompt|instructions?|initial prompt)"
    r"|repeat (?:everything|the text) (?:above|you see)"
    r"|(?:forget|ignore) (?:the )?instructions (?:and )?(?:print|output|repeat)"
    r"|!\[.*?\]\([a-z0-9+.-]+://[^\s)]*(?:prompt|token|key|cookie|exfil|session|leak|system)[^\s)]*\)",
    re.I)
_JAILBRK = re.compile(
    r"jailbreak|do anything now|dan mode|developer mode|unrestricted (?:mode|access)"
    r"|no (?:rules|restrictions|limitations|filter|filters) (?:apply|now)?"
    r"|(?:bypass|evade|break) (?:your|the|safety|content) (?:filters?|restrictions?|guardrails?)"
    r"|free from (?:all )?(?:rules|restrictions)"
    r"|end of (?:input|conversation|text)|now respond as|respond as (?:dan|a different|the assistant)",
    re.I)
_TAGS = re.compile(
    r"</?(?:system|instructions?|developer|prompt|system-instruction)>?|</system[ >]"
    r"|<\|s\||<\|im_start\||<\|system\||<\|user\|>?"
    r"|(?:##|###|\[)\s*system(?:\]|\s*prompt)|\[system\]|\[instructions?\]"
    r"|<<SYS>>|\[INST\]|\[/INST\]"
    r"|<!--.*?instructions?.*?-->"
    r"|(?:^|[\s(\[])system[:\])]",
    re.I | re.DOTALL)
_FORCE = re.compile(
    r"you must (?:now )?|you will (?:now )?(?:obey|follow|respond|act)"
    r"|you are (?:required|obligated|programmed) to"
    r"|always respond (?:with|in|exactly)"
    r"|respond (?:only|exactly) with"
    r"|do not (?:mention|say|tell|reveal|repeat|include)",
    re.I)
_OPSEC = re.compile(
    r"do not (?:mention|reveal|discuss|tell)"
    r"|never (?:mention|reveal|discuss|tell)"
    r"|this conversation is (?:private|confidential|secret)"
    r"|the (?:user|instruction|prompt) (?:above|below) is (?:secret|confidential|fake|a test)",
    re.I)

# Obfuscation: spaced-out / reversed / encoded
_SPACED = re.compile(r"i\s+g\s+n\s+o\s+r\s+e|i\s+n\s+s\s+t\s+r\s+u\s+c\s+t\s+i\s+o\s+n|s\s+y\s+s\s+t\s+e\s+m\s+p\s+r\s+o\s+m\s+p\s+t|f\s+o\s+r\s+g\s+e\s+t", re.I)
_B64 = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")
_HEX = re.compile(r"(?:[0-9a-fA-F]{2}:){16,}|[0-9a-fA-F]{64,}")
_ZERO_WIDTH = re.compile("[\u200B-\u200D\uFEFF\u2060\u00AD]")

BLOCK_SCORE = 16
SUSPECT_SCORE = 8

_VERDICT_CACHE: dict[str, tuple[str, int, tuple[str, ...]]] = {}
_CACHE_MAX = 1024


class Verdict:
    """Immutable-ish guard result. bool(verdict) == (level != "blocked")."""
    __slots__ = ("level", "score", "reasons")

    def __init__(self, level: str, score: int, reasons: list[str]):
        self.level, self.score, self.reasons = level, score, reasons

    def __bool__(self) -> bool:
        return self.level != "blocked"

    def __repr__(self) -> str:
        return f"<Verdict {self.level} score={self.score} reasons={self.reasons}>"

    def short(self) -> str:
        return f"[guard:{self.level}] {'; '.join(self.reasons) if self.reasons else ''}"


def _norm(text: str) -> str:
    """Single normalization pass: NFKD Unicode decomp, homoglyph translation, drop zero-width chars, collapse ws."""
    t = unicodedata.normalize("NFKD", text).translate(_HOMOGLYPHS)
    t = _ZERO_WIDTH.sub("", t).lower()
    return re.sub(r"\s+", " ", t)


def _mask_code(text: str) -> str:
    """Mask markdown code blocks before running heuristics to avoid false positives on technical code samples."""
    return re.sub(r"```[\s\S]*?```", " [code_block] ", text)


def _score_window(t: str) -> tuple[int, list[str]]:
    """Score one normalized text window (~3k chars). Returns (score, reasons).
    Signals must co-occur within a window to stack toward blocking."""
    score, reasons = 0, []

    def bump(w: int, why: str) -> None:
        nonlocal score
        score += w
        if why not in reasons:
            reasons.append(why)

    # High severity threats checked on full window
    if _HIJACK.search(t):
        bump(9, "instruction-hijack directive")
    if _FRAMING.search(t):
        bump(5, "role/framing takeover")
    if _EXFIL.search(t):
        bump(14, "prompt-exfiltration request")
    jhits = len(set(_JAILBRK.findall(t)))
    if jhits:
        bump(4 * min(jhits, 5), "jailbreak phrasing")

    # Code-masked text for markup, coercive syntax, and directive density to prevent false positives on code
    t_clean = _mask_code(t)
    if _TAGS.search(t_clean):
        bump(4, "system/instruction markup")
    if _FORCE.search(t_clean):
        bump(1, "coercive phrasing")
    if _OPSEC.search(t_clean):
        bump(2, "opsec/confidential framing")

    obf = bool(_SPACED.search(t))
    if obf:
        bump(5, "spaced-out obfuscation")
        # De-obfuscate single-letter spaced runs and re-check the core attacks
        # on the collapsed text ("f o r g e t a l l ..." -> "forget all ...").
        collapsed = re.sub(r"(?<=\b[a-z]) (?=[a-z]\b)", "", t)
        if collapsed != t:
            for pat, w, why in ((_HIJACK, 11, "hijack directive (obfuscated)"),
                                (_EXFIL, 12, "exfil request (obfuscated)"),
                                (_FRAMING, 6, "role framing (obfuscated)"),
                                (_JAILBRK, 5, "jailbreak (obfuscated)")):
                if pat.search(collapsed):
                    bump(w, why)
    if _B64.search(t):
        bump(3, "large encoded payload")
    if _HEX.search(t):
        bump(2, "large hex payload")

    # Structural: instruction-like prologue in the first 150 chars.
    head150 = t_clean[:150].strip()
    if re.search(r"^(?:do not|never|always|you must|ignore|repeat|respond|print|output|stop|forget)\b", head150):
        bump(4, "instruction-shaped opening")

    # Directive density: >=6 instruction-family words in code-masked text.
    words = t_clean.split()
    fam = sum(1 for w in words[:400] if w in (
        "ignore", "forget", "instructions", "instruction", "must", "never",
        "always", "repeat", "reveal", "system", "prompt", "override", "disregard", "obey"))
    if fam >= 6 and len(words) <= 400:
        bump(2, "high directive density")

    return score, reasons


def scan(text: str, url: str = "", title: str = "") -> Verdict:
    """Analyze retrieved text for prompt-injection attempts. Cheap + cached.

    Long pages are scanned in overlapping windows over the FULL text (not just
    the first 3k chars), so injections buried deep in a page are still caught.
    The highest-scoring window decides the verdict."""
    text = text or ""
    text_hash = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
    key = hashlib.sha1(f"{url}\x00{text_hash}".encode("utf-8")).hexdigest()
    hit = _VERDICT_CACHE.get(key)
    if hit:
        return Verdict(hit[0], hit[1], list(hit[2]))  # reasons always a list

    t = _norm(text)
    if len(t) <= 3600:
        score, reasons = _score_window(t)
    else:
        win, step, cap = 3200, 2800, 24000
        starts = list(range(0, min(len(t), cap), step))
        if len(t) > cap:
            starts.append(max(0, len(t) - 2000))  # always cover the tail
        score, reasons = 0, []
        for s in starts:
            ws, wr = _score_window(t[s:s + win])
            if ws <= 0:
                continue
            if ws > score:
                score, reasons = ws, list(wr)
            else:
                reasons.extend(w for w in wr if w not in reasons)

    if score >= BLOCK_SCORE:
        level = "blocked"
    elif score >= SUSPECT_SCORE:
        level = "suspect"
    else:
        level = "ok"

    if POLICY == "off" and level != "ok":
        level, score, reasons = "ok", 0, []
    if len(_VERDICT_CACHE) >= _CACHE_MAX:
        _VERDICT_CACHE.clear()
    _VERDICT_CACHE[key] = (level, score, tuple(reasons))
    return Verdict(level, score, reasons)
