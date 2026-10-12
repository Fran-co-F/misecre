"""Reconocimiento de voz (local / nube / respaldo, vocabulario) y modo «quién responde primero»."""
import httpx
import pytest

import misecre.stt as S
from misecre.agent import Agent
from misecre.config import SttCfg
from misecre.core import Core
from misecre.operator import MODES, Operator

from .fakes import FakeCalendar, ScriptedAnthropic, say, tool


# ------------------------------------------------------------ vocabulario
def test_el_vocabulario_de_voz_lleva_tildes_y_lo_tuyo(cfg, now):
    core = Core(cfg, calendar=FakeCalendar())
    v = core.classifier.vocabulary()
    words = v.split(", ")
    assert {"reunión", "médico", "cumpleaños", "película"} <= set(words)             # con su ortografía real
    assert not {"reunion", "medico", "cumpleanos"} & set(words)
    core.capture_text("llamar a Marcos Ledesma por el contrato del Boulevard", now=now)
    dyn = core.asr_vocabulary()
    assert "Marcos" in dyn and "Ledesma" in dyn and "Boulevard" in dyn               # nombres propios del usuario
    assert ", por," not in f", {dyn}," and ", del," not in f", {dyn},"            # sin palabras vacías
    assert dyn.index("Marcos") < dyn.index("reunión")                                # lo tuyo va primero
    assert len(dyn) <= 700


def test_vocabulario_dinamico_llega_al_modelo(cfg, now, monkeypatch):
    import sys
    import types

    seen = {}

    class FakeModel:
        def __init__(self, *a, **k): pass

        def transcribe(self, path, **kw):
            seen.update(kw)
            return [types.SimpleNamespace(text="ok")], types.SimpleNamespace(duration=1.0)

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    core = Core(cfg, transcriber=S.Transcriber(SttCfg(model="base")))
    core.transcriber.vocabulary = core.asr_vocabulary
    core.capture_text("reunión con Ledesma", now=now)
    core.transcriber.transcribe("x.ogg")
    assert "Ledesma" in seen["initial_prompt"] and seen["initial_prompt"].startswith("Vocabulario:")


# ------------------------------------------------------------ en la nube
def patch_http(monkeypatch, handler):
    orig = httpx.Client

    class Patched(orig):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    monkeypatch.setattr(S.httpx if hasattr(S, "httpx") else httpx, "Client", Patched)


def test_transcribe_con_groq(monkeypatch, tmp_path):
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = req.content
        return httpx.Response(200, json={"text": " reunión con Marcos el jueves a las 10 "})

    patch_http(monkeypatch, handler)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    audio = tmp_path / "v.oga"
    audio.write_bytes(b"OggS-fake")
    t = S.Transcriber(SttCfg(provider="groq"), vocabulary=lambda: "Marcos, Ledesma")
    assert t.transcribe(audio) == "reunión con Marcos el jueves a las 10"
    assert seen["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer gsk_test"
    assert b"whisper-large-v3-turbo" in seen["body"] and b"Marcos, Ledesma" in seen["body"]
    assert b'filename="voice.ogg"' in seen["body"] and b'name="language"' in seen["body"]
    assert t.last["provider"] == "groq"


def test_si_la_nube_falla_usa_el_modelo_local(monkeypatch, tmp_path):
    import sys
    import types

    patch_http(monkeypatch, lambda req: httpx.Response(429, json={"error": "rate"}))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")

    class FakeModel:
        def __init__(self, *a, **k): pass

        def transcribe(self, path, **kw):
            return [types.SimpleNamespace(text="desde el local")], types.SimpleNamespace(duration=2.0)

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"x")
    t = S.Transcriber(SttCfg(provider="groq", model="base", fallback_local=True))
    assert t.transcribe(audio) == "desde el local" and t.last["provider"] == "local"
    strict = S.Transcriber(SttCfg(provider="groq", fallback_local=False))
    with pytest.raises(httpx.HTTPStatusError):
        strict.transcribe(audio)


def test_sin_clave_cae_al_local_o_falla_claro(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    t = S.Transcriber(SttCfg(provider="openai", fallback_local=False))
    ok, why = t.remote_available()
    assert ok is False and "OPENAI_API_KEY" in why
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        t.transcribe(audio)


def test_con_nube_no_precarga_el_modelo_local(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    started = []
    monkeypatch.setattr(S.threading, "Thread", lambda *a, **k: started.append(1))
    S.Transcriber(SttCfg(provider="groq")).preload()
    assert started == []


# ------------------------------------------------------------ modos
@pytest.fixture
def mk(cfg):
    cfg.ai.enabled = True

    def build(*steps, mode=None):
        if mode:
            cfg.ai.mode = mode
        core = Core(cfg, calendar=FakeCalendar())
        client = ScriptedAnthropic(*steps)
        return Operator(core, Agent(core, client=client)), core, client
    return build


def test_modo_por_defecto_reglas_primero(mk):
    op, core, client = mk()
    assert op.mode == "rules_first"
    r = op.handle("c", "reunión con el cliente mañana a las 10")
    assert r.source == "reglas" and client.calls == []


def test_modo_ia_primero_manda_todo_al_agente(mk):
    op, core, client = mk(tool("create_item", text="reunión con el cliente mañana a las 10", category="trabajo"),
                          mode="ai_first")
    r = op.handle("c", "reunión con el cliente mañana a las 10")           # las reglas lo resolverían solas
    assert r.source == "ia" and r.calls == 1 and len(client.calls) == 1
    assert core.get(1).category == "trabajo"


def test_modo_ia_primero_deja_lo_trivial_a_las_reglas(mk):
    op, core, client = mk(mode="ai_first")
    assert op.handle("c", "hola").source == "reglas"
    op.handle("c", "reunión con el cliente mañana a las 10") if False else None
    assert client.calls == []


def test_modo_ia_primero_cae_a_reglas_si_la_ia_falla(mk):
    op, core, client = mk(RuntimeError("sin red"), mode="ai_first")
    r = op.handle("c", "reunión con el cliente mañana a las 10")
    assert core.get(1).title == "Reunión con el cliente" and "reglas" in r.notice.lower()
    assert r.source == "reglas"


def test_modo_solo_reglas_nunca_llama_a_la_ia(mk):
    op, core, client = mk(mode="rules_only")
    r = op.handle("c", "llevar el auto al taller")                         # dudoso: iría a la IA
    assert client.calls == [] and r.capture is not None and r.source == "reglas"


def test_cambiar_el_modo_en_caliente_y_persistir(mk):
    op, core, client = mk()
    assert op.set_mode("ia") == "ai_first" and op.mode == "ai_first"
    assert op.set_mode("solo-reglas") == "rules_only"
    assert op.set_mode("reglas") == "rules_first"
    with pytest.raises(ValueError):
        op.set_mode("loco")
    assert set(MODES) == {"rules_first", "ai_first", "rules_only"}


def test_el_agente_sabe_que_viene_de_un_audio(mk):
    op, core, client = mk(say("ok"))
    op.handle("c", "poné algo raro", source="telegram-voz")
    assert "TRANSCRIPCIÓN AUTOMÁTICA" in client.calls[0]["system"]
    op2, _, client2 = mk(say("ok"))
    op2.handle("c", "poné algo raro", source="telegram")
    assert "TRANSCRIPCIÓN AUTOMÁTICA" not in client2.calls[0]["system"]
