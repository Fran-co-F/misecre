"""Adaptador de Telegram: captura por voz/texto, CRUD completo y configuración.

Convenciones de interfaz
  * Cada captura tiene un número corto `#n`; todos los comandos lo aceptan ("/mover 12 el viernes").
  * Un mensaje de captura muestra solo las filas de botones que siguen pendientes; al tocar una,
    desaparece. Sin pendientes, el teclado se elimina.
  * `/editar #n` abre un panel con todo lo modificable.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from datetime import timedelta
from functools import wraps
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

from . import digest, learning
from .agent import Agent
from .core import TYPES, WEEKDAYS_ES, Core, Result
from .notify import Message
from .operator import Operator, Reply
from .runtime import Runtime

log = logging.getLogger("misecre.bot")
ICON = {"trabajo": "💼", "personal": "🏠", "ocio": "🎮"}
TYPE_ICON = {"evento": "📅", "tarea": "☐", "nota": "🗒"}
NO_KEYBOARD = InlineKeyboardMarkup([])

COMMANDS = [
    ("anotar", "Anotar sin IA: /anotar texto"), ("olvidar", "Borrar la memoria del chat con la IA"),
    ("hoy", "Agenda de hoy"), ("manana", "Agenda de mañana"), ("semana", "Próximos 7 días"),
    ("mes", "Próximos 30 días"), ("tareas", "Tareas pendientes"), ("inbox", "Lo que falta categorizar"),
    ("buscar", "Buscar: /buscar texto"), ("ver", "Ver una nota: /ver 12"),
    ("editar", "Editar una nota: /editar 12"), ("mover", "Mover: /mover 12 el viernes a las 10"),
    ("hecha", "Marcar hecha: /hecha 12"), ("borrar", "Borrar: /borrar 12"),
    ("restaurar", "Restaurar: /restaurar 12"), ("papelera", "Ver la papelera"),
    ("deshacer", "Deshacer la última acción"), ("resumen", "Resumen: /resumen hoy|semana|mes"),
    ("modo", "Quién responde primero: reglas o IA"), ("pulso", "Cómo está todo ahora"), ("calendario", "Qué categorías van a Google Calendar"),
    ("avisos", "Política de avisos"), ("silencio", "Horas de silencio"), ("pausa", "Pausar avisos"),
    ("reglas", "Reglas de clasificación"), ("sugerencias", "Reglas sugeridas"),
    ("sync", "Sincronizar ahora"), ("estado", "Estado del sistema"), ("ayuda", "Ayuda"),
]


# ============================================================== formato
def _fmt_when(r: Result) -> str:
    return r.when_label()


def _marker(r: Result) -> str:
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
        cal = " 📆" if r.event_id else (" ⚠️sin calendario" if r.category else " ❓sin categoría")
    return f"#{r.seq} {icon} {_marker(r)} {r.title}" + (f" — {when}" if when else "") + cal


def _format(r: Result) -> str:
    lines = []
    if r.source.endswith("voz"):
        lines.append(f"🗣 “{r.transcript}”\n")
    if r.category:
        mark = "✓" if "category" not in r.pending else "•"
        suffix = " (IA)" if r.auto_source == "ai" and "category" in r.pending else ""
        head = f"{mark} {ICON.get(r.category, '')} {r.category.capitalize()}{suffix} · {r.type}"
    else:
        head = f"❓ Sin categoría · {r.type}"
    lines += [f"#{r.seq} {head}", r.title]
    if when := r.when_label():
        lines.append(f"📅 {when}")
    if r.event_id:
        lines.append("📆 en tu calendario")
    elif r.when_label() and r.status != "borrada":
        lines.append("📆 (todavía no está en el calendario: falta categoría)" if not r.category
                     else "📆 (no se pudo crear en el calendario)")
    if r.status == "hecha":
        lines.append("✅ Hecha")
    if r.status == "borrada":
        lines.append("🗑 En la papelera")
    if "hour" in r.pending and (opt := r.hour_options()):
        lines.append(f"⚠️ ¿{opt[0]:%H:%M} o {opt[1]:%H:%M}?")
    if "time" in r.pending:
        lines.append("🕘 ¿Le ponemos hora o queda todo el día?")
    lines += [f"⚠️ {w}" for w in r.warnings]
    return "\n".join(lines)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _keyboard(core: Core, r: Result) -> InlineKeyboardMarkup:
    """Solo las filas que siguen pendientes; sin pendientes => sin teclado."""
    if not r.pending:
        return NO_KEYBOARD
    rows = []
    if "category" in r.pending:
        rows.append([
            _btn(f"{ICON.get(c, '•')} {c.capitalize()}" + (" ✓" if c == r.category else ""),
                 f"cat:{r.id}:{c}")
            for c in core.cfg.category_names
        ])
    if "hour" in r.pending and (opt := r.hour_options()):
        rows.append([_btn(f"🌅 {opt[0]:%H:%M}", f"hr:{r.id}:am"), _btn(f"🌆 {opt[1]:%H:%M}", f"hr:{r.id}:pm")])
    if "time" in r.pending:
        rows.append([_btn("🕘 Ponerle hora", f"tm:{r.id}:ask"), _btn("📅 Todo el día", f"tm:{r.id}:skip")])
    rows.append([_btn("🗑 Borrar", f"del:{r.id}")])
    return InlineKeyboardMarkup(rows)


def _edit_keyboard(core: Core, r: Result, sub: str | None = None) -> InlineKeyboardMarkup:
    if sub == "dur":
        return InlineKeyboardMarkup([
            [_btn(f"{m} min" if m < 60 else f"{m // 60} h" + (f" {m % 60}" if m % 60 else ""), f"ed:{r.id}:{m}")
             for m in (15, 30, 45, 60)],
            [_btn(f"{m // 60} h" + (" 30" if m % 60 else ""), f"ed:{r.id}:{m}") for m in (90, 120, 180, 240)],
            [_btn("↩ Volver", f"ev:{r.id}")],
        ])
    cats = [_btn(f"{ICON.get(c, '•')}" + (" ✓" if c == r.category else f" {c.capitalize()}"),
                 f"ec:{r.id}:{c}") for c in core.cfg.category_names]
    types = [_btn(f"{TYPE_ICON[t]} {t}" + (" ✓" if t == r.type else ""), f"et:{r.id}:{t}") for t in TYPES]
    rows = [
        [_btn("✏️ Título", f"ex:{r.id}:title"), _btn("📅 Cuándo", f"ex:{r.id}:when")],
        cats, types,
    ]
    if r.start:
        rows.append([_btn("⏱ Duración", f"ex:{r.id}:dur")])
    rows.append([
        _btn("↩ Reabrir" if r.status == "hecha" else "✅ Hecha", f"eh:{r.id}"),
        _btn("🗑 Borrar", f"eq:{r.id}"),
    ])
    return InlineKeyboardMarkup(rows)


def _tasks_view(core: Core) -> tuple[str, InlineKeyboardMarkup]:
    tasks = core.tasks()
    if not tasks:
        return "✅ No tenés tareas pendientes.", NO_KEYBOARD
    overdue_ids = {t.id for t in core.overdue()}
    lines = [f"📝 Tareas pendientes ({len(tasks)})"]
    buttons = []
    for t in tasks:
        due = f" — {WEEKDAYS_ES[t.day.weekday()]} {t.day:%d/%m}" if t.day else ""
        late = " ⚠️atrasada" if t.id in overdue_ids else ""
        lines.append(f"#{t.seq} {ICON.get(t.category or '', '❓')} {t.title}{due}{late}")
        buttons.append(_btn(f"✅ #{t.seq}", f"done:{t.id}"))
    return "\n".join(lines), InlineKeyboardMarkup([buttons[i:i + 4] for i in range(0, len(buttons), 4)])


def _parse_duration(text: str) -> int:
    """'30', '30m', '2h', '1h30', '90 min' -> minutos."""
    t = text.strip().lower().replace(",", ".")
    m = re.fullmatch(r"(\d+)\s*h(?:oras?)?\s*(\d+)?\s*(?:m|min|minutos?)?", t)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    m = re.fullmatch(r"(\d+)\s*(?:m|min|minutos?)?", t)
    if m:
        return int(m.group(1))
    raise ValueError("No entendí la duración. Probá con “45”, “90 min” o “2h”.")


HELP = """\
💬 *Hablame normal* (texto o audio): “mete un recordatorio de comprar pan”, “ya fui a comprar pan”,
“pasame lo del dentista para el viernes”, “¿qué tengo mañana?”. Con la IA activa opero la agenda por vos
y te cuento qué hice (botón ↩ para deshacer). Sin IA, anoto con reglas y te pregunto lo dudoso.
/anotar texto → anotar sin IA · /olvidar → borrar la memoria del chat

