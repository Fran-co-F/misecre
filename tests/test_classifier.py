import pytest

from misecre.classifier import Classifier
from misecre.parser import parse_when


@pytest.mark.parametrize(
    "text, category, typ",
    [
        ("reunión con el cliente mañana a las 10", "trabajo", "evento"),
        ("turno con el dentista el viernes a las 15", "personal", "evento"),
        ("ir al cine con amigos el sábado a las 22", "ocio", "evento"),
        ("mandar el informe al jefe", "trabajo", "tarea"),
        ("comprar leche", None, "tarea"),
        ("acordarme de algo", None, "nota"),
        ("entregar el presupuesta mañana", "trabajo", "tarea"),  # error de transcripción
    ],
)
def test_classify(text, category, typ, cfg, now):
    c = Classifier(cfg).classify(text, parse_when(text, now))
    assert c.category == category
    assert c.type == typ


def test_empate_va_a_inbox(cfg, now):
    text = "reunión por el cumpleaños"  # trabajo 1 vs personal 1
    c = Classifier(cfg).classify(text, parse_when(text, now))
    assert c.category is None
