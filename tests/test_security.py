import asyncio
import re
import shutil
import subprocess
import tempfile

import pytest
from fastapi.testclient import TestClient

from misecre.api import build_api
from misecre.auth import AuthLimiter, TokenStore, hash_token, ip_allowed, parse_networks
from misecre.core import Core
from misecre.listener import AuthError, listen, parse_sse_lines
from misecre.notify import Message, Notifier
from misecre.runtime import Runtime
from misecre.web import PAGE

from .fakes import FakeCalendar


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("MISECRE_API_TOKEN", raising=False)


@pytest.fixture
def core(cfg):
    return Core(cfg, calendar=FakeCalendar())


# ----------------------------------------------------------------- tokens
def test_token_se_guarda_hasheado_y_se_verifica(core):
    ts = TokenStore(core.db)
    raw = ts.create("celu")
    assert raw.startswith("msc_") and len(raw) > 40
    row = core.db.token_by_hash(hash_token(raw))
    assert row["name"] == "celu" and raw not in str(dict(row))        # el token en claro no se guarda
    assert ts.verify(raw) == "celu"
    assert ts.verify(raw + "x") is None and ts.verify("") is None


def test_revocar_afecta_solo_a_ese_dispositivo(core):
    ts = TokenStore(core.db)
    a, b = ts.create("celu"), ts.create("notebook")
    assert ts.revoke("celu") is True and ts.revoke("celu") is False
    assert ts.verify(a) is None and ts.verify(b) == "notebook"
    assert [t["name"] for t in ts.list() if t["revoked"]] == ["celu"]


def test_nombre_duplicado_o_invalido(core):
    ts = TokenStore(core.db)
    ts.create("celu")
    with pytest.raises(ValueError):
        ts.create("celu")
    with pytest.raises(ValueError):
        ts.create("  ")


def test_token_maestro_por_entorno(core, monkeypatch):
    monkeypatch.setenv("MISECRE_API_TOKEN", "maestro-123")
    assert TokenStore(core.db).verify("maestro-123") == "env"


# --------------------------------------------------------------- limitador
def test_bloqueo_tras_intentos_fallidos_y_desbloqueo():
    t = [0.0]
    lim = AuthLimiter(max_fail=3, window=60, block=120, clock=lambda: t[0])
    for _ in range(2):
        lim.fail("1.2.3.4")
    assert not lim.blocked("1.2.3.4")
    lim.fail("1.2.3.4")
    assert lim.blocked("1.2.3.4") and not lim.blocked("9.9.9.9")
    t[0] = 121
    assert not lim.blocked("1.2.3.4")


def test_lista_blanca_de_ips():
    nets = parse_networks(["10.0.0.0/8", "203.0.113.7"])
    assert ip_allowed("10.1.2.3", nets) and ip_allowed("203.0.113.7", nets)
    assert not ip_allowed("8.8.8.8", nets) and not ip_allowed("basura", nets)
    assert ip_allowed("8.8.8.8", [])                                   # sin lista: todas
    with pytest.raises(ValueError):
        parse_networks(["no-es-una-ip"])


# ---------------------------------------------------------- API protegida
def protected(core, monkeypatch, **api_cfg):
    for k, v in api_cfg.items():
        setattr(core.cfg.api, k, v)
    token = TokenStore(core.db).create("test")
    return TestClient(build_api(core), base_url="http://127.0.0.1:8787"), token


