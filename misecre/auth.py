"""Autenticación de la API: tokens por dispositivo, límite de intentos y lista blanca de IPs.

Modelo (pensado para exponer el servicio en un VPS):
  * Cada dispositivo (celu, notebook, listener de escritorio) tiene su PROPIO token. Se muestra una
    sola vez al crearlo y en la DB queda solo su hash SHA-256: una DB filtrada no entrega accesos.
  * Se puede revocar uno sin afectar al resto (`misecre token revoke celu`).
  * `MISECRE_API_TOKEN` (variable de entorno) sigue valiendo como token maestro.
  * Sin ningún token configurado y escuchando solo en loopback, la API queda abierta (uso local).
  * Intentos fallidos: tras N errores desde una IP, se bloquea esa IP un rato (429).
  * Lista blanca opcional por IP/CIDR, evaluada antes que la autenticación.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime

from .db import DB

TOKEN_PREFIX = "msc_"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class TokenStore:
    def __init__(self, db: DB):
        self.db = db
        self._last_touch: dict[str, float] = {}

    def create(self, name: str) -> str:
        name = name.strip()
        if not name or len(name) > 40:
            raise ValueError("El nombre del dispositivo debe tener entre 1 y 40 caracteres")
        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
        try:
            self.db.token_add(name, hash_token(raw), datetime.now().isoformat(timespec="seconds"))
        except sqlite3.IntegrityError as e:
            raise ValueError(f"Ya hay un token activo llamado “{name}” (revocalo antes: "
                             f"misecre token revoke {name})") from e
        return raw

    def verify(self, raw: str) -> str | None:
        """-> nombre del dispositivo, o None. Compara hashes en tiempo constante."""
        if not raw:
            return None
        env = os.environ.get("MISECRE_API_TOKEN", "")
        if env and hmac.compare_digest(raw.encode(), env.encode()):
            return "env"
        row = self.db.token_by_hash(hash_token(raw))
        if row is None:
            return None
        now = time.monotonic()
        if now - self._last_touch.get(row["name"], 0) > 60:    # no escribir en la DB en cada request
            self._last_touch[row["name"]] = now
            self.db.token_touch(row["name"], datetime.now().isoformat(timespec="seconds"))
        return row["name"]

    def revoke(self, name: str) -> bool:
        return self.db.token_revoke(name)

    def list(self) -> list[dict]:
        return [dict(r) for r in self.db.tokens()]

    def configured(self) -> bool:
        return bool(os.environ.get("MISECRE_API_TOKEN")) or self.db.has_active_tokens()


class AuthLimiter:
    """Bloquea una IP tras `max_fail` intentos fallidos dentro de `window` segundos."""

    def __init__(self, max_fail: int = 10, window: float = 300, block: float = 300, clock=time.monotonic):
        self.max_fail, self.window, self.block, self.clock = max_fail, window, block, clock
        self._fails: dict[str, list[float]] = {}
        self._blocked: dict[str, float] = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        with self._lock:
            until = self._blocked.get(ip)
            if until is None:
                return False
            if self.clock() >= until:
                del self._blocked[ip]
                self._fails.pop(ip, None)
                return False
            return True

    def _prune(self, now: float) -> None:
        """Descarta IPs sin actividad reciente (un barrido desde miles de IPs no debe llenar la memoria)."""
        if len(self._fails) < 2048:
            return
        for ip in [i for i, hits in self._fails.items() if not hits or now - hits[-1] >= self.window]:
            self._fails.pop(ip, None)
        for ip in [i for i, until in self._blocked.items() if now >= until]:
            self._blocked.pop(ip, None)

    def fail(self, ip: str) -> None:
        with self._lock:
            now = self.clock()
            self._prune(now)
            hits = [t for t in self._fails.get(ip, []) if now - t < self.window] + [now]
            self._fails[ip] = hits
            if len(hits) >= self.max_fail:
                self._blocked[ip] = now + self.block

    def ok(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


def parse_networks(specs: list[str]) -> list[ipaddress._BaseNetwork]:
    out = []
    for s in specs:
        try:
            out.append(ipaddress.ip_network(s.strip(), strict=False))
        except ValueError as e:
            raise ValueError(f"api.allowed_ips: “{s}” no es una IP/CIDR válida") from e
    return out


def ip_allowed(ip: str, nets: list) -> bool:
    if not nets:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)
