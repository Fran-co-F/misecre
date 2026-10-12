"""Voz -> texto con faster-whisper (local, gratis).

Dos cuidados para que no se sienta lento ni frágil:
  * Si el modelo ya está en el caché local NO se consulta el HF Hub (esa consulta de red, sin timeout,
    podía demorar el primer audio decenas de segundos cuando la conexión flaqueaba).
  * Se precarga al arrancar (`stt.preload`), así el primer audio no paga la carga.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

from .config import SttCfg

log = logging.getLogger("misecre.stt")


# Proveedores en la nube con la API de transcripción compatible con OpenAI
REMOTE = {
    "groq": ("https://api.groq.com/openai/v1/audio/transcriptions", "whisper-large-v3-turbo", "GROQ_API_KEY"),
    "openai": ("https://api.openai.com/v1/audio/transcriptions", "whisper-1", "OPENAI_API_KEY"),
}
_MIME = {".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".webm": "audio/webm", ".wav": "audio/wav",
         ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".flac": "audio/flac"}

# RAM aproximada (GB) que ocupa cada modelo con compute_type int8
MODEL_RAM_GB = {"tiny": 0.3, "base": 0.5, "small": 1.2, "medium": 2.8, "large": 5.0, "large-v2": 5.0,
                "large-v3": 5.0, "turbo": 2.0}


def mem_available_gb() -> float | None:
    """RAM disponible (Linux). None si no se puede saber."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return None


def model_ram_gb(name: str) -> float:
    return MODEL_RAM_GB.get(name.split(".")[-1].replace("distil-", ""), MODEL_RAM_GB.get(name, 1.5))


class Transcriber:
    def __init__(self, cfg: SttCfg, vocabulary: str | Callable[[], str] = ""):
        self.cfg = cfg
        self.vocabulary = vocabulary               # texto fijo o función (vocabulario dinámico del usuario)
        self._model = None
        self._lock = threading.Lock()
        self.last: dict = {}                       # tiempos de la última transcripción (para /estado)

    def _vocab(self) -> str:
        v = self.vocabulary() if callable(self.vocabulary) else self.vocabulary
        return (v or "")[:800]

    def remote_available(self) -> tuple[bool, str]:
        if self.cfg.provider not in REMOTE:
            return False, "provider local"
        env = REMOTE[self.cfg.provider][2]
        return (True, "") if os.environ.get(env) else (False, f"falta {env} en .env")

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            t = time.time()
            kw = dict(device=self.cfg.device, compute_type=self.cfg.compute_type)
            try:
                self._model = WhisperModel(self.cfg.model, local_files_only=True, **kw)
                origin = "caché local"
            except Exception:  # noqa: BLE001  (no está descargado todavía)
                log.info("modelo %s no está en el caché: descargando…", self.cfg.model)
                self._model = WhisperModel(self.cfg.model, **kw)
                origin = "descargado"
            log.info("modelo de voz %s cargado en %.1fs (%s)", self.cfg.model, time.time() - t, origin)
        return self._model

    def preload(self) -> None:
        """Carga el modelo en segundo plano (no bloquea el arranque). Si no hay RAM suficiente NO lo precarga:
        un OOM al arrancar mata todo el servicio, mientras que sin precarga solo falla el audio."""
        if self.cfg.provider in REMOTE and self.remote_available()[0]:
            return                                  # se transcribe en la nube: no hace falta cargar nada
        free, need = mem_available_gb(), model_ram_gb(self.cfg.model)
        if free is not None and free < need * 1.3:
            log.warning("RAM disponible %.1f GB < %.1f GB que necesita «%s»: NO se precarga. Con stt.model más "
                        "chico (base/small) evitás que el sistema mate el proceso.", free, need * 1.3, self.cfg.model)
            return

        def run():
            try:
                with self._lock:
                    self._load()
            except Exception:  # noqa: BLE001
                log.exception("no pude precargar el modelo de voz")
        threading.Thread(target=run, name="stt-preload", daemon=True).start()

    def _transcribe_remote(self, path: Path) -> str:
        import httpx

        url, default_model, env = REMOTE[self.cfg.provider]
        key = os.environ.get(env)
        if not key:
            raise RuntimeError(f"falta {env} en .env")
        suffix = path.suffix.lower()
        name = "voice.ogg" if suffix in (".oga", ".opus", ".ogg") else f"audio{suffix or '.webm'}"
        with httpx.Client(timeout=self.cfg.api_timeout_s) as client:
            r = client.post(
                url, headers={"Authorization": f"Bearer {key}"},
                files={"file": (name, path.read_bytes(), _MIME.get(suffix, "application/octet-stream"))},
                data={"model": self.cfg.api_model or default_model, "language": self.cfg.language,
                      "prompt": self._vocab(), "response_format": "json", "temperature": "0"})
        r.raise_for_status()
        return (r.json().get("text") or "").strip()

    def _transcribe_local(self, path: Path) -> tuple[str, float]:
        with self._lock:
            model = self._load()
            vocab = self._vocab()
            kwargs = dict(
                language=self.cfg.language,
                initial_prompt=(f"Vocabulario: {vocab}." if vocab else None),
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                beam_size=self.cfg.beam_size,
                condition_on_previous_text=False,     # evita arrastrar errores y repeticiones entre frases
            )
            segments, info = model.transcribe(str(path), **kwargs)
            text = " ".join(s.text.strip() for s in segments).strip()
            return text, float(getattr(info, "duration", 0.0))

    def transcribe(self, audio_path: str | Path) -> str:
        path = Path(audio_path)
        t0 = time.time()
        used = "local"
        text = ""
        if self.cfg.provider in REMOTE:
            try:
                text = self._transcribe_remote(path)
                used = self.cfg.provider
            except Exception as e:  # noqa: BLE001
                log.warning("transcripción en %s falló (%s)%s", self.cfg.provider, type(e).__name__,
                            ": uso el modelo local" if self.cfg.fallback_local else "")
                if not self.cfg.fallback_local:
                    raise
        dur = 0.0
        if used == "local":
            text, dur = self._transcribe_local(path)
        took = time.time() - t0
        self.last = {"audio_s": round(dur, 1), "took_s": round(took, 1),
                     "model": self.cfg.api_model or REMOTE[used][1] if used in REMOTE else self.cfg.model,
                     "provider": used}
        log.info("voz [%s]: %.1fs de proceso", used, took)
        return text