*Ver*  /hoy /manana /semana /mes /tareas /inbox /papelera
*Buscar*  /buscar texto · /ver 12
*Editar*  /editar 12 · /mover 12 el viernes a las 10 · /titulo 12 nuevo título
  /duracion 12 90 · /hecha 12 · /reabrir 12 · /borrar 12 · /restaurar 12 · /deshacer
*Resumen*  /resumen hoy|semana|mes
*Avisos*  /avisos · /avisos trabajo 15,5 · /avisos ocio resumen · /silencio 23:00-07:00 · /pausa 2h
*Reglas*  /reglas · /regla ocio paddle · /regla quitar ocio juego · /sugerencias
*Sistema*  /sync · /estado
"""


# ================================================================ bot
async def register_commands(app: Application) -> bool:
    """Publica el menú de comandos (lo que aparece al escribir "/", como BotFather)."""
    try:
        await app.bot.set_my_commands([BotCommand(c, d[:256]) for c, d in COMMANDS])
        return True
    except Exception:  # noqa: BLE001
        log.warning("no pude registrar el menú de comandos", exc_info=True)
        return False


def telegram_channel(app: Application, chat_ids: list[int]):
    """Canal de notificación hacia todos los chats permitidos."""
    async def send(msg: Message) -> None:
        markup = None
        if msg.buttons:
            markup = InlineKeyboardMarkup([[_btn(t, d) for t, d in row] for row in msg.buttons])
        for cid in chat_ids:
            await app.bot.send_message(cid, msg.text, reply_markup=markup)
    return send


def build_bot(core: Core, runtime: Runtime, agent: Agent | None = None,
              operator: Operator | None = None) -> Application | None:
    token = os.environ.get("MISECRE_TELEGRAM_TOKEN")
    if not token:
        return None
    allowed = set(core.cfg.telegram.allowed_chat_ids)
    rem = runtime.reminders
    agent = agent or Agent(core)
    operator = operator or Operator(core, agent)

    def ok(update: Update) -> bool:
        return update.effective_chat is not None and update.effective_chat.id in allowed

    def cmd(fn):
        """Autoriza y traduce errores de dominio a mensajes claros."""
        @wraps(fn)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not ok(update):
                return
            try:
                await fn(update, context)
            except KeyError as e:
                ref = str(e.args[0]).lstrip("#")
                await update.message.reply_text(f"No encontré #{ref}.")
            except (ValueError, LookupError) as e:
                await update.message.reply_text(f"⚠️ {e}")
            except Exception:  # noqa: BLE001
                log.exception("comando")
                await update.message.reply_text("⚠️ Algo falló (mirá el log).")
        return wrapper

    async def render(bot, chat_id: int, message_id: int, text: str, markup=None):
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def show(update: Update, r: Result):
        await update.message.reply_text(_format(r), reply_markup=_keyboard(core, r))

    def arg_ref(context) -> str:
        if not context.args:
            raise ValueError("Falta el número: por ejemplo /ver 12")
        return context.args[0]

    # ----------------------------------------------------- básicos
    async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
        cid = update.effective_chat.id
        if ok(update):
            await update.message.reply_text("Listo. Mandame una nota de voz o texto. /ayuda para ver todo.")
        else:
            await update.message.reply_text(
                f"Tu chat_id es {cid}. Agregalo a telegram.allowed_chat_ids en config.yaml y reiniciá.")

    @cmd
    async def ayuda(update, _):
        await update.message.reply_text(HELP, parse_mode="Markdown")

    # ------------------------------------------------------ captura
    async def answer_pending_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
        w = context.user_data.get("await")
        if not w:
            return False
        try:
            if w["kind"] == "time":
                r = await asyncio.to_thread(core.set_time, w["id"], text)
                ack = f"✓ Hora puesta: {r.when_label()}"
            elif w["kind"] == "title":
                r = await asyncio.to_thread(core.update_title, w["id"], text)
                ack = f"✓ Título: {r.title}"
            else:
                r = await asyncio.to_thread(core.reschedule, w["id"], text)
                ack = f"✓ Movida a {r.when_label()}"
        except (ValueError, KeyError) as e:
            await update.message.reply_text(f"⚠️ {e}. Probá de nuevo o /cancelar.")
            return True
        context.user_data.pop("await", None)
        markup = _edit_keyboard(core, r) if w.get("edit") else _keyboard(core, r)
        await render(context.bot, w["chat_id"], w["message_id"], _format(r), markup)
        await update.message.reply_text(ack)
        return True

    @cmd
    async def cancelar(update, context):
        context.user_data.pop("await", None)
        await update.message.reply_text("Listo, cancelado.")

    async def send_reply(update: Update, reply: Reply, prefix: str = "") -> None:
        extra = "".join(f"\n{x}" for x in reply.learned)
        if reply.notice:
            prefix = f"{reply.notice}\n\n{prefix}"
        if reply.capture is not None:                              # hay algo que resolver con botones
            r = reply.capture
            await update.message.reply_text(prefix + _format(r) + extra, reply_markup=_keyboard(core, r))
            return
        rows = [[_btn(f"✅ #{c.seq} {c.title[:34]}", f"pick:{c.id}")] for c in reply.candidates]
        if reply.mutations:
            rows.append([_btn(f"↩ Deshacer ({reply.mutations})", f"agundo:{reply.mutations}:{reply.undo_to}")])
        tag = " 🤖" if reply.source == "ia" else ""
        await update.message.reply_text(prefix + reply.text + tag + extra,
                                        reply_markup=InlineKeyboardMarkup(rows) if rows else None)

    async def operator_turn(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                            source: str) -> None:
        """Reglas primero; la IA solo si las reglas no resuelven. Siempre responde algo útil."""
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id, "typing")
        try:
            reply = await asyncio.to_thread(operator.handle, f"tg:{chat_id}", text, source)
        except ValueError as e:
            await update.message.reply_text(f"⚠️ {e}")
            return
        await send_reply(update, reply)

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ok(update):
            return
        if await answer_pending_input(update, context, update.message.text):
            return
        await operator_turn(update, context, update.message.text, "telegram")

    async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ok(update):
            return
        media = update.message.voice or update.message.audio
        status = await update.message.reply_text("🎙 Transcribiendo…")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "voice.ogg"
            await (await media.get_file()).download_to_drive(path)
            try:
                text = await asyncio.to_thread(core.transcribe, path)
            except ValueError as e:
                await status.edit_text(f"⚠️ {e}")
                return
            except Exception:  # noqa: BLE001
                log.exception("fallo procesando audio")
                await status.edit_text("⚠️ Error procesando el audio (mirá el log).")
                return
        await status.edit_text(f"🗣 “{text}”")
        if context.user_data.get("await"):
            await answer_pending_input(update, context, text)
            return
        await operator_turn(update, context, text, "telegram-voz")

    @cmd
    async def anotar(update, context):
        """Anota solo con reglas (sin IA), mostrando los botones de lo dudoso."""
        text = " ".join(context.args)
        if not text:
            raise ValueError("Escribí qué anotar: /anotar comprar pan mañana")
        await show(update, await asyncio.to_thread(core.capture_text, text, "telegram", None, False))

    @cmd
    async def olvidar(update, _):
        n = agent.forget(f"tg:{update.effective_chat.id}")
        await update.message.reply_text(f"🧹 Listo, borré la memoria del chat ({n} mensajes).")

    # --------------------------------------------------- consultas
    async def _agenda(update: Update, days: int, label: str, start_offset: int = 0):
        day = core.today() + timedelta(days=start_offset)
        rows = [core._from_row(r) for r in core.agenda(day, days)]
        if not rows:
            await update.message.reply_text(f"Nada agendado para {label}.")
            return
        out = [f"📋 {label}"]
        current = None
        for r in rows:
            d = r.start.date() if r.start else r.day
            if days > 1 and d != current:
                current = d
                out.append(f"\n{WEEKDAYS_ES[d.weekday()].capitalize()} {d:%d/%m}")
            out.append(line(r))
        await update.message.reply_text("\n".join(out))

    @cmd
    async def hoy(update, _):
        await _agenda(update, 1, "hoy")

    @cmd
    async def manana(update, _):
        await _agenda(update, 1, "mañana", 1)

    @cmd
    async def semana(update, _):
        await _agenda(update, 7, "los próximos 7 días")

    @cmd
    async def mes(update, _):
        await _agenda(update, 30, "los próximos 30 días")

    @cmd
    async def tareas(update, _):
        text, kb = _tasks_view(core)
        await update.message.reply_text(text, reply_markup=kb)

    @cmd
    async def inbox(update, _):
        rows = core.inbox()
        if not rows:
            await update.message.reply_text("📭 Nada sin categoría.")
            return
        buttons = [[_btn(f"#{r.seq} · {r.title[:28]}", f"ver:{r.id}")] for r in rows[:10]]
        await update.message.reply_text(
            f"❓ Sin categoría ({len(rows)})\n" + "\n".join(line(r) for r in rows[:10]),
            reply_markup=InlineKeyboardMarkup(buttons))

    @cmd
    async def buscar(update, context):
        q = " ".join(context.args)
        if not q:
            raise ValueError("Escribí qué buscar: /buscar dentista")
        rows = core.search(q)
        await update.message.reply_text(
            f"🔎 {len(rows)} resultado(s) para “{q}”\n" + "\n".join(line(r, True) for r in rows)
            if rows else f"🔎 Sin resultados para “{q}”.")

    @cmd
    async def ver(update, context):
        r = core.get(arg_ref(context))
        await update.message.reply_text(_format(r) + f"\n🗣 “{r.transcript}”",
                                        reply_markup=_edit_keyboard(core, r))

    @cmd
    async def papelera(update, _):
        rows = core.trash()
        if not rows:
            await update.message.reply_text("🗑 La papelera está vacía.")
            return
        kb = InlineKeyboardMarkup([[_btn(f"♻️ #{r.seq} {r.title[:24]}", f"rst:{r.id}")] for r in rows])
        await update.message.reply_text("🗑 Papelera (tocá para restaurar)", reply_markup=kb)

    # ------------------------------------------------ modificar
    @cmd
    async def editar(update, context):
        r = core.get(arg_ref(context))
        await update.message.reply_text(_format(r), reply_markup=_edit_keyboard(core, r))

    @cmd
    async def mover(update, context):
        ref = arg_ref(context)
        when = " ".join(context.args[1:])
        if not when:
            raise ValueError("Decime cuándo: /mover 12 el viernes a las 10")
        r = await asyncio.to_thread(core.reschedule, ref, when)
        await show(update, r)

    @cmd
    async def titulo(update, context):
        ref = arg_ref(context)
        r = await asyncio.to_thread(core.update_title, ref, " ".join(context.args[1:]))
        await show(update, r)

    @cmd
    async def duracion(update, context):
        ref = arg_ref(context)
        r = await asyncio.to_thread(core.set_duration, ref, _parse_duration(" ".join(context.args[1:])))
        await show(update, r)

    @cmd
    async def hecha(update, context):
        await show(update, await asyncio.to_thread(core.complete, arg_ref(context)))

    @cmd
    async def reabrir(update, context):
        await show(update, await asyncio.to_thread(core.reopen, arg_ref(context)))

    @cmd
    async def borrar(update, context):
        r = core.get(arg_ref(context))
        await asyncio.to_thread(core.delete, r.id)
        await update.message.reply_text(f"🗑 #{r.seq} {r.title} → papelera. (/deshacer o /restaurar {r.seq})")

    @cmd
    async def restaurar(update, context):
        await show(update, await asyncio.to_thread(core.restore, arg_ref(context)))

    @cmd
    async def deshacer(update, _):
        action, r = await asyncio.to_thread(core.undo)
        if r is None:
            await update.message.reply_text(f"↩ Deshice: {action}. (la captura quedó en la papelera)")
        else:
            await update.message.reply_text(f"↩ Deshice: {action}\n\n{_format(r)}")

    # ------------------------------------------------ resumen/config
    @cmd
    async def resumen(update, context):
        which = (context.args[0].lower() if context.args else "hoy")
        kind = {"hoy": "daily", "dia": "daily", "día": "daily", "semana": "weekly", "mes": "monthly"}.get(which)
        if kind is None:
            raise ValueError("Usá /resumen hoy, /resumen semana o /resumen mes")
        await update.message.reply_text(digest.BUILDERS[kind](core))

    @cmd
    async def avisos(update, context):
        if context.args:
            cat = context.args[0].lower()
            spec = " ".join(context.args[1:]).lower().strip()
            if cat not in core.cfg.category_names:
                raise ValueError(f"Categoría inválida. Opciones: {', '.join(core.cfg.category_names)}")
            if not spec:
                raise ValueError("Ej: /avisos trabajo 15,5 · /avisos ocio resumen · /avisos ocio ninguno")
            if spec in ("resumen", "solo resumen", "digest"):
                rem.set_policy(cat, [], True)
            elif spec in ("ninguno", "nada", "off"):
                rem.set_policy(cat, [], False)
            else:
                mins = [int(x) for x in re.split(r"[,\s]+", spec.replace("min", "").strip()) if x]
                rem.set_policy(cat, mins, False)
        out = ["🔔 Avisos"]
        for c in core.cfg.category_names:
            p = rem.policy_for(c)
            what = "solo en el resumen" if p.digest_only else (
                ", ".join(f"{m} min antes" for m in p.before) if p.before else "sin avisos")
            out.append(f"{ICON.get(c, '•')} {c}: {what}")
        out.append(f"🤫 Silencio: {rem.quiet_spec()}")
        until = rem.paused_until(core.now())
        if until:
            out.append(f"⏸ En pausa hasta las {until:%H:%M}")
        await update.message.reply_text("\n".join(out))

    @cmd
    async def calendario(update, context):
        a = [x.lower() for x in context.args]
        if len(a) == 2 and a[1] in ("on", "off", "si", "no"):
            warns = await asyncio.to_thread(core.set_calendar_for, a[0], a[1] in ("on", "si"))
            if warns:
                await update.message.reply_text("⚠️ " + "; ".join(warns[:2]))
        elif a:
            raise ValueError("Usá /calendario ocio off  (o on)")
        out = ["📆 Calendario de Google por categoría"]
        for c in core.cfg.category_names:
            on = core.calendar_wanted(c)
            p = rem.policy_for(c)
            avis = "solo en el resumen" if p.digest_only else (
                ", ".join(f"{m} min antes" for m in p.before) if p.before else "sin avisos")
            out.append(f"{ICON.get(c, '•')} {c}: {'✅ va al calendario' if on else '🔔 solo avisos'} · avisos: {avis}")
        out.append("\nCambiar: /calendario ocio off · avisos: /avisos ocio 30,10")
        await update.message.reply_text("\n".join(out))

    @cmd
    async def modo(update, context):
        from .operator import MODES

        if context.args:
            operator.set_mode(context.args[0])
        cur = operator.mode
        out = [f"🧭 Responde primero: {MODES[cur]}", ""]
        out += [f"{'▶' if k == cur else '  '} {k}: {v}" for k, v in MODES.items()]
        out += ["", "Cambiar: /modo reglas · /modo ia · /modo solo-reglas"]
        if cur != "rules_only" and not agent.available:
            out.append(f"⚠️ La IA no está activa ({agent.why_unavailable()}): se usan solo reglas.")
        await update.message.reply_text("\n".join(out))

    @cmd
    async def pulso(update, _):
        await update.message.reply_text(digest.build_pulse(core, empty_ok=True))

    @cmd
    async def silencio(update, context):
        if not context.args:
            await update.message.reply_text(f"🤫 Silencio: {rem.quiet_spec()}\nCambiar: /silencio 23:00-07:00 · /silencio off")
            return
        rem.set_quiet(context.args[0])
        await update.message.reply_text(f"🤫 Silencio: {rem.quiet_spec()}")

    @cmd
    async def pausa(update, context):
        if not context.args or context.args[0].lower() in ("off", "no", "reanudar"):
            rem.resume()
            await update.message.reply_text("▶️ Avisos reanudados.")
            return
        t = context.args[0].lower()
        m = re.fullmatch(r"(\d+)\s*(h|m|min)?", t)
        if not m:
            raise ValueError("Usá /pausa 30m, /pausa 2h o /pausa off")
        minutes = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
        until = rem.pause(minutes)
        await update.message.reply_text(f"⏸ Sin avisos hasta las {until:%H:%M}. (/pausa off para reanudar)")

    @cmd
    async def reglas(update, _):
        out = ["📚 Reglas de clasificación"]
        for c, words in core.rules_view().items():
            out.append(f"\n{ICON.get(c, '•')} {c} ({len(words)})\n" + ", ".join(words))
        out.append("\nAgregar: /regla ocio paddle · Quitar: /regla quitar ocio juego")
        await update.message.reply_text("\n".join(out))

    @cmd
    async def regla(update, context):
        a = [x.lower() for x in context.args]
        if len(a) >= 3 and a[0] in ("quitar", "borrar", "sacar"):
            await asyncio.to_thread(core.remove_rule, a[1], " ".join(a[2:]))
            await update.message.reply_text(f"➖ Quité “{' '.join(a[2:])}” de {a[1]}")
        elif len(a) >= 2:
            await asyncio.to_thread(core.add_rule, a[0], " ".join(a[1:]))
            await update.message.reply_text(f"➕ Agregué “{' '.join(a[1:])}” a {a[0]}")
        else:
            raise ValueError("Ej: /regla ocio paddle · /regla quitar ocio juego")

    @cmd
    async def sugerencias(update, _):
        sug = learning.suggest_rules(core)
        acc = learning.accuracy(core)
        head = f"🎯 Acierto reciente: {acc[0]}/{acc[1]}" if acc else "🎯 Todavía no hay correcciones."
        if not sug:
            await update.message.reply_text(head + "\nNo tengo reglas nuevas para sugerir.")
            return
        rows, lines = [], [head, "\n💡 Reglas sugeridas (por tus correcciones):"]
        for s in sug[:6]:
            lines.append(f"• “{s.word}” → {s.category} ({s.count} veces)")
            rows.append([_btn(f"➕ {s.word} → {s.category}", f"rule:{s.category}:{s.word}"),
                         _btn("✖", f"norule:{s.category}:{s.word}")])
        await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    @cmd
    async def sync(update, _):
        msg = await update.message.reply_text("🔄 Sincronizando…")
        rep = await asyncio.to_thread(runtime.syncer.run)
        await msg.edit_text(rep.text())

    @cmd
    async def estado(update, _):
        s = core.stats()
        now = core.now()
        out = ["🩺 Estado",
               f"📚 {s['activas']} activas · {s['pendientes']} pendientes · {s['hechas']} hechas",
               f"📆 Calendario: {'activo' if s['calendario_activo'] else '⚠️ DESACTIVADO (calendar.enabled)'} — "
               f"{s['en_calendario']} en calendario, {s['sin_calendario']} con fecha sin evento"]
        if s["sin_categoria"]:
            out.append(f"❓ Sin categoría: {s['sin_categoria']} (/inbox)")
        last = core.db.kv_get("sync:last")
        out.append(f"🔄 Última sincronización: {last[11:16] + ' ' + last[:10] if last else 'nunca'}")
        last = getattr(core.transcriber, "last", None) or {}
        prov = last.get("provider") or core.cfg.stt.provider
        out.append(f"🎙 Voz: {prov if prov != 'local' else core.cfg.stt.model}" + (
            f" · último audio: {last['took_s']}s" if last else ""))
        st = operator.stats_today()
        out.append(f"🧭 Modo {operator.mode} · hoy resolví {st['reglas']} con reglas y {st['ia']} con IA")
        if agent.available:
            u = agent.usage_today()
            out.append(f"🤖 Agente: {core.cfg.ai.model} · hoy {u['calls']}/"
                       f"{core.cfg.ai.agent_max_calls_per_day} llamadas · ~US${u['usd']:.4f}")
        else:
            out.append(f"🤖 Agente: apagado ({agent.why_unavailable()}) — anoto con reglas")
        silenced = rem.silenced(now)
        out.append(f"🔔 Avisos: {'⏸ ' + silenced if silenced else 'activos'} · silencio {rem.quiet_spec()}")
        acc = learning.accuracy(core)
        if acc:
            out.append(f"🎯 Acierto de categoría: {acc[0]}/{acc[1]}")
        if (n := len(learning.suggest_rules(core))):
            out.append(f"💡 {n} regla(s) sugerida(s): /sugerencias")
        await update.message.reply_text("\n".join(out))

    # -------------------------------------------------- callbacks
    async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not ok(update):
            await q.answer()
            return
        kind, cid, *rest = q.data.split(":")
        msg = q.message
        chat, mid = msg.chat_id, msg.message_id
        arg = rest[0] if rest else None

        async def redraw(r: Result, edit: bool = False, sub: str | None = None):
            markup = _edit_keyboard(core, r, sub) if edit else _keyboard(core, r)
            await render(context.bot, chat, mid, _format(r), markup)

        async def ask(kind_: str, prompt: str, edit: bool = False):
            context.user_data["await"] = {"kind": kind_, "id": cid, "chat_id": chat, "message_id": mid,
                                          "edit": edit}
            await q.answer()
            await msg.reply_text(prompt + " (o /cancelar)")

        try:
            # --- mensaje de captura (teclado de pendientes)
            if kind == "cat":
                await redraw(await asyncio.to_thread(core.set_category, cid, arg))
                await q.answer(f"{arg.capitalize()} ✓")
            elif kind == "hr":
                await redraw(await asyncio.to_thread(core.set_hour, cid, arg))
                await q.answer("Hora actualizada")
            elif kind == "tm" and arg == "skip":
                await redraw(await asyncio.to_thread(core.skip_time, cid))
                await q.answer("Todo el día")
            elif kind == "tm" and arg == "ask":
                await ask("time", "¿A qué hora? Escribila o mandá un audio (ej: “18:30”).")
            elif kind == "del":
                await asyncio.to_thread(core.delete, cid)
                await render(context.bot, chat, mid, "🗑 Borrada. (/deshacer para recuperarla)", NO_KEYBOARD)
                await q.answer()
            # --- panel de edición
            elif kind == "ver":
                await msg.reply_text(_format(core.get(cid)), reply_markup=_edit_keyboard(core, core.get(cid)))
                await q.answer()
            elif kind == "ev":
                await redraw(core.get(cid), edit=True)
                await q.answer()
            elif kind == "ec":
                await redraw(await asyncio.to_thread(core.set_category, cid, arg), edit=True)
                await q.answer(f"{arg.capitalize()} ✓")
            elif kind == "et":
                await redraw(await asyncio.to_thread(core.set_type, cid, arg), edit=True)
                await q.answer(f"Tipo: {arg}")
            elif kind == "ex" and arg == "title":
                await ask("title", "¿Cuál es el nuevo título?", edit=True)
            elif kind == "ex" and arg == "when":
                await ask("when", "¿Para cuándo? (ej: “el viernes a las 10”, “mañana”, “sin hora”)", edit=True)
            elif kind == "ex" and arg == "dur":
                await redraw(core.get(cid), edit=True, sub="dur")
                await q.answer()
            elif kind == "ed":
                await redraw(await asyncio.to_thread(core.set_duration, cid, int(arg)), edit=True)
                await q.answer("Duración actualizada")
            elif kind == "eh":
                r = core.get(cid)
                r = await asyncio.to_thread(core.reopen if r.status == "hecha" else core.complete, cid)
                await redraw(r, edit=True)
                await q.answer("Hecha ✅" if r.status == "hecha" else "Reabierta")
            elif kind == "eq":
                await asyncio.to_thread(core.delete, cid)
                await render(context.bot, chat, mid, "🗑 Borrada. (/deshacer para recuperarla)", NO_KEYBOARD)
                await q.answer()
            # --- listas
            elif kind == "done":
                await asyncio.to_thread(core.complete, cid)
                text, kb = _tasks_view(core)
                await render(context.bot, chat, mid, text, kb)
                await q.answer("Hecha ✅")
            elif kind == "rst":
                r = await asyncio.to_thread(core.restore, cid)
                await render(context.bot, chat, mid, f"♻️ Restaurada: #{r.seq} {r.title}", NO_KEYBOARD)
                await q.answer("Restaurada")
            # --- elegir cuál de varios candidatos completar
            elif kind == "pick":
                r = await asyncio.to_thread(core.complete, cid)
                await render(context.bot, chat, mid, f"✅ Marqué #{r.seq} «{r.title}» como hecho", NO_KEYBOARD)
                await q.answer("Hecha ✅")
            # --- deshacer lo que hizo el agente
            elif kind == "agundo":
                done = 0
                expected = int(rest[0]) if rest else 0
                if expected and core.db.history_last_id() != expected:
                    # Ya pasaron otras cosas después: deshacer "lo último" tocaría algo ajeno.
                    await q.answer("Ya hubo otros cambios después; usá /deshacer o /ver", show_alert=True)
                    try:
                        await context.bot.edit_message_reply_markup(chat, mid, reply_markup=NO_KEYBOARD)
                    except BadRequest:
                        pass
                    return
                for _ in range(int(cid)):
                    try:
                        await asyncio.to_thread(core.undo)
                        done += 1
                    except LookupError:
                        break
                await q.answer(f"Deshice {done} cambio(s)")
                try:
                    await context.bot.edit_message_reply_markup(chat, mid, reply_markup=NO_KEYBOARD)
                except BadRequest:
                    pass
                await msg.reply_text(f"↩ Deshice {done} cambio(s).")
            # --- avisos
            elif kind in ("snz", "rdone"):
                if kind == "snz":
                    fire = await asyncio.to_thread(rem.snooze, cid)
                    await q.answer(f"Te aviso a las {fire:%H:%M}")
                else:
                    await asyncio.to_thread(core.complete, cid)
                    await q.answer("Hecha ✅")
                rows = [row for row in (msg.reply_markup.inline_keyboard if msg.reply_markup else [])
                        if not any((b.callback_data or "").endswith(cid) for b in row)]
                try:
                    await context.bot.edit_message_reply_markup(
                        chat, mid, reply_markup=InlineKeyboardMarkup(rows) if rows else NO_KEYBOARD)
                except BadRequest:
                    pass
            # --- reglas sugeridas
            elif kind == "rule":
                word = ":".join(rest)
                await asyncio.to_thread(core.add_rule, cid, word)
                await q.answer(f"“{word}” → {cid}")
                await _drop_row(context, msg, q.data)
            elif kind == "norule":
                learning.reject(core, cid, ":".join(rest))
                await q.answer("Descartada")
                await _drop_row(context, msg, q.data)
            else:
                await q.answer()
        except KeyError:
            await q.answer("Esa captura ya no existe", show_alert=True)
        except (ValueError, LookupError) as e:
            await q.answer(str(e)[:150], show_alert=True)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
            await q.answer()

    async def _drop_row(context, msg, data: str):
        word = data.split(":", 2)[2]
        rows = [row for row in (msg.reply_markup.inline_keyboard if msg.reply_markup else [])
                if not any((b.callback_data or "").endswith(f":{word}") for b in row)]
        try:
            await context.bot.edit_message_reply_markup(
                msg.chat_id, msg.message_id, reply_markup=InlineKeyboardMarkup(rows) if rows else NO_KEYBOARD)
        except BadRequest:
            pass

    async def on_error(_: object, context: ContextTypes.DEFAULT_TYPE):
        log.error("error en handler", exc_info=context.error)

    # Pool más holgado y timeouts explícitos: con el default (1 conexión para el polling) aparecían
    # "Pool timeout: all connections in the pool are occupied" cuando la red parpadeaba.
    app = (Application.builder().token(token)
           .connection_pool_size(16).pool_timeout(20.0).connect_timeout(15.0)
           .read_timeout(30.0).write_timeout(30.0)
           .get_updates_connection_pool_size(4).get_updates_pool_timeout(20.0)
           .get_updates_read_timeout(30.0).build())
    app.add_error_handler(on_error)
    handlers = {
        "start": start, "ayuda": ayuda, "help": ayuda, "cancelar": cancelar,
        "anotar": anotar, "olvidar": olvidar, "pulso": pulso, "calendario": calendario, "modo": modo,
        "hoy": hoy, "manana": manana, "semana": semana, "mes": mes,
        "tareas": tareas, "inbox": inbox, "buscar": buscar, "ver": ver, "papelera": papelera,
        "editar": editar, "mover": mover, "titulo": titulo, "duracion": duracion, "hecha": hecha,
        "reabrir": reabrir, "borrar": borrar, "restaurar": restaurar, "deshacer": deshacer,
        "resumen": resumen, "avisos": avisos, "silencio": silencio, "pausa": pausa, "reglas": reglas,
        "regla": regla, "sugerencias": sugerencias, "sync": sync, "estado": estado,
    }
    for name, fn in handlers.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(callback))
    return app
