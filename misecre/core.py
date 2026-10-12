"""Núcleo: capturas (texto/audio) y su ciclo de vida completo.

Modelo mental: la fila de SQLite es la verdad *dentro del proceso*. Toda operación
  1) toma una foto del estado previo (para /deshacer),
  2) muta la fila,
  3) llama a `_publish`, que deja el evento de Google Calendar coincidiendo con la fila.
La sincronización con Google Calendar (cambios hechos del otro lado) vive en sync.py
y usa estas mismas piezas.

Pendientes de una captura (ambigüedades que resuelve la persona):
  category  la categoría no está confirmada (o no se pudo decidir)
  hour      la hora 1-7 se asumió PM y no se sabe si era AM
  time      tiene fecha pero no hora (¿todo el día o a una hora?)
Cada decisión se registra en `feedback` (auto vs elegido) para aprender.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .ai import HaikuClassifier
from .classifier import Classifier, load_user_rules, save_user_rules
from .config import Config, load_config
from .db import DB, SNAPSHOT_FIELDS
from .parser import fold, parse_when
from .titles import clean_title

log = logging.getLogger("misecre")

WEEKDAYS_ES = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
TYPES = ("evento", "tarea", "nota")


def locked(fn):
    """Serializa las operaciones que mutan: bot, sincronización y API comparten el mismo Core."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return fn(self, *args, **kwargs)
    return wrapper


def pending_of(row) -> list[str]:
    p = []
    if not row["cat_confirmed"]:
        p.append("category")
    if row["hour_pending"]:
        p.append("hour")
    if row["time_pending"]:
        p.append("time")
    return p


def sig_of(title: str | None, start, end, day) -> str:
    """Huella de lo que se ve en el calendario (título + cuándo). Detecta cambios sin ruido."""
    def norm(v):
        if v is None:
            return ""
        if isinstance(v, datetime):
            return v.replace(second=0, microsecond=0).isoformat()
        return str(v)[:16]
    raw = "|".join([(title or "").strip(), norm(start), norm(end), norm(day)])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


@dataclass
class Result:
    id: str
    category: str | None
    type: str
    title: str
    start: datetime | None
    end: datetime | None
    day: date | None
    transcript: str
    seq: int = 0
    source: str = ""
    event_id: str | None = None
    ambiguous_hour: bool = False
    needs_category: bool = False
    pending: list[str] = field(default_factory=list)
    status: str = "pendiente"
    warnings: list[str] = field(default_factory=list)
    auto_source: str | None = None
    calendar_off: bool = False          # su categoría no se publica en Google (solo avisos)

    def when_label(self) -> str:
        if self.start:
            s = f"{WEEKDAYS_ES[self.start.weekday()]} {self.start:%d/%m %H:%M}"
            return s + (f"–{self.end:%H:%M}" if self.end else "")
        if self.day:
            return f"{WEEKDAYS_ES[self.day.weekday()]} {self.day:%d/%m} (todo el día)"
        return ""

    def hour_options(self) -> tuple[time, time] | None:
        """(am, pm) para el botón de desambiguación."""
        if not self.start:
            return None
        pm = self.start if self.start.hour >= 12 else self.start + timedelta(hours=12)
        return (pm - timedelta(hours=12)).time(), pm.time()


