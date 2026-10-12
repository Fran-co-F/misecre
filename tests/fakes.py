"""Dobles de prueba: calendario de Google en memoria y cliente de Anthropic simulado."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

from misecre.gcal import CalEvent


class FakeCalendar:
    """Implementa la misma interfaz que GoogleCalendar, con estado en memoria.

    `user_*` simulan cambios hechos a mano en Google Calendar.
    """

    def __init__(self):
        self.events: dict[str, CalEvent] = {}
        self.n = 0
        self.log: list[tuple] = []
        self.clock = 0
        self.created, self.moved, self.deleted, self.updated = [], [], [], []

    # -- interfaz usada por Core --------------------------------------
    def calendar_id(self, category):
        return f"cal-{category}"

    def create_event(self, category, title, description, *, start, end, day):
        self.n += 1
        eid = f"ev{self.n}"
        self.clock += 1
        self.events[eid] = CalEvent(eid, f"cal-{category}", category, title, start, end,
                                    None if start else day, updated=f"{self.clock:06d}")
        self.log.append(("create", eid, category, title, start, day))
        self.created.append((category, title, start, end, day))
        return eid, f"cal-{category}"

    def update_event(self, event_id, calendar_id, title, description, *, start, end, day):
        self.clock += 1
        e = self.events[event_id]
        self.events[event_id] = replace(e, title=title, start=start, end=end,
                                        day=None if start else day, updated=f"{self.clock:06d}")
        self.log.append(("update", event_id, title, start, day))
        self.updated.append((event_id, calendar_id, start, end, day))

    def move_event(self, event_id, from_cal, to_category):
        self.clock += 1
        e = self.events[event_id]
        self.events[event_id] = replace(e, calendar_id=f"cal-{to_category}", category=to_category,
                                        updated=f"{self.clock:06d}")
        # en Google, mover resetea los recordatorios; GoogleCalendar.move_event los vuelve a silenciar
        self.events[event_id] = replace(self.events[event_id], reminders_default=False)
        self.log.append(("move", event_id, from_cal, to_category))
        self.moved.append((event_id, from_cal, to_category))
        return event_id, f"cal-{to_category}"

    def silence_event(self, event_id, calendar_id):
        self.clock += 1
        e = self.events[event_id]
        self.events[event_id] = replace(e, reminders_default=False, updated=f"{self.clock:06d}")
        self.log.append(("silence", event_id))

    def delete_event(self, event_id, calendar_id):
        self.events.pop(event_id, None)
        self.log.append(("delete", event_id))
        self.deleted.append((event_id, calendar_id))

    def list_changes(self, category, updated_min, past_days=1):
        out = []
        for e in self.events.values():
            if e.category == category and (not updated_min or e.updated > updated_min):
                out.append(e)
        out += [e for e in getattr(self, "_cancelled", []) if e.category == category
                and (not updated_min or e.updated > updated_min)]
        return out

    # -- cosas que hace "el usuario" en la UI de Google ---------------
    def user_edit(self, event_id, **changes):
        self.clock += 1
        self.events[event_id] = replace(self.events[event_id], updated=f"{self.clock:06d}", **changes)

    def user_delete(self, event_id):
        self.clock += 1
        e = self.events.pop(event_id)
        self._cancelled = getattr(self, "_cancelled", []) + [
            replace(e, deleted=True, updated=f"{self.clock:06d}")]

    def user_create(self, category, title, start=None, day=None, end=None):
        self.n += 1
        self.clock += 1
        eid = f"ev{self.n}"
        self.events[eid] = CalEvent(eid, f"cal-{category}", category, title, start, end, day,
                                    updated=f"{self.clock:06d}", reminders_default=True)
        return eid

    def user_move_calendar(self, event_id, category):
        self.clock += 1
        e = self.events[event_id]
        self.events[event_id] = replace(e, calendar_id=f"cal-{category}", category=category,
                                        updated=f"{self.clock:06d}", reminders_default=True)


class FakeAnthropic:
    """Simula anthropic.Anthropic().messages.create devolviendo JSON estructurado."""

    def __init__(self, payload=None, error: Exception | None = None):
        self.payload, self.error, self.calls = payload, error, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


class ScriptedAnthropic:
    """Cliente de Anthropic guionado: cada llamada a messages.create consume el próximo paso del guion.

    Un paso es una respuesta ya armada (`tool(...)` / `say(...)`) o una función(kwargs) que la devuelve.
    """

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        if not self.steps:
            raise AssertionError("el agente pidió más llamadas de las del guion")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(kw) if callable(step) else step


_n = [0]


def tool(name: str, **inp):
    _n[0] += 1
    block = SimpleNamespace(type="tool_use", id=f"toolu_{_n[0]}", name=name, input=inp)
    return SimpleNamespace(content=[block], stop_reason="tool_use")


def say(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason)


def tools(*calls):
    """Una respuesta con VARIAS tool_use a la vez (llamadas en paralelo). calls = (nombre, {input})."""
    blocks = []
    for name, inp in calls:
        _n[0] += 1
        blocks.append(SimpleNamespace(type="tool_use", id=f"toolu_{_n[0]}", name=name, input=inp))
    return SimpleNamespace(content=blocks, stop_reason="tool_use")
