from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _load_env(config: str | None) -> None:
    """.env en cwd, junto al config (config/.. y config/) y en ~/.misecre."""
    candidates = [Path.cwd() / ".env"]
    if config:
        c = Path(config).resolve().parent
        candidates += [c / ".env", c.parent / ".env"]
    else:
        candidates.append(Path(__file__).resolve().parent.parent / ".env")
    candidates.append(Path.home() / ".misecre" / ".env")
    for p in candidates:
        if p.exists():
            load_dotenv(p, override=False)


def _setup_logging(log_dir: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_dir / "misecre.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "httpx2", "apscheduler"):  # sus INFO imprimen URLs con el token
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _print_result(r) -> None:
    cat = r.category or "SIN CATEGORÍA (Inbox)"
    print(f"#{r.seq} [{cat}] {r.type}: {r.title}")
    if r.when_label():
        print(f"  cuándo: {r.when_label()}")
    print(f"  calendario: {'sí' if r.event_id else 'no'}")
    for w in r.warnings:
        print(f"  ⚠ {w}")


async def _bounded(coro, seconds: float, what: str) -> None:
    """Espera `coro` como mucho `seconds`: un cierre que se cuelga es peor que uno poco elegante."""
    try:
        await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        logging.warning("%s no terminó en %.0fs; sigo con el cierre", what, seconds)
    except Exception:  # noqa: BLE001
        logging.exception("error al cerrar: %s", what)


async def _run(core) -> None:
    import contextlib
    import signal

    import uvicorn

    from .agent import Agent
    from .api import build_api
    from .operator import Operator
    from .notify import Notifier
    from .runtime import Runtime
    from .telegram_bot import build_bot, register_commands, telegram_channel

    notifier = Notifier(core.cfg.notifications.channels)
    runtime = Runtime(core, notifier)
    agent = Agent(core)
    operator = Operator(core, agent)
    app = build_api(core, runtime, agent=agent, operator=operator)
    app.state.stopping = False
    server = uvicorn.Server(uvicorn.Config(
        app, host=core.cfg.api.host, port=core.cfg.api.port, log_level="warning",
        timeout_graceful_shutdown=2))                 # no esperar eternamente a clientes SSE abiertos

    @contextlib.contextmanager
    def _no_signals():                                # las señales las manejamos nosotros, abajo
        yield

    server.capture_signals = _no_signals              # uvicorn >= 0.29
    server.install_signal_handlers = lambda: None     # uvicorn anterior

    bot = build_bot(core, runtime, agent, operator)
    chat_ids = core.cfg.telegram.allowed_chat_ids
    if bot is None:
        logging.warning("MISECRE_TELEGRAM_TOKEN no definido: solo API y notificaciones de escritorio")
    elif not chat_ids:
        logging.warning("telegram.allowed_chat_ids vacío: el bot ignora todo salvo /start")
    for k in core.cfg.unknown_keys:
        logging.warning("config.yaml: clave desconocida «%s» (se ignora). Revisá el nombre y la indentación.", k)
    token_file = core.cfg.data_dir / "token.json"
    if not core.cfg.calendar.enabled and token_file.exists():
        logging.warning("⚠️ Hay credenciales de Google (%s) pero calendar.enabled es false: los eventos NO se "
                        "crean en Google Calendar. Activalo en config.yaml.", token_file)
    elif core.cfg.calendar.enabled and core.skipped_categories():
        logging.info("Categorías sin calendario (solo avisos): %s", ", ".join(sorted(core.skipped_categories())))
    if core.cfg.digests.periodic:
        logging.info("Resúmenes periódicos a las: %s", ", ".join(core.cfg.digests.periodic))
    if core.transcriber is not None and core.cfg.stt.preload:
        core.transcriber.preload()
    why = agent.why_unavailable()
    logging.info("agente de IA: %s", f"activo ({core.cfg.ai.model})" if why is None else f"apagado — {why}")
    logging.info("misecre en http://%s:%s", core.cfg.api.host, core.cfg.api.port)

    # ---- diagnóstico: `kill -USR1 <pid>` vuelca las pilas de todos los hilos al log
    if hasattr(signal, "SIGUSR1"):
        import faulthandler
        try:
            log_dir = core.cfg.log_dir
            log_dir.mkdir(parents=True, exist_ok=True)
            core._fault_file = open(log_dir / "stacks.log", "a", encoding="utf-8")   # noqa: SIM115
            faulthandler.register(signal.SIGUSR1, file=core._fault_file, all_threads=True)
            logging.info("diagnóstico: kill -USR1 %s vuelca las pilas en %s", os.getpid(), log_dir / "stacks.log")
        except Exception:  # noqa: BLE001
            logging.warning("no pude registrar el diagnóstico de pilas")

    # ---- señales: 1ª = cierre ordenado, 2ª = forzar
    import time

    stop = asyncio.Event()
    first = {"t": None}

    def on_signal() -> None:
        now = time.monotonic()
        if first["t"] is None:
            first["t"] = now
            logging.info("Cerrando… (Ctrl+C otra vez para forzar)")
            stop.set()
        elif now - first["t"] > 1.0:       # `uv run` + python reciben la misma señal: ignorar el eco
            logging.warning("Cierre forzado")
            logging.shutdown()
            os._exit(130)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, on_signal)
        except (NotImplementedError, RuntimeError):   # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(on_signal))

    async def serve_api() -> None:
        """Abre la API; si no puede (puerto ocupado, red aún no lista) reintenta sin tumbar el resto."""
        attempt = 0
        while not stop.is_set():
            srv = server if attempt == 0 else uvicorn.Server(server.config)
            srv.capture_signals = _no_signals
            srv.install_signal_handlers = lambda: None
            holder["server"] = srv
            try:
                await srv.serve()
                return
            except SystemExit:                        # uvicorn sale así si no puede abrir el puerto
                attempt += 1
                if attempt == 1 or attempt % 20 == 0:
                    logging.error(
                        "La API no pudo abrir %s:%s (¿ocupado por otro programa o la interfaz aún no existe?). "
                        "El bot, los avisos y la sincronización siguen funcionando; reintento cada 15 s.",
                        core.cfg.api.host, core.cfg.api.port)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass

    holder: dict = {"server": server}

    api_task = asyncio.create_task(serve_api())
    rt_task = asyncio.create_task(runtime.run_forever())

    if bot is not None:
        notifier.register("telegram", telegram_channel(bot, chat_ids))
        await bot.initialize()
        await register_commands(bot)
        await bot.start()
        await bot.updater.start_polling()

    await stop.wait()

    # ---- cierre ordenado, cada paso con plazo
    app.state.stopping = True
    holder["server"].should_exit = True
    rt_task.cancel()
    if bot is not None:
        await _bounded(bot.updater.stop(), 6, "bot (polling)")
        await _bounded(bot.stop(), 6, "bot")
        await _bounded(bot.shutdown(), 6, "bot (shutdown)")
    await _bounded(asyncio.shield(api_task), 5, "API")
    api_task.cancel()
    logging.info("Cerrado.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="misecre")
    p.add_argument("-c", "--config", help="ruta a config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="levanta API + bot + avisos + sincronización")
    sub.add_parser("check", help="valida config, token, calendario e IA")
    sub.add_parser("doctor", help="diagnóstico completo del entorno (pegalo si algo no anda)")
    sub.add_parser("google-auth", help="autoriza Google y crea/deja visibles los calendarios")
    sub.add_parser("sync", help="sincroniza con Google Calendar ahora")
    sub.add_parser("selftest-calendar", help="prueba contra Google: crea, edita, mueve y borra un evento")
    c = sub.add_parser("capture", help="procesa un texto como si fuera una nota")
    c.add_argument("text")
    t = sub.add_parser("transcribe", help="transcribe un audio y lo procesa")
    t.add_argument("audio")
    d = sub.add_parser("digest", help="imprime un resumen")
    d.add_argument("kind", nargs="?", default="daily", choices=["daily", "weekly", "monthly", "pulse"])
    sub.add_parser("chat", help="chatea con el agente desde la terminal (prueba)")
    ai = sub.add_parser("ai-test", help="prueba la clasificación con IA (usa ANTHROPIC_API_KEY)")
    ai.add_argument("text")
    s = sub.add_parser("service", help="instala/gestiona el arranque automático")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("--listener", action="store_true", help="gestiona el listener de avisos en vez del servidor")
    sub.add_parser("open", help="abre la vista web en el navegador")
    li = sub.add_parser("listen", help="muestra en este escritorio los avisos de un servidor misecre")
    li.add_argument("--server", help="URL del servidor (o MISECRE_SERVER)")
    li.add_argument("--token", help="token del dispositivo (o MISECRE_TOKEN)")
    tk = sub.add_parser("token", help="tokens de acceso por dispositivo (web, listener, scripts)")
    tk.add_argument("action", choices=["create", "list", "revoke"])
    tk.add_argument("name", nargs="?", help="nombre del dispositivo, ej: celu")
    a = p.parse_args(argv)

    _load_env(a.config)

    if a.cmd == "service":
        from . import service

        _setup_logging(None)
        try:
            print({"install": lambda: service.install(a.config, a.listener),
                   "uninstall": lambda: service.uninstall(a.listener),
                   "status": lambda: service.status(a.listener)}[a.action]())
        except RuntimeError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        return 0

    if a.cmd == "listen":
        from .listener import AuthError, listen
        from .notify import desktop_available

        ok_desktop, why_desktop = desktop_available()
        if not ok_desktop:
            print(f"✗ Este equipo no puede mostrar notificaciones: {why_desktop}", file=sys.stderr)
            return 1

        server = a.server or os.environ.get("MISECRE_SERVER")
        token = a.token or os.environ.get("MISECRE_TOKEN")
        if not server or not token:
            print("Falta --server/--token (o MISECRE_SERVER y MISECRE_TOKEN en ~/.misecre/.env)", file=sys.stderr)
            return 1
        _setup_logging(Path.home() / ".misecre" / "logs")
        try:
            asyncio.run(listen(server, token))
        except AuthError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            pass
        return 0

    from .core import Core

    core = Core.from_config(a.config)
    _setup_logging(core.cfg.log_dir if a.cmd == "run" else None)

    if a.cmd == "check":
        cfg = core.cfg
        print(f"config OK · zona {cfg.timezone} · categorías {cfg.category_names}")
        print(f"calendario: {'activado' if cfg.calendar.enabled else 'desactivado'} · STT: {cfg.stt.model}")
        print(f"telegram token: {'sí' if os.environ.get('MISECRE_TELEGRAM_TOKEN') else 'NO'}"
              f" · chats permitidos: {cfg.telegram.allowed_chat_ids}")
        from .agent import Agent as _Agent

        ag = _Agent(core)
        why = ag.why_unavailable()
        print(f"agente de IA: {'ACTIVO (' + cfg.ai.model + ')' if why is None else 'apagado — ' + why}")
        for k in cfg.unknown_keys:
            hint = " (¿es una sección? anidala: «ai:» y debajo «  enabled: true»)" if "." not in k else ""
            print(f"⚠ clave desconocida en config.yaml: «{k}», se ignora{hint}")
        print(f"avisos: {cfg.notifications.categories} · silencio {cfg.notifications.quiet_hours}")
        print(f"datos: {cfg.data_dir}")
    elif a.cmd == "doctor":
        from .doctor import run_doctor

        return run_doctor(core, a.config)
    elif a.cmd == "google-auth":
        if core.calendar is None:
            print("calendar.enabled es false en config.yaml", file=sys.stderr)
            return 1
        core.calendar.authorize()
        print("Google autorizado y calendarios listos.")
    elif a.cmd == "capture":
        _print_result(core.capture_text(a.text, source="cli"))
    elif a.cmd == "transcribe":
        _print_result(core.capture_audio(a.audio, source="cli-audio"))
    elif a.cmd == "sync":
        from .sync import Syncer

        print(Syncer(core).run().text())
    elif a.cmd == "digest":
        from . import digest

        print(digest.build_pulse(core) if a.kind == "pulse" else digest.BUILDERS[a.kind](core))
    elif a.cmd == "chat":
        from .agent import Agent
        from .operator import Operator

        operator = Operator(core, Agent(core))
        why = operator.agent.why_unavailable()
        print("Chat (Ctrl+D para salir). " + ("IA activa como respaldo." if why is None
                                                else f"Solo reglas (IA apagada: {why})."))
        while True:
            try:
                msg = input("› ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not msg:
                continue
            try:
                r = operator.handle("cli", msg, "cli")
                print(f"{r.text}" + (f"  [{r.source}, {r.calls} llamada(s) IA]" if r.source == "ia" else "  [reglas]"))
                for x in r.learned:
                    print(x)
            except ValueError as e:
                print(f"⚠ {e}")
    elif a.cmd == "ai-test":
        core.cfg.ai.enabled = True
        if not core.ai.available:
            print("Falta ANTHROPIC_API_KEY (la API se factura aparte del plan Claude Pro/Max).", file=sys.stderr)
            return 1
        print(core.ai.classify(a.text))
    elif a.cmd == "selftest-calendar":
        from .selftest import run_calendar_selftest

        return run_calendar_selftest(core)
    elif a.cmd == "token":
        from .auth import TokenStore

        store = TokenStore(core.db)
        if a.action == "list":
            rows = store.list()
            if not rows:
                print("No hay tokens. Creá uno con: misecre token create <dispositivo>")
            for r in rows:
                print(f"{'✗ revocado' if r['revoked'] else '✓ activo  '}  {r['name']:<20} "
                      f"creado {r['created']}  último uso {r['last_used'] or 'nunca'}")
        elif not a.name:
            print("Falta el nombre del dispositivo", file=sys.stderr)
            return 1
        elif a.action == "create":
            try:
                raw = store.create(a.name)
            except ValueError as e:
                print(f"✗ {e}", file=sys.stderr)
                return 1
            print(f"Token para “{a.name}” (se muestra UNA sola vez, guardalo ahora):\n\n  {raw}\n")
            print("Usalo en la web (pantalla de entrada), en MISECRE_TOKEN del listener o como\n"
                  "'Authorization: Bearer <token>'. Para revocarlo: misecre token revoke " + a.name)
        else:
            print("✓ revocado" if store.revoke(a.name) else f"No hay un token activo llamado “{a.name}”")
    elif a.cmd == "open":
        import webbrowser

        webbrowser.open(f"http://{core.cfg.api.host}:{core.cfg.api.port}/")
    elif a.cmd == "run":
        try:
            asyncio.run(_run(core))
        except KeyboardInterrupt:
            pass
        # Hilos de trabajo bloqueados (p. ej. una llamada de red) no deben impedir que el proceso termine.
        logging.shutdown()
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
