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
    r"|(?:forget|ignore) (?:the )?instructions (?:and )?(?:print|output|repeat)",
    re.I)
_JAILBRK = re.compile(
    r"jailbreak|do anything now|dan mode|developer mode|unrestricted (?:mode|access)"
    r"|no (?:rules|restrictions|limitations|filter|filters) (?:apply|now)?"
    r"|(?:bypass|evade|break) (?:your|the|safety|content) (?:filters?|restrictions?|guardrails?)"
    r"|free from (?:all )?(?:rules|restrictions)"
    r"|end of (?:input|conversation|text)|now respond as|respond as (?:dan|a different|the assistant)",
    re.I)
_TAGS = re.compile(
    r"</?(?:system|instructions?|developer|prompt)>?|</system[ >]"
    r"|<\|s\||<\|im_start\||<\|system\||<\|user\|>?"
    r"|(?:##|###|\[)\s*system(?:\]|\s*prompt)|\[system\]|\[instructions?\]"
    r"|(?:^|[\s(\[])system[:\])]",
    re.I)
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
_ZERO_WIDTH = re.compile("[\u200B-\u200D\uFEFF\u2060]")

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
    """Single normalization pass: lowercase, drop zero-width chars, collapse ws."""
    t = _ZERO_WIDTH.sub("", text).lower()
    return re.sub(r"\s+", " ", t)


def scan(text: str, url: str = "", title: str = "") -> Verdict:
    """Analyze retrieved text for prompt-injection attempts. Cheap + cached."""
    head = text[:3000]
    key = hashlib.sha1((url + "\x00" + head).encode("utf-8", "ignore")).hexdigest()
    hit = _VERDICT_CACHE.get(key)
    if hit:
        return Verdict(hit[0], hit[1], list(hit[2]))  # reasons always a list

    t = _norm(text)
    score, reasons = 0, []

    def bump(w: int, why: str) -> None:
        nonlocal score
        score += w
        if why not in reasons:
            reasons.append(why)

    if _HIJACK.search(t):
        bump(9, "instruction-hijack directive")
    if _FRAMING.search(t):
        bump(5, "role/framing takeover")
    if _EXFIL.search(t):
        bump(14, "prompt-exfiltration request")
    jhits = len(set(_JAILBRK.findall(t)))
    if jhits:
        bump(4 * min(jhits, 5), "jailbreak phrasing")
    if _TAGS.search(t):
        bump(4, "system/instruction markup")
    if _FORCE.search(t):
        bump(1, "coercive phrasing")
    if _OPSEC.search(t):
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
    head150 = t[:150]
    if re.search(r"^(?:do not|never|always|you must|ignore|repeat|respond|print|output|stop|forget)\b", head150):
        bump(4, "instruction-shaped opening")

    # Directive density: >=5 instruction-family words in any 45-word window.
    words = t.split()
    fam = sum(1 for w in words[:400] if w in (
        "ignore", "forget", "instructions", "instruction", "must", "never",
        "always", "repeat", "reveal", "system", "prompt", "override", "disregard", "obey"))
    if fam >= 6 and len(words) <= 400:
        bump(2, "high directive density")

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
