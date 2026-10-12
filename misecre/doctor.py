"""`misecre doctor`: diagnóstico del entorno, para pegarlo cuando algo no anda (solo lectura)."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import urllib.request

from .core import Core
from .notify import desktop_available
from .stt import mem_available_gb, model_ram_gb

OK, WARN, BAD = "✓", "⚠", "✗"


def _sh(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=8).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def run_doctor(core: Core, config_path: str | None = None) -> int:
    cfg = core.cfg
    rows: list[tuple[str, str, str]] = []

    def add(level: str, what: str, detail: str = "") -> None:
        rows.append((level, what, detail))

    add(OK, "sistema", f"{platform.system()} {platform.release()} · Python {sys.version.split()[0]}")
    add(OK, "datos", str(cfg.data_dir))
    free_disk = shutil.disk_usage(cfg.data_dir).free / 1e9
    add(OK if free_disk > 1 else WARN, "disco libre", f"{free_disk:.1f} GB")

    # --- configuración
    if cfg.unknown_keys:
        add(WARN, "config.yaml", f"claves desconocidas (se ignoran): {', '.join(cfg.unknown_keys)}")
    else:
        add(OK, "config.yaml", f"zona {cfg.timezone}")

    # --- Telegram
    if os.environ.get("MISECRE_TELEGRAM_TOKEN"):
        ids = cfg.telegram.allowed_chat_ids
        add(OK if ids else WARN, "Telegram", f"token cargado · chats permitidos: {ids or 'NINGUNO (el bot ignora todo)'}")
    else:
        add(WARN, "Telegram", "falta MISECRE_TELEGRAM_TOKEN en .env (solo funcionan API y escritorio)")

    # --- Google
    tok = cfg.data_dir / "token.json"
    if cfg.calendar.enabled:
        if tok.exists():
            add(OK, "Google Calendar", "activado · token.json presente")
        else:
            add(BAD, "Google Calendar", f"activado pero falta {tok} (corré: misecre google-auth en una PC con navegador)")
    elif tok.exists():
        add(BAD, "Google Calendar", "calendar.enabled es false pero HAY token: los eventos NO se crean en Google")
    else:
        add(WARN, "Google Calendar", "desactivado")
    if core.skipped_categories():
        add(OK, "calendario por categoría", f"sin publicar en Google: {', '.join(sorted(core.skipped_categories()))}")

    # --- IA
    why = None
    from .agent import Agent
    why = Agent(core).why_unavailable()
    add(OK if why is None else WARN, "agente de IA", f"activo ({cfg.ai.model})" if why is None else f"apagado — {why}")
    from .operator import MODES, Operator
    add(OK, "modo", f"{Operator(core).mode}: {MODES[Operator(core).mode]}")

    # --- voz y memoria
    need, free = model_ram_gb(cfg.stt.model), mem_available_gb()
    from .stt import REMOTE
    if cfg.stt.provider in REMOTE:
        env = REMOTE[cfg.stt.provider][2]
        if os.environ.get(env):
            add(OK, "voz", f"en la nube con {cfg.stt.provider} ({cfg.stt.api_model or REMOTE[cfg.stt.provider][1]})"
                           + (f"; respaldo local: {cfg.stt.model}" if cfg.stt.fallback_local else ""))
        else:
            add(BAD, "voz", f"stt.provider={cfg.stt.provider} pero falta {env} en .env")
    elif free is None:
        add(OK, "voz", f"modelo {cfg.stt.model} (no pude leer la RAM)")
    elif free < need * 1.3:
        add(BAD, "voz / RAM", f"modelo «{cfg.stt.model}» necesita ~{need:.1f} GB y hay {free:.1f} GB libres: "
                              f"el sistema puede matar el proceso. Usá stt.model: base o small")
    elif free < need * 2.0:
        add(WARN, "voz / RAM", f"modelo {cfg.stt.model} (~{need:.1f} GB) con solo {free:.1f} GB libres: margen "
                               f"justo. Un swap de 1–2 GB evita que el sistema mate el proceso (ver README)")
    else:
        add(OK, "voz / RAM", f"modelo {cfg.stt.model} (~{need:.1f} GB) · RAM libre {free:.1f} GB")

    # --- API y acceso
    from .auth import TokenStore
    n_tokens = sum(1 for t in TokenStore(core.db).list() if not t["revoked"])
    host, port = cfg.api.host, cfg.api.port
    try:
        body = urllib.request.urlopen(f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}/health",
                                      timeout=3).read()
        add(OK, "API", f"responde en {host}:{port} ({body.decode()[:40]})")
    except Exception as e:  # noqa: BLE001
        add(WARN, "API", f"no responde en {host}:{port} ({type(e).__name__}): ¿está corriendo `misecre run`?")
    add(OK if n_tokens else WARN, "tokens de acceso",
        f"{n_tokens} activo(s)" if n_tokens else "ninguno: por proxy/red la API NO responde. misecre token create <nombre>")
    if host not in ("127.0.0.1", "localhost", "::1"):
        import ipaddress
        try:
            addr = ipaddress.ip_address(host)
            private = addr.is_private and not addr.is_unspecified     # 0.0.0.0 = todas las interfaces
        except ValueError:
            private = False
        if private:
            add(OK, "api.host", f"{host}: red privada (Docker/VPN), no es accesible desde internet")
        else:
            add(WARN, "api.host", f"{host}: escucha en una IP pública o en todas (0.0.0.0): "
                                  f"el firewall debe permitir solo a quien corresponda")
    if cfg.api.trust_proxy:
        add(OK, "proxy", f"trust_proxy activo ({cfg.api.trusted_proxies} proxy)")

    # --- notificaciones de escritorio
    ok, why_d = desktop_available()
    headless = sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if ok:
        add(OK, "notificaciones de escritorio", "disponibles")
    elif headless:
        add(OK, "notificaciones de escritorio",
            "servidor sin escritorio: se usa solo Telegram (es lo normal; podés sacar «desktop» de "
            "notifications.channels)")
    else:
        add(WARN, "notificaciones de escritorio", f"no disponibles: {why_d}")

    # --- servicio (Linux)
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        active = _sh("systemctl", "--user", "is-active", "misecre")
        add(OK if active == "active" else WARN, "servicio systemd",
            active or "no instalado (misecre service install)")
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if user and shutil.which("loginctl"):
            linger = _sh("loginctl", "show-user", user, "-p", "Linger").split("=")[-1]
            headless = not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            if linger == "no":
                add(BAD if headless else WARN, "linger",
                    "desactivado: en un servidor el servicio se DETIENE al cerrar la sesión SSH. "
                    f"Arreglo: sudo loginctl enable-linger {user}")
            elif linger == "yes":
                add(OK, "linger", "activado (el servicio sobrevive al cierre de sesión)")
    # --- último error en el log
    log_file = cfg.log_dir / "misecre.log"
    if log_file.exists():
        from datetime import datetime, timedelta

        errs = [ln for ln in log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                if " ERROR " in ln or " CRITICAL " in ln]
        cutoff = core.now() - timedelta(hours=24)
        recent = []
        for ln in errs:
            try:
                if datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S") >= cutoff:
                    recent.append(ln)
            except ValueError:
                recent.append(ln)                                  # sin fecha parseable: por las dudas
        if recent:
            add(WARN, "log", f"{log_file} · {len(recent)} error(es) en las últimas 24 h"
                             f"\n      último: {recent[-1][:170]}")
        else:
            add(OK, "log", f"{log_file} · sin errores en las últimas 24 h"
                           + (f" ({len(errs)} anteriores)" if errs else ""))

    width = max(len(r[1]) for r in rows)
    for level, what, detail in rows:
        print(f"{level} {what:<{width}}  {detail}")
    bad = sum(1 for r in rows if r[0] == BAD)
    warn = sum(1 for r in rows if r[0] == WARN)
    print("\n" + (f"{bad} problema(s) grave(s), {warn} aviso(s)." if bad else
                  (f"Sin problemas graves ({warn} aviso(s))." if warn else "Todo en orden.")))
    return 1 if bad else 0
