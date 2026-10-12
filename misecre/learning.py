"""Aprendizaje por feedback: propone reglas a partir de tus correcciones. Nunca modifica solas."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .core import Core
from .parser import MONTHS, WEEKDAYS, fold

_WORD = re.compile(r"[a-z]+")
STOPWORDS = set("""
para como pero porque cuando donde desde hasta entre sobre tambien tengo tiene tenes quiero quiere
hacer hace hacen estoy esta estan estar este esto esos esas ellos ellas nosotros vamos vamos
mucho mucha muchos muchas algo alguien nada todo toda todos todas otro otra otros otras
recordame recordar recordatorio armame agendame anota anotame mañana manana hoy ayer
semana mes dias dia horas hora minutos tarde noche medio mediodia
con sin por que del los las una unos unas
""".split())
STOPWORDS |= set(WEEKDAYS) | set(MONTHS)


@dataclass
class Suggestion:
    category: str
    word: str
    count: int
    example: str
    agent: int = 0          # cuántas de las evidencias son decisiones de la IA
    human: int = 0          # cuántas son correcciones tuyas


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall(fold(text)) if len(t) >= 4 and t not in STOPWORDS}


def _evidence(core: Core) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """palabra -> categoría -> [(origen, transcripción)]. Origen: 'human' (tu corrección) o 'agent' (decisión de la IA)."""
    known = {c: set(kws) for c, kws in core.rules_view().items()}
    all_known = set().union(*known.values()) if known else set()
    out: dict[str, dict[str, list[tuple[str, str]]]] = defaultdict(lambda: defaultdict(list))
    for fb in core.db.feedback():
        kind, chosen, auto = fb["kind"], fb["chosen_value"], fb["auto_value"]
        if chosen not in known:
            continue
        if kind == "category" and chosen != auto:
            src = "human"
        elif kind == "agent_category":
            src = "agent"
        else:
            continue
        for tok in _tokens(fb["transcript"] or ""):
            if tok not in all_known:
                out[tok][chosen].append((src, fb["transcript"]))
    return out


def suggest_rules(core: Core) -> list[Suggestion]:
    """Palabras con evidencia consistente (siempre la misma categoría) y todavía sin regla.

    Cuenta correcciones tuyas (`learning.min_count`) y decisiones de la IA (`learning.agent_min_count`)."""
    cfg = core.cfg.learning
    out = []
    for word, by_cat in _evidence(core).items():
        if len(by_cat) != 1:                       # evidencia en conflicto: no se propone
            continue
        cat, ev = next(iter(by_cat.items()))
        human = sum(1 for s, _ in ev if s == "human")
        agent = sum(1 for s, _ in ev if s == "agent")
        if core.db.kv_get(f"rule_rejected:{cat}:{word}") is not None:
            continue
        if human >= cfg.min_count or (agent and human + agent >= cfg.agent_min_count):
            out.append(Suggestion(cat, word, human + agent, ev[-1][1], agent=agent, human=human))
    return sorted(out, key=lambda s: (-s.count, s.word))


def auto_learn(core: Core) -> list[Suggestion]:
    """Convierte en reglas lo que la IA decidió de forma consistente. Reversible (`/regla quitar`)."""
    if not core.cfg.learning.auto_apply:
        return []
    learned = []
    for s in suggest_rules(core):
        if s.agent >= 1 and s.count >= core.cfg.learning.agent_min_count:
            core.add_rule(s.category, s.word)
            core.db.kv_set(f"learned:{s.category}:{s.word}", core.now().isoformat())
            learned.append(s)
    return learned


def reject(core: Core, category: str, word: str) -> None:
    core.db.kv_set(f"rule_rejected:{category}:{fold(word)}", "1")


def accuracy(core: Core, last: int = 50) -> tuple[int, int] | None:
    """(aciertos, total) sobre las últimas correcciones/confirmaciones tuyas de categoría."""
    rows = core.db.feedback("category")[-last:]
    if not rows:
        return None
    return sum(1 for r in rows if r["auto_value"] == r["chosen_value"]), len(rows)
