"""Checks on model output against the docs it was given (help path)."""

from __future__ import annotations

import difflib
import re
from urllib.parse import urlparse

URL_RE = re.compile(r"https?://[^\s<>()\"'`\[\]]+|\b(?:[a-z0-9-]+\.){2,}[a-z]{2,}\b", re.I)
SOURCES_RE = re.compile(r"^\s*\**\s*sources?\s*\**\s*:\s*(.*)$", re.I | re.M)
TRAILING = ".,;:!?*)"


def _split_trailing(u: str) -> tuple[str, str]:
    trail = ""
    while u and u[-1] in TRAILING:
        trail = u[-1] + trail
        u = u[:-1]
    return u, trail


def known_addresses(doc_text: str) -> set[str]:
    known: set[str] = set()
    for m in URL_RE.finditer(doc_text):
        u, _ = _split_trailing(m.group(0))
        known.add(u)
        if "://" in u:
            host = urlparse(u).netloc
            if host:
                known.add(host)
    return known


def ground_urls(answer: str, doc_text: str, cutoff: float = 0.85) -> tuple[str, list[tuple[str, str]], bool]:
    """Returns (answer with near-miss addresses fixed, corrections, all_grounded)."""
    known = known_addresses(doc_text)
    lowered = doc_text.lower()
    corrections: list[tuple[str, str]] = []
    grounded = True

    def fix(m: re.Match) -> str:
        nonlocal grounded
        u, trail = _split_trailing(m.group(0))
        if u.lower() in lowered:
            return u + trail
        best = difflib.get_close_matches(u, list(known), n=1, cutoff=cutoff)
        if best:
            corrections.append((u, best[0]))
            return best[0] + trail
        grounded = False
        return u + trail

    return URL_RE.sub(fix, answer), corrections, grounded


def cited_sources(answer: str, hits: list) -> tuple[str, list]:
    """Strip the model's trailing "Sources: 1, 3" line and return only the hits it cited.
    No line, or "Sources: none", means no sources are shown."""
    matches = list(SOURCES_RE.finditer(answer))
    if not matches:
        return answer.strip(), []
    m = matches[-1]
    body = answer[:m.start()].rstrip()
    cited, seen = [], set()
    for n in re.findall(r"\d+", m.group(1)):
        i = int(n)
        if 1 <= i <= len(hits) and i not in seen:
            seen.add(i)
            cited.append(hits[i - 1])
    return body, cited
