import sys
import types

from misecre.config import SttCfg
from misecre.stt import Transcriber


def fake_module(monkeypatch, cached: bool):
    calls = []

    class FakeModel:
        def __init__(self, size, **kw):
            calls.append({"size": size, **kw})
            if kw.get("local_files_only") and not cached:
                raise OSError("no está en el caché")

        def transcribe(self, path, **kw):
            seg = types.SimpleNamespace(text=" hola mundo ")
            info = types.SimpleNamespace(duration=3.2)
            calls.append({"transcribe": kw})
            return [seg], info

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return calls


def test_usa_el_cache_local_sin_ir_a_la_red(monkeypatch):
    calls = fake_module(monkeypatch, cached=True)
    t = Transcriber(SttCfg(model="medium", beam_size=1), vocabulary="reunión, cliente")
    assert t.transcribe("x.ogg") == "hola mundo"
    assert [c for c in calls if "size" in c] == [
        {"size": "medium", "local_files_only": True, "device": "auto", "compute_type": "int8"}]
    tr = [c for c in calls if "transcribe" in c][0]["transcribe"]
    assert tr["beam_size"] == 1 and tr["initial_prompt"] == "Vocabulario: reunión, cliente." and tr["language"] == "es"
    assert tr["condition_on_previous_text"] is False and tr["vad_parameters"]["min_silence_duration_ms"] == 500
    assert t.last["audio_s"] == 3.2 and "took_s" in t.last


def test_si_no_esta_en_cache_descarga(monkeypatch):
    calls = fake_module(monkeypatch, cached=False)
    Transcriber(SttCfg(model="small")).transcribe("x.ogg")
    loads = [c for c in calls if "size" in c]
    assert loads[0].get("local_files_only") is True and "local_files_only" not in loads[1]


def test_precarga_no_bloquea_y_carga_una_vez(monkeypatch):
    calls = fake_module(monkeypatch, cached=True)
    t = Transcriber(SttCfg(model="small"))
    t.preload()
    for th in __import__("threading").enumerate():
        if th.name == "stt-preload":
            th.join(5)
    t.transcribe("x.ogg")
    assert len([c for c in calls if "size" in c]) == 1
