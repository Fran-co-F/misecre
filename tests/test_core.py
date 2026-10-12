from datetime import datetime

import pytest

from misecre.core import Core

from .fakes import FakeCalendar


@pytest.fixture
def core(cfg):
    return Core(cfg, calendar=FakeCalendar())


def fb(core):
    return [dict(r) for r in core.db._conn.execute("SELECT kind, auto_value, chosen_value FROM feedback")]


def test_evento_trabajo(core, now):
    r = core.capture_text("reunión con el cliente el jueves a las 10 por el presupuesto", now=now)
    assert r.category == "trabajo" and r.type == "evento"
    assert r.start == datetime(2026, 10, 15, 10, 0)
    assert r.title == "Reunión con el cliente por el presupuesto"
    assert r.event_id == "ev1"
    assert core.calendar.created[0][0] == "trabajo"


def test_ambiguo_va_a_inbox_y_sin_evento(core, now):
    r = core.capture_text("comprar pan mañana a las 9", now=now)
    assert r.category is None and r.needs_category
    assert core.calendar.created == []


def test_corregir_categoria_crea_el_evento(core, now):
    r = core.capture_text("algo importante mañana a las 9", now=now)
    assert r.needs_category and r.type == "evento"
    r2 = core.set_category(r.id, "personal")
    assert r2.category == "personal" and r2.event_id == "ev1"
    assert core.calendar.created[0][0] == "personal"


def test_recategorizar_mueve_evento_de_calendario(core, now):
    r = core.capture_text("cena con amigos el sábado a las 21", now=now)
    assert r.category == "ocio"
    core.set_category(r.id, "personal")
    assert core.calendar.moved == [("ev1", "cal-ocio", "personal")]


def test_borrar_va_a_papelera_y_borra_el_evento(core, now):
    r = core.capture_text("turno con el dentista mañana a las 16", now=now)
    core.delete(r.id)
    assert core.calendar.deleted == [("ev1", "cal-personal")]
    assert core.get(r.id).status == "borrada"


def test_google_caido_no_pierde_la_captura(cfg, now):
    class Boom(FakeCalendar):
        def create_event(self, *a, **k):
            raise RuntimeError("sin red")

    core = Core(cfg, calendar=Boom())
    r = core.capture_text("reunión con el jefe mañana a las 11", now=now)
    assert r.event_id is None and core.get(r.id).title == "Reunión con el jefe"
    assert any("calendario" in w.lower() for w in r.warnings)


def test_tarea_con_fecha_va_al_calendario_todo_el_dia(core, now):
    r = core.capture_text("entregar el informe al jefe el viernes", now=now)
    assert r.type == "tarea" and r.category == "trabajo" and r.event_id == "ev1"
    cat, _title, start, _end, day = core.calendar.created[0]
    assert cat == "trabajo" and start is None and str(day) == "2026-10-16"


def test_recordatorio_hablado(core, now):
    r = core.capture_text(
        "armame un recordatorio para mañana de jugar al control resonant durante tres horas.", now=now)
    assert r.title == "Jugar al control resonant"
    assert r.category == "ocio" and r.event_id == "ev1"
    assert str(core.calendar.created[0][4]) == "2026-10-13"


def test_duracion_en_evento_con_hora(core, now):
    r = core.capture_text("partido con amigos mañana a las 20 durante dos horas", now=now)
    assert r.start == datetime(2026, 10, 13, 20, 0) and r.end == datetime(2026, 10, 13, 22, 0)


def test_agenda_del_dia(core, now):
    core.capture_text("reunión con el cliente mañana a las 10", now=now)
    core.capture_text("cena con amigos el sábado a las 21", now=now)
    rows = core.agenda(datetime(2026, 10, 13).date())
    assert [r["title"] for r in rows] == ["Reunión con el cliente"]


# ---------------------------------------------------------------- ambigüedades
def test_pendientes_iniciales(core, now):
    assert core.capture_text("reunión con el cliente el jueves a las 10", now=now).pending == ["category"]
    assert core.capture_text("cena con amigos el sábado a las 5", now=now).pending == ["category", "hour"]
    assert core.capture_text("recordame comprar pan mañana", now=now).pending == ["category", "time"]
    assert core.capture_text("algo suelto sin fecha", now=now).pending == ["category"]


def test_confirmar_categoria_limpia_pendiente_y_registra(core, now):
    r = core.capture_text("reunión con el cliente el jueves a las 10", now=now)
    assert core.set_category(r.id, "trabajo").pending == []
    assert fb(core) == [{"kind": "category", "auto_value": "trabajo", "chosen_value": "trabajo"}]


def test_corregir_categoria_registra_auto_vs_elegida(core, now):
    r = core.capture_text("cena con amigos el sábado a las 21", now=now)
    core.set_category(r.id, "personal")
    assert fb(core) == [{"kind": "category", "auto_value": "ocio", "chosen_value": "personal"}]


def test_categoria_elegida_por_el_agente_no_es_correccion_humana(core, now):
    r = core.capture_text("paddle con Pedro", now=now)
    r2 = core.set_category(r.id, "ocio", by_user=False)
    assert r2.category == "ocio" and "category" in r2.pending      # sigue sin confirmar
    assert [f for f in fb(core) if f["kind"] == "category"] == []   # no cuenta como corrección humana
    assert [f["kind"] for f in fb(core)] == ["agent_category"]      # pero queda registrada para aprender


def test_hora_am_pm(core, now):
    r = core.capture_text("reunión con el cliente mañana a las 5", now=now)
    assert r.start.hour == 17 and "hour" in r.pending
    r2 = core.set_hour(r.id, "am")
    assert r2.start == datetime(2026, 10, 13, 5, 0) and r2.end == datetime(2026, 10, 13, 6, 0)
    assert "hour" not in r2.pending
    assert core.calendar.events["ev1"].start == datetime(2026, 10, 13, 5, 0)
    assert {"kind": "hour", "auto_value": "pm", "chosen_value": "am"} in fb(core)


def test_ponerle_hora_a_algo_de_todo_el_dia(core, now):
    r = core.capture_text("reunión con el cliente mañana", now=now)
    assert r.start is None and "time" in r.pending and r.event_id == "ev1"
    r2 = core.set_time(r.id, "a las 18:30")
    assert r2.start == datetime(2026, 10, 13, 18, 30) and "time" not in r2.pending
    assert core.calendar.updated[-1][2] == datetime(2026, 10, 13, 18, 30)


def test_set_time_con_texto_invalido(core, now):
    r = core.capture_text("reunión con el cliente mañana", now=now)
    with pytest.raises(ValueError):
        core.set_time(r.id, "no sé cuándo")


def test_dejar_todo_el_dia(core, now):
    r = core.capture_text("reunión con el cliente mañana", now=now)
    r2 = core.skip_time(r.id)
    assert "time" not in r2.pending and r2.start is None
    assert {"kind": "time", "auto_value": "todo-el-dia", "chosen_value": "todo-el-dia"} in fb(core)


def test_tareas_pendientes_y_completar(core, now):
    core.capture_text("comprar pan", now=now)
    b = core.capture_text("entregar el informe al jefe el viernes", now=now)
    core.capture_text("reunión con el cliente mañana a las 10", now=now)   # evento: no es tarea
    assert [t.title for t in core.tasks()] == ["Entregar el informe al jefe", "Comprar pan"]
    assert core.complete(b.id).status == "hecha"
    assert [t.title for t in core.tasks()] == ["Comprar pan"]
