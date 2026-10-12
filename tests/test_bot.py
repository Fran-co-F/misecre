"""Prueba los handlers del bot con objetos de Telegram simulados (sin red)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler

from misecre.core import Core
from misecre.notify import Notifier
from misecre.runtime import Runtime
from misecre.telegram_bot import (
    COMMANDS, _edit_keyboard, _format, _keyboard, _parse_duration, _tasks_view, build_bot, line,
)

from .fakes import FakeCalendar

CHAT = 1


@pytest.fixture
def env(cfg, monkeypatch):
    monkeypatch.setenv("MISECRE_TELEGRAM_TOKEN", "123456:TEST")
    cfg.telegram.allowed_chat_ids = [CHAT]
    core = Core(cfg, calendar=FakeCalendar())
    rt = Runtime(core, Notifier([]))
    app = build_bot(core, rt)
    handlers = {}
    for h in app.handlers[0]:
        if isinstance(h, CommandHandler):
            for c in h.commands:
                handlers[c] = h.callback
        elif isinstance(h, CallbackQueryHandler):
            handlers["__callback__"] = h.callback
    return SimpleNamespace(core=core, rt=rt, app=app, h=handlers)


def mk_update(chat=CHAT):
    reply = SimpleNamespace(edit_text=AsyncMock())
    msg = SimpleNamespace(reply_text=AsyncMock(return_value=reply), text="")
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat), message=msg), msg


def mk_context(args=()):
    return SimpleNamespace(args=list(args), user_data={}, bot=AsyncMock())


def say(env, name, *args, chat=CHAT, context=None):
    update, msg = mk_update(chat)
    asyncio.run(env.h[name](update, context or mk_context(args)))
    return [c.args[0] for c in msg.reply_text.await_args_list], msg


def press(env, data, context=None, reply_markup=None):
    q = SimpleNamespace(
        data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
        message=SimpleNamespace(chat_id=CHAT, message_id=7, reply_markup=reply_markup,
                                reply_text=AsyncMock()))
    update = SimpleNamespace(callback_query=q, effective_chat=SimpleNamespace(id=CHAT))
    ctx = context or mk_context()
    asyncio.run(env.h["__callback__"](update, ctx))
    edits = [c for c in ctx.bot.edit_message_text.await_args_list]
    return q, ctx, edits


def capture(env, text="reunión con el cliente el jueves a las 10"):
    return env.core.capture_text(text)


# ----------------------------------------------------------------- seguridad
def test_ignora_chats_no_autorizados(env):
    capture(env)
    out, _ = say(env, "hoy", chat=999)
    assert out == []
    out, _ = say(env, "start", chat=999)
    assert "chat_id es 999" in out[0]


def test_todos_los_comandos_del_menu_tienen_handler(env):
    missing = [c for c, _ in COMMANDS if c not in env.h]
    assert missing == []


# ---------------------------------------------------------------- consultas
def test_buscar_ver_y_numeros(env):
    r = capture(env)
    out, _ = say(env, "buscar", "cliente")
    assert "#1" in out[0] and "Reunión con el cliente" in out[0]
    out, _ = say(env, "ver", "#1")
    assert "Reunión con el cliente" in out[0] and "🗣" in out[0]
    out, _ = say(env, "ver", "99")
    assert out == ["No encontré #99."]
    out, _ = say(env, "ver")
    assert "Falta el número" in out[0]
    assert r.seq == 1


def test_listas_marcan_estado_de_calendario(env):
    env.core.capture_text("reunión con el cliente hoy a las 23")
    env.core.capture_text("comprar pan hoy a las 23")                 # sin categoría
    out, _ = say(env, "hoy")
    assert "📆" in out[0] and "sin categoría" in out[0]


# ------------------------------------------------------------------- CRUD
def test_mover_titulo_duracion_hecha_borrar_restaurar_deshacer(env):
    capture(env)
    out, _ = say(env, "mover", "1", "el", "viernes", "a", "las", "15")
    assert "vie" in out[0] and "15:00" in out[0]
    out, _ = say(env, "titulo", "1", "Reunión", "con", "Marcos")
    assert "Reunión con Marcos" in out[0]
    out, _ = say(env, "duracion", "1", "2h")
    assert "15:00–17:00" in out[0]
    out, _ = say(env, "hecha", "1")
    assert "✅ Hecha" in out[0]
    out, _ = say(env, "reabrir", "1")
    assert "Hecha" not in out[0]
    out, _ = say(env, "borrar", "1")
    assert "papelera" in out[0]
    out, _ = say(env, "papelera")
    assert "Papelera" in out[0]
    out, _ = say(env, "restaurar", "1")
    assert "Reunión con Marcos" in out[0]
    out, _ = say(env, "deshacer")
    assert "Deshice: restaurar" in out[0]


def test_errores_son_mensajes_claros(env):
    capture(env)
    out, _ = say(env, "mover", "1", "cuando", "puedas")
    assert out[0].startswith("⚠️ No entendí cuándo")
    out, _ = say(env, "duracion", "1", "mucho")
    assert "No entendí la duración" in out[0]
    out, _ = say(env, "deshacer")                                 # hay historia (la creación)
    out, _ = say(env, "deshacer")
    assert "nada para deshacer" in out[0].lower()


# ----------------------------------------------------------------- avisos
def test_avisos_silencio_y_pausa(env):
    out, _ = say(env, "avisos", "ocio", "30,10")
    assert "ocio: 30 min antes, 10 min antes" in out[0]
    out, _ = say(env, "avisos", "ocio", "resumen")
    assert "ocio: solo en el resumen" in out[0]
    out, _ = say(env, "avisos", "xx", "10")
    assert "Categoría inválida" in out[0]
    out, _ = say(env, "silencio", "22:00-08:00")
    assert "22:00-08:00" in out[0]
    out, _ = say(env, "pausa", "2h")
    assert "Sin avisos hasta" in out[0]
    out, _ = say(env, "pausa", "off")
    assert "reanudados" in out[0]


# ------------------------------------------------------------------ reglas
def test_reglas_y_sugerencias(env):
    out, _ = say(env, "regla", "ocio", "paddle")
    assert "Agregué" in out[0]
    assert env.core.capture_text("paddle con Pedro").category == "ocio"
    out, _ = say(env, "regla", "quitar", "ocio", "paddle")
    assert "Quité" in out[0]
    for t in ("tenis con Pedro", "reservar tenis"):
        env.core.set_category(env.core.capture_text(t).id, "ocio")
    out, msg = say(env, "sugerencias")
    assert "“tenis” → ocio" in out[0]
    kb = msg.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert kb[0][0].callback_data == "rule:ocio:tenis"
    press(env, "rule:ocio:tenis")
    assert env.core.capture_text("tenis mañana").category == "ocio"


def test_estado_y_resumen(env):
    capture(env)
    out, _ = say(env, "estado")
    assert "Calendario: activo" in out[0] and "1 en calendario" in out[0]
    out, _ = say(env, "resumen", "semana")
    assert "Semana del" in out[0]
    out, _ = say(env, "resumen", "xx")
    assert "Usá /resumen" in out[0]
    out, _ = say(env, "sync")
    assert out == ["🔄 Sincronizando…"]


# --------------------------------------------------------------- callbacks
def test_boton_de_categoria_y_hora_quitan_filas(env):
    r = env.core.capture_text("cena con amigos el sábado a las 5")
    assert r.pending == ["category", "hour"]
    q, ctx, edits = press(env, f"cat:{r.id}:ocio")
    kb = edits[-1].kwargs["reply_markup"].inline_keyboard
    assert [b.callback_data for row in kb for b in row][:2] != [f"cat:{r.id}:trabajo"]
    assert not any(b.callback_data.startswith("cat:") for row in kb for b in row)   # fila desaparecida
    assert any(b.callback_data.startswith("hr:") for row in kb for b in row)         # la otra sigue
    q, ctx, edits = press(env, f"hr:{r.id}:pm")
    assert len(edits[-1].kwargs["reply_markup"].inline_keyboard) == 0                # sin teclado


def test_poner_hora_por_texto(env):
    r = env.core.capture_text("reunión con el cliente mañana")
    ctx = mk_context()
    press(env, f"tm:{r.id}:ask", context=ctx)
    assert ctx.user_data["await"]["kind"] == "time"
    from misecre.telegram_bot import build_bot  # noqa: F401
    # el siguiente texto del usuario se interpreta como hora
    handler = next(h.callback for h in env.app.handlers[0]
                   if h.__class__.__name__ == "MessageHandler" and "TEXT" in repr(h.filters))
    update, msg = mk_update()
    update.message.text = "a las 18:30"
    asyncio.run(handler(update, ctx))
    assert env.core.get(r.id).start.strftime("%H:%M") == "18:30"
    assert "await" not in ctx.user_data


def test_panel_de_edicion(env):
    r = capture(env)
    press(env, f"ec:{r.id}:personal")
    assert env.core.get(r.id).category == "personal"
    press(env, f"et:{r.id}:tarea")
    assert env.core.get(r.id).type == "tarea"
    press(env, f"ed:{r.id}:45")
    assert (env.core.get(r.id).end - env.core.get(r.id).start).seconds == 45 * 60
    press(env, f"eh:{r.id}")
    assert env.core.get(r.id).status == "hecha"
    ctx = mk_context()
    press(env, f"ex:{r.id}:title", context=ctx)
    assert ctx.user_data["await"]["kind"] == "title" and ctx.user_data["await"]["edit"] is True


def test_recordatorio_posponer_y_hecho_quitan_su_fila(env):
    r = capture(env)
    kb = SimpleNamespace(inline_keyboard=[
        [SimpleNamespace(callback_data=f"snz:{r.id}"), SimpleNamespace(callback_data=f"rdone:{r.id}")]])
    q, ctx, _ = press(env, f"snz:{r.id}", reply_markup=kb)
    q.answer.assert_awaited()
    assert env.core.db.snoozes_due("9999-01-01") != []
    ctx.bot.edit_message_reply_markup.assert_awaited()
    q, ctx, _ = press(env, f"rdone:{r.id}", reply_markup=kb)
    assert env.core.get(r.id).status == "hecha"


def test_tareas_con_botones(env):
    env.core.capture_text("comprar pan")
    text, kb = _tasks_view(env.core)
    assert "#1" in text and kb.inline_keyboard[0][0].callback_data.startswith("done:")
    r = env.core.get(1)
    press(env, f"done:{r.id}")
    assert env.core.tasks() == []


# ------------------------------------------------------------- formato puro
def test_parse_duration():
    assert [_parse_duration(x) for x in ("30", "30m", "45 min", "2h", "1h30", "1 h 15")] == [30, 30, 45, 120, 90, 75]
    with pytest.raises(ValueError):
        _parse_duration("rato")


def test_format_muestra_numero_y_pendientes(env):
    r = env.core.capture_text("cena con amigos el sábado a las 5")
    t = _format(r)
    assert t.startswith("#1 ") and "¿05:00 o 17:00?" in t
    assert _keyboard(env.core, r).inline_keyboard
    assert _edit_keyboard(env.core, env.core.get(1))
    assert line(r).startswith("#1 ")


def test_menu_de_comandos_es_valido_y_se_publica(env):
    import re

    from misecre.telegram_bot import register_commands

    names = [c for c, _ in COMMANDS]
    assert len(names) == len(set(names)) <= 100
    assert all(re.fullmatch(r"[a-z0-9_]{1,32}", n) for n in names)        # reglas de Telegram
    assert all(1 <= len(d) <= 256 for _, d in COMMANDS)
    fake = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()))
    assert asyncio.run(register_commands(fake)) is True
    sent = fake.bot.set_my_commands.await_args.args[0]
    assert [c.command for c in sent] == names


# ============================================================ agente (chat)
from misecre.agent import Agent  # noqa: E402

from .fakes import ScriptedAnthropic, tool  # noqa: E402
from .fakes import say as llm_say  # noqa: E402


@pytest.fixture
def aenv(cfg, monkeypatch):
    """Bot con agente de IA guionado."""
    monkeypatch.setenv("MISECRE_TELEGRAM_TOKEN", "123456:TEST")
    cfg.telegram.allowed_chat_ids = [CHAT]
    cfg.ai.enabled = True
    core = Core(cfg, calendar=FakeCalendar())
    rt = Runtime(core, Notifier([]))

    def make(*steps):
        client = ScriptedAnthropic(*steps)
        agent = Agent(core, client=client)
        app = build_bot(core, rt, agent)
        handlers, text_handler = {}, None
        for h in app.handlers[0]:
            if isinstance(h, CommandHandler):
                for c in h.commands:
                    handlers[c] = h.callback
            elif isinstance(h, CallbackQueryHandler):
                handlers["__callback__"] = h.callback
            elif h.__class__.__name__ == "MessageHandler" and "TEXT" in repr(h.filters):
                text_handler = h.callback
        return SimpleNamespace(core=core, rt=rt, app=app, h=handlers, text=text_handler,
                               client=client, agent=agent)
    return make


def say_text(env, text, context=None):
    update, msg = mk_update()
    update.message.text = text
    ctx = context or mk_context()
    asyncio.run(env.text(update, ctx))
    return msg, ctx


def test_texto_dudoso_va_a_la_ia_y_ofrece_deshacer(aenv):
    env = aenv(tool("create_item", text="comprar pan", category="personal"))
    msg, ctx = say_text(env, "mete un recordatorio de ir a comprar pan")
    ctx.bot.send_chat_action.assert_awaited()                        # "escribiendo…"
    sent = msg.reply_text.await_args
    assert sent.args[0].startswith("✓ Anoté #1 «Comprar pan»") and sent.args[0].endswith("🤖")
    kb = sent.kwargs["reply_markup"].inline_keyboard
    assert kb[0][0].callback_data.startswith("agundo:2:") and "Deshacer (2)" in kb[0][0].text
    assert env.core.get(1).title == "Comprar pan"
    assert len(env.client.calls) == 1                                # UNA sola llamada


def test_lo_que_resuelven_las_reglas_no_gasta_ia(aenv):
    env = aenv()                                                     # cualquier llamada al modelo fallaría
    msg, _ = say_text(env, "reunión con el cliente el jueves a las 10")
    assert msg.reply_text.await_args.args[0].startswith("✓ Anoté #1 «Reunión con el cliente»")
    assert "🤖" not in msg.reply_text.await_args.args[0]
    msg, _ = say_text(env, "qué tengo hoy")
    assert "Hoy" in msg.reply_text.await_args.args[0]
    assert env.client.calls == []


def test_ya_fui_se_completa_sin_ia_y_con_deshacer(aenv):
    env = aenv()
    env.core.capture_text("ir a comprar pan")
    msg, _ = say_text(env, "ya fui a comprar pan")
    assert msg.reply_text.await_args.args[0] == "✅ Marqué #1 «Ir a comprar pan» como hecho"
    assert env.core.get(1).status == "hecha" and env.client.calls == []
    assert msg.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data.startswith("agundo:1:")


def test_varios_candidatos_muestran_botones_para_elegir(aenv):
    env = aenv()
    env.core.capture_text("comprar pan integral")
    env.core.capture_text("comprar pan lactal")
    msg, _ = say_text(env, "ya compré el pan")
    kb = msg.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert [row[0].callback_data.split(":")[0] for row in kb] == ["pick", "pick"]
    press(env, kb[1][0].callback_data)
    assert [t.title for t in env.core.tasks()] == ["Comprar pan integral"]    # completó la elegida


def test_boton_deshacer_del_agente(aenv):
    env = aenv(tool("create_item", text="comprar pan"))
    msg, _ = say_text(env, "anotá pan")
    assert env.core.get(1).status == "pendiente"
    data = msg.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    q, ctx, _ = press(env, data)
    assert env.core.get(1).status == "borrada"                       # la creación se deshizo
    q.answer.assert_awaited()


def test_deshacer_de_la_ia_revierte_crear_y_categorizar_juntos(aenv):
    """Regresión: antes el botón (1) solo revertía la categoría y el ítem quedaba creado."""
    env = aenv(tool("create_item", text="llevar el auto al taller", category="personal"))
    msg, _ = say_text(env, "llevar el auto al taller")
    data = msg.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert data.startswith("agundo:2:")
    press(env, data)
    assert env.core.get(1).status == "borrada"


def test_boton_viejo_no_deshace_cosas_ajenas(aenv):
    env = aenv(tool("create_item", text="llevar el auto al taller", category="personal"))
    msg, _ = say_text(env, "llevar el auto al taller")
    data = msg.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    env.core.capture_text("comprar pan")                              # pasó otra cosa después
    q, ctx, _ = press(env, data)
    assert env.core.get(1).status == "pendiente" and env.core.get(2).status == "pendiente"   # no tocó nada
    assert "otros cambios" in q.answer.await_args.args[0]


def test_ia_caida_cae_a_reglas_sin_perder_la_nota(aenv):
    env = aenv(RuntimeError("sin red"))
    msg, _ = say_text(env, "llevar el auto al taller")               # categoría dudosa => intenta la IA
    texts = [c.args[0] for c in msg.reply_text.await_args_list]
    assert "IA" in texts[0] and "Llevar el auto al taller" in texts[0]
    assert env.core.get(1).title == "Llevar el auto al taller"      # y quedó anotado igual


def test_anotar_es_determinista_aunque_haya_agente(aenv):
    env = aenv()                                                     # cualquier llamada al modelo fallaría
    out, _ = say(env, "anotar", "comprar", "pan", "mañana", context=None)
    assert env.core.get(1).title == "Comprar pan" and env.client.calls == []


def test_olvidar_borra_la_memoria(aenv):
    env = aenv(llm_say("¿Querés que lo anote en alguna categoría?"))
    say_text(env, "poné algo raro de ayer")                          # sin reglas => IA => queda memoria
    assert env.core.db.chat_recent("tg:1", "0", 10)
    out, _ = say(env, "olvidar")
    assert "borré la memoria" in out[0] and env.core.db.chat_recent("tg:1", "0", 10) == []


def test_la_espera_de_un_dato_tiene_prioridad_sobre_el_agente(aenv):
    env = aenv()                                                     # el agente no debe invocarse
    r = env.core.capture_text("reunión con el cliente mañana")
    ctx = mk_context()
    ctx.user_data["await"] = {"kind": "time", "id": r.id, "chat_id": CHAT, "message_id": 7, "edit": False}
    say_text(env, "a las 18:30", context=ctx)
    assert env.core.get(r.id).start.strftime("%H:%M") == "18:30" and env.client.calls == []


def test_estado_muestra_el_agente_y_el_reparto_reglas_ia(aenv):
    env = aenv()
    say_text(env, "qué tengo hoy")
    out, _ = say(env, "estado")
    assert "Agente: claude-haiku-5-5" in out[0] and "US$" in out[0]
    assert "hoy resolví 1 con reglas y 0 con IA" in out[0] and "Modo rules_first" in out[0]
