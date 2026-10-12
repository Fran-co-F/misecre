from datetime import date, datetime

import pytest

from misecre.core import Core

from .fakes import FakeAnthropic, FakeCalendar


@pytest.fixture
def core(cfg):
    return Core(cfg, calendar=FakeCalendar())


def mk(core, now, text="reunión con el cliente el jueves a las 10"):
    return core.capture_text(text, now=now)


# ------------------------------------------------------------------ seq
def test_numero_corto_y_referencias(core, now):
    a, b = mk(core, now), mk(core, now, "cena con amigos el sábado a las 21")
    assert (a.seq, b.seq) == (1, 2)
    for ref in (2, "2", "#2", b.id):
        assert core.get(ref).id == b.id
    with pytest.raises(KeyError):
        core.get(99)


def test_capture_sin_contenido_se_rechaza(core, now):
    with pytest.raises(ValueError):
        core.capture_text("armame un recordatorio para mañana", now=now)


# ---------------------------------------------------------------- update
def test_cambiar_titulo_propaga_al_evento(core, now):
    r = mk(core, now)
    r2 = core.update_title("#1", "Reunión con Marcos")
    assert r2.title == "Reunión con Marcos" and core.get(r.id).title == "Reunión con Marcos"
    assert core.calendar.events["ev1"].title == "Reunión con Marcos"


def test_mover_conserva_lo_que_no_se_menciona(core, now):
    r = mk(core, now)                                    # jueves 15/10 10:00
    r2 = core.reschedule(r.id, "el viernes")
    assert r2.start == datetime(2026, 10, 16, 10, 0)     # misma hora, otro día
    r3 = core.reschedule(r.id, "a las 15")
    assert r3.start == datetime(2026, 10, 16, 15, 0)     # mismo día, otra hora
    assert r3.end == datetime(2026, 10, 16, 16, 0)       # misma duración
    r4 = core.reschedule(r.id, "mañana a las 9 y media")
    assert r4.start == datetime(2026, 10, 13, 9, 30)
    assert core.calendar.events["ev1"].start == datetime(2026, 10, 13, 9, 30)


def test_mover_a_todo_el_dia_y_volver(core, now):
    r = mk(core, now)
    r2 = core.reschedule(r.id, "el viernes sin hora")
    assert r2.start is None and r2.day == date(2026, 10, 16)
    ev = core.calendar.events["ev1"]
    assert ev.start is None and ev.day == date(2026, 10, 16)
    r3 = core.reschedule(r.id, "a las 18")
    assert r3.start == datetime(2026, 10, 16, 18, 0)


def test_mover_con_texto_incomprensible(core, now):
    r = mk(core, now)
    with pytest.raises(ValueError):
        core.reschedule(r.id, "cuando puedas")


def test_duracion(core, now):
    r = mk(core, now)
    r2 = core.set_duration(r.id, 90)
    assert r2.end == datetime(2026, 10, 15, 11, 30)
    assert core.calendar.events["ev1"].end == datetime(2026, 10, 15, 11, 30)
    with pytest.raises(ValueError):
        core.set_duration(r.id, 1)


def test_cambiar_tipo(core, now):
    r = core.capture_text("comprar pan", now=now)
    assert r.type == "tarea"
    assert core.set_type(r.id, "nota").type == "nota"
    assert core.tasks() == []
    with pytest.raises(ValueError):
        core.set_type(r.id, "otra")


def test_hecha_y_reabrir(core, now):
    r = core.capture_text("entregar el informe al jefe el viernes", now=now)
    assert core.complete(r.id).status == "hecha"
    assert core.tasks() == []
    assert core.reopen(r.id).status == "pendiente"
    assert len(core.tasks()) == 1


# ---------------------------------------------------------------- delete
def test_borrar_papelera_y_restaurar(core, now):
    r = mk(core, now)
    core.set_category(r.id, "trabajo")
    core.delete(r.id)
    assert "ev1" not in core.calendar.events
    assert [t.seq for t in core.trash()] == [1]
    assert core.agenda(date(2026, 10, 15)) == []

    r2 = core.restore("#1")
    assert r2.status == "pendiente"
    assert r2.event_id and r2.event_id in core.calendar.events      # evento recreado
    assert core.trash() == []
    with pytest.raises(ValueError):
        core.restore("#1")


# ------------------------------------------------------------------ undo
def test_deshacer_cambio_de_titulo(core, now):
    r = mk(core, now)
    core.update_title(r.id, "Otro título")
    action, res = core.undo()
    assert action == "título" and res.title == "Reunión con el cliente"
    assert core.calendar.events["ev1"].title == "Reunión con el cliente"


