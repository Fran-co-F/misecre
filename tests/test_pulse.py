"""Resúmenes periódicos ("cómo está todo") a horas configurables."""
import asyncio
from datetime import datetime

import pytest

from misecre import digest
from misecre.core import Core
from misecre.notify import Notifier
from misecre.runtime import Runtime

from .fakes import FakeCalendar


def at(h, m=0, day=12):
    return datetime(2026, 10, day, h, m)


@pytest.fixture
def core(cfg):
    cfg.digests.periodic = ["08:00", "10:00", "14:00", "18:00"]
    return Core(cfg, calendar=FakeCalendar())


def test_contenido_del_pulso(core):
    core.capture_text("reunión con el cliente hoy a las 15", now=at(9))
    core.capture_text("comprar pan", now=at(9))
    core.capture_text("algo sin categoría hoy a las 20", now=at(9))
    txt = digest.build_pulse(core, at(14))
    assert txt.startswith("🕑 14:00 · así está todo")
    assert "▶ Próximo: 15:00 💼 #1 Reunión con el cliente (en 1 h)" in txt
    assert "Falta hoy (2)" in txt and "Tareas pendientes (1)" in txt and "#2 Comprar pan" in txt
    assert "Sin categoría: 2" in txt                              # "comprar pan" y "algo sin categoría"


def test_pulso_vacio(core):
    assert "Todo despejado" in digest.build_pulse(core, at(14))
    assert digest.build_pulse(core, at(14), empty_ok=False) is None


def test_se_dispara_a_cada_hora_una_sola_vez(core):
    assert digest.due_digests(core, at(9, 59)) == ["daily"] or "pulse:10:00" not in digest.due_digests(core, at(9, 59))
    due = digest.due_digests(core, at(10, 0))
    assert "pulse:10:00" in due
    digest.mark_digest(core, "pulse:10:00", at(10, 0))
    assert not any(k.startswith("pulse") for k in digest.due_digests(core, at(10, 30)))
    assert "pulse:14:00" in digest.due_digests(core, at(14, 5))


def test_a_la_hora_del_resumen_diario_no_se_duplica(core):
    due = digest.due_digests(core, at(8, 0))
    assert "daily" in due and "pulse:08:00" not in due


def test_pc_que_prende_tarde_manda_solo_el_ultimo_pulso_vigente(core):
    # prendió a las 14:30: el de las 10:00 ya no sirve; solo el de las 14:00 (dentro de 60 min)
    due = [k for k in digest.due_digests(core, at(14, 30)) if k.startswith("pulse")]
    assert due == ["pulse:14:00"]
    digest.mark_digest(core, "pulse:14:00", at(14, 30))
    assert not any(k.startswith("pulse") for k in digest.due_digests(core, at(14, 40)))
    # fuera de ventana: no se manda nada atrasado
    assert not any(k.startswith("pulse") for k in digest.due_digests(core, at(16, 0)))


def test_el_pulso_atrasado_se_marca_para_no_reaparecer(core):
    digest.mark_digest(core, "pulse:14:00", at(14, 0))
    assert core.db.kv_get("digest:pulse:10:00") == "2026-10-12"          # las anteriores quedan marcadas


def test_el_runtime_manda_el_pulso(core):
    notifier = Notifier([])
    sent = []

    async def fake(m):
        sent.append(m)

    notifier.register("telegram", fake)
    notifier.enabled.add("telegram")
    rt = Runtime(core, notifier)
    core.capture_text("reunión con el cliente hoy a las 15", now=at(9))
    asyncio.run(rt.tick(at(14, 0)))
    pulse = [m for m in sent if m.title.startswith("Resumen de las")]
    assert len(pulse) == 1 and "Próximo" in pulse[0].text
    asyncio.run(rt.tick(at(14, 1)))
    assert len([m for m in sent if m.title.startswith("Resumen de las")]) == 1


def test_si_no_hay_nada_no_molesta(core):
    notifier = Notifier([])
    sent = []

    async def fake(m):
        sent.append(m)

    notifier.register("telegram", fake)
    notifier.enabled.add("telegram")
    rt = Runtime(core, notifier)
    asyncio.run(rt.tick(at(14, 0)))
    assert [m for m in sent if m.title.startswith("Resumen de las")] == []
    assert core.db.kv_get("digest:pulse:14:00") == "2026-10-12"          # igual se marca: no reaparece
