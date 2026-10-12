"""El operador: reglas primero, IA solo de respaldo, y la IA enseña a las reglas."""
import pytest

from misecre import learning
from misecre.agent import Agent
from misecre.core import Core
from misecre.operator import HELP_MODIFY, HELP_NONE, Operator

from .fakes import FakeCalendar, ScriptedAnthropic, say, tool


@pytest.fixture
def mk(cfg):
    cfg.ai.enabled = True

    def build(*steps, ai=True):
        core = Core(cfg, calendar=FakeCalendar())
        client = ScriptedAnthropic(*steps)
        agent = Agent(core, client=client if ai else None)
        if not ai:
            cfg.ai.enabled = False
        return Operator(core, agent), core, client
    return build


# ------------------------------------------------------- 0 llamadas
def test_crear_con_reglas_no_llama_a_la_ia(mk):
    op, core, client = mk()
    r = op.handle("c", "reunión con el cliente el jueves a las 10")
    assert r.source == "reglas" and r.calls == 0 and client.calls == []
    assert core.get(1).category == "trabajo" and core.get(1).event_id
    assert r.text.startswith("✓ Anoté #1 «Reunión con el cliente»") and r.capture is None


def test_ya_fui_a_comprar_pan_sin_ia(mk):
    op, core, client = mk()
    core.capture_text("ir a comprar pan")
    r = op.handle("c", "ya fui a comprar pan")
    assert r.source == "reglas" and client.calls == []
    assert r.text == "✅ Marqué #1 «Ir a comprar pan» como hecho" and core.get(1).status == "hecha"


def test_consultas_sin_ia(mk):
    op, core, client = mk()
    core.capture_text("reunión con el cliente mañana a las 10")
    assert "Reunión con el cliente" in op.handle("c", "qué tengo mañana").text
    assert "Tareas pendientes: nada." == op.handle("c", "qué me falta").text
    assert client.calls == []


def test_deshacer_y_recategorizar_sin_ia(mk):
    op, core, client = mk()
    op.handle("c", "reunión con el cliente mañana a las 10")
    r = op.handle("c", "es de ocio")                                    # corrección HUMANA sobre lo último
    assert r.text == "✓ #1 «Reunión con el cliente» → ocio" and core.get(1).category == "ocio"
    assert [(f["auto_value"], f["chosen_value"]) for f in core.db.feedback("category")] == [("trabajo", "ocio")]
    assert op.handle("c", "deshacelo").text.startswith("↩ Deshice")
    assert op.handle("c", "deshacelo").text.startswith("↩ Deshice")     # la creación
    assert op.handle("c", "deshacelo").text == "No hay nada para deshacer."
    assert client.calls == []


def test_saludo(mk):
    op, _, client = mk()
    assert op.handle("c", "hola").text.startswith("¡Hola!") and client.calls == []


# ------------------------------------------------------- IA de respaldo
def test_categoria_dudosa_usa_ia_con_una_sola_llamada(mk):
    op, core, client = mk(tool("create_item", text="llevar el auto al taller", category="personal"))
    r = op.handle("c", "llevar el auto al taller")
    assert r.source == "ia" and r.calls == 1 and len(client.calls) == 1
    assert core.get(1).category == "personal" and r.text.startswith("✓ Anoté #1")


def test_la_ia_recibe_el_contexto_de_lo_ultimo_que_se_toco(mk):
    op, core, client = mk(tool("update_item", ref=1, when="el viernes"))
    op.handle("c", "reunión con el cliente el jueves a las 10")            # reglas (#1)
    op.handle("c", "pasala para el viernes")                               # "modificar" => IA
    assert "#1 «Reunión con el cliente» (pendiente" in client.calls[0]["system"]
    assert core.get(1).start.weekday() == 4


def test_ia_caida_en_una_creacion_cae_a_reglas_con_aviso(mk):
    op, core, client = mk(RuntimeError("sin red"))
    r = op.handle("c", "llevar el auto al taller")
    assert r.notice.startswith("⚠️") and "Llevar el auto al taller" in r.text
    assert core.get(1).title == "Llevar el auto al taller"                 # no se pierde


