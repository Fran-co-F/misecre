"""Canales de notificación. Telegram lo registra el bot; el escritorio se resuelve acá.

Escritorio:
  Linux   -> `notify-send` (libnotify; está en casi todo entorno gráfico)
  Windows -> toast nativo vía PowerShell (sin dependencias)       [no probado por el autor]
  macOS   -> `osascript`                                           [no probado por el autor]
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable

log = logging.getLogger("misecre.notify")

Button = tuple[str, str]  # (texto, callback_data)


@dataclass
class Message:
    text: str
    buttons: list[list[Button]] | None = None
    title: str = "misecre"
    kind: str = "info"        # reminder | digest | sync | info


Channel = Callable[[Message], Awaitable[None]]

_WIN_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$n.Item(0).AppendChild($t.CreateTextNode($env:MISECRE_TITLE)) | Out-Null
$n.Item(1).AppendChild($t.CreateTextNode($env:MISECRE_BODY)) | Out-Null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('misecre').Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def desktop_available() -> tuple[bool, str]:
    """¿Se pueden mostrar notificaciones nativas en ESTA máquina? -> (sí/no, motivo si no)."""
    if sys.platform.startswith("linux"):
        if not shutil.which("notify-send"):
            return False, "notify-send no está instalado (en Debian/Ubuntu: apt install libnotify-bin)"
        if not (os.environ.get("DBUS_SESSION_BUS_ADDRESS") or os.environ.get("DISPLAY")
                or os.environ.get("WAYLAND_DISPLAY")):
            return False, "no hay sesión gráfica (servidor sin escritorio)"
        return True, ""
    if sys.platform == "win32":
        return (True, "") if shutil.which("powershell") else (False, "no encuentro powershell")
    if sys.platform == "darwin":
        return (True, "") if shutil.which("osascript") else (False, "no encuentro osascript")
    return False, f"sistema no soportado ({sys.platform})"


async def desktop_channel(msg: Message) -> None:
    """Muestra la notificación. LANZA si no pudo: un canal que no entregó no debe contar como entregado."""
    title, body = msg.title, msg.text
    ok, why = desktop_available()
    if not ok:
        raise RuntimeError(f"notificaciones de escritorio no disponibles: {why}")
    try:
        if sys.platform.startswith("linux"):
            exe = shutil.which("notify-send")
            proc = await asyncio.create_subprocess_exec(exe, "-a", "misecre", title, body[:900])
        elif sys.platform == "win32":
            env = {**os.environ, "MISECRE_TITLE": title, "MISECRE_BODY": body[:900]}
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", _WIN_TOAST, env=env)
        elif sys.platform == "darwin":
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", f'display notification "{esc(body[:900])}" with title "{esc(title)}"')
        else:
            raise RuntimeError(f"sistema no soportado ({sys.platform})")
        rc = await proc.wait()
    except FileNotFoundError as e:
        raise RuntimeError(f"no encontré el comando de notificaciones: {e}") from e
    if rc != 0:
        raise RuntimeError(f"el comando de notificaciones terminó con código {rc}")


class Notifier:
    def __init__(self, enabled: list[str]):
        self.enabled = set(enabled)
        self.channels: dict[str, Channel] = {}
        if "desktop" in self.enabled:
            ok, why = desktop_available()
            if ok:
                self.channels["desktop"] = desktop_channel
            else:                                   # p. ej. un VPS: se avisa una vez y se sigue con Telegram
                log.info("Notificaciones de escritorio desactivadas en esta máquina: %s", why)
        self.history: list[Message] = []          # últimos enviados (útil en tests y /estado)
        self.subscribers: list[asyncio.Queue] = []  # clientes conectados a /events (web, listener)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)

    def register(self, name: str, fn: Channel) -> None:
        self.channels[name] = fn

    async def send(self, msg: Message, only: set[str] | None = None) -> bool:
        """True si al menos un canal lo entregó (o si no hay canales: nada que reintentar)."""
        self.history = (self.history + [msg])[-20:]
        attempted = delivered = 0
        for q in list(self.subscribers):          # clientes en vivo: cuentan como entrega
            try:
                q.put_nowait({"title": msg.title, "text": msg.text, "kind": msg.kind})
                attempted += 1
                delivered += 1
            except asyncio.QueueFull:
                pass
        for name, fn in self.channels.items():
            if name not in self.enabled or (only is not None and name not in only):
                continue
            attempted += 1
            try:
                await fn(msg)
                delivered += 1
            except Exception:  # noqa: BLE001
                log.exception("canal %s", name)
        return attempted == 0 or delivered > 0
