"""Categorías que avisan pero NO van a Google Calendar (p. ej. ocio)."""
from datetime import datetime

import pytest

from misecre.core import Core
from misecre.reminders import Reminders
from misecre.render import confirm, line
from misecre.sync import Syncer

from .fakes import FakeCalendar


def at(h, m=0, day=12):
    return datetime(2026, 10, day, h, m)


@pytest.fixture
def core(cfg):
    cfg.calendar.skip_categories = ["ocio"]
    return Core(cfg, calendar=FakeCalendar())


def test_ocio_no_va_a_google_pero_queda_guardado(core, now):
    r = core.capture_text("cena con amigos el sábado a las 21", now=now)
    assert r.category == "ocio" and r.event_id is None and r.calendar_off
    assert core.calendar.created == []
    assert "🔔 solo avisos" in confirm(r) and "🔔" in line(r) and "sin calendario" not in line(r)
    t = core.capture_text("reunión con el cliente mañana a las 10", now=now)
    assert t.event_id == "ev1"                                   # trabajo sí


def test_ocio_igual_avisa_si_se_configura(core, now):
    rem = Reminders(core)
    core.capture_text("cena con amigos hoy a las 21", now=at(9))
    assert rem.collect(at(20, 40)) == []                         # por defecto ocio es solo resumen
    rem.set_policy("ocio", [30, 10], digest_only=False)
    assert [d.key for d in rem.collect(at(20, 35))] == ["b30"]   # avisa sin pasar por Google
    assert core.calendar.created == []


def test_apagar_la_categoria_retira_los_eventos_y_prenderla_los_recrea(cfg, now):
    cfg.calendar.skip_categories = []
    core = Core(cfg, calendar=FakeCalendar())
    r = core.capture_text("cena con amigos el sábado a las 21", now=now)
    assert r.event_id and len(core.calendar.events) == 1
    core.set_calendar_for("ocio", False)
    assert core.calendar.events == {} and core.get(r.id).event_id is None
    assert core.get(r.id).status == "pendiente"                  # la captura y sus avisos siguen
    assert core.skipped_categories() == {"ocio"}
    core.set_calendar_for("ocio", True)
    assert len(core.calendar.events) == 1 and core.get(r.id).event_id


def test_la_sync_no_lee_ni_importa_de_categorias_apagadas(core, now):
    core.calendar.user_create("ocio", "Fiesta", start=datetime(2026, 10, 20, 22, 0))
    core.calendar.user_create("personal", "Dentista", start=datetime(2026, 10, 20, 17, 0))
    rep = Syncer(core).run()
    assert rep.imported == 1 and [r["title"] for r in core.db.all_captures()] == ["Dentista"]


def test_la_sync_retira_eventos_que_quedaron_de_una_categoria_apagada(cfg, now):
    cfg.calendar.skip_categories = []
    core = Core(cfg, calendar=FakeCalendar())
    core.capture_text("cena con amigos el sábado a las 21", now=now)
    core.cfg.calendar.skip_categories = ["ocio"]                 # se apaga por config y se reinicia
    rep = Syncer(core).run()
    assert core.calendar.events == {} and any("Retiré" in c for c in rep.changes)


def test_estadisticas_no_cuentan_como_faltante_lo_apagado(core, now):
    core.capture_text("cena con amigos el sábado a las 21", now=now)
    s = core.stats()
    assert s["con_fecha"] == 0 and s["sin_calendario"] == 0


def test_override_en_caliente_gana_a_la_config(core):
    core.set_calendar_for("ocio", True)                          # config dice skip, el comando dice on
    assert core.calendar_wanted("ocio") is True
    core.set_calendar_for("trabajo", False)
    assert core.skipped_categories() == {"trabajo"}
    with pytest.raises(ValueError):
        core.set_calendar_for("xx", False)
