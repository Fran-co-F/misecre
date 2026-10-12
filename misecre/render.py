"""Texto de respuestas: lo comparten el bot, la web y el operador."""
from __future__ import annotations

from .core import WEEKDAYS_ES, Result

ICON = {"trabajo": "💼", "personal": "🏠", "ocio": "🎮"}
TYPE_ICON = {"evento": "📅", "tarea": "☐", "nota": "🗒"}


def marker(r: Result) -> str:
    if r.status == "hecha":
        return "✅"
    if r.status == "borrada":
        return "🗑"
    return TYPE_ICON.get(r.type, "•")


def line(r: Result, with_day: bool = False) -> str:
    """Una línea para listas: #n icono título — cuándo [estado de calendario]."""
    icon = ICON.get(r.category or "", "❓")
    when = ""
    if r.start:
        when = f"{WEEKDAYS_ES[r.start.weekday()]} {r.start:%d/%m %H:%M}" if with_day else f"{r.start:%H:%M}"
    elif r.day:
        when = f"{WEEKDAYS_ES[r.day.weekday()]} {r.day:%d/%m}" if with_day else "todo el día"
    cal = ""
    if (r.start or r.day) and r.status != "borrada":
        if r.event_id:
            cal = " 📆"
        elif r.calendar_off:
            cal = " 🔔"                      # solo avisos: su categoría no va a Google
        else:
            cal = " ⚠️sin calendario" if r.category else " ❓sin categoría"
    return f"#{r.seq} {icon} {marker(r)} {r.title}" + (f" — {when}" if when else "") + cal


def list_text(rows: list[Result], title: str, with_day: bool = True) -> str:
    if not rows:
        return f"{title}: nada."
    out = [f"📋 {title}"]
    current = None
    for r in rows:
        d = r.start.date() if r.start else r.day
        if with_day and d and d != current:
            current = d
            out.append(f"\n{WEEKDAYS_ES[d.weekday()].capitalize()} {d:%d/%m}")
        out.append(line(r))
    return "\n".join(out)


def confirm(r: Result, verb: str = "Anoté") -> str:
    """Confirmación corta de una captura: lo que quedó, no lo que se pidió."""
    bits = [f"✓ {verb} #{r.seq} «{r.title}»"]
    if when := r.when_label():
        bits.append(when)
    if r.category:
        bits.append(f"{ICON.get(r.category, '')} {r.category}")
    elif r.type != "nota":
        bits.append("❓ sin categoría")
    if r.event_id:
        bits.append("📆")
    elif r.calendar_off and (r.start or r.day):
        bits.append("🔔 solo avisos")
    out = " · ".join(bits)
    if "hour" in r.pending and (opt := r.hour_options()):
        out += f"\n⚠️ Lo dejé a las {r.start:%H:%M}: ¿o era {opt[0]:%H:%M}? Decime «a las {opt[0].hour} de la mañana»."
    return out


def outcome(r: Result, actions: set[str]) -> str:
    """Confirmación de lo que se hizo sobre un ítem (arma el sistema, no el modelo: es exacta y gratis)."""
    if "deleted" in actions:
        return f"🗑 Borré #{r.seq} «{r.title}» (se puede deshacer)"
    if "created" in actions:
        return confirm(r, "Anoté")
    if "done" in actions:
        return f"✅ Marqué #{r.seq} «{r.title}» como hecho"
    if "reopen" in actions:
        return f"↩ Reabrí #{r.seq} «{r.title}»"
    if "restore" in actions:
        return f"♻️ Restauré #{r.seq} «{r.title}»"
    return confirm(r, "Actualicé")
