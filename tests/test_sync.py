from datetime import date, datetime, timedelta

import pytest

from misecre.core import Core
from misecre.sync import Syncer

from .fakes import FakeCalendar


@pytest.fixture
def core(cfg):
    return Core(cfg, calendar=FakeCalendar())


@pytest.fixture
def syncer(core):
    return Syncer(core)


def mk(core, now, text="reunión con el cliente el jueves a las 10"):
    r = core.capture_text(text, now=now)
    return core.set_category(r.id, r.category) if r.category else r


def row(core, ref=1):
    return core.db.get_by_seq(ref)


# ===================================================== Calendar -> DB
def test_sin_cambios_es_idempotente(core, syncer, now):
    mk(core, now)
    assert not syncer.run().changed
    assert not syncer.run().changed


def test_sin_google_no_hace_nada(cfg, now):
    c = Core(cfg)
    c.capture_text("reunión con el cliente el jueves a las 10", now=now)
    rep = Syncer(c).run()
    assert not rep.changed and not rep.errors


def test_mover_evento_en_google_actualiza_la_db_y_los_avisos(core, syncer, now):
    mk(core, now)
    core.db.sent_mark(row(core)["id"], "b15", "x")
    core.calendar.user_edit("ev1", start=datetime(2026, 10, 16, 15, 0), end=datetime(2026, 10, 16, 16, 0))
    rep = syncer.run()
    assert any("cuándo" in c for c in rep.changes)
    assert row(core)["start"] == "2026-10-16T15:00:00"
    assert not core.db.sent_has(row(core)["id"], "b15")           # el aviso vuelve a estar pendiente
    assert not syncer.run().changed                                # y es estable


def test_renombrar_en_google_actualiza_el_titulo(core, syncer, now):
    mk(core, now)
    core.calendar.user_edit("ev1", title="Reunión con Marcos")
    syncer.run()
    assert core.get(1).title == "Reunión con Marcos"
    assert not syncer.run().changed


def test_borrar_en_google_manda_a_la_papelera_y_es_reversible(core, syncer, now):
    mk(core, now)
    core.calendar.user_delete("ev1")
    rep = syncer.run()
    assert any("Borraste" in c for c in rep.changes)
    assert row(core)["status"] == "borrada" and row(core)["event_id"] is None
    assert [t.seq for t in core.trash()] == [1]
    assert not syncer.run().changed
    core.restore(1)
    assert len(core.calendar.events) == 1                          # el evento se recrea


def test_mover_entre_calendarios_cambia_la_categoria(core, syncer, now):
    mk(core, now)                                                  # trabajo
    core.calendar.user_move_calendar("ev1", "ocio")
    syncer.run()
    assert core.get(1).category == "ocio"
    assert row(core)["status"] == "pendiente"                      # no se interpretó como borrado


def test_evento_creado_a_mano_en_google_se_importa(core, syncer, now):
    core.calendar.user_create("personal", "Dentista", start=datetime(2026, 10, 20, 17, 0),
                              end=datetime(2026, 10, 20, 17, 30))
    rep = syncer.run()
    assert rep.imported == 1
    r = core.get(1)
    assert (r.category, r.type, r.title, r.source) == ("personal", "evento", "Dentista", "calendar")
    assert r.end - r.start == timedelta(minutes=30) and r.pending == []
    assert not syncer.run().changed                                # no se vuelve a importar
    assert len(core.db.all_captures()) == 1


def test_importa_dia_completo_y_no_importa_eventos_viejos(core, syncer, now):
    core.calendar.user_create("ocio", "Feriado largo", day=date(2026, 10, 12))
    core.calendar.user_create("ocio", "Viejo", day=date.today() - timedelta(days=30))
    rep = syncer.run()
    titles = [r["title"] for r in core.db.all_captures()]
    assert "Feriado largo" in titles and "Viejo" not in titles and rep.imported == 1


def test_no_importa_si_esta_desactivado(core, syncer, now):
    core.cfg.sync.import_calendar_events = False
    core.calendar.user_create("personal", "Dentista", start=datetime(2026, 10, 20, 17, 0))
    assert syncer.run().imported == 0


def test_no_importa_eventos_de_la_autoprueba(core, syncer, now):
    core.calendar.user_create("personal", "[misecre-selftest] prueba", start=datetime(2026, 10, 20, 3, 0))
    assert syncer.run().imported == 0


