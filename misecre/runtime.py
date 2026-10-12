"""Orquesta lo periódico: avisos, resúmenes y sincronización con Google Calendar."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from . import digest
from .core import Core
from .notify import Message, Notifier
from .reminders import Reminders, describe
from .sync import Syncer

log = logging.getLogger("misecre.runtime")
TICK_SECONDS = 30


class Runtime:
    def __init__(self, core: Core, notifier: Notifier):
        self.core = core
        self.notifier = notifier
        self.reminders = Reminders(core)
        self.syncer = Syncer(core)
        self._last_sync: datetime | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------ un tick
    async def tick(self, now: datetime | None = None) -> None:
        """Una pasada completa. Separado del bucle para poder probarlo."""
        now = now or self.core.now()
        await self._reminders(now)
        await self._digests(now)
        await self._sync(now)

    async def _reminders(self, now: datetime) -> None:
        due = await asyncio.to_thread(self.reminders.collect, now)
        if not due:
            return
        lines = [describe(d) for d in due]
        buttons = []
        for d in due:
            buttons.append([(f"⏰ +{self.core.cfg.notifications.snooze_minutes} min · #{d.result.seq}",
                             f"snz:{d.result.id}"),
                            (f"✅ #{d.result.seq}", f"rdone:{d.result.id}")])
        if await self.notifier.send(Message("\n".join(lines), buttons, kind="reminder")):
            for d in due:                      # solo se marcan si alguien lo recibió: si no, se reintenta
                await asyncio.to_thread(self.reminders.mark, d, now)

    async def _digests(self, now: datetime) -> None:
        if self.reminders.silenced(now):
            return
        for kind in await asyncio.to_thread(digest.due_digests, self.core, now):
            if kind.startswith("pulse:"):
                skip_empty = self.core.cfg.digests.periodic_skip_empty
                text = await asyncio.to_thread(digest.build_pulse, self.core, now, empty_ok=not skip_empty)
                if text is None:                                  # nada para mostrar: se marca y no se molesta
                    await asyncio.to_thread(digest.mark_digest, self.core, kind, now)
                    continue
                title = f"Resumen de las {kind[6:]}"
            else:
                text = await asyncio.to_thread(digest.BUILDERS[kind], self.core, now)
                title = {"daily": "Tu día", "weekly": "Tu semana", "monthly": "Tu mes"}[kind]
            if await self.notifier.send(Message(text, title=title, kind="digest")):
                await asyncio.to_thread(digest.mark_digest, self.core, kind, now)

    async def _sync(self, now: datetime) -> None:
        cfg = self.core.cfg.sync
        if not cfg.enabled:
            return
        if self._last_sync and now - self._last_sync < timedelta(minutes=cfg.interval_min):
            return
        self._last_sync = now
        rep = await asyncio.to_thread(self.syncer.run)
        if rep.changed:
            await self.notifier.send(Message(rep.text(), title="Sincronización", kind="sync"))
        if rep.errors:                         # un mismo error (p.ej. sin red) se avisa una sola vez
            sig = "|".join(rep.errors)
            if sig != self._last_error:
                self._last_error = sig
                if not rep.changed:
                    await self.notifier.send(Message(rep.text(), title="Sincronización", kind="sync"))
        else:
            self._last_error = None

    # --------------------------------------------------------------- bucle
    async def run_forever(self) -> None:
        log.info("runtime: avisos, resúmenes y sincronización activos")
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001  (un tick fallido no mata el servicio)
                log.exception("tick")
            await asyncio.sleep(TICK_SECONDS)
