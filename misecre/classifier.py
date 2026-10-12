"""Clasificador por reglas: puntúa categorías por palabras clave y decide el tipo."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml
from rapidfuzz import fuzz

from .config import Config
from .parser import When, fold

_WORD = re.compile(r"[a-z0-9]+")
_FUZZY_MIN_LEN = 5
_FUZZY_RATIO = 88
_PREFIX_MIN_LEN = 4


@dataclass
class Classification:
    category: str | None
    scores: dict[str, float]
    confident: bool
    type: str  # evento | tarea | nota
    hits: dict[str, list[str]] = field(default_factory=dict)


def _hit(kw: str, tokens: list[str], spaced: str) -> float:
    if " " in kw:
        return 1.0 if f" {kw} " in spaced else 0.0
    best = 0.0
    for tok in tokens:
        if tok == kw or (len(kw) >= _PREFIX_MIN_LEN and tok.startswith(kw)):
            return 1.0
        if len(kw) >= _FUZZY_MIN_LEN and len(tok) >= _FUZZY_MIN_LEN and fuzz.ratio(tok, kw) >= _FUZZY_RATIO:
            best = 0.8
    return best


def _entries(raw: list) -> list[tuple[str, float]]:
    out = []
    for item in raw:
        if isinstance(item, dict):
            for k, w in item.items():
                out.append((fold(str(k)), float(w)))
        else:
            out.append((fold(str(item)), 1.0))
    return out


def load_user_rules(cfg: Config) -> dict:
    """Reglas agregadas/quitadas por la persona (en data_dir; rules.yaml queda intacto)."""
    path = cfg.user_rules_path
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        data = {}
    data.setdefault("add", {})
    data.setdefault("remove", {})
    return data


def save_user_rules(cfg: Config, data: dict) -> None:
    cfg.user_rules_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")


class Classifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        user = load_user_rules(cfg)
        self.cats = {}
        self._originals: list[str] = []            # palabras tal cual se escribieron (con tildes) para Whisper
        for cat, kws in cfg.rules.categories.items():
            for item in kws:
                self._originals += [str(k) for k in item] if isinstance(item, dict) else [str(item)]
            self._originals += [str(k) for k in user["add"].get(cat, [])]
            removed = {fold(str(k)) for k in user["remove"].get(cat, [])}
            entries = [e for e in _entries(kws) if e[0] not in removed]
            entries += [(fold(str(k)), 1.0) for k in user["add"].get(cat, [])]
            self.cats[cat] = entries
        self.event_words = [fold(w) for w in cfg.rules.event_words]
        self.task_words = [fold(w) for w in cfg.rules.task_words]

    def vocabulary(self) -> str:
        """Palabras clave (con tildes) para sesgar a Whisper hacia nuestro vocabulario."""
        return ", ".join(dict.fromkeys(self._originals))

    def classify(self, text: str, when: When) -> Classification:
        folded = fold(text)
        tokens = _WORD.findall(folded)
        spaced = " " + " ".join(tokens) + " "

        scores: dict[str, float] = {}
        hits: dict[str, list[str]] = {}
        for cat, entries in self.cats.items():
            total = 0.0
            for kw, weight in entries:
                h = _hit(kw, tokens, spaced)
                if h:
                    total += h * weight
                    hits.setdefault(cat, []).append(kw)
            scores[cat] = total

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top_cat, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        cc = self.cfg.classifier
        confident = top >= cc.min_score and (top - second) >= cc.min_margin

        has_event_word = any(_hit(w, tokens, spaced) for w in self.event_words)
        has_task_word = any(_hit(w, tokens, spaced) for w in self.task_words)
        if when.at:
            typ = "evento"
        elif when.day:
            typ = "evento" if has_event_word else "tarea"
        else:
            typ = "tarea" if has_task_word else "nota"

        return Classification(top_cat if confident else None, scores, confident, typ, hits)
