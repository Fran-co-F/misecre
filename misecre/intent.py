"""Enrutador por reglas: entiende los pedidos más comunes SIN llamar a la IA.

    "ya fui a comprar pan"      -> completar el ítem pendiente que coincide
    "qué tengo mañana"          -> listar
    "deshacelo" / "es de ocio"  -> deshacer / recategorizar lo último
    "reunión el jueves a las 10"-> crear (si las reglas deciden la categoría)

Lo que no entiende (modificaciones, preguntas libres, categoría dudosa) devuelve `none`/`create` no
confiable y lo resuelve la IA, que es el respaldo. Cada decisión de la IA alimenta el aprendizaje
(learning.py) para que la próxima vez lo resuelvan las reglas.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .core import Core, Result
from .parser import fold, parse_when

_WORD = re.compile(r"[a-zñáéíóúü0-9]+", re.I)

# pretéritos irregulares de 1ª persona; los regulares terminan en é/í ("compré", "salí")
_IRREGULAR = {"fui", "hice", "vi", "di", "puse", "tuve", "pude", "dije", "traje", "quise", "supe", "estuve",
              "anduve", "cogi", "lei", "oi", "fue"}
_DONE = {"listo", "lista", "hecho", "hecha", "terminado", "terminada", "completado", "completada",
         "resuelto", "resuelta", "cumplido", "cumplida"}
_FILLER = {"ya", "lo", "la", "los", "las", "le", "les", "me", "el", "al", "a", "de", "del", "con", "en", "un",
           "una", "unos", "unas", "que", "por", "para", "esta", "este", "esto", "eso", "esa", "ese", "mi",
           "mis", "tu", "su", "y", "e", "o", "ok", "dale", "bueno", "che", "tambien", "ahi", "recien",
           "ahora", "eh", "mmm", "este", "pues", "bien", "todo", "toda", "listo", "lista", "hecho", "hecha"}
_GREETING = re.compile(
    r"^(hola|buenas|buen dia|buenos dias|buenas tardes|buenas noches|gracias|muchas gracias|genial|perfecto|"
    r"ok|dale|buenisimo|joya|chau|nos vemos|todo bien|que tal|como va)\W*$", re.I)
_MODIFY = re.compile(
    r"\b(mov[eé]|mova|pas[aá]l[oa]|pas[aá]me|cambi[aá]l?[oa]?|borr[aá]l?[oa]?|elimin[aá]l?[oa]?|cancel[aá]l?[oa]?|"
    r"renombr[aá]l?[oa]?|edit[aá]l?[oa]?|sac[aá]l?[oa]?|pon[eé]l?[oa]?|agreg[aá]l?[oa]?|restaur[aá]l?[oa]?|"
    r"recuper[aá]l?[oa]?|reabr[ií]l?[oa]?|posterg[aá]l?[oa]?|adelant[aá]l?[oa]?|reprogram[aá]l?[oa]?)\b", re.I)
_QUESTION = re.compile(r"^(podes|puedes|me podes|me ayudas|como|cuando|cuanto|cuantos|donde|quien|por que|"
                       r"para que|cual|que )\b|\?", re.I)
_CREATE_HINT = re.compile(
    r"\b(record[aá]me|recordame|recordatorio|record[aá]rme|anot[aá]me?|agend[aá]me?|agend[aá]|"
    r"met[eé]|mete|ten(go|es)|hay que|tengo que|hay|debo|necesito|acordate|no te olvides)\b", re.I)

_QUERY = [
    ("today", re.compile(r"\b(que|qué) (tengo|hay|hago) (para |de )?hoy\b|\bagenda (de )?hoy\b|\bmi dia\b|\bhoy tengo\b", re.I)),
    ("tomorrow", re.compile(r"\b(que|qué) (tengo|hay|hago) (para |de )?(mañana|manana)\b|\bagenda (de )?(mañana|manana)\b", re.I)),
    ("week", re.compile(r"\b(que|qué) (tengo|hay) (esta|la|para la|en la) semana\b|\bagenda (de )?(la )?semana\b|"
                        r"\bproximos dias\b|\bpróximos días\b", re.I)),
    ("overdue", re.compile(r"\b(atrasad[oa]s?|vencid[oa]s?)\b", re.I)),
    ("inbox", re.compile(r"\bsin categor[ií]a\b", re.I)),
    ("tasks", re.compile(r"\b(que|qué) (me )?(falta|queda)\b|\b(que|qué) tengo pendiente\b|\bmis tareas\b|"
                         r"\btareas pendientes\b|\bpendientes\b", re.I)),
]
_UNDO = re.compile(r"\b(deshac\w*|volve\w* atras|me equivoque|me equivoqué|cancel[aá] eso|borr[aá] lo [uú]ltimo)\b", re.I)
_RECAT = re.compile(r"\b(es|va|ponel[oa]|pasal[oa]|cambial[oa]|dejal[oa]|ponelo|movelo)\b.*\b(trabajo|personal|ocio)\b", re.I)


@dataclass
class Route:
    kind: str                       # query|complete|choose|undo|recategorize|create|greeting|modify|none
    scope: str | None = None
    item: Result | None = None
    candidates: list[Result] = field(default_factory=list)
    category: str | None = None
    confident: bool = False         # create: ¿las reglas decidieron la categoría?
    reason: str = ""


def _stem(w: str) -> str:
    return w[:5] if len(w) >= 5 else w


def _tokens(text: str) -> list[str]:
    return [t for t in _WORD.findall(fold(text))]


class Router:
    def __init__(self, core: Core):
        self.core = core

    # ----------------------------------------------------------- entrada
    def route(self, text: str, last_refs: list[int] | None = None) -> Route:
        t = text.strip()
        low = t.lower()
        words = _tokens(t)
        if not words:
            return Route("none", reason="vacío")
        if _GREETING.match(low):
            return Route("greeting")
        if len(words) <= 6 and _UNDO.search(low) and not parse_when(t, self.core.now()).day:
            return Route("undo")
        if len(words) <= 8 and last_refs and (m := _RECAT.search(low)) and not _CREATE_HINT.search(low):
            return Route("recategorize", category=m.group(2).lower())
        for scope, rx in _QUERY:
            if rx.search(low) and len(words) <= 10:
                return Route("query", scope=scope)
        done = self._completion(t, low, words)
        if done is not None:
            return done
        if _MODIFY.search(low):
            return Route("modify", reason="pedido de modificación")
        if _QUESTION.search(low) and not _CREATE_HINT.search(low):
            return Route("none", reason="pregunta")
        return self._create(t)

    # ------------------------------------------------------- completar
    def _completion(self, text: str, low: str, words: list[str]) -> Route | None:
        raw = _WORD.findall(low)
        has_ya = "ya" in raw
        pret_any = any(w in _IRREGULAR or (len(w) >= 4 and w[-1] in "éí") for w in raw)
        # sin "ya", un pretérito suelto ("compré pan") solo cuenta si es claramente un verbo: len >= 5
        pret_strict = any(w in _IRREGULAR or (len(w) >= 5 and w[-1] in "éí") for w in raw)
        starts_done = raw[0] in _DONE or low.startswith(("ya esta", "ya está", "ya lo hice", "ya la hice"))
        explicit = starts_done or (has_ya and pret_any)
        implicit = pret_strict and len(raw) <= 7 and not _CREATE_HINT.search(low)
        if not (explicit or implicit):
            return None
        when = parse_when(text, self.core.now())
        if when.day or when.at:
            return None                                  # tiene fecha: es un pedido nuevo, no "ya lo hice"
        query = [w for w in words if w not in _FILLER and w not in _IRREGULAR and w not in _DONE]
        if not query:                                    # "listo" / "ya está" a secas
            return None
        pend = [r for r in (self.core._from_row(x) for x in self.core.db.all_captures())
                if r.status == "pendiente" and r.type != "nota"]
        qstems = [_stem(w) for w in query]
        scored: list[tuple[float, Result]] = []
        for r in pend:
            title = {_stem(w) for w in _tokens(r.title)}
            hit = sum(1 for s in qstems if s in title)
            if hit:
                scored.append((hit / len(qstems), r))
        if not scored or max(s for s, _ in scored) < 0.5:
            # explícito => hay que decirlo ("no encontré…"); implícito => puede ser una captura nueva
            return Route("none", reason="no hay pendientes que coincidan") if explicit else None
        best = max(s for s, _ in scored)
        top = [r for s, r in sorted(scored, key=lambda x: -x[0]) if s >= best - 1e-9]
        if len(top) == 1:
            return Route("complete", item=top[0])
        return Route("choose", candidates=top[:5])

    # ----------------------------------------------------------- crear
    def _create(self, text: str) -> Route:
        now = self.core.now()
        when = parse_when(text, now)
        cls = self.core.classifier.classify(text, when)
        dated = bool(when.day or when.at)
        signal = dated or cls.confident or bool(_CREATE_HINT.search(text.lower())) or cls.type == "tarea"
        if not signal and len(_tokens(text)) <= 2:
            return Route("none", reason="poco contenido")
        return Route("create", category=cls.category, confident=cls.confident)
