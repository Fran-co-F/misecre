"""Sincronización de dos vías entre misecre (SQLite) y Google Calendar.

Qué hace en cada pasada:
  * Cambios hechos en Google (mover, renombrar, cambiar de calendario, borrar) -> se aplican a la DB.
  * Eventos creados a mano en Google -> se importan como capturas.
  * Todo lo categorizado y con fecha debe tener su evento -> si falta, se crea (reparación).
  * Los avisos nativos de Google se silencian: los emite misecre, una sola vez.

Es idempotente: compara VALORES, no solo marcas de tiempo, así que el "eco" de nuestras propias
escrituras (Google actualiza `updated` al recibir lo que enviamos) no produce cambios.
El borrado en Google manda la captura a la papelera (reversible con /restaurar).
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .core import WEEKDAYS_ES, Core, sig_of

log = logging.getLogger("misecre.sync")


@dataclass
class SyncReport:
    changes: list[str] = field(default_factory=list)
    imported: int = 0
    healed: int = 0
    silenced: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changes or self.imported or self.healed)

    def text(self) -> str:
        if not self.changed and not self.errors:
            return "✓ Todo sincronizado, sin cambios."
        lines = ["🔄 Sincronización"]
        lines += [f"• {c}" for c in self.changes]
        if self.healed:
            lines.append(f"• Reparé {self.healed} evento(s) que faltaban en el calendario")
        lines += [f"⚠️ {e}" for e in self.errors]
        return "\n".join(lines)


def _label(start: datetime | None, day: date | None) -> str:
    if start:
        return f"{WEEKDAYS_ES[start.weekday()]} {start:%d/%m %H:%M}"
    if day:
        return f"{WEEKDAYS_ES[day.weekday()]} {day:%d/%m} (todo el día)"
    return "sin fecha"


class Syncer:
    def __init__(self, core: Core):
        self.core = core
        self.cfg = core.cfg
        self.db = core.db
        self._lock = threading.Lock()

    def run(self) -> SyncReport:
        rep = SyncReport()
        if self.core.calendar is None:
            return rep                                     # sin Google no hay nada que sincronizar
        if not self._lock.acquire(blocking=False):
            rep.errors.append("Ya hay una sincronización en curso")
            return rep
        try:
            with self.core.lock:                           # sin ediciones intercaladas del bot / la API
                touched: list[str] = []
                cursors = self._collect(touched, rep)
                for cid in dict.fromkeys(touched):
                    rep.errors += self.core._publish(cid)
                for cat, cursor in cursors.items():        # el cursor solo avanza si todo salió bien
                    self.db.kv_set(f"sync:cal:{cat}", cursor)
                self._heal(rep)
                self.db.kv_set("sync:last", self.core.now().isoformat())
        except Exception as e:  # noqa: BLE001
            log.exception("sync")
            rep.errors.append(str(e))
        finally:
            self._lock.release()
        return rep

    # ------------------------------------------------------------ lectura
    def _collect(self, touched: list[str], rep: SyncReport) -> dict[str, str]:
        cal, core, db, cfg = self.core.calendar, self.core, self.db, self.cfg
        per_cat: dict[str, list] = {}
        skipped = core.skipped_categories()
        for cat in cfg.category_names:
            if cat in skipped:                                 # no se publica en Google: ni se lee
                continue
            try:
                per_cat[cat] = cal.list_changes(cat, db.kv_get(f"sync:cal:{cat}"), cfg.sync.import_past_days)
            except Exception as e:  # noqa: BLE001
                log.exception("sync calendario")
                rep.errors.append(f"No pude leer el calendario {cat}: {e}")
        alive = {e.id for evs in per_cat.values() for e in evs if not e.deleted}
        horizon = core.today() - timedelta(days=cfg.sync.import_past_days)
        cursors: dict[str, str] = {}

        for cat, events in per_cat.items():
            for e in events:
                row = db.get_by_event_id(e.id)
                if e.deleted:
                    # Si el mismo id sigue vivo en otro calendario, fue un "mover", no un borrado.
                    if row is not None and e.id not in alive and row["status"] != "borrada":
                        core._touch(row["id"], status="borrada", event_id=None, calendar_id=None, cal_sig=None)
                        rep.changes.append(
                            f"🗑 Borraste “{row['title']}” en Google Calendar: la pasé a la papelera (/restaurar)")
                elif row is None:
                    self._import_event(e, horizon, rep)
                elif row["status"] != "borrada":
                    self._apply_event(row, e, touched, rep)
                if not e.deleted and e.reminders_default and cfg.sync.silence_google_reminders:
                    self._silence(e, rep)
            newest = max((e.updated for e in events if e.updated), default=None)
            if newest:
                cursors[cat] = newest
        return cursors

    def _silence(self, e, rep: SyncReport) -> None:
        try:
            self.core.calendar.silence_event(e.id, e.calendar_id)
            rep.silenced += 1
        except Exception as ex:  # noqa: BLE001
            rep.errors.append(f"No pude silenciar los avisos de Google de “{e.title}”: {ex}")

    def _import_event(self, e, horizon: date, rep: SyncReport) -> None:
        cfg, core, db = self.cfg, self.core, self.db
        if not cfg.sync.import_calendar_events or e.recurring or not (e.start or e.day):
            return
        if e.title.startswith("[misecre-selftest]"):          # evento de `misecre selftest-calendar`
            return
        when_day = e.start.date() if e.start else e.day
        if when_day < horizon:
            return
        now = core.now()
        title = e.title or "(sin título)"
        end = e.end if e.start and e.end else (
            e.start + timedelta(minutes=cfg.default_event_minutes) if e.start else None)
        uid = now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        db.insert_capture({
            "id": uid, "created": now.isoformat(), "updated": now.isoformat(), "source": "calendar",
            "transcript": title, "title": title, "category": e.category, "type": "evento",
            "start": e.start.isoformat() if e.start else None,
            "end": end.isoformat() if end else None,
            "all_day_date": e.day.isoformat() if e.day and not e.start else None,
            "status": "pendiente", "cat_confirmed": 1,
            "event_id": e.id, "calendar_id": e.calendar_id,
            "cal_sig": sig_of(title, e.start, end, e.day),
            "auto_category": e.category, "auto_confident": 1, "auto_source": "calendar",
        })
        core._record(uid, "importar", None)
        rep.imported += 1
        rep.changes.append(f"📥 Importé de Google Calendar: {title} ({_label(e.start, e.day)}) · {e.category}")

    def _apply_event(self, row, e, touched: list[str], rep: SyncReport) -> None:
        core = self.core
        cid = row["id"]
        fields: dict = {}
        what: list[str] = []

        if row["calendar_id"] and e.calendar_id != row["calendar_id"] and e.category != row["category"]:
            fields.update(category=e.category, calendar_id=e.calendar_id, cat_confirmed=1)
            what.append(f"calendario → {e.category}")

        end = e.end if e.start and e.end else (
            e.start + timedelta(minutes=self.cfg.default_event_minutes) if e.start else None)
        sig_e = sig_of(e.title, e.start, end, e.day)
        if sig_e != row["cal_sig"]:
            if e.title and e.title != row["title"]:
                fields["title"] = e.title
                what.append(f"título → “{e.title}”")
            new_start = e.start.replace(second=0, microsecond=0).isoformat() if e.start else None
            new_end = end.replace(second=0, microsecond=0).isoformat() if end else None
            new_day = e.day.isoformat() if e.day and not e.start else None
            if (new_start, new_end, new_day) != (row["start"], row["end"], row["all_day_date"]):
                fields.update(start=new_start, end=new_end, all_day_date=new_day,
                              time_pending=0, hour_pending=0)
                what.append(f"cuándo → {_label(e.start, e.day)}")
            fields["cal_sig"] = sig_e

        if not fields:
            return
        if what:
            core._touch(cid, **fields)
            rep.changes.append(f"📅 Cambiaste “{row['title']}” en Google Calendar: {', '.join(what)}")
        else:
            self.db.update_capture(cid, **fields)          # solo se actualiza la huella: sin cambio real

    # ------------------------------------------------------------ reparar
    def _heal(self, rep: SyncReport) -> None:
        skipped = self.core.skipped_categories()
        for row in self.db.all_captures():
            if row["event_id"] and row["category"] in skipped:        # quedó un evento de una categoría apagada
                self.core._publish(row["id"])
                rep.changes.append(f"Retiré de Google «{row['title']}» (la categoría {row['category']} "
                                   f"no se publica en el calendario)")
                continue
            # Una edición local cuyo update a Google falló deja event_id puesto pero cal_sig viejo: Google no
            # reporta ningún cambio, así que hay que reintentarla acá o el calendario queda desfasado.
            if row["event_id"] and row["cal_sig"] and row["status"] != "borrada" \
                    and self.core.calendar_wanted(row["category"]) and self.core.row_sig(row) != row["cal_sig"]:
                warnings = self.core._publish(row["id"])
                if self.db.get_capture(row["id"])["cal_sig"] == self.core.row_sig(row):
                    rep.healed += 1
                else:
                    rep.errors += warnings
                continue
            if self.core.calendar_wanted(row["category"]) and (row["start"] or row["all_day_date"]) \
                    and not row["event_id"] and row["status"] != "borrada":
                warnings = self.core._publish(row["id"])
                if self.db.get_capture(row["id"])["event_id"]:
                    rep.healed += 1
                else:
                    rep.errors += warnings
