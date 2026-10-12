"""Cliente liviano: se conecta a un servidor misecre y muestra sus avisos como notificaciones nativas.

Pensado para el caso "el core corre en un VPS": el servidor no tiene escritorio, así que cada PC
corre `misecre listen` (sin GUI, sin dependencias nuevas) y recibe los avisos por SSE.

  MISECRE_SERVER=https://misecre.ejemplo.com   MISECRE_TOKEN=msc_...   misecre listen
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import httpx

from .notify import Message, desktop_channel

log = logging.getLogger("misecre.listener")


class AuthError(Exception):
    """El servidor rechazó el token: reintentar no sirve."""


def parse_sse_lines(lines) -> list[dict]:
    """Convierte líneas SSE en eventos JSON (ignora comentarios/keep-alives)."""
    out, data = [], []
    for line in lines:
        line = line.rstrip("\r\n")
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "" and data:
            try:
                out.append(json.loads("\n".join(data)))
            except json.JSONDecodeError:
                log.warning("evento SSE inválido: %r", data)
            data = []
    return out


async def listen(server: str, token: str, on_message: Callable[[Message], Awaitable[None]] = desktop_channel,
                 *, max_backoff: float = 60.0, once: bool = False) -> None:
    """Se mantiene conectado y reconecta con backoff exponencial. `once` es para tests."""
    url = server.rstrip("/") + "/events"
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    backoff = 2.0
    while True:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=15)) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code in (401, 403):
                        raise AuthError(f"El servidor rechazó el token (HTTP {resp.status_code})")
                    resp.raise_for_status()
                    log.info("conectado a %s", url)
                    backoff = 2.0
                    buf: list[str] = []
                    async for line in resp.aiter_lines():
                        buf.append(line)
                        if line == "":
                            for ev in parse_sse_lines(buf):
                                await on_message(Message(ev.get("text", ""), title=ev.get("title", "misecre"),
                                                         kind=ev.get("kind", "info")))
                            buf = []
        except AuthError:
            raise
        except (httpx.HTTPError, OSError) as e:
            log.warning("sin conexión (%s); reintento en %.0fs", e, backoff)
        if once:
            return
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)
