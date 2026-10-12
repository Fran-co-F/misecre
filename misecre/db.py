"""SQLite: la fuente de verdad. (Las columnas note_* son legado de cuando existía el vault de Obsidian: no se usan.)"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures(
    id TEXT PRIMARY KEY,
    seq INTEGER,
    created TEXT NOT NULL,
    updated TEXT,
    source TEXT,
    transcript TEXT,
    title TEXT,
    category TEXT,
    type TEXT,
    start TEXT,
    end TEXT,
    all_day_date TEXT,
    note_path TEXT,
    note_mtime REAL,
    note_hash TEXT,
    event_id TEXT,
    calendar_id TEXT,
    cal_sig TEXT,
    status TEXT DEFAULT 'pendiente',
    cat_confirmed INTEGER DEFAULT 0,
    hour_pending INTEGER DEFAULT 0,
    time_pending INTEGER DEFAULT 0,
    auto_category TEXT,
    auto_confident INTEGER DEFAULT 0,
    auto_source TEXT
);
CREATE TABLE IF NOT EXISTS calendars(
    category TEXT PRIMARY KEY,
    calendar_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    capture_id TEXT,
    kind TEXT NOT NULL,          -- category | hour | time
    auto_value TEXT,             -- lo que decidió el sistema
    chosen_value TEXT,           -- lo que eligió la persona
    transcript TEXT
);
CREATE TABLE IF NOT EXISTS history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    action TEXT NOT NULL,
    before TEXT                  -- JSON del estado previo (NULL = la captura no existía)
);
CREATE TABLE IF NOT EXISTS api_tokens(
    name TEXT PRIMARY KEY,
    hash TEXT NOT NULL UNIQUE,   -- sha256 del token: el token en claro no se guarda
    created TEXT NOT NULL,
    last_used TEXT,
    revoked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chat_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key TEXT NOT NULL,
    ts TEXT NOT NULL,
    role TEXT NOT NULL,          -- user | assistant
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sent_log(
    capture_id TEXT NOT NULL,
    key TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (capture_id, key)
);
CREATE TABLE IF NOT EXISTS snoozes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id TEXT NOT NULL,
    fire_at TEXT NOT NULL,
    sent_at TEXT
);
"""

# columnas agregadas después de la v0: se migran con ALTER TABLE
MIGRATIONS = {
    "seq": "INTEGER", "updated": "TEXT", "note_mtime": "REAL", "note_hash": "TEXT", "cal_sig": "TEXT",
    "cat_confirmed": "INTEGER DEFAULT 0", "hour_pending": "INTEGER DEFAULT 0",
    "time_pending": "INTEGER DEFAULT 0", "auto_category": "TEXT",
    "auto_confident": "INTEGER DEFAULT 0", "auto_source": "TEXT",
}

SNAPSHOT_FIELDS = (
    "title", "category", "type", "start", "end", "all_day_date", "status",
    "cat_confirmed", "hour_pending", "time_pending",
)


