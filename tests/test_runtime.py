import asyncio
from datetime import datetime

import pytest

from misecre import learning
from misecre.core import Core
from misecre.notify import Notifier
from misecre.runtime import Runtime

from .fakes import FakeCalendar


def at(h, m=0, day=12):
    return datetime(2026, 10, day, h, m)


@pytest.fixture
def core(cfg):
    return Core(cfg, calendar=FakeCalendar())


@pytest.fixture
def rt(core):
    n = Notifier(["telegram"])
    sent = []

    async def fake(msg):
        sent.append(msg)

    n.register("telegram", fake)
    r = Runtime(core, n)
    r.sent = sent
    return r


def run(coro):
    return asyncio.run(coro)


def test_tick_envia_aviso_con_botones_y_no_repite(core, rt):
    r = core.capture_text("reunión con el cliente hoy a las 15", now=at(9))
    core.set_category(r.id, "trabajo")
    run(rt.tick(at(14, 50)))
    texts = [m.text for m in rt.sent]
    assert any("en 10 min · #1 Reunión con el cliente" in t for t in texts)
    msg = next(m for m in rt.sent if "en 10 min" in m.text)
    assert [b[1] for b in msg.buttons[0]] == [f"snz:{r.id}", f"rdone:{r.id}"]
    n = len(rt.sent)
    run(rt.tick(at(14, 51)))
    assert len(rt.sent) == n                                   # sin repetidos


def test_tick_manda_resumen_diario_una_sola_vez(core, rt):
    core.capture_text("reunión con el cliente hoy a las 10", now=at(7))
    run(rt.tick(at(8, 0)))
    assert any(m.text.startswith("🌅") for m in rt.sent)
    n = len(rt.sent)
    run(rt.tick(at(8, 1)))
    assert not any(m.text.startswith("🌅") for m in rt.sent[n:])


def test_tick_sincroniza_y_avisa_si_hubo_cambios(core, rt):
    core.calendar.user_create("personal", "Dentista", start=datetime(2026, 10, 20, 17, 0))
    run(rt.tick(at(12, 0)))
    assert any("Importé de Google Calendar: Dentista" in m.text for m in rt.sent)


def test_tick_no_empuja_en_horas_de_silencio(core, rt):
    core.capture_text("reunión con el cliente mañana a las 7 de la mañana", now=at(22))
    run(rt.tick(at(6, 50, day=13)))
    assert not any("⏰" in m.text for m in rt.sent)


# -------------------------------------------------------------- aprendizaje
def test_sugerencias_de_reglas(core):
    for text in ("paddle con Pedro", "reservar paddle para el finde", "jugar paddle con Ana"):
        r = core.capture_text(text, now=at(9))
        core.set_category(r.id, "ocio")
    s = learning.suggest_rules(core)
    assert [(x.category, x.word) for x in s] == [("ocio", "paddle")]
    assert s[0].count == 2          # la 3ª ("jugar…") ya caía en ocio: no fue corrección
    core.add_rule("ocio", "paddle")
    assert learning.suggest_rules(core) == []


def test_sugerencia_rechazada_no_vuelve(core):
    for text in ("paddle con Pedro", "reservar paddle"):
        core.set_category(core.capture_text(text, now=at(9)).id, "ocio")
    learning.reject(core, "ocio", "paddle")
    assert learning.suggest_rules(core) == []


def test_precision(core):
    a = core.capture_text("reunión con el cliente mañana", now=at(9))
    core.set_category(a.id, "trabajo")                       # acierto
    b = core.capture_text("paddle con Pedro", now=at(9))
    core.set_category(b.id, "ocio")                          # corrección
    assert learning.accuracy(core) == (1, 2)


def test_aviso_no_se_marca_si_ningun_canal_lo_entrego(core):
    n = Notifier(["telegram"])

    async def boom(msg):
        raise ConnectionError("sin red")

    n.register("telegram", boom)
    rt = Runtime(core, n)
    r = core.capture_text("reunión con el cliente hoy a las 15", now=at(9))
    core.set_category(r.id, "trabajo")
    run(rt.tick(at(14, 50)))
    assert core.db.sent_has(r.id, "b15") is False              # no entregado => se reintenta
    ok = []

    async def good(msg):
        ok.append(msg)

    n.register("telegram", good)
    run(rt.tick(at(14, 51)))
    assert ok and core.db.sent_has(r.id, "b15") is True


def test_error_de_sync_repetido_se_avisa_una_vez(core, rt):
    class Down(FakeCalendar):
        def list_changes(self, *a, **k):
            raise ConnectionError("sin red")

    core.calendar = Down()
    run(rt.tick(at(12, 0)))
    run(rt.tick(at(12, 10)))
    run(rt.tick(at(12, 20)))
    errs = [m for m in rt.sent if "No pude leer el calendario" in m.text]
    assert len(errs) == 1
