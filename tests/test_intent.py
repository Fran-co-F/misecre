import pytest

from misecre.core import Core
from misecre.intent import Router

from .fakes import FakeCalendar


@pytest.fixture
def core(cfg):
    return Core(cfg, calendar=FakeCalendar())


@pytest.fixture
def router(core):
    return Router(core)


def add(core, text, now):
    return core.capture_text(text, now=now)


# ------------------------------------------------------- completar
@pytest.mark.parametrize("phrase", [
    "ya fui a comprar pan", "ya compré el pan", "listo lo del pan", "compré pan",
    "ya está, terminé de comprar el pan", "hice lo de comprar pan",
])
def test_completa_el_pendiente_que_coincide(core, router, now, phrase):
    r = add(core, "ir a comprar pan", now)
    route = router.route(phrase)
    assert route.kind == "complete" and route.item.seq == r.seq


def test_varios_candidatos_pregunta_cual(core, router, now):
    add(core, "comprar pan integral", now)
    add(core, "comprar pan lactal", now)
    route = router.route("ya compré el pan")
    assert route.kind == "choose" and {c.title for c in route.candidates} == {
        "Comprar pan integral", "Comprar pan lactal"}


def test_el_mas_especifico_gana(core, router, now):
    add(core, "comprar pan", now)
    add(core, "comprar leche", now)
    route = router.route("ya compré la leche")
    assert route.kind == "complete" and route.item.title == "Comprar leche"


def test_ya_hecho_sin_pendiente_que_coincida_no_inventa(core, router, now):
    add(core, "llamar al banco", now)
    assert router.route("ya fui al gimnasio").kind == "none"


def test_ignora_lo_ya_hecho_y_las_notas(core, router, now):
    r = add(core, "ir a comprar pan", now)
    core.complete(r.id)
    assert router.route("ya fui a comprar pan").kind == "none"


def test_cafe_no_es_un_ya_lo_hice(core, router, now):
    add(core, "ir a comprar pan", now)
    assert router.route("tomar un café con Pedro mañana a las 10").kind == "create"
    assert router.route("café con Pedro").kind != "complete"


def test_con_fecha_futura_es_un_pedido_nuevo(core, router, now):
    add(core, "comprar pan", now)
    assert router.route("comprar pan mañana a las 9").kind == "create"
    assert router.route("ya fui a comprar pan el viernes").kind == "create"


# ------------------------------------------------------- consultas
@pytest.mark.parametrize("phrase, scope", [
    ("qué tengo hoy", "today"), ("que tengo para hoy?", "today"), ("qué tengo mañana", "tomorrow"),
    ("qué tengo esta semana", "week"), ("qué me falta", "tasks"), ("mis tareas", "tasks"),
    ("qué tengo pendiente", "tasks"), ("tareas atrasadas", "overdue"),
])
def test_consultas(router, phrase, scope):
    r = router.route(phrase)
    assert r.kind == "query" and r.scope == scope


# ------------------------------------------------------- deshacer / recategorizar
def test_deshacer(router):
    assert router.route("deshacelo").kind == "undo"
    assert router.route("me equivoqué, deshacé eso").kind == "undo"
    assert router.route("deshacé lo del jueves a las 10").kind != "undo"     # tiene fecha: otra cosa


def test_recategorizar_solo_con_contexto(router):
    assert router.route("es de ocio").kind != "recategorize"
    r = router.route("es de ocio", last_refs=[3])
    assert r.kind == "recategorize" and r.category == "ocio"
    assert router.route("ponelo en trabajo", last_refs=[3]).category == "trabajo"
    assert router.route("reunión de trabajo el jueves a las 10", last_refs=[3]).kind == "create"


# ------------------------------------------------------- crear
def test_crear_con_categoria_confiable(router):
    r = router.route("reunión con el cliente el jueves a las 10")
    assert (r.kind, r.category, r.confident) == ("create", "trabajo", True)


def test_crear_dudoso_no_es_confiable(router):
    r = router.route("llevar el auto al taller")
    assert r.kind == "create" and r.confident is False and r.category is None


def test_saludos_y_modificaciones_y_preguntas(router):
    assert router.route("hola").kind == "greeting"
    assert router.route("gracias").kind == "greeting"
    assert router.route("mové lo del dentista al viernes").kind == "modify"
    assert router.route("borrá lo del pan").kind == "modify"
    assert router.route("¿cómo hago para cambiar la hora?").kind == "none"
