"""Arma un título legible quitando fecha/hora y muletillas de apertura."""
from __future__ import annotations

import re

from .parser import fold

_EDGE = " ,.;:-¡!¿?"
_LABEL = re.compile(r"^(?:recordatorio|recordame|nota|tarea|evento|anotar|anota)\s*[:\-–—]\s*", re.I)
_LEAD = re.compile(r"^(?:que|de|el|la|para|a|y|con respecto a)\s+", re.I)
_TRAIL = re.compile(r"\s+(?:el|la|para|a|de|en|y|que|este|esta)$", re.I)


def clean_title(text: str, spans: list[tuple[int, int]], prefixes: list[str]) -> str:
    keep = [True] * len(text)
    for a, b in spans:
        for i in range(a, min(b, len(text))):
            keep[i] = False
    s = "".join(c for c, k in zip(text, keep) if k)
    s = re.sub(r"\s+", " ", s).strip(_EDGE)

    s = _LABEL.sub("", s)                              # "recordatorio: ir a comprar pan"
    ordered = sorted((fold(p) for p in prefixes), key=len, reverse=True)
    changed = True
    while changed and s:
        changed = False
        f = fold(s)
        for p in ordered:
            if f == p or f.startswith(p + " "):
                s = s[len(p):].strip(_EDGE)
                changed = True
                break
        if _LEAD.match(s):
            s = _LEAD.sub("", s).strip(_EDGE)
            changed = True
        if _TRAIL.search(s):
            s = _TRAIL.sub("", s).strip(_EDGE)
            changed = True
    return s[:1].upper() + s[1:] if s else ""