class DB:
    def __init__(self, path: Path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            have = {r["name"] for r in self._conn.execute("PRAGMA table_info(captures)")}
            for col, ddl in MIGRATIONS.items():
                if col not in have:
                    self._conn.execute(f"ALTER TABLE captures ADD COLUMN {col} {ddl}")
            # capturas viejas sin número corto: se numeran por fecha de creación
            nulls = self._conn.execute(
                "SELECT id FROM captures WHERE seq IS NULL ORDER BY created, id").fetchall()
            if nulls:
                nxt = (self._conn.execute("SELECT COALESCE(MAX(seq),0) FROM captures").fetchone()[0]) + 1
                for i, r in enumerate(nulls):
                    self._conn.execute("UPDATE captures SET seq=? WHERE id=?", (nxt + i, r["id"]))
            self._conn.commit()

    @contextmanager
    def _tx(self):
        with self._lock:
            with self._conn:
                yield self._conn

    def _all(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _one(self, sql: str, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    # ----------------------------------------------------------- capturas
    def insert_capture(self, row: dict) -> int:
        with self._tx() as c:
            if "seq" not in row:
                row["seq"] = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM captures").fetchone()[0]
            cols = ", ".join(row)
            c.execute(f"INSERT INTO captures({cols}) VALUES({', '.join('?' for _ in row)})", list(row.values()))
        return row["seq"]

    def update_capture(self, cid: str, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._tx() as c:
            c.execute(f"UPDATE captures SET {sets} WHERE id=?", [*fields.values(), cid])

    def get_capture(self, cid: str):
        return self._one("SELECT * FROM captures WHERE id=?", (cid,))

    def get_by_seq(self, seq: int):
        return self._one("SELECT * FROM captures WHERE seq=?", (seq,))

    def get_by_event_id(self, event_id: str):
        return self._one("SELECT * FROM captures WHERE event_id=?", (event_id,))

    def all_captures(self, include_deleted: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM captures" + ("" if include_deleted else " WHERE status != 'borrada'")
        return self._all(sql + " ORDER BY seq")

    def captures_between(self, start_iso: str, end_iso: str) -> list[sqlite3.Row]:
        """Con hora en [start, end) más los de día completo en ese rango. Excluye borradas."""
        return self._all(
            """SELECT * FROM captures
               WHERE status != 'borrada'
                 AND ((start >= ? AND start < ?) OR (start IS NULL AND all_day_date >= ? AND all_day_date < ?))
               ORDER BY COALESCE(start, all_day_date || 'T00:00')""",
            (start_iso, end_iso, start_iso[:10], end_iso[:10]),
        )

    def pending_tasks(self, limit: int = 30) -> list[sqlite3.Row]:
        return self._all(
            """SELECT * FROM captures WHERE type='tarea' AND status='pendiente'
               ORDER BY COALESCE(all_day_date, substr(start,1,10), '9999'), seq LIMIT ?""", (limit,))

    def inbox(self, limit: int = 30) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM captures WHERE category IS NULL AND status != 'borrada' ORDER BY seq DESC LIMIT ?",
            (limit,))

    def trashed(self, limit: int = 15) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM captures WHERE status='borrada' ORDER BY updated DESC, seq DESC LIMIT ?",
                         (limit,))

    # ---------------------------------------------------------- calendarios
    def get_calendar_id(self, category: str) -> str | None:
        r = self._one("SELECT calendar_id FROM calendars WHERE category=?", (category,))
        return r["calendar_id"] if r else None

    def set_calendar_id(self, category: str, calendar_id: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO calendars(category, calendar_id) VALUES(?, ?) "
                "ON CONFLICT(category) DO UPDATE SET calendar_id=excluded.calendar_id",
                (category, calendar_id),
            )

    # ------------------------------------------------------------- feedback
    def add_feedback(self, ts: str, capture_id: str, kind: str, auto_value: str | None,
                     chosen_value: str, transcript: str | None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO feedback(ts, capture_id, kind, auto_value, chosen_value, transcript) "
                "VALUES(?,?,?,?,?,?)",
                (ts, capture_id, kind, auto_value, chosen_value, transcript),
            )

    def feedback(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return self._all("SELECT * FROM feedback WHERE kind=? ORDER BY id", (kind,))
        return self._all("SELECT * FROM feedback ORDER BY id")

    # -------------------------------------------------------------- historial
    def history_add(self, ts: str, capture_id: str, action: str, before: dict | None) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO history(ts, capture_id, action, before) VALUES(?,?,?,?)",
                      (ts, capture_id, action, json.dumps(before) if before is not None else None))
            # se conservan las últimas 50 acciones
            c.execute("DELETE FROM history WHERE id <= (SELECT MAX(id) FROM history) - 50")

    def history_last_id(self) -> int:
        r = self._one("SELECT COALESCE(MAX(id), 0) AS m FROM history")
        return int(r["m"])

    def history_since(self, last_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM history WHERE id > ? ORDER BY id", (last_id,))

    def history_pop(self):
        with self._tx() as c:
            r = c.execute("SELECT * FROM history ORDER BY id DESC LIMIT 1").fetchone()
            if r:
                c.execute("DELETE FROM history WHERE id=?", (r["id"],))
            return r

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else default

    def kv_set(self, key: str, value: str | None) -> None:
        with self._tx() as c:
            if value is None:
                c.execute("DELETE FROM kv WHERE key=?", (key,))
            else:
                c.execute("INSERT INTO kv(key, value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ------------------------------------------------------ avisos enviados
    def sent_has(self, capture_id: str, key: str) -> bool:
        return self._one("SELECT 1 FROM sent_log WHERE capture_id=? AND key=?", (capture_id, key)) is not None

    def sent_mark(self, capture_id: str, key: str, ts: str) -> None:
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO sent_log(capture_id, key, sent_at) VALUES(?,?,?)",
                      (capture_id, key, ts))

    def sent_clear(self, capture_id: str) -> None:
        """Al mover un evento, sus avisos vuelven a estar pendientes."""
        with self._tx() as c:
            c.execute("DELETE FROM sent_log WHERE capture_id=? AND key LIKE 'b%'", (capture_id,))
            c.execute("DELETE FROM sent_log WHERE capture_id=? AND key LIKE 'missed%'", (capture_id,))

    def snooze_add(self, capture_id: str, fire_at: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO snoozes(capture_id, fire_at) VALUES(?,?)", (capture_id, fire_at))

    def snoozes_due(self, now_iso: str) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM snoozes WHERE sent_at IS NULL AND fire_at <= ?", (now_iso,))

    def snooze_done(self, snooze_id: int, ts: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE snoozes SET sent_at=? WHERE id=?", (ts, snooze_id))

    # ------------------------------------------------------- tokens de API
    def token_add(self, name: str, token_hash: str, ts: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM api_tokens WHERE name=? AND revoked=1", (name,))   # se puede reutilizar el nombre
            c.execute("INSERT INTO api_tokens(name, hash, created) VALUES(?,?,?)", (name, token_hash, ts))

    def token_by_hash(self, token_hash: str):
        return self._one("SELECT * FROM api_tokens WHERE hash=? AND revoked=0", (token_hash,))

    def token_touch(self, name: str, ts: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE api_tokens SET last_used=? WHERE name=?", (ts, name))

    def tokens(self) -> list[sqlite3.Row]:
        return self._all("SELECT name, created, last_used, revoked FROM api_tokens ORDER BY created")

    def token_revoke(self, name: str) -> bool:
        with self._tx() as c:
            return c.execute("UPDATE api_tokens SET revoked=1 WHERE name=? AND revoked=0", (name,)).rowcount > 0

    def has_active_tokens(self) -> bool:
        return self._one("SELECT 1 FROM api_tokens WHERE revoked=0 LIMIT 1") is not None

    # ------------------------------------------------- memoria del agente
    def chat_add(self, chat_key: str, ts: str, role: str, content: str, keep: int = 40) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO chat_history(chat_key, ts, role, content) VALUES(?,?,?,?)",
                      (chat_key, ts, role, content))
            c.execute("""DELETE FROM chat_history WHERE chat_key=? AND id NOT IN
                         (SELECT id FROM chat_history WHERE chat_key=? ORDER BY id DESC LIMIT ?)""",
                      (chat_key, chat_key, keep))

    def chat_recent(self, chat_key: str, since_iso: str, limit: int) -> list[sqlite3.Row]:
        rows = self._all(
            "SELECT role, content FROM chat_history WHERE chat_key=? AND ts>=? ORDER BY id DESC LIMIT ?",
            (chat_key, since_iso, limit))
        return list(reversed(rows))

    def chat_clear(self, chat_key: str) -> int:
        with self._tx() as c:
            return c.execute("DELETE FROM chat_history WHERE chat_key=?", (chat_key,)).rowcount
