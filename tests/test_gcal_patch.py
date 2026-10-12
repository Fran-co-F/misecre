"""GoogleCalendar.update_event: PATCH acotado (sin pisar datos de Google) y duración de varios días."""
from datetime import date, datetime
from types import SimpleNamespace

from misecre.gcal import GoogleCalendar


class Exec:
    def __init__(self, result=None):
        self.result = result

    def execute(self):
        return self.result


class FakeEvents:
    def __init__(self, current=None):
        self.calls, self.current = [], current or {}

    def get(self, **kw):
        self.calls.append(("get", kw))
        return Exec(self.current)

    def patch(self, **kw):
        self.calls.append(("patch", kw))
        return Exec({})

    def update(self, **kw):                     # no debe usarse: reemplaza el recurso entero
        raise AssertionError("update() pisa los datos de Google")


def make(cfg, events):
    cal = GoogleCalendar(cfg, db=None)
    cal._svc = SimpleNamespace(events=lambda: events)
    return cal


def patch_body(events):
    return next(kw for name, kw in events.calls if name == "patch")["body"]


def test_solo_toca_titulo_cuando_y_avisos(cfg):
    ev = FakeEvents()
    cal = make(cfg, ev)
    cal.update_event("e1", "c1", "Nuevo título", "texto original", start=datetime(2026, 10, 15, 10, 0),
                     end=datetime(2026, 10, 15, 11, 0), day=None)
    body = patch_body(ev)
    assert set(body) == {"summary", "start", "end", "reminders"}      # sin description / location / attendees
    assert body["summary"] == "Nuevo título"
    assert body["start"]["dateTime"].startswith("2026-10-15T10:00") and body["start"]["date"] is None
    assert body["reminders"] == {"useDefault": False, "overrides": []}


def test_evento_de_varios_dias_mantiene_su_duracion(cfg):
    ev = FakeEvents(current={"start": {"date": "2026-10-20"}, "end": {"date": "2026-10-23"}})   # 3 días
    cal = make(cfg, ev)
    cal.update_event("e1", "c1", "Viaje", "", start=None, end=None, day=date(2026, 10, 27))
    body = patch_body(ev)
    assert body["start"] == {"date": "2026-10-27", "dateTime": None, "timeZone": None}
    assert body["end"]["date"] == "2026-10-30"                         # +3 días, no +1
    assert body["end"]["dateTime"] is None                            # y limpia el horario


def test_todo_el_dia_nuevo_dura_un_dia(cfg):
    ev = FakeEvents(current={"start": {"dateTime": "2026-10-20T10:00:00-03:00"},
                             "end": {"dateTime": "2026-10-20T11:00:00-03:00"}})
    cal = make(cfg, ev)
    cal.update_event("e1", "c1", "x", "", start=None, end=None, day=date(2026, 10, 21))
    assert patch_body(ev)["end"]["date"] == "2026-10-22"
