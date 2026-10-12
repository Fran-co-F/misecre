"""Operador: decide QUIÉN resuelve cada mensaje. Reglas primero, IA solo como respaldo.

    mensaje ──► Router (reglas, 0 llamadas) ──► resuelto            ← la mayoría
                      │ no entiende / categoría dudosa
                      ▼
                  Agente IA (1-2 llamadas) ──► resuelto + APRENDE   ← respaldo
                                                  │
                                    learning.auto_learn: la próxima vez lo resuelven las reglas

El gasto de IA debería bajar con el uso: cada decisión de la IA que se repite se convierte en una regla
(reversible con `/regla quitar`).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from . import learning
from .agent import Agent, AgentError
from .core import Core, Result
from .intent import Router
from .render import confirm, list_text, outcome

log = logging.getLogger("misecre.operator")

HELP_NONE = ("No te entendí. Podés decirme qué anotar («comprar pan mañana»), qué hiciste («ya fui a comprar pan»), "
             "o preguntar («qué tengo hoy»). Con /ayuda ves todo.")
HELP_MODIFY = ("Para cambiar algo usá /editar 12, /mover 12 el viernes, /titulo 12 …, /hecha 12 o /borrar 12. "
               "Con la IA activa te lo hago hablando.")


@dataclass
class Reply:
    text: str
    source: str = "reglas"                 # reglas | ia
    mutations: int = 0
    refs: list[int] = field(default_factory=list)
    candidates: list[Result] = field(default_factory=list)
    capture: Result | None = None          # para mostrar los botones de pendientes (hora, categoría)
    learned: list[str] = field(default_factory=list)
    calls: int = 0
    route: str = ""
    undo_to: int = 0                       # id del último cambio en el historial al responder (ata el botón)
    notice: str = ""                       # aviso previo (p. ej. "la IA no respondió; lo anoté con reglas")


MODES = {
    "rules_first": "reglas primero, IA de respaldo (la más barata)",
    "ai_first": "IA primero, reglas de respaldo (entiende mejor, cuesta más)",
    "rules_only": "solo reglas, sin IA",
}
MODE_ALIASES = {
    "reglas": "rules_first", "sistema": "rules_first", "auto": "rules_first", "rules_first": "rules_first",
    "ia": "ai_first", "ai": "ai_first", "ai_first": "ai_first",
    "solo-reglas": "rules_only", "sinia": "rules_only", "sin-ia": "rules_only", "off": "rules_only",
    "rules_only": "rules_only",
}


class Operator:
    def __init__(self, core: Core, agent: Agent | None = None):
        self.core = core
        self.agent = agent or Agent(core)
        self.router = Router(core)

    # ------------------------------------------------------ modo
    @property
    def mode(self) -> str:
        m = self.core.db.kv_get("ai_mode") or self.core.cfg.ai.mode
        return m if m in MODES else "rules_first"

    def set_mode(self, name: str) -> str:
        m = MODE_ALIASES.get(name.strip().lower())
        if m is None:
            raise ValueError("Modo inválido. Usá: reglas, ia o solo-reglas")
        self.core.db.kv_set("ai_mode", m)
        return m

    @property
    def ai_enabled(self) -> bool:
        """¿Puede intervenir la IA ahora? (clave + activada + el modo lo permite)"""
        return self.agent.available and self.mode != "rules_only"

    # ------------------------------------------------------ estadísticas
    def _bump(self, source: str) -> None:
        key = f"route:{self.core.today().isoformat()}:{source}"
        self.core.db.kv_set(key, str(int(self.core.db.kv_get(key, "0") or 0) + 1))

    def stats_today(self) -> dict:
        d = self.core.today().isoformat()
        g = lambda s: int(self.core.db.kv_get(f"route:{d}:{s}", "0") or 0)  # noqa: E731
        return {"reglas": g("reglas"), "ia": g("ia")}

    # ------------------------------------------------- contexto reciente
    def _last_refs(self, chat_key: str) -> list[int]:
        try:
            return [int(x) for x in json.loads(self.core.db.kv_get(f"last_refs:{chat_key}", "[]") or "[]")]
        except (ValueError, TypeError):
            return []

    def _remember(self, chat_key: str, refs: list[int]) -> None:
        if not refs:
            return
        keep = (self._last_refs(chat_key) + refs)[-6:]
        self.core.db.kv_set(f"last_refs:{chat_key}", json.dumps(list(dict.fromkeys(reversed(keep)))[::-1]))

    def _context(self, chat_key: str) -> str:
        lines = []
        for ref in self._last_refs(chat_key)[-4:]:
            try:
                r = self.core.get(ref)
            except KeyError:
                continue
            st = {"borrada": "en la papelera", "hecha": "hecho", "pendiente": "pendiente"}.get(r.status, r.status)
            when = f", {r.when_label()}" if r.when_label() else ""
            lines.append(f"#{r.seq} «{r.title}» ({st}{when})")
        return "\n".join(lines)

    # ------------------------------------------------------------ consultas
    def _scope(self, scope: str) -> tuple[str, list[Result]]:
        core = self.core
        today = core.today()
        agenda = lambda d, n: [core._from_row(r) for r in core.agenda(d, n)]  # noqa: E731
        return {
            "today": ("Hoy", agenda(today, 1)),
            "tomorrow": ("Mañana", agenda(today + timedelta(days=1), 1)),
            "week": ("Próximos 7 días", agenda(today, 7)),
            "tasks": ("Tareas pendientes", core.tasks()),
            "overdue": ("Atrasadas", core.overdue()),
            "inbox": ("Sin categoría", core.inbox()),
        }[scope]

    # ---------------------------------------------------------------- main
    def handle(self, chat_key: str, text: str, source: str = "telegram") -> Reply:
        text = text.strip()
        if not text:
            raise ValueError("Mensaje vacío")
        core = self.core
        last = self._last_refs(chat_key)
        route = self.router.route(text, last)
        kind = route.kind
        ai_ok = self.ai_enabled
        voice = source.endswith("voz")
        notice = ""

        # ----- modo "IA primero": todo va al agente salvo lo trivial; las reglas quedan de respaldo
        if self.mode == "ai_first" and ai_ok and kind not in ("greeting", "undo"):
            try:
                return self._by_ai(chat_key, text, kind, voice)
            except AgentError as e:
                log.warning("IA no disponible: %s", e)
                notice = f"⚠️ {e}\nProbé con las reglas."

        if kind == "greeting":
            return self._done(Reply("¡Hola! Decime qué anotar o qué hiciste. /ayuda para ver todo.", route=kind))

        if kind == "undo":
            try:
                action, r = core.undo()
            except LookupError:
                return self._done(Reply("No hay nada para deshacer.", route=kind))
            self._remember(chat_key, [r.seq] if r else [])
            return self._done(Reply(f"↩ Deshice: {action}" + (f"\n{confirm(r, 'Quedó')}" if r else ""), route=kind))

        if kind == "recategorize":
            ref = last[-1]
            r = core.set_category(ref, route.category, by_user=True)       # corrección humana: alimenta el aprendizaje
            return self._done(Reply(f"✓ #{r.seq} «{r.title}» → {route.category}", mutations=1,
                                    refs=[r.seq], route=kind))

        if kind == "query":
            title, rows = self._scope(route.scope)
            return self._done(Reply(list_text(rows, title, with_day=route.scope in ("week", "tasks")),
                                    route=kind))

        if kind == "complete":
            r = core.complete(route.item.seq)
            self._remember(chat_key, [r.seq])
            return self._done(Reply(outcome(r, {"done"}), mutations=1, refs=[r.seq], route=kind))

        if kind == "choose":
            names = "\n".join(f"#{c.seq} {c.title}" for c in route.candidates)
            return self._done(Reply(f"¿Cuál de estas?\n{names}", candidates=route.candidates, route=kind))

        if kind == "create" and (route.confident or not ai_ok or notice):
            out = self._create_by_rules(chat_key, text, source, route.kind)
            out.notice = out.notice or notice
            return out

        # ----- respaldo: la IA (categoría dudosa, modificaciones, preguntas libres)
        if ai_ok and not notice:
            try:
                return self._by_ai(chat_key, text, route.kind, voice)
            except AgentError as e:
                log.warning("IA no disponible: %s", e)
                if kind == "create":
                    out = self._create_by_rules(chat_key, text, source, "create")
                    out.notice = f"⚠️ {e}\nLo anoté con las reglas."
                    return out
                return self._done(Reply(f"⚠️ {e}", route=kind))
        out = Reply(HELP_MODIFY if kind == "modify" else HELP_NONE, route=kind, notice=notice)
        return self._done(out)

    # ----------------------------------------------------------- caminos
    def _create_by_rules(self, chat_key: str, text: str, source: str, route: str) -> Reply:
        r = self.core.capture_text(text, source=source, use_ai=False)
        self._remember(chat_key, [r.seq])
        # Botones solo si hay algo que resolver (hora ambigua, falta hora, sin categoría) o no hay IA
        need = bool({"hour", "time"} & set(r.pending)) or r.category is None or not self.ai_enabled
        return self._done(Reply(confirm(r), mutations=1, refs=[r.seq], capture=r if need else None, route=route))

    def _by_ai(self, chat_key: str, text: str, route: str, from_voice: bool = False) -> Reply:
        a = self.agent.chat(chat_key, text, context=self._context(chat_key), from_voice=from_voice)
        self._remember(chat_key, a.refs)
        learned = []
        if a.mutations:
            for s in learning.auto_learn(self.core):
                learned.append(f"🧠 Aprendí: «{s.word}» → {s.category}. Si no te cierra: /regla quitar {s.category} {s.word}")
        return self._done(Reply(a.text, source="ia", mutations=a.mutations, refs=a.refs,
                                learned=learned, calls=a.calls, route=route))

    def _done(self, reply: Reply) -> Reply:
        self._bump(reply.source)
        if reply.mutations:
            reply.undo_to = self.core.db.history_last_id()
        return reply
