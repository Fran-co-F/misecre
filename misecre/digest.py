"""Resúmenes (diario / semanal / mensual)."""
from __future__ import annotations

import calendar as _cal
from datetime import date, datetime, timedelta

from .core import WEEKDAYS_ES, Core, Result
from .reminders import parse_hhmm

ICON = {"trabajo": "💼", "personal": "🏠", "ocio": "🎮"}
DAY_NAMES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTH_NAMES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
               "septiembre", "octubre", "noviembre", "diciembre"]
WEEKDAY_ALIASES = {
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2, "jueves": 3, "viernes": 4,
    "sabado": 5, "sábado": 5, "domingo": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}


def _item(r: Result, with_day: bool = False) -> str:
    icon = ICON.get(r.category or "", "❓")
    box = "☐ " if r.type == "tarea" else ""
    if r.start:
        when = f"{WEEKDAYS_ES[r.start.weekday()]} {r.start:%H:%M}" if with_day else f"{r.start:%H:%M}"
    else:
        when = f"{WEEKDAYS_ES[r.day.weekday()]}" if (with_day and r.day) else "todo el día"
    cal = "" if r.event_id or r.calendar_off or not (r.start or r.day) else " ·sin calendario"
    return f"{when} {icon} #{r.seq} {box}{r.title}{cal}"


def _rows(core: Core, day: date, days: int) -> list[Result]:
    return [core._from_row(r) for r in core.agenda(day, days)]


def build_daily(core: Core, now: datetime | None = None) -> str:
    now = now or core.now()
    today = now.date()
    items = [r for r in _rows(core, today, 1) if r.status != "hecha"]
    done = [r for r in _rows(core, today, 1) if r.status == "hecha"]
    lines = [f"🌅 {DAY_NAMES[today.weekday()].capitalize()} {today:%d/%m}"]
    lines.append("")
    if items:
        lines.append("📅 Hoy")
        lines += [f"  • {_item(r)}" for r in items]
    else:
        lines.append("📅 Hoy no tenés nada agendado.")
    if done:
        lines.append(f"✅ Hechas hoy: {len(done)}")
    overdue = core.overdue()
    if overdue:
        lines += ["", f"⚠️ Atrasadas ({len(overdue)})"]
        lines += [f"  • #{r.seq} {r.title} (era el {r.day:%d/%m})" for r in overdue[:8] if r.day]
    undated = [t for t in core.tasks() if not t.day]
    if undated:
        lines += ["", f"📝 Tareas sin fecha ({len(undated)})"]
        lines += [f"  • #{t.seq} {t.title}" for t in undated[:6]]
    inbox = core.inbox()
    if inbox:
        lines += ["", f"❓ Sin categoría: {len(inbox)} → /inbox"]
    tomorrow = [r for r in _rows(core, today + timedelta(days=1), 1) if r.status != "hecha"]
    if tomorrow:
        lines += ["", f"👀 Mañana ({len(tomorrow)})"]
        lines += [f"  • {_item(r)}" for r in tomorrow[:5]]
    return "\n".join(lines)


def build_weekly(core: Core, now: datetime | None = None) -> str:
    now = now or core.now()
    today = now.date()
    start = today + timedelta(days=1) if now.weekday() == 6 else today
    lines = [f"📆 Semana del {start:%d/%m} al {(start + timedelta(days=6)):%d/%m}", ""]
    total = 0
    for i in range(7):
        d = start + timedelta(days=i)
        rows = [r for r in _rows(core, d, 1) if r.status != "hecha"]
        total += len(rows)
        if rows:
            lines.append(f"{DAY_NAMES[d.weekday()].capitalize()} {d:%d/%m}")
            lines += [f"  • {_item(r)}" for r in rows]
    if total == 0:
        lines.append("Semana libre: no tenés nada agendado.")
    week_ago = (now - timedelta(days=7)).isoformat()
    done = [r for r in core.db.all_captures()
            if r["status"] == "hecha" and (r["updated"] or "") >= week_ago]
    lines += ["", f"✅ Cerraste {len(done)} cosa(s) en los últimos 7 días"]
    overdue = core.overdue()
    if overdue:
        lines.append(f"⚠️ Atrasadas: {len(overdue)} → /tareas")
    return "\n".join(lines)


def build_monthly(core: Core, now: datetime | None = None) -> str:
    now = now or core.now()
    first = now.date().replace(day=1)
    last_day = _cal.monthrange(first.year, first.month)[1]
    rows = [r for r in _rows(core, first, last_day) if r.status != "hecha"]
    lines = [f"🗓 {MONTH_NAMES[first.month - 1].capitalize()} {first.year}", ""]
    if not rows:
        lines.append("Mes sin nada agendado.")
    else:
        by_cat: dict[str, int] = {}
        for r in rows:
            by_cat[r.category or "sin categoría"] = by_cat.get(r.category or "sin categoría", 0) + 1
        lines.append(f"{len(rows)} cosa(s): " + " · ".join(
            f"{ICON.get(c, '❓')} {n} {c}" for c, n in sorted(by_cat.items())))
        lines.append("")
        for r in rows[:20]:
            d = r.start.date() if r.start else r.day
            lines.append(f"  • {d:%d/%m} {_item(r).split(' ', 1)[1] if r.start else _item(r)}")
        if len(rows) > 20:
            lines.append(f"  … y {len(rows) - 20} más (/mes)")
    overdue = core.overdue()
    if overdue:
        lines += ["", f"⚠️ Atrasadas: {len(overdue)} → /tareas"]
    return "\n".join(lines)


