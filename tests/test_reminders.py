from datetime import datetime, timedelta

import pytest

from misecre import digest
from misecre.core import Core
from misecre.reminders import Reminders, describe, in_quiet

from .fakes import FakeCalendar


@pytest.fixture
def core(cfg):
    return Core(cfg, calendar=FakeCalendar())


@pytest.fixture
def rem(core):
    return Reminders(core)


def at(h, m=0, day=12):
    return datetime(2026, 10, day, h, m)


def mk(core, text, now=None):
    r = core.capture_text(text, now=now or at(9))
    return core.set_category(r.id, r.category) if r.category else r


# ----------------------------------------------------------- horas de silencio
@pytest.mark.parametrize("h, expected", [(23, True), (0, True), (6, True), (7, False), (12, False), (22, False)])
def test_silencio_cruza_medianoche(h, expected):
    assert in_quiet(at(h, 30), "23:00-07:00") is expected


def test_silencio_apagado():
    assert in_quiet(at(3), "off") is False and in_quiet(at(3), "") is False


# ------------------------------------------------------------------- avisos
def test_aviso_trabajo_15_min_antes_una_sola_vez(core, rem):
    mk(core, "reunión con el cliente hoy a las 15")                       # trabajo: before [15]
    assert rem.collect(at(14, 30)) == []                                  # todavía no
    due = rem.collect(at(14, 46))
    assert len(due) == 1 and due[0].kind == "before" and due[0].minutes_left == 14
    rem.mark(due[0], at(14, 46))
    assert rem.collect(at(14, 50)) == []                                  # dedupe
    assert rem.collect(at(15, 1)) == []                                   # no es "perdido": ya se avisó


def test_personal_avisa_60_y_10(core, rem):
    mk(core, "turno con el dentista hoy a las 18")
    d1 = rem.collect(at(17, 5))
    assert [d.key for d in d1] == ["b60"]
    rem.mark(d1[0], at(17, 5))
    d2 = rem.collect(at(17, 52))
    assert [d.key for d in d2] == ["b10"]


def test_ocio_no_empuja_solo_resumen(core, rem):
    mk(core, "cena con amigos hoy a las 21")
    assert rem.collect(at(20, 55)) == []


def test_todo_el_dia_avisa_a_la_hora_configurada(core, rem):
    mk(core, "entregar el informe al jefe hoy")
    assert rem.collect(at(8, 59)) == []
    d = rem.collect(at(9, 0))
    assert [x.kind for x in d] == ["allday"]
    rem.mark(d[0], at(9, 0))
    assert rem.collect(at(12)) == []


def test_mover_el_evento_reprograma_los_avisos(core, rem):
    r = mk(core, "reunión con el cliente hoy a las 15")
    d = rem.collect(at(14, 50))
    rem.mark(d[0], at(14, 50))
    core.reschedule(r.id, "hoy a las 17")
    assert rem.collect(at(15, 5)) == []
    assert [x.key for x in rem.collect(at(16, 50))] == ["b15"]            # vuelve a estar pendiente


def test_evento_hecho_o_borrado_no_avisa(core, rem):
    r = mk(core, "reunión con el cliente hoy a las 15")
    core.complete(r.id)
    assert rem.collect(at(14, 50)) == []


def test_horas_de_silencio_y_catch_up(core, rem):
    mk(core, "reunión con el cliente mañana a las 7 de la mañana", now=at(22))   # mañana 07:00
    assert rem.collect(at(6, 50, day=13)) == []                           # 06:50: silencio hasta las 07
    assert [d.kind for d in rem.collect(at(7, 0, day=13))] == ["missed"]  # ya empezó: se reporta como perdido


def test_pausa(core, rem):
    mk(core, "reunión con el cliente hoy a las 15")
    until = rem.pause(120, now=at(14, 30))                                # hasta las 16:30
    assert until == at(16, 30)
    assert rem.collect(at(14, 50)) == []
    rem.resume()
    assert len(rem.collect(at(14, 50))) == 1