def test_api_exige_token_si_hay_tokens(core, monkeypatch):
    c, token = protected(core, monkeypatch)
    assert c.get("/agenda").status_code == 401
    assert c.get("/agenda", headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = c.get("/agenda", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    assert c.get("/health").json() == {"ok": True}                      # público y sin datos
    assert c.get("/status", headers={"Authorization": f"Bearer {token}"}).json()["device"] == "test"


def test_fuerza_bruta_termina_en_429(core, monkeypatch):
    c, token = protected(core, monkeypatch)
    codes = [c.get("/agenda", headers={"Authorization": "Bearer mal"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]
    # bloqueada la IP, ni el token correcto pasa mientras dure el bloqueo
    assert c.get("/agenda", headers={"Authorization": f"Bearer {token}"}).status_code == 429


def test_lista_blanca_en_la_api(core, monkeypatch):
    c, token = protected(core, monkeypatch, allowed_ips=["10.0.0.0/8"], trust_proxy=True)
    h = {"Authorization": f"Bearer {token}"}
    assert c.get("/agenda", headers={**h, "X-Forwarded-For": "8.8.8.8"}).status_code == 403
    assert c.get("/", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 403    # ni la página
    assert c.get("/agenda", headers={**h, "X-Forwarded-For": "10.1.1.1"}).status_code == 200


def test_x_forwarded_for_se_ignora_sin_trust_proxy(core, monkeypatch):
    c, token = protected(core, monkeypatch, allowed_ips=["10.0.0.0/8"], trust_proxy=False)
    r = c.get("/agenda", headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": "10.1.1.1"})
    assert r.status_code == 403            # no se puede falsificar la IP con un header


def test_sin_tokens_y_fuera_de_loopback_se_rechaza(core):
    core.cfg.api.host = "0.0.0.0"
    c = TestClient(build_api(core), base_url="http://127.0.0.1:8787")
    r = c.get("/agenda")
    assert r.status_code == 500 and "token" in r.json()["detail"].lower()


def test_cabeceras_de_seguridad_y_csp_con_nonce(core, monkeypatch):
    c, _ = protected(core, monkeypatch)
    r = c.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
    assert f'nonce="{nonce}"' in r.text and "__NONCE__" not in r.text
    assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert "const NEEDS_TOKEN = true;" in r.text
    assert c.get("/openapi.json").status_code == 404 and c.get("/docs").status_code == 404


# ------------------------------------------------------ endpoints de control
@pytest.fixture
def ctl(core):
    n = Notifier([])
    rt = Runtime(core, n)
    return TestClient(build_api(core, rt), base_url="http://127.0.0.1:8787"), core, rt


def test_ajustes_avisos_silencio_pausa_reglas(ctl):
    c, core, rt = ctl
    s = c.get("/settings").json()
    assert s["policies"]["trabajo"] == {"before": [15], "digest_only": False}
    s = c.post("/settings/policy", json={"category": "ocio", "before": [30, 5], "digest_only": False}).json()
    assert s["policies"]["ocio"] == {"before": [30, 5], "digest_only": False}
    assert c.post("/settings/policy", json={"category": "xx", "before": []}).status_code == 422
    assert c.post("/settings/quiet", json={"spec": "22:00-08:00"}).json()["quiet"] == "22:00-08:00"
    assert c.post("/settings/quiet", json={"spec": "99:99-x"}).status_code == 422
    assert c.post("/settings/pause", json={"minutes": 60}).json()["paused_until"] is not None
    assert c.post("/settings/pause", json={"minutes": None}).json()["paused_until"] is None
    s = c.post("/rules", json={"category": "ocio", "word": "paddle"}).json()
    assert "paddle" in s["rules"]["ocio"]
    s = c.post("/rules", json={"category": "ocio", "word": "paddle", "action": "remove"}).json()
    assert "paddle" not in s["rules"]["ocio"]


def test_resolver_ambiguedades_por_api(ctl):
    c, core, rt = ctl
    r = c.post("/capture/text", json={"text": "cena con amigos el sábado a las 5"}).json()
    assert r["pending"] == ["category", "hour"]
    r = c.post(f"/capture/{r['id']}/hour", json={"part": "am"}).json()
    assert r["start"].endswith("T05:00:00") and "hour" not in r["pending"]
    t = c.post("/capture/text", json={"text": "reunión con el cliente mañana"}).json()
    assert "time" in t["pending"]
    t = c.post(f"/capture/{t['id']}/time", json={"text": "18:30"}).json()
    assert t["start"].endswith("T18:30:00")
    u = c.post("/capture/text", json={"text": "reunión con el cliente pasado mañana"}).json()
    assert "time" not in c.post(f"/capture/{u['id']}/all-day").json()["pending"]
    assert "fire_at" in c.post(f"/capture/{u['id']}/snooze", json={}).json()


def test_sugerencias_por_api(ctl):
    c, core, rt = ctl
    for t in ("tenis con Pedro", "reservar tenis"):
        core.set_category(core.capture_text(t).id, "ocio")
    s = c.get("/settings").json()["suggestions"]
    assert s and s[0]["word"] == "tenis"
    c.post("/suggestions", json={"category": "ocio", "word": "tenis", "action": "accept"})
    assert c.get("/settings").json()["suggestions"] == []
    assert c.post("/suggestions", json={"category": "ocio", "word": "x", "action": "?"}).status_code == 422


# ------------------------------------------------------------ SSE / listener
def test_notifier_entrega_a_suscriptores_y_cuenta_como_entrega():
    n = Notifier([])
    q = n.subscribe()
    assert asyncio.run(n.send(Message("hola", title="T", kind="reminder"))) is True
    assert q.get_nowait() == {"title": "T", "text": "hola", "kind": "reminder"}
    n.unsubscribe(q)
    assert n.subscribers == []


def test_parse_sse():
    lines = [": conectado", "", 'data: {"title": "A", "text": "uno", "kind": "info"}', "",
             ": keep-alive", "", "data: no-es-json", "",
             'data: {"title": "B", "text": "dos"}', ""]
    evs = parse_sse_lines(lines)
    assert [e["title"] for e in evs] == ["A", "B"]


def test_listener_rechaza_token_invalido(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(401, json={"detail": "token inválido"})

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    class Patched(orig):
        def __init__(self, *a, **k):
            super().__init__(*a, transport=transport, **k)

    monkeypatch.setattr("misecre.listener.httpx.AsyncClient", Patched)
    with pytest.raises(AuthError):
        asyncio.run(listen("http://x", "mal", once=True))


def test_listener_entrega_eventos(monkeypatch):
    import httpx

    body = ': conectado\n\ndata: {"title": "Aviso", "text": "en 10 min", "kind": "reminder"}\n\n'

    def handler(request):
        assert request.headers["authorization"] == "Bearer msc_ok"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    orig = httpx.AsyncClient

    class Patched(orig):
        def __init__(self, *a, **k):
            super().__init__(*a, transport=httpx.MockTransport(handler), **k)

    monkeypatch.setattr("misecre.listener.httpx.AsyncClient", Patched)
    got = []

    async def on(msg):
        got.append(msg)

    asyncio.run(listen("http://x", "msc_ok", on, once=True))
    assert [(m.title, m.text, m.kind) for m in got] == [("Aviso", "en 10 min", "reminder")]


def test_unidad_systemd_del_listener():
    from misecre.service import unit_text

    t = unit_text("/x/config.yaml", listener=True)
    assert " -m misecre listen" in t and "-c" not in t.split("ExecStart=")[1].split("\n")[0]


# ------------------------------------------------------------------- web
def test_la_web_no_usa_innerhtml_ni_eval():
    assert "innerHTML" not in PAGE and "eval(" not in PAGE and "document.write" not in PAGE


@pytest.mark.skipif(shutil.which("node") is None, reason="node no disponible")
def test_el_javascript_de_la_web_es_valido():
    script = re.search(r'<script nonce="__NONCE__">(.*)</script>', PAGE, re.S).group(1)
    script = script.replace("__NEEDS_TOKEN__", "true")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script)
    r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ------------------------------------- publicar detrás de un proxy (nginx / NPM)
def test_detras_de_proxy_sin_tokens_no_responde(core):
    """Escucha en loopback pero llega por nginx: sin token NO debe quedar abierta a internet."""
    c = TestClient(build_api(core), base_url="http://127.0.0.1:8787")
    r = c.get("/agenda", headers={"X-Forwarded-For": "8.8.8.8"})
    assert r.status_code == 500 and "misecre token create" in r.json()["detail"]
    r = c.get("/agenda", headers={"X-Real-IP": "8.8.8.8"})
    assert r.status_code == 500
    page = c.get("/", headers={"X-Forwarded-For": "8.8.8.8"})
    assert page.status_code == 503 and "token create" in page.text
    assert c.get("/agenda").status_code == 200                    # local directo: sigue andando


def test_con_trust_proxy_sin_tokens_tampoco(core):
    core.cfg.api.trust_proxy = True
    c = TestClient(build_api(core), base_url="http://127.0.0.1:8787")
    assert c.get("/agenda").status_code == 500


def test_ip_real_es_la_que_agrego_el_proxy_no_la_del_cliente(core, monkeypatch):
    c, token = protected(core, monkeypatch, allowed_ips=["10.0.0.0/8"], trust_proxy=True)
    h = {"Authorization": f"Bearer {token}"}
    # nginx con $proxy_add_x_forwarded_for: "<lo que mandó el cliente>, <IP real que vio nginx>"
    assert c.get("/agenda", headers={**h, "X-Forwarded-For": "10.1.1.1, 8.8.8.8"}).status_code == 403
    assert c.get("/agenda", headers={**h, "X-Forwarded-For": "8.8.8.8, 10.1.1.1"}).status_code == 200
    # con $remote_addr: un solo valor
    assert c.get("/agenda", headers={**h, "X-Forwarded-For": "10.2.2.2"}).status_code == 200


def test_el_limite_de_intentos_no_se_evade_falsificando_el_header(core, monkeypatch):
    c, token = protected(core, monkeypatch, trust_proxy=True)
    codes = [c.get("/agenda", headers={"Authorization": "Bearer mal",
                                        "X-Forwarded-For": f"1.2.3.{i}, 9.9.9.9"}).status_code
             for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]   # misma IP real (9.9.9.9) => se bloquea


def test_varios_proxies_confiables(core, monkeypatch):
    c, token = protected(core, monkeypatch, allowed_ips=["10.0.0.0/8"], trust_proxy=True, trusted_proxies=2)
    h = {"Authorization": f"Bearer {token}"}
    # cliente -> CDN -> nginx: "<cliente>, <IP que vio el CDN>"; con 2 proxies se toma el 2º desde la derecha
    assert c.get("/agenda", headers={**h, "X-Forwarded-For": "10.9.9.9, 10.1.1.1"}).status_code == 200


# --------------------------------------------- notificaciones de escritorio / doctor
def test_canal_de_escritorio_no_entrega_si_no_hay_notify_send(monkeypatch):
    import misecre.notify as N

    monkeypatch.setattr(N.shutil, "which", lambda name: None)
    ok, why = N.desktop_available()
    assert ok is False and "notify-send" in why
    n = N.Notifier(["desktop"])
    assert "desktop" not in n.channels                         # no se registra: no cuenta como entregado
    import pytest as _p
    with _p.raises(RuntimeError):
        asyncio.run(N.desktop_channel(N.Message("x")))


def test_sin_sesion_grafica_tampoco(monkeypatch):
    import misecre.notify as N

    monkeypatch.setattr(N.shutil, "which", lambda name: "/usr/bin/notify-send")
    for var in ("DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(var, raising=False)
    ok, why = N.desktop_available()
    assert ok is False and "sesión gráfica" in why


def test_si_telegram_falla_y_el_escritorio_no_existe_el_aviso_se_reintenta(core, monkeypatch):
    import misecre.notify as N

    monkeypatch.setattr(N.shutil, "which", lambda name: None)
    n = Notifier(["telegram", "desktop"])

    async def boom(msg):
        raise ConnectionError("sin red")

    n.register("telegram", boom)
    assert asyncio.run(n.send(Message("hola"))) is False        # antes devolvía True por el canal fantasma


def test_no_precarga_whisper_si_no_hay_ram(monkeypatch, caplog):
    import misecre.stt as S
    from misecre.config import SttCfg

    monkeypatch.setattr(S, "mem_available_gb", lambda: 1.0)
    started = []
    monkeypatch.setattr(S.threading, "Thread", lambda *a, **k: started.append(1))
    S.Transcriber(SttCfg(model="medium")).preload()
    assert started == [] and "NO se precarga" in caplog.text


def test_estimacion_de_ram_de_modelos():
    from misecre.stt import model_ram_gb

    assert model_ram_gb("tiny") < model_ram_gb("small") < model_ram_gb("medium") < model_ram_gb("large-v3")
    assert model_ram_gb("modelo-raro") == 1.5


def test_doctor_detecta_calendario_apagado_con_token(core, capsys):
    from misecre.doctor import run_doctor

    (core.cfg.data_dir / "token.json").write_text("{}")
    core.cfg.calendar.enabled = False
    assert run_doctor(core) == 1
    out = capsys.readouterr().out
    assert "✗ Google Calendar" in out and "NO se crean en Google" in out


def test_doctor_no_alarma_por_errores_viejos_ni_por_red_privada(core, capsys):
    from datetime import timedelta

    from misecre.doctor import run_doctor

    core.cfg.api.host = "172.18.0.1"
    log = core.cfg.log_dir
    log.mkdir(parents=True, exist_ok=True)
    old = (core.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    (log / "misecre.log").write_text(f"{old},000 ERROR asyncio: algo viejo\n", encoding="utf-8")
    run_doctor(core)
    out = capsys.readouterr().out
    assert "✓ log" in out and "sin errores en las últimas 24 h (1 anteriores)" in out
    assert "✓ api.host" in out and "red privada" in out


def test_doctor_marca_errores_recientes(core, capsys):
    from misecre.doctor import run_doctor

    log = core.cfg.log_dir
    log.mkdir(parents=True, exist_ok=True)
    now = core.now().strftime("%Y-%m-%d %H:%M:%S")
    (log / "misecre.log").write_text(f"{now},000 ERROR root: algo reciente\n", encoding="utf-8")
    run_doctor(core)
    assert "⚠ log" in capsys.readouterr().out


def test_doctor_api_publica_si_advierte(core, capsys):
    from misecre.doctor import run_doctor

    core.cfg.api.host = "0.0.0.0"
    run_doctor(core)
    assert "⚠ api.host" in capsys.readouterr().out


# ------------------------------------------------------------ endurecimiento
def test_limites_de_tamano_en_la_api(core):
    c = TestClient(build_api(core), base_url="http://127.0.0.1:8787")
    assert c.post("/capture/text", json={"text": "x" * 4001}).status_code == 422
    assert c.post("/agent/chat", json={"text": "x" * 4001}).status_code == 422
    big = b"0" * (25 * 1024 * 1024 + 10)
    r = c.post("/capture/audio", files={"file": ("a.ogg", big, "audio/ogg")})
    assert r.status_code == 413


def test_el_limitador_descarta_ips_viejas():
    t = [0.0]
    lim = AuthLimiter(max_fail=10, window=60, block=60, clock=lambda: t[0])
    for i in range(3000):
        lim.fail(f"10.0.{i // 256}.{i % 256}")
    t[0] = 1000
    lim.fail("1.1.1.1")                                           # dispara la poda
    assert len(lim._fails) < 100


@pytest.mark.skipif(__import__("os").name != "posix", reason="permisos POSIX")
def test_la_carpeta_de_datos_es_privada(cfg):
    import stat

    assert stat.S_IMODE(cfg.data_dir.stat().st_mode) == 0o700


def test_se_puede_recrear_un_token_revocado(core):
    ts = TokenStore(core.db)
    old = ts.create("celu")
    assert ts.revoke("celu")
    new = ts.create("celu")                                     # rotar sin cambiar el nombre
    assert new != old and ts.verify(old) is None and ts.verify(new) == "celu"
    with pytest.raises(ValueError, match="token activo"):
        ts.create("celu")


# -------------------------------- CSRF / DNS rebinding en modo local sin tokens
def test_local_sin_tokens_rechaza_host_ajeno(core):
    c = TestClient(build_api(core), base_url="http://127.0.0.1:8787")
    assert c.get("/agenda").status_code == 200
    # DNS rebinding: el navegador envía el Host del dominio del atacante
    r = c.get("/agenda", headers={"Host": "evil.example.com:8787"})
    assert r.status_code == 403 and "DNS rebinding" in r.json()["detail"]
    assert c.get("/agenda", headers={"Host": "localhost:8787"}).status_code == 200


def test_local_sin_tokens_rechaza_post_de_otra_pagina(core):
    c = TestClient(build_api(core), base_url="http://127.0.0.1:8787")
    r = c.post("/undo", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403 and "Origen no permitido" in r.json()["detail"]
    ok = c.post("/capture/text", json={"text": "comprar pan"}, headers={"Origin": "http://127.0.0.1:8787"})
    assert ok.status_code == 200                                      # la propia web sí
    assert c.post("/capture/text", json={"text": "comprar leche"}).status_code == 200   # sin Origin (curl)


def test_con_tokens_no_se_aplica_la_regla_de_host(core, monkeypatch):
    """Detrás de nginx el Host es tu dominio: ahí la defensa son los tokens."""
    c, token = protected(core, monkeypatch)
    r = c.get("/agenda", headers={"Authorization": f"Bearer {token}", "Host": "misecre.midominio.com"})
    assert r.status_code == 200


def test_detras_de_proxy_sin_token_sigue_diciendo_que_falta_el_token(core):
    c = TestClient(build_api(core), base_url="http://127.0.0.1:8787")
    r = c.get("/agenda", headers={"Host": "misecre.midominio.com", "X-Forwarded-For": "8.8.8.8"})
    assert r.status_code == 500 and "token create" in r.json()["detail"]      # mensaje útil, no un 403 mudo


def test_el_html_de_la_web_no_reintenta_con_token_invalido():
    from misecre.web import PAGE

    assert "r.status === 401" in PAGE and "streaming = false" in PAGE
    assert "toISOString().slice(0,10)" not in PAGE                    # la fecha de «hoy» es local, no UTC
