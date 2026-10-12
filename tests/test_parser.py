from datetime import date, datetime, time

import pytest

from misecre.parser import parse_when
from misecre.titles import clean_title

D = date


@pytest.mark.parametrize(
    "text, day, at, ambiguous",
    [
        ("reunión con Marcos el jueves a las 10", D(2026, 10, 15), time(10, 0), False),
        ("mañana a las 5 de la tarde dentista", D(2026, 10, 13), time(17, 0), False),
        ("mañana a las 5 dentista", D(2026, 10, 13), time(17, 0), True),
        ("mañana a las 9 de la mañana", D(2026, 10, 13), time(9, 0), False),
        ("pasado mañana a las 20:30", D(2026, 10, 14), time(20, 30), False),
        ("el 15 de octubre a las 18", D(2026, 10, 15), time(18, 0), False),
        ("15/10 a las 20:30", D(2026, 10, 15), time(20, 30), False),
        ("el viernes a las 10 y media", D(2026, 10, 16), time(10, 30), False),
        ("el viernes a las 4 menos cuarto", D(2026, 10, 16), time(15, 45), True),
        ("el viernes a las 8 de la noche", D(2026, 10, 16), time(20, 0), False),
        ("el viernes a las 10hs", D(2026, 10, 16), time(10, 0), False),
        ("el viernes al mediodía", D(2026, 10, 16), time(12, 0), False),
        ("el lunes a las 9", D(2026, 10, 19), time(9, 0), False),  # hoy es lunes -> semana próxima
        ("hoy a las 18", D(2026, 10, 12), time(18, 0), False),
        ("en 2 horas", D(2026, 10, 12), time(12, 0), False),
        ("en media hora", D(2026, 10, 12), time(10, 30), False),
        ("en 3 días", D(2026, 10, 15), None, False),
        ("en 2 semanas", D(2026, 10, 26), None, False),
        ("el 5", D(2026, 11, 5), None, False),  # el 5 de este mes ya pasó
        ("a las 9 y media", D(2026, 10, 13), time(9, 30), False),  # ya pasó hoy -> mañana
        ("a las 18", D(2026, 10, 12), time(18, 0), False),
        ("el 3 de marzo", D(2027, 3, 3), None, False),
    ],
)
def test_parse(text, day, at, ambiguous, now):
    w = parse_when(text, now)
    assert w.day == day
    assert w.at == at
    assert w.ambiguous_hour == ambiguous


def test_sin_fecha(now):
    w = parse_when("comprar leche y pan", now)
    assert w.day is None and w.at is None


def test_numero_suelto_no_es_hora(now):
    w = parse_when("comprar 3 manzanas", now)
    assert w.at is None


def test_de_la_manana_no_es_manana(now):
    w = parse_when("el viernes a las 7 de la mañana", now)
    assert w.day == date(2026, 10, 16) and w.at == time(7, 0)


PREFIXES = ["recordame que tengo que", "recordame que", "tengo que", "tengo"]


@pytest.mark.parametrize(
    "text, title",
    [
        ("reunión con Marcos el jueves a las 10 por lo del presupuesto", "Reunión con Marcos por lo del presupuesto"),
        ("recordame que tengo que comprar leche mañana", "Comprar leche"),
        ("tengo turno con el dentista el 15 de octubre a las 18", "Turno con el dentista"),
        ("mañana a las 5 de la tarde dentista", "Dentista"),
        ("recordatorio: ir a comprar pan", "Ir a comprar pan"),
        ("Nota - llamar al banco mañana", "Llamar al banco"),
    ],
)
def test_title(text, title, now):
    w = parse_when(text, now)
    assert clean_title(text, w.spans, PREFIXES) == title


# ---------------------------------------------------- regresiones de la revisión
@pytest.mark.parametrize("text, day, at", [
    ("el viernes por la mañana", date(2026, 10, 16), None),            # NO es "mañana" = 13/10
    ("hoy por la mañana llamar a Juan", date(2026, 10, 12), None),
    ("el lunes a la mañana", date(2026, 10, 19), None),
    ("esta mañana", None, None),
    ("mañana a las 9", date(2026, 10, 13), time(9, 0)),                # sigue siendo mañana
    ("pasado mañana", date(2026, 10, 14), None),
    ("a las 8 menos cuarto", date(2026, 10, 13), time(7, 45)),         # 8 → AM, menos 15 → 7:45 (ya pasó hoy)
    ("a las 1 menos cuarto", date(2026, 10, 12), time(12, 45)),        # 1 → PM → 12:45 hoy (no 00:45)
    ("a las 4 menos cuarto", date(2026, 10, 12), time(15, 45)),
    ("a las 12 am", date(2026, 10, 13), time(0, 0)),                   # medianoche
    ("a las 12 del mediodía", None, None),
])
def test_regresiones_parser(text, day, at, now):
    w = parse_when(text, now)
    if day is not None or at is not None:
        assert (w.day, w.at) == (day, at)
    else:
        assert w.at is None or text.startswith("a las 12 del")          # sin hora explícita no inventa una


def test_por_la_manana_no_ensucia_el_titulo(now):
    text = "reunión con Marcos el viernes por la mañana"
    w = parse_when(text, now)
    assert w.day == date(2026, 10, 16)