def _until(delta: timedelta) -> str:
    mins = max(0, int(delta.total_seconds() // 60))
    return f"en {mins} min" if mins < 60 else f"en {mins // 60} h" + (f" {mins % 60} min" if mins % 60 else "")


def build_pulse(core: Core, now: datetime | None = None, *, empty_ok: bool = True) -> str | None:
    """Foto de "cómo está todo" a esta hora: lo que falta hoy, tareas, atrasadas y sin categoría.

    Devuelve None si no hay nada para mostrar y `empty_ok` es False (para no molestar con un vacío)."""
    now = now or core.now()
    today = now.date()
    todays = [r for r in _rows(core, today, 1) if r.status == "pendiente"]
    upcoming = [r for r in todays if r.start is None or r.start >= now - timedelta(minutes=15)]
    nxt = next((r for r in upcoming if r.start and r.start >= now), None)
    tasks = [t for t in core.tasks() if not (t.day == today)]
    overdue = core.overdue()
    inbox = core.inbox()
    if not (upcoming or tasks or overdue or inbox) and not empty_ok:
        return None
    lines = [f"🕑 {now:%H:%M} · así está todo"]
    if nxt:
        lines.append(f"▶ Próximo: {nxt.start:%H:%M} {ICON.get(nxt.category or '', '❓')} #{nxt.seq} "
                     f"{nxt.title} ({_until(nxt.start - now)})")
    if upcoming:
        lines += ["", f"📅 Falta hoy ({len(upcoming)})"]
        lines += [f"  • {_item(r)}" for r in upcoming[:8]]
    if tasks:
        lines += ["", f"📝 Tareas pendientes ({len(tasks)})"]
        lines += [f"  • #{t.seq} {t.title}" + (f" — {WEEKDAYS_ES[t.day.weekday()]} {t.day:%d/%m}" if t.day else "")
                  for t in tasks[:6]]
    if overdue:
        lines += ["", f"⚠️ Atrasadas ({len(overdue)})"]
        lines += [f"  • #{r.seq} {r.title}" for r in overdue[:5]]
    if inbox:
        lines += ["", f"❓ Sin categoría: {len(inbox)} → /inbox"]
    if len(lines) == 1:
        lines.append("Todo despejado por ahora ✨")
    return "\n".join(lines)


BUILDERS = {"daily": build_daily, "weekly": build_weekly, "monthly": build_monthly}


# ---------------------------------------------------------- programación
def _period_key(kind: str, now: datetime) -> str:
    if kind == "daily":
        return now.date().isoformat()
    if kind == "weekly":
        y, w, _ = now.isocalendar()
        return f"{y}-W{w:02d}"
    return f"{now.year}-{now.month:02d}"


def due_digests(core: Core, now: datetime | None = None) -> list[str]:
    now = now or core.now()
    cfg = core.cfg.digests
    if not cfg.enabled:
        return []
    out = []
    # diario: se manda desde la hora configurada y hasta 10 h después (no tiene sentido a la noche)
    at = parse_hhmm(cfg.daily)
    at_dt = datetime.combine(now.date(), at)
    if at_dt <= now < at_dt + timedelta(hours=10):
        out.append("daily")
    wd = WEEKDAY_ALIASES.get(cfg.weekly.day.strip().lower(), 6)
    if now.weekday() == wd and now.time() >= parse_hhmm(cfg.weekly.at):
        out.append("weekly")
    last = _cal.monthrange(now.year, now.month)[1]
    if now.day == min(cfg.monthly.day, last) and now.time() >= parse_hhmm(cfg.monthly.at):
        out.append("monthly")
    out = [k for k in out if core.db.kv_get(f"digest:{k}") != _period_key(k, now)]
    # pulsos periódicos: "pulse:HH:MM". Uno por hora configurada y por día; si la PC prendió tarde, solo el
    # más reciente dentro de la ventana (nunca una ráfaga de pulsos atrasados).
    window = timedelta(minutes=cfg.periodic_window_min)
    pulses = []
    for spec in cfg.periodic:
        try:
            t = parse_hhmm(spec)
        except (ValueError, AttributeError):
            continue
        if spec.strip() == cfg.daily.strip():
            continue                                       # a esa hora ya sale el resumen diario
        at_dt = datetime.combine(now.date(), t)
        if at_dt <= now < at_dt + window and core.db.kv_get(f"digest:pulse:{spec}") != now.date().isoformat():
            pulses.append((at_dt, f"pulse:{spec}"))
    if pulses:
        out.append(max(pulses)[1])
    return out


def mark_digest(core: Core, kind: str, now: datetime | None = None) -> None:
    now = now or core.now()
    if kind.startswith("pulse:"):
        # marca la hora disparada y también las anteriores del día (no se re-mandan atrasadas)
        for spec in core.cfg.digests.periodic:
            try:
                if parse_hhmm(spec) <= parse_hhmm(kind[6:]):
                    core.db.kv_set(f"digest:pulse:{spec}", now.date().isoformat())
            except (ValueError, AttributeError):
                continue
        return
    core.db.kv_set(f"digest:{kind}", _period_key(kind, now))
