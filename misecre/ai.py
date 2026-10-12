"""Fallback con IA (Claude Haiku). Opcional, apagado por defecto.

Solo se consulta cuando las reglas no logran decidir la categoría. Recibe el texto ya
transcripto y devuelve categoría, tipo y un título corto. Las fechas las sigue resolviendo
el parser determinista. Cualquier falla (sin clave, sin red, cuota) devuelve None: la captura
nunca depende de este módulo.

Costo orientativo (claude-haiku-5-5: US$0,10 por millón de tokens de entrada y US$0,50 de
salida): ~350 tokens de entrada + ~60 de salida por consulta = ~US$0,00007.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date

from .config import AiCfg
from .db import DB

log = logging.getLogger("misecre.ai")

SYSTEM = """Sos el clasificador de un asistente personal que recibe notas dictadas por voz en español rioplatense.
Dada una nota, devolvé:
- category: una de las categorías disponibles, o "ninguna" si de verdad no se puede saber.
- type: "evento" (algo en una fecha/hora), "tarea" (algo para hacer) o "nota" (información suelta).
- title: título breve y claro (máx. 8 palabras), sin fechas ni horas ni muletillas ("recordame", "tengo que").
- confidence: de 0 a 1, qué tan seguro estás de la categoría.
Reglas: no inventes datos; si hay duda entre dos categorías, bajá la confianza; respondé solo el JSON."""


@dataclass
class AiResult:
    category: str | None
    type: str
    title: str
    confidence: float


class HaikuClassifier:
    def __init__(self, cfg: AiCfg, db: DB, categories: dict[str, list[str]], client=None, today=None):
        self.cfg = cfg
        self._today = today or date.today
        self.db = db
        self.categories = categories
        self._client = client

    @property
    def available(self) -> bool:
        return self.cfg.enabled and (self._client is not None or bool(os.environ.get("ANTHROPIC_API_KEY")))

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(timeout=self.cfg.timeout_s, max_retries=1)
        return self._client

    # ------------------------------------------------------------ cuota
    def _quota_key(self) -> str:
        return f"ai_calls:{self._today().isoformat()}"

    def calls_today(self) -> int:
        return int(self.db.kv_get(self._quota_key(), "0") or 0)

    def _schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": [*self.categories, "ninguna"]},
                "type": {"type": "string", "enum": ["evento", "tarea", "nota"]},
                "title": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["category", "type", "title", "confidence"],
            "additionalProperties": False,
        }

    def _prompt(self, text: str) -> str:
        cats = "\n".join(f"- {c}: {', '.join(kws[:12])}" for c, kws in self.categories.items())
        return f"Categorías disponibles (con palabras de ejemplo):\n{cats}\n\nNota: {text}"

    def classify(self, text: str) -> AiResult | None:
        if not self.available:
            return None
        if self.calls_today() >= self.cfg.max_calls_per_day:
            log.warning("IA: se alcanzó el tope diario (%s)", self.cfg.max_calls_per_day)
            return None
        try:
            client = self._get_client()
            resp = client.messages.create(
                model=self.cfg.model,
                max_tokens=1500,  # margen por si el modelo razona antes de responder
                system=SYSTEM,
                messages=[{"role": "user", "content": self._prompt(text)}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": self._schema()},
                },
            )
            self.db.kv_set(self._quota_key(), str(self.calls_today() + 1))
            raw = next((b.text for b in resp.content if b.type == "text"), "")
            data = json.loads(raw)
            cat = data.get("category")
            return AiResult(
                category=cat if cat in self.categories else None,
                type=data.get("type", "nota"),
                title=(data.get("title") or "").strip()[:80],
                confidence=float(data.get("confidence", 0)),
            )
        except Exception:  # noqa: BLE001  (la IA es opcional: nunca rompe la captura)
            log.exception("IA: falló la consulta")
            return None
