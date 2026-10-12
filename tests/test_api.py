import pytest
from fastapi.testclient import TestClient

from misecre.api import build_api
from misecre.core import Core


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.delenv("MISECRE_API_TOKEN", raising=False)
    return TestClient(build_api(Core(cfg)), base_url="http://127.0.0.1:8787")


def test_flujo_api(client):
    r = client.post("/capture/text", json={"text": "turno con el dentista mañana a las 16"})
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == "personal" and body["type"] == "evento"

    r = client.post(f"/capture/{body['id']}/category", json={"category": "ocio"})
    assert r.json()["category"] == "ocio"

    assert client.post(f"/capture/{body['id']}/category", json={"category": "xx"}).status_code == 422
    assert client.delete(f"/capture/{body['id']}").status_code == 200
    assert client.delete("/capture/nope").status_code == 404


def test_token_requerido_si_esta_configurado(cfg, monkeypatch):
    monkeypatch.setenv("MISECRE_API_TOKEN", "s3cret")
    c = TestClient(build_api(Core(cfg)), base_url="http://127.0.0.1:8787")
    assert c.post("/capture/text", json={"text": "hola"}).status_code == 401
    ok = c.post("/capture/text", json={"text": "hola"}, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


# --------------------------------------------------------------- CRUD / v1
from misecre.core import Core as _Core  # noqa: E402
from .fakes import FakeCalendar  # noqa: E402


@pytest.fixture
def full(cfg, monkeypatch):
    monkeypatch.delenv("MISECRE_API_TOKEN", raising=False)
    core = _Core(cfg, calendar=FakeCalendar())
    return TestClient(build_api(core), base_url="http://127.0.0.1:8787"), core


def test_patch_y_consultas(full):
    c, core = full
    r = c.post("/capture/text", json={"text": "reunión con el cliente el jueves a las 10"}).json()
    assert r["seq"] == 1 and r["in_calendar"]

    r = c.patch("/capture/1", json={"title": "Reunión con Marcos", "when": "el viernes", "duration_min": 90}).json()
    assert r["title"] == "Reunión con Marcos"
    assert r["start"] == "2026-10-16T10:00:00".replace("2026", r["start"][:4]) or r["start"].endswith("T10:00:00")
    assert r["end"].endswith("T11:30:00")

    assert c.patch("/capture/1", json={"status": "hecha"}).json()["status"] == "hecha"
    assert c.patch("/capture/1", json={"status": "otra"}).status_code == 422
    assert c.patch("/capture/1", json={"when": "cuando puedas"}).status_code == 422
    assert [x["seq"] for x in c.get("/search", params={"q": "marcos"}).json()] == [1]
    assert c.get("/capture/1").json()["id"] == r["id"]
    assert c.get("/capture/99").status_code == 404


def test_borrar_restaurar_deshacer(full):
    c, core = full
    c.post("/capture/text", json={"text": "turno con el dentista mañana a las 16"})
    assert c.delete("/capture/1").status_code == 200
    assert [x["seq"] for x in c.get("/trash").json()] == [1]
    assert c.post("/capture/1/restore").json()["status"] == "pendiente"
    c.delete("/capture/1")
    u = c.post("/undo").json()
    assert u["undone"] == "borrar" and u["capture"]["status"] == "pendiente"
    rest = [c.post("/undo").json()["undone"] for _ in range(3)]
    assert rest == ["restaurar", "borrar", "crear"]                                     # historia completa
    assert c.post("/undo").status_code == 422                                           # sin más historia


def test_status_digest_y_home(full):
    c, core = full
    c.post("/capture/text", json={"text": "comprar pan"})
    s = c.get("/status").json()
    assert s["activas"] == 1 and s["sin_categoria"] == 1 and s["calendario_activo"] is True
    assert "text" in c.get("/digest/daily").json()
    assert c.get("/digest/otro").status_code == 404
    html = c.get("/").text
    assert "<title>misecre</title>" in html and "__NEEDS_TOKEN__" not in html


def test_agent_chat_api_con_ia_como_respaldo(cfg, monkeypatch):
    from misecre.agent import Agent

    from .fakes import ScriptedAnthropic, tool

    monkeypatch.delenv("MISECRE_API_TOKEN", raising=False)
    cfg.ai.enabled = True
    core = _Core(cfg, calendar=FakeCalendar())
    client = ScriptedAnthropic(tool("create_item", text="comprar pan", category="personal"))
    c = TestClient(build_api(core, agent=Agent(core, client=client)), base_url="http://127.0.0.1:8787")
    r = c.post("/agent/chat", json={"text": "anotá comprar pan"}).json()
    assert r["source"] == "ia" and r["mutations"] == 2 and r["calls"] == 1 and r["refs"] == [1]
    assert r["reply"].startswith("✓ Anoté #1 «Comprar pan»")
    st = c.get("/status").json()
    assert st["agent"] is True and st["routing_today"] == {"reglas": 0, "ia": 1}
    assert c.delete("/agent/chat").json()["forgotten"] == 2


def test_chat_api_resuelve_con_reglas_sin_ia(full):
    c, core = full
    r = c.post("/agent/chat", json={"text": "reunión con el cliente mañana a las 10"}).json()
    assert r["source"] == "reglas" and r["calls"] == 0 and r["mutations"] == 1
    r = c.post("/agent/chat", json={"text": "qué tengo mañana"}).json()
    assert r["source"] == "reglas" and "Reunión con el cliente" in r["reply"]
    assert c.post("/agent/chat", json={"text": "   "}).status_code == 422