def test_sin_ia_lo_dudoso_muestra_botones(mk):
    op, core, _ = mk(ai=False)
    r = op.handle("c", "llevar el auto al taller")
    assert r.source == "reglas" and r.capture is not None and r.capture.category is None


def test_sin_ia_las_modificaciones_y_lo_incomprensible_orientan(mk):
    op, core, _ = mk(ai=False)
    assert op.handle("c", "mové lo del dentista al viernes").text == HELP_MODIFY
    assert op.handle("c", "¿cómo hago para esto?").text == HELP_NONE


def test_varios_candidatos(mk):
    op, core, _ = mk()
    core.capture_text("comprar pan integral")
    core.capture_text("comprar pan lactal")
    r = op.handle("c", "ya compré el pan")
    assert len(r.candidates) == 2 and r.mutations == 0


# ------------------------------------------------------- aprendizaje
def test_la_ia_ensena_a_las_reglas_y_deja_de_usarse(mk):
    op, core, client = mk(
        tool("create_item", text="paddle con Pedro", category="ocio"),
        tool("create_item", text="paddle con Ana", category="ocio"),
    )
    r1 = op.handle("c", "paddle con Pedro")
    assert r1.source == "ia" and r1.learned == []                          # 1 decisión: todavía no
    r2 = op.handle("c", "paddle con Ana")
    assert r2.source == "ia" and len(r2.learned) == 1
    assert "«paddle» → ocio" in r2.learned[0] and "/regla quitar ocio paddle" in r2.learned[0]
    assert "paddle" in core.rules_view()["ocio"]

    r3 = op.handle("c", "paddle con Lucía mañana a las 19")                # ahora lo resuelven las reglas
    assert r3.source == "reglas" and r3.calls == 0 and len(client.calls) == 2
    assert core.get(3).category == "ocio"
    assert op.stats_today() == {"reglas": 1, "ia": 2}


def test_aprendizaje_es_reversible(mk):
    op, core, client = mk(tool("create_item", text="paddle uno", category="ocio"),
                          tool("create_item", text="paddle dos", category="ocio"))
    op.handle("c", "paddle uno")
    op.handle("c", "paddle dos")
    assert "paddle" in core.rules_view()["ocio"]
    core.remove_rule("ocio", "paddle")
    assert "paddle" not in core.rules_view()["ocio"]


def test_no_aprende_si_hay_evidencia_en_conflicto(mk):
    op, core, client = mk(tool("create_item", text="pádel con Pedro", category="ocio"),
                          tool("create_item", text="pádel con Ana", category="personal"))
    op.handle("c", "pádel con Pedro")
    r = op.handle("c", "pádel con Ana")
    assert r.learned == [] and "pádel" not in core.rules_view()["ocio"]


def test_correccion_humana_posterior_frena_el_aprendizaje(mk):
    op, core, client = mk(tool("create_item", text="gimnasio funcional", category="ocio"),
                          tool("create_item", text="gimnasio nocturno", category="ocio"))
    op.handle("c", "gimnasio funcional")
    core.set_category(1, "personal", by_user=True)                         # vos la corregís
    r = op.handle("c", "gimnasio nocturno")
    assert r.learned == []                                                 # hay evidencia de 2 categorías


def test_auto_apply_apagado_solo_sugiere(mk):
    op, core, client = mk(tool("create_item", text="boxeo con Juan", category="ocio"),
                          tool("create_item", text="boxeo con Ana", category="ocio"))
    core.cfg.learning.auto_apply = False
    op.handle("c", "boxeo con Juan")
    r = op.handle("c", "boxeo con Ana")
    assert r.learned == [] and "boxeo" not in core.rules_view()["ocio"]
    assert [(s.word, s.category) for s in learning.suggest_rules(core)] == [("boxeo", "ocio")]   # igual se sugiere