class Core:
    def __init__(self, cfg: Config, *, calendar=None, transcriber=None, db: DB | None = None,
                 ai_client=None):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.db = db or DB(cfg.db_path)
        self.classifier = Classifier(cfg)
        self.calendar = calendar
        self.transcriber = transcriber
        self.ai = HaikuClassifier(cfg.ai, self.db, self._keywords(), client=ai_client, today=self.today)

    @classmethod
    def from_config(cls, path: str | Path | None = None) -> "Core":
        from .stt import Transcriber

        cfg = load_config(path)
        db = DB(cfg.db_path)
        cal = None
        if cfg.calendar.enabled:
            from .gcal import GoogleCalendar

            cal = GoogleCalendar(cfg, db)
        core = cls(cfg, calendar=cal, transcriber=Transcriber(cfg.stt), db=db)
        core.transcriber.vocabulary = core.asr_vocabulary      # dinámico: se actualiza con lo que vas anotando
        return core

    def asr_vocabulary(self, limit: int = 700) -> str:
        """Vocabulario para sesgar el reconocimiento de voz: primero lo TUYO (palabras de tus últimos ítems:
        nombres, lugares, proyectos, con su ortografía real) y después las palabras clave de las reglas."""
        stop = {"para", "como", "pero", "este", "esta", "esto", "unos", "unas", "desde", "hasta", "sobre",
                "tengo", "hacer", "recordame", "recordatorio", "tarea", "por", "del", "con", "los", "las",
                "una", "que", "mis", "sus", "ese", "esa", "eso", "mañana", "manana", "hoy", "ayer"}
        mine: dict[str, None] = {}
        for r in reversed(self.db.all_captures()[-80:]):
            for w in re.findall(r"[\wáéíóúüñÁÉÍÓÚÜÑ]{3,}", r["title"] or ""):
                if w.lower() not in stop:
                    mine.setdefault(w, None)
        parts = list(mine) + [k for k in self.classifier.vocabulary().split(", ") if k]
        out, size = [], 0
        for w in dict.fromkeys(parts):
            if size + len(w) + 2 > limit:
                break
            out.append(w)
            size += len(w) + 2
        return ", ".join(out)

    def _keywords(self) -> dict[str, list[str]]:
        return {c: [k for k, _ in entries] for c, entries in self.classifier.cats.items()}

    # ----------------------------------------------- calendario por categoría
    def skipped_categories(self) -> set[str]:
        raw = self.db.kv_get("cal_skip")
        if raw is not None:
            try:
                return {str(c) for c in json.loads(raw)}
            except (ValueError, TypeError):
                pass
        return set(self.cfg.calendar.skip_categories)

    def calendar_wanted(self, category: str | None) -> bool:
        return bool(category) and category not in self.skipped_categories()

    @locked
    def set_calendar_for(self, category: str, on: bool) -> list[str]:
        """Activa/desactiva la publicación en Google de una categoría. Al apagarla se retiran sus eventos
        (las capturas y sus avisos quedan); al prenderla, la sincronización los vuelve a crear."""
        if category not in self.cfg.category_names:
            raise ValueError(f"Categoría inválida: {category}")
        skip = self.skipped_categories()
        skip.discard(category) if on else skip.add(category)
        self.db.kv_set("cal_skip", json.dumps(sorted(skip)))
        warnings: list[str] = []
        for row in self.db.all_captures():
            if row["category"] == category:
                warnings += self._publish(row["id"])
        return warnings

    def now(self) -> datetime:
        return datetime.now(self.cfg.tz).replace(tzinfo=None, second=0, microsecond=0)

    def today(self) -> date:
        return self.now().date()

    # ============================================================ captura
    def transcribe(self, audio_path: str | Path) -> str:
        if self.transcriber is None:
            raise RuntimeError("STT no configurado")
        text = self.transcriber.transcribe(audio_path)
        if not text:
            raise ValueError("No se entendió el audio (transcripción vacía)")
        return text

    def capture_audio(self, audio_path: str | Path, source: str = "audio") -> Result:
        return self.capture_text(self.transcribe(audio_path), source=source)

    @locked
    def capture_text(self, text: str, source: str = "texto", now: datetime | None = None,
                     use_ai: bool = True) -> Result:
        text = text.strip()
        if not text:
            raise ValueError("Texto vacío")
        now = now or self.now()
        when = parse_when(text, now)
        cls = self.classifier.classify(text, when)
        title = clean_title(text, when.spans, self.cfg.rules.drop_prefixes)
        if not title:
            raise ValueError("Me falta el contenido: ¿qué querés que anote? (ej: “comprar pan mañana”)")

        category, typ, auto_source, confident = cls.category, cls.type, "rules", cls.confident
        if category is None and use_ai and self.ai.available:
            ai = self.ai.classify(text)
            if ai and ai.category and ai.confidence >= self.cfg.ai.min_confidence:
                category, auto_source, confident = ai.category, "ai", True
                if ai.title:
                    title = ai.title
                if not (when.start or when.day):
                    typ = ai.type

        start = when.start
        length = when.duration or timedelta(minutes=self.cfg.default_event_minutes)
        end = start + length if start else None
        uid = now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]

        seq = self.db.insert_capture({
            "id": uid, "created": now.isoformat(), "updated": now.isoformat(), "source": source,
            "transcript": text, "title": title, "category": category, "type": typ,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "all_day_date": when.day.isoformat() if when.day and not start else None,
            "status": "pendiente", "cat_confirmed": 0,
            "hour_pending": int(bool(when.ambiguous_hour and start)),
            "time_pending": int(bool(when.day and not start)),
            "auto_category": category, "auto_confident": int(confident), "auto_source": auto_source,
        })
        self._record(uid, "crear", None)
        warnings = self._publish(uid)
        res = self._result(uid)
        res.warnings = warnings
        assert res.seq == seq
        return res

    # ====================================================== resoluciones
    def get(self, ref) -> Result:
        return self._result(self._row(ref)["id"])

    @locked
    def set_category(self, ref, category: str, by_user: bool = True) -> Result:
        """`by_user=False`: la eligió el agente de IA; no cuenta como corrección humana ni la confirma."""
        if category not in self.cfg.category_names:
            raise ValueError(f"Categoría inválida: {category}")
        row = self._row(ref)
        self._record(row["id"], "categoría", row)
        if by_user:
            self._touch(row["id"], category=category, cat_confirmed=1)
            self._feedback(row, "category", row["auto_category"] or "", category)
        else:
            self._touch(row["id"], category=category, auto_category=category, auto_source="agent")
            self._feedback(row, "agent_category", row["auto_category"] or "", category)
        return self._finish(row["id"])

    @locked
    def set_hour(self, ref, part: str) -> Result:
        """part: 'am' | 'pm' — resuelve una hora que se había asumido PM."""
        if part not in ("am", "pm"):
            raise ValueError("part debe ser 'am' o 'pm'")
        row = self._row(ref)
        if not row["start"]:
            raise ValueError("La captura no tiene hora")
        start = datetime.fromisoformat(row["start"])
        end = datetime.fromisoformat(row["end"])
        if part == "am" and start.hour >= 12:
            start, end = start - timedelta(hours=12), end - timedelta(hours=12)
        elif part == "pm" and start.hour < 12:
            start, end = start + timedelta(hours=12), end + timedelta(hours=12)
        self._record(row["id"], "hora", row)
        self._touch(row["id"], start=start.isoformat(), end=end.isoformat(), hour_pending=0)
        self._feedback(row, "hour", "pm", part)
        return self._finish(row["id"])

    @locked
    def set_time(self, ref, text: str) -> Result:
        """Le pone hora a una captura que tenía solo fecha. `text`: "18:30", "a las 5 de la tarde"..."""
        row = self._row(ref)
        if not row["all_day_date"]:
            raise ValueError("La captura ya tiene hora")
        w = parse_when(text, self.now())
        if w.at is None:
            raise ValueError("No entendí la hora")
        start = datetime.combine(date.fromisoformat(row["all_day_date"]), w.at)
        end = start + (w.duration or timedelta(minutes=self.cfg.default_event_minutes))
        self._record(row["id"], "poner hora", row)
        self._touch(row["id"], start=start.isoformat(), end=end.isoformat(), all_day_date=None,
                    time_pending=0, hour_pending=int(w.ambiguous_hour))
        self._feedback(row, "time", "todo-el-dia", f"{w.at:%H:%M}")
        return self._finish(row["id"])

    @locked
    def skip_time(self, ref) -> Result:
        row = self._row(ref)
        self._record(row["id"], "todo el día", row)
        self._touch(row["id"], time_pending=0)
        self._feedback(row, "time", "todo-el-dia", "todo-el-dia")
        return self._finish(row["id"])

    # ============================================================ CRUD
    @locked
    def update_title(self, ref, title: str) -> Result:
        title = " ".join(title.split())
        if not title:
            raise ValueError("El título no puede estar vacío")
        row = self._row(ref)
        self._record(row["id"], "título", row)
        self._touch(row["id"], title=title)
        return self._finish(row["id"])

    @locked
    def reschedule(self, ref, text: str) -> Result:
        """Cambia fecha y/u hora con lenguaje natural: "el viernes", "a las 10", "mañana a las 9:30",
        "sin hora"/"todo el día". Lo que no se menciona se conserva."""
        row = self._row(ref)
        cur_start = datetime.fromisoformat(row["start"]) if row["start"] else None
        cur_end = datetime.fromisoformat(row["end"]) if row["end"] else None
        cur_day = (cur_start.date() if cur_start
                   else date.fromisoformat(row["all_day_date"]) if row["all_day_date"] else None)
        f = fold(text)
        w = parse_when(text, self.now())
        all_day = any(p in f for p in ("todo el dia", "sin hora", "sin horario"))

        day = w.day
        if w.date_inferred and cur_day:      # solo dijo la hora: se queda en su día
            day = cur_day
        day = day or cur_day
        if day is None:
            raise ValueError("No entendí cuándo. Probá con “el viernes a las 10” o “mañana”.")
        if not all_day and w.day is None and w.at is None:
            raise ValueError("No entendí cuándo. Probá con “el viernes a las 10” o “mañana”.")

        if all_day:
            at = None
        else:
            at = w.at or (cur_start.time() if (cur_start and w.day) else None)

        length = w.duration or (cur_end - cur_start if cur_start and cur_end
                                else timedelta(minutes=self.cfg.default_event_minutes))
        self._record(row["id"], "mover", row)
        if at is not None:
            start = datetime.combine(day, at)
            self._touch(row["id"], start=start.isoformat(), end=(start + length).isoformat(),
                        all_day_date=None, time_pending=0,
                        hour_pending=int(w.ambiguous_hour and w.at is not None))
        else:
            self._touch(row["id"], start=None, end=None, all_day_date=day.isoformat(),
                        hour_pending=0, time_pending=0)
        return self._finish(row["id"])

    @locked
    def set_duration(self, ref, minutes: int) -> Result:
        row = self._row(ref)
        if not row["start"]:
            raise ValueError("Solo los eventos con hora tienen duración")
        if not 5 <= minutes <= 24 * 60:
            raise ValueError("La duración tiene que estar entre 5 minutos y 24 horas")
        start = datetime.fromisoformat(row["start"])
        self._record(row["id"], "duración", row)
        self._touch(row["id"], end=(start + timedelta(minutes=minutes)).isoformat())
        return self._finish(row["id"])

    @locked
    def set_type(self, ref, typ: str) -> Result:
        if typ not in TYPES:
            raise ValueError(f"Tipo inválido: {typ} (usá {', '.join(TYPES)})")
        row = self._row(ref)
        self._record(row["id"], "tipo", row)
        self._touch(row["id"], type=typ)
        return self._finish(row["id"])

    @locked
    def complete(self, ref) -> Result:
        row = self._row(ref)
        self._record(row["id"], "hecha", row)
        self._touch(row["id"], status="hecha")
        return self._finish(row["id"])

    @locked
    def reopen(self, ref) -> Result:
        row = self._row(ref)
        self._record(row["id"], "reabrir", row)
        self._touch(row["id"], status="pendiente")
        return self._finish(row["id"])

    @locked
    def delete(self, ref) -> None:
        row = self._row(ref)
        self._record(row["id"], "borrar", row)
        self._touch(row["id"], status="borrada")
        self._publish(row["id"])

    @locked
    def restore(self, ref) -> Result:
        row = self._row(ref)
        if row["status"] != "borrada":
            raise ValueError("Esa captura no está en la papelera")
        self._record(row["id"], "restaurar", row)
        self._touch(row["id"], status="pendiente")
        return self._finish(row["id"])

    @locked
    def undo(self) -> tuple[str, Result | None]:
        """Revierte la última acción registrada. -> (acción deshecha, estado resultante)."""
        h = self.db.history_pop()
        if h is None:
            raise LookupError("No hay nada para deshacer")
        cid = h["capture_id"]
        if h["before"] is None:                      # era una creación
            self._touch(cid, status="borrada")
            self._publish(cid)
            return h["action"], None
        self._touch(cid, **json.loads(h["before"]))
        warnings = self._publish(cid)
        res = self._result(cid)
        res.warnings = warnings
        return h["action"], res

    # ========================================================= consultas
    def agenda(self, day: date, days: int = 1):
        a = datetime.combine(day, datetime.min.time())
        return self.db.captures_between(a.isoformat(), (a + timedelta(days=days)).isoformat())

    def tasks(self) -> list[Result]:
        return [self._from_row(r) for r in self.db.pending_tasks()]

    def overdue(self) -> list[Result]:
        today = self.today().isoformat()
        out = []
        for r in self.db.pending_tasks(200):
            due = r["all_day_date"] or (r["start"] or "")[:10]
            if due and due < today:
                out.append(self._from_row(r))
        return out

    def inbox(self) -> list[Result]:
        return [self._from_row(r) for r in self.db.inbox()]

    def trash(self) -> list[Result]:
        return [self._from_row(r) for r in self.db.trashed()]

    def search(self, query: str, limit: int = 15) -> list[Result]:
        words = [w for w in fold(query).split() if w]
        if not words:
            return []
        out = []
        for r in reversed(self.db.all_captures()):
            hay = fold(f"{r['title']} {r['transcript']} {r['category'] or ''}")
            if all(w in hay for w in words):
                out.append(self._from_row(r))
                if len(out) >= limit:
                    break
        return out

    def stats(self) -> dict:
        rows = self.db.all_captures()
        skipped = self.skipped_categories()
        dated = [r for r in rows if (r["start"] or r["all_day_date"]) and r["category"] not in skipped]
        return {
            "activas": len(rows),
            "pendientes": sum(1 for r in rows if r["status"] == "pendiente"),
            "hechas": sum(1 for r in rows if r["status"] == "hecha"),
            "sin_categoria": sum(1 for r in rows if r["category"] is None),
            "con_fecha": len(dated),
            "en_calendario": sum(1 for r in dated if r["event_id"]),
            "sin_calendario": sum(1 for r in dated if not r["event_id"]),
            "calendario_activo": self.calendar is not None,
        }

    # ========================================================== reglas
    @locked
    def add_rule(self, category: str, word: str) -> None:
        category = category.lower()
        if category not in self.cfg.category_names:
            raise ValueError(f"Categoría inválida: {category}")
        word = fold(word.strip())
        if not word:
            raise ValueError("Falta la palabra")
        data = load_user_rules(self.cfg)
        if word in data["remove"].get(category, []):       # la había quitado: se la reactiva
            data["remove"][category].remove(word)
        lst = data["add"].setdefault(category, [])
        if word not in lst:
            lst.append(word)
        save_user_rules(self.cfg, data)
        self.reload_rules()

    @locked
    def remove_rule(self, category: str, word: str) -> None:
        category = category.lower()
        if category not in self.cfg.category_names:
            raise ValueError(f"Categoría inválida: {category}")
        word = fold(word.strip())
        data = load_user_rules(self.cfg)
        if word in data["add"].get(category, []):
            data["add"][category].remove(word)
        else:  # regla de fábrica: se registra como quitada
            data["remove"].setdefault(category, []).append(word)
        save_user_rules(self.cfg, data)
        self.reload_rules()

    def reload_rules(self) -> None:
        self.classifier = Classifier(self.cfg)
        self.ai.categories = self._keywords()
        if self.transcriber is not None and not callable(self.transcriber.vocabulary):
            self.transcriber.vocabulary = self.classifier.vocabulary()

    def rules_view(self) -> dict[str, list[str]]:
        return {c: [k for k, _ in entries] for c, entries in self.classifier.cats.items()}

    # ======================================================== internos
    def _row(self, ref):
        """Acepta número corto (42, "#42", "42") o id largo."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.lstrip("#").isdigit()):
            row = self.db.get_by_seq(int(str(ref).lstrip("#")))
        else:
            row = self.db.get_capture(str(ref))
        if row is None:
            raise KeyError(ref)
        return row

    def _feedback(self, row, kind: str, auto: str, chosen: str) -> None:
        self.db.add_feedback(self.now().isoformat(), row["id"], kind, auto, chosen, row["transcript"])

    def _record(self, cid: str, action: str, row) -> None:
        before = {k: row[k] for k in SNAPSHOT_FIELDS} if row is not None else None
        self.db.history_add(self.now().isoformat(), cid, action, before)

    def _touch(self, cid: str, **fields) -> None:
        if "start" in fields or "all_day_date" in fields:
            self.db.sent_clear(cid)            # mover un evento = sus avisos vuelven a estar pendientes
        self.db.update_capture(cid, updated=self.now().isoformat(), **fields)

    def _finish(self, cid: str) -> Result:
        warnings = self._publish(cid)
        res = self._result(cid)
        res.warnings = warnings
        return res

    def _publish(self, cid: str) -> list[str]:
        """Deja el evento de Calendar coincidiendo con la fila. Devuelve advertencias (nunca lanza)."""
        warnings: list[str] = []
        if self.calendar is not None:
            self._publish_calendar(cid, warnings)
        return warnings

    def row_sig(self, row) -> str:
        start = datetime.fromisoformat(row["start"]) if row["start"] else None
        end = datetime.fromisoformat(row["end"]) if row["end"] else None
        day = date.fromisoformat(row["all_day_date"]) if row["all_day_date"] else None
        return sig_of(row["title"], start, end, day)

    def _publish_calendar(self, cid: str, warnings: list[str]) -> None:
        row = self.db.get_capture(cid)
        cal = self.calendar
        start = datetime.fromisoformat(row["start"]) if row["start"] else None
        end = datetime.fromisoformat(row["end"]) if row["end"] else None
        day = date.fromisoformat(row["all_day_date"]) if row["all_day_date"] else None
        want = (row["status"] != "borrada" and self.calendar_wanted(row["category"])
                and (start or day))
        sig = sig_of(row["title"], start, end, day)
        try:
            if want:
                if row["event_id"]:
                    new_cal = cal.calendar_id(row["category"])
                    cal_id = row["calendar_id"]
                    if cal_id != new_cal:
                        cal.move_event(row["event_id"], cal_id, row["category"])
                        cal_id = new_cal
                    if sig != row["cal_sig"]:
                        cal.update_event(row["event_id"], cal_id, row["title"], row["transcript"] or "",
                                         start=start, end=end, day=day)
                    self.db.update_capture(cid, calendar_id=cal_id, cal_sig=sig)
                else:
                    eid, cal_id = cal.create_event(
                        row["category"], row["title"], row["transcript"] or "", start=start, end=end, day=day)
                    self.db.update_capture(cid, event_id=eid, calendar_id=cal_id, cal_sig=sig)
            elif row["event_id"]:
                cal.delete_event(row["event_id"], row["calendar_id"])
                self.db.update_capture(cid, event_id=None, calendar_id=None, cal_sig=None)
        except Exception as e:  # noqa: BLE001  (la nota no se pierde si Google falla)
            log.exception("calendar")
            warnings.append(f"Calendario: {e}")

    def _result(self, cid: str) -> Result:
        return self._from_row(self.db.get_capture(cid))

    def _from_row(self, row) -> Result:
        d = date.fromisoformat
        start = datetime.fromisoformat(row["start"]) if row["start"] else None
        return Result(
            id=row["id"], seq=row["seq"] or 0, category=row["category"], type=row["type"], title=row["title"],
            start=start,
            end=datetime.fromisoformat(row["end"]) if row["end"] else None,
            day=(start.date() if start else d(row["all_day_date"]) if row["all_day_date"] else None),
            transcript=row["transcript"] or "", source=row["source"] or "", event_id=row["event_id"],
            ambiguous_hour=bool(row["hour_pending"]), needs_category=row["category"] is None,
            pending=pending_of(row), status=row["status"] or "pendiente", auto_source=row["auto_source"],
            calendar_off=bool(row["category"]) and row["category"] in self.skipped_categories(),
        )