def test_deshacer_borrado(core, now):
    r = mk(core, now)
    core.delete(r.id)
    action, res = core.undo()
    assert action == "borrar" and res.status == "pendiente"
    assert len(core.calendar.events) == 1


def test_deshacer_creacion(core, now):
    r = mk(core, now)
    action, res = core.undo()
    assert action == "crear" and res is None
    assert core.get(r.id).status == "borrada" and core.calendar.events == {}


def test_deshacer_sin_historial(core):
    with pytest.raises(LookupError):
        core.undo()


# ---------------------------------------------------------------- consultas
def test_busqueda_sin_tildes_ni_mayusculas(core, now):
    mk(core, now)
    mk(core, now, "turno con el dentista mañana a las 16")
    assert [r.seq for r in core.search("REUNION cliente")] == [1]
    assert [r.seq for r in core.search("dentista")] == [2]
    assert core.search("zzz") == []


def test_inbox_y_estadisticas(core, now):
    mk(core, now)
    core.capture_text("comprar pan mañana a las 9", now=now)      # sin categoría
    assert [r.seq for r in core.inbox()] == [2]
    s = core.stats()
    assert s["activas"] == 2 and s["sin_categoria"] == 1
    assert s["en_calendario"] == 1 and s["sin_calendario"] == 1


def test_atrasadas(core, now):
    core.capture_text("entregar el informe al jefe el viernes", now=now)
    assert core.overdue() == []
    core.db.update_capture(core.get(1).id, all_day_date="2026-10-01")
    assert [r.seq for r in core.overdue()] == [1]


# --------------------------------------------------------------- reglas
def test_regla_de_usuario_cambia_la_clasificacion(core, now):
    assert core.capture_text("paddle con Pedro", now=now).category is None
    core.add_rule("ocio", "paddle")
    assert core.capture_text("paddle con Pedro", now=now).category == "ocio"
    core.remove_rule("ocio", "paddle")
    assert core.capture_text("paddle con Pedro", now=now).category is None


def test_quitar_regla_de_fabrica(core, now):
    assert core.capture_text("ir al cine", now=now).category == "ocio"
    core.remove_rule("ocio", "cine")
    assert core.capture_text("ir al cine", now=now).category is None


# ------------------------------------------------------------------- IA
AI_OK = {"category": "personal", "type": "tarea", "title": "Llevar el auto al taller", "confidence": 0.9}


def test_ia_desactivada_por_defecto(cfg, now):
    fake = FakeAnthropic(AI_OK)
    c = Core(cfg, ai_client=fake)
    assert c.capture_text("llevar el auto al taller", now=now).category is None
    assert fake.calls == []


def test_ia_resuelve_lo_que_las_reglas_no(cfg, now):
    cfg.ai.enabled = True
    fake = FakeAnthropic(AI_OK)
    c = Core(cfg, ai_client=fake)
    r = c.capture_text("llevar el auto al taller", now=now)
    assert r.category == "personal" and r.auto_source == "ai"
    assert r.title == "Llevar el auto al taller" and r.type == "tarea"
    assert r.pending == ["category"]            # igual hay que confirmarla
    call = fake.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert "json_schema" in str(call["output_config"])
    assert c.ai.calls_today() == 1


def test_ia_no_se_consulta_si_las_reglas_deciden(cfg, now):
    cfg.ai.enabled = True
    fake = FakeAnthropic(AI_OK)
    c = Core(cfg, ai_client=fake)
    assert c.capture_text("reunión con el cliente mañana a las 10", now=now).auto_source == "rules"
    assert fake.calls == []


def test_ia_con_poca_confianza_o_con_error_no_rompe(cfg, now):
    cfg.ai.enabled = True
    low = Core(cfg, ai_client=FakeAnthropic({**AI_OK, "confidence": 0.2}))
    assert low.capture_text("llevar el auto al taller", now=now).category is None
    boom = Core(cfg, ai_client=FakeAnthropic(error=RuntimeError("sin red")))
    r = boom.capture_text("llevar el auto al taller", now=now)
    assert r.category is None and boom.get(r.id).title


def test_ia_respeta_tope_diario(cfg, now):
    cfg.ai.enabled = True
    cfg.ai.max_calls_per_day = 1
    fake = FakeAnthropic(AI_OK)
    c = Core(cfg, ai_client=fake)
    c.capture_text("llevar el auto al taller", now=now)
    c.capture_text("llevar el auto al lavadero", now=now)
    assert len(fake.calls) == 1