# ===================================================== lo nuestro -> Google
def test_ediciones_locales_no_generan_cambios_al_sincronizar(core, syncer, now):
    r = mk(core, now)
    core.update_title(r.id, "Otro título")
    core.reschedule(r.id, "el viernes a las 15")
    core.set_category(r.id, "personal")
    assert not syncer.run().changed                                # el "eco" de Google es un no-op
    assert core.calendar.events["ev1"].title == "Otro título"
    assert core.calendar.events["ev1"].category == "personal"


def test_fusion_titulo_local_y_hora_de_google_se_conservan_ambos(core, syncer, now):
    """Se edita el título acá y la hora en Google antes de sincronizar: ninguno pisa al otro."""
    r = mk(core, now)
    core.calendar.user_edit("ev1", start=datetime(2026, 10, 16, 9, 0), end=datetime(2026, 10, 16, 10, 0))
    syncer.run()
    core.update_title(r.id, "Título local")
    syncer.run()
    assert core.get(1).title == "Título local"
    assert core.calendar.events["ev1"].title == "Título local"
    assert core.calendar.events["ev1"].start == datetime(2026, 10, 16, 9, 0)


# ============================================================ reparación
def test_reparar_evento_faltante(core, syncer, now):
    r = mk(core, now)
    core.calendar.events.clear()
    core.db.update_capture(r.id, event_id=None, calendar_id=None, cal_sig=None)
    rep = syncer.run()
    assert rep.healed == 1 and len(core.calendar.events) == 1
    assert row(core)["event_id"] in core.calendar.events


def test_sin_categoria_no_se_repara_ni_se_crea_evento(core, syncer, now):
    core.capture_text("comprar pan mañana a las 9", now=now)
    rep = syncer.run()
    assert rep.healed == 0 and core.calendar.events == {}


def test_borradas_no_se_reparan(core, syncer, now):
    r = mk(core, now)
    core.delete(r.id)
    assert syncer.run().healed == 0 and core.calendar.events == {}


# ============================================== avisos de Google silenciados
def test_silencia_avisos_de_google_en_eventos_importados(core, syncer, now):
    eid = core.calendar.user_create("personal", "Dentista", start=datetime(2026, 10, 20, 17, 0))
    assert core.calendar.events[eid].reminders_default is True      # como los crea la UI de Google
    rep = syncer.run()
    assert rep.silenced == 1 and core.calendar.events[eid].reminders_default is False
    assert not syncer.run().changed and syncer.run().silenced == 0


def test_silencia_evento_movido_de_calendario_desde_google(core, syncer, now):
    mk(core, now)
    core.calendar.user_move_calendar("ev1", "ocio")                 # Google resetea los avisos
    assert core.calendar.events["ev1"].reminders_default is True
    assert syncer.run().silenced == 1
    assert core.calendar.events["ev1"].reminders_default is False


def test_no_silencia_si_esta_desactivado(core, syncer, now):
    core.cfg.sync.silence_google_reminders = False
    core.calendar.user_create("personal", "Dentista", start=datetime(2026, 10, 20, 17, 0))
    assert syncer.run().silenced == 0


# ================================================================ concurrencia
def test_concurrencia_bot_y_sync_no_corrompen_nada(core, syncer, now):
    import threading

    errors = []

    def worker(i):
        try:
            r = core.capture_text(f"reunión con el cliente número {i} el viernes a las 10", now=now)
            core.update_title(r.id, f"Reunión {i}")
            core.reschedule(r.id, "el lunes a las 11")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def syncing():
        for _ in range(4):
            try:
                syncer.run()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)] + \
              [threading.Thread(target=syncing) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    rows = core.db.all_captures()
    assert len(rows) == 8 and len({r["seq"] for r in rows}) == 8
    assert len(core.calendar.events) == 8                          # ni duplicados ni huérfanos
    assert not syncer.run().changed


def test_reintenta_una_edicion_local_cuyo_update_a_google_fallo(core, syncer, now):
    """Regresión: event_id puesto + cal_sig viejo => Google no reporta nada y quedaba desfasado para siempre."""
    r = mk(core, now)
    orig = core.calendar.update_event

    def fallar(*a, **k):
        raise ConnectionError("503")

    core.calendar.update_event = fallar
    core.update_title(r.id, "Título nuevo")                      # el update a Google falla
    assert core.calendar.events["ev1"].title == "Reunión con el cliente"
    core.calendar.update_event = orig                            # vuelve la red
    rep = syncer.run()
    assert core.calendar.events["ev1"].title == "Título nuevo" and rep.healed == 1
    assert not syncer.run().changed                              # y queda estable