def test_pc_apagada_se_avisa_tarde_o_como_perdido(core, rem):
    mk(core, "reunión con el cliente hoy a las 15")
    late = rem.collect(at(14, 58))                                        # prendió la PC 2 min antes
    assert late and late[0].minutes_left == 2
    rem.mark(late[0], at(14, 58))
    mk(core, "reunión con el cliente hoy a las 11", now=at(9))
    missed = rem.collect(at(11, 40))                                      # prendió después de empezar
    assert [d.kind for d in missed] == ["missed"]
    rem.mark(missed[0], at(11, 40))
    assert rem.collect(at(11, 45)) == []


def test_perdidos_viejos_se_descartan(core, rem):
    mk(core, "reunión con el cliente hoy a las 8")
    assert rem.collect(at(21, 0)) == []                                   # pasaron > 12 h


def test_posponer(core, rem):
    r = mk(core, "reunión con el cliente hoy a las 15")
    d = rem.collect(at(14, 50))
    rem.mark(d[0], at(14, 50))
    fire = rem.snooze(r.id, 5, now=at(14, 51))                            # 14:56
    assert fire == at(14, 56)
    assert rem.collect(at(14, 55)) == []
    due = rem.collect(at(14, 57))
    assert [x.kind for x in due] == ["snooze"]
    rem.mark(due[0], at(14, 57))
    assert rem.collect(at(14, 58)) == []


def test_texto_de_avisos(core, rem):
    mk(core, "reunión con el cliente hoy a las 15")
    d = rem.collect(at(14, 50))[0]
    assert describe(d) == "⏰ en 10 min · #1 Reunión con el cliente (15:00)"


# -------------------------------------------------------------- resúmenes
def test_digest_diario_contenido(core):
    mk(core, "reunión con el cliente hoy a las 10", now=at(7))
    mk(core, "entregar el informe al jefe mañana", now=at(7))
    core.capture_text("comprar pan", now=at(7))
    txt = digest.build_daily(core, at(8))
    assert "Lunes 12/10" in txt and "10:00 💼 #1 Reunión con el cliente" in txt
    assert "Mañana (1)" in txt and "Sin categoría: 1" in txt and "sin fecha (1)" in txt


def test_digest_marca_eventos_sin_calendario(core):
    core.capture_text("comprar pan hoy a las 9", now=at(7))               # sin categoría => sin evento
    assert "·sin calendario" in digest.build_daily(core, at(8))


def test_digests_programados_y_sin_repetir(core):
    assert digest.due_digests(core, at(7, 59)) == []
    assert digest.due_digests(core, at(8, 0)) == ["daily"]
    digest.mark_digest(core, "daily", at(8, 0))
    assert digest.due_digests(core, at(9, 0)) == []
    assert digest.due_digests(core, at(19, 0)) == []                       # >10 h después: ya no
    assert digest.due_digests(core, at(20, 0, day=18)) == ["weekly"]       # domingo 18/10
    assert digest.due_digests(core, at(8, 0, day=1)) == ["monthly", "daily"] or \
        set(digest.due_digests(core, at(8, 0, day=1))) == {"daily", "monthly"}


def test_digest_diario_catch_up_si_prende_tarde(core):
    assert digest.due_digests(core, at(13, 0)) == ["daily"]


def test_digest_semanal_y_mensual(core):
    mk(core, "reunión con el cliente el viernes a las 10", now=at(7))
    assert "Viernes 16/10" in digest.build_weekly(core, at(20, day=11))
    mk(core, "cena con amigos el 25 de octubre a las 21", now=at(7))
    m = digest.build_monthly(core, at(8))
    assert "Octubre 2026" in m and "2 cosa(s)" in m


def test_politica_de_una_categoria_que_no_esta_en_la_config(cfg, now):
    """Una categoría agregada en rules.yaml sin sección en notifications.categories respeta /avisos."""
    cfg.rules.categories["estudio"] = ["parcial"]
    core = Core(cfg, calendar=FakeCalendar())
    rem = Reminders(core)
    assert rem.policy_for("estudio").before == [60, 10]           # sin ajuste: cae a «personal»
    rem.set_policy("estudio", [30], digest_only=False)
    assert rem.policy_for("estudio").before == [30]               # con ajuste: se respeta
    assert rem.policy_for("personal").before == [60, 10]          # y no contamina a las demás
