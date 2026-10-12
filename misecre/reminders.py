"""Avisos propios. Google queda en silencio; acá se decide cuándo y cuánto avisar.

No hay jobstore: en cada tick se *recalcula* qué vence a partir de la DB. Mover o borrar un evento
reprograma solo, y reiniciar la PC no pierde nada. La deduplicación vive en `sent_log`.

Reglas:
  * por categoría: `before: [60, 10]` minutos antes; `digest_only` => nunca empuja, solo resumen.
  * lo de todo el día avisa una vez, ese día, a `allday_at`.
  * horas de silencio y /pausa: no se emite nada; al terminar, lo que siga vigente sale en un único mensaje.
  * si el evento ya empezó y nunca se avisó (PC apagada) => un aviso "te lo perdiste" (hasta 12 h después).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .core import Core, Result

MISSED_WINDOW = timedelta(hours=12)


def parse_hhmm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def in_quiet(now: datetime, spec: str) -> bool:
    """spec "23:00-07:00" (cruza medianoche) o "13:00-15:00". Vacío/"off" => nunca."""
    if not spec or spec.strip().lower() in ("off", "no", "none", "-"):
        return False
    a, b = (parse_hhmm(x) for x in spec.split("-"))
    t = now.time()
    return (a <= t < b) if a <= b else (t >= a or t < b)


@dataclass
class Due:
    result: Result
    kind: str                 # before | allday | snooze | missed
    key: str
    minutes_left: int | None = None
    snooze_id: int | None = None


class Reminders:
    def __init__(self, core: Core):
        self.core = core
        self.cfg = core.cfg
        self.db = core.db

    # -------------------------------------------------------------- pausa
    def paused_until(self, now: datetime) -> datetime | None:
        raw = self.db.kv_get("paused_until")
        if not raw:
            return None
        until = datetime.fromisoformat(raw)
        return until if until > now else None

    def pause(self, minutes: int, now: datetime | None = None) -> datetime:
        until = (now or self.core.now()) + timedelta(minutes=minutes)
        self.db.kv_set("paused_until", until.isoformat())
        return until

    def resume(self) -> None:
        self.db.kv_set("paused_until", None)

    def quiet_spec(self) -> str:
        return self.db.kv_get("quiet_hours") or self.cfg.notifications.quiet_hours

    def set_quiet(self, spec: str) -> None:
        spec = spec.strip().lower()
        if spec not in ("off", "no", ""):
            a, b = spec.split("-")
            parse_hhmm(a), parse_hhmm(b)         # valida el formato (lanza ValueError)
        self.db.kv_set("quiet_hours", "off" if spec in ("off", "no", "") else spec)

    def silenced(self, now: datetime) -> str | None:
        if self.paused_until(now):
            return "pausa"
        if in_quiet(now, self.quiet_spec()):
            return "silencio"
        return None

    # ------------------------------------------------------------ política
    def _policy(self, category: str | None):
        """Política de avisos: 1) tu ajuste en caliente (/avisos), 2) la del config.yaml, 3) la de «personal»
        si la categoría no está en la config (o el ítem no tiene categoría)."""
        from .config import CategoryPolicy

        pol = self.cfg.notifications.categories
        fallback = "personal" if "personal" in pol else next(iter(pol))
        key = category if category else fallback
        raw = self.db.kv_get(f"policy:{key}")
        if raw:
            return CategoryPolicy(**json.loads(raw))
        return pol.get(key) or pol[fallback]

    def policy_for(self, category: str):
        return self._policy(category)

    def set_policy(self, category: str, before: list[int], digest_only: bool) -> None:
        if category not in self.cfg.category_names:
            raise ValueError(f"Categoría inválida: {category}")
        if any(m <= 0 or m > 24 * 60 for m in before):
            raise ValueError("Los minutos tienen que estar entre 1 y 1440")
        self.db.kv_set(f"policy:{category}", json.dumps({"before": sorted(set(before), reverse=True),
                                                         "digest_only": digest_only}))

    def collect(self, now: datetime | None = None) -> list[Due]:
        """Qué hay que avisar *ahora*. Sin efectos secundarios (ver `mark`)."""
        now = now or self.core.now()
        if self.silenced(now):
            return []
        out: list[Due] = []
        allday_at = parse_hhmm(self.cfg.notifications.allday_at)
        today = now.date().isoformat()

        for row in self.db.all_captures():
            if row["status"] != "pendiente":
                continue
            cid = row["id"]
            pol = self._policy(row["category"])
            if pol.digest_only:
                continue
            def result():
                return self.core._from_row(row)

            if row["start"]:
                start = datetime.fromisoformat(row["start"])
                sent_any = False
                for m in sorted(pol.before, reverse=True):
                    key = f"b{m}"
                    if self.db.sent_has(cid, key):
                        sent_any = True
                        continue
                    if start - timedelta(minutes=m) <= now < start:
                        out.append(Due(result(), "before", key, int((start - now).total_seconds() // 60)))
                        sent_any = True
                        break                      # un solo aviso por tick por evento
                if (not sent_any and pol.before and start <= now < start + MISSED_WINDOW
                        and not self.db.sent_has(cid, "missed")):
                    out.append(Due(result(), "missed", "missed"))
            elif row["all_day_date"] == today and now.time() >= allday_at:
                key = f"d{today}"
                if not self.db.sent_has(cid, key):
                    out.append(Due(result(), "allday", key))

        for s in self.db.snoozes_due(now.isoformat()):
            row = self.db.get_capture(s["capture_id"])
            if row is not None and row["status"] == "pendiente":
                out.append(Due(self.core._from_row(row), "snooze", f"s{s['id']}", snooze_id=s["id"]))
            else:
                self.db.snooze_done(s["id"], now.isoformat())
        return out

    def mark(self, due: Due, now: datetime | None = None) -> None:
        ts = (now or self.core.now()).isoformat()
        if due.kind == "snooze":
            self.db.snooze_done(due.snooze_id, ts)
        else:
            self.db.sent_mark(due.result.id, due.key, ts)

    def snooze(self, ref, minutes: int | None = None, now: datetime | None = None) -> datetime:
        row = self.core._row(ref)
        minutes = minutes or self.cfg.notifications.snooze_minutes
        fire = (now or self.core.now()) + timedelta(minutes=minutes)
        self.db.snooze_add(row["id"], fire.isoformat())
        return fire


def describe(due: Due) -> str:
    r = due.result
    hhmm = f" ({r.start:%H:%M})" if r.start else ""
    tag = f"#{r.seq} {r.title}"
    if due.kind == "before":
        n = due.minutes_left or 0
        cuando = "ahora" if n <= 0 else f"en {n} min"
        return f"⏰ {cuando} · {tag}{hhmm}"
    if due.kind == "allday":
        return f"📌 Hoy · {tag}"
    if due.kind == "missed":
        return f"⌛ Te lo perdiste · {tag}{hhmm}"
    return f"⏰ Recordatorio · {tag}{hhmm}"
