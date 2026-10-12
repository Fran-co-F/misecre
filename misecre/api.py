"""API HTTP del core: la web de control, el listener de escritorio y scripts.

Seguridad (ver auth.py): tokens por dispositivo, límite de intentos por IP, lista blanca de IPs y
cabeceras estrictas. Pensada para exponerse detrás de HTTPS (Caddy/nginx) o de Tailscale.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import digest, learning
from .auth import AuthLimiter, TokenStore, ip_allowed, parse_networks
from .agent import Agent
from .core import Core, Result
from .operator import Operator
from .notify import Notifier
from .web import PAGE

LOOPBACK = ("127.0.0.1", "localhost", "::1")


MAX_TEXT = 4000                      # caracteres por mensaje
MAX_AUDIO_BYTES = 25 * 1024 * 1024    # 25 MB por audio


class TextIn(BaseModel):
    text: str = Field(max_length=MAX_TEXT)
    source: str = Field(default="api", max_length=40)


class CategoryIn(BaseModel):
    category: str


class PatchIn(BaseModel):
    title: str | None = None
    when: str | None = None            # lenguaje natural: "el viernes a las 10", "sin hora"
    category: str | None = None
    type: str | None = None
    duration_min: int | None = None
    status: str | None = None          # pendiente | hecha


class HourIn(BaseModel):
    part: str                          # am | pm


class TimeIn(BaseModel):
    text: str


class SnoozeIn(BaseModel):
    minutes: int | None = None


class PolicyIn(BaseModel):
    category: str
    before: list[int] = []
    digest_only: bool = False


class QuietIn(BaseModel):
    spec: str


class ModeIn(BaseModel):
    mode: str


class CalendarIn(BaseModel):
    category: str
    on: bool


class PauseIn(BaseModel):
    minutes: int | None = None         # None = reanudar


class RuleIn(BaseModel):
    category: str
    word: str
    action: str = "add"                # add | remove


class ChatIn(BaseModel):
    text: str = Field(max_length=MAX_TEXT)


class SuggestionIn(BaseModel):
    category: str
    word: str
    action: str                        # accept | reject


def _dump(r: Result) -> dict:
    return {
        "id": r.id, "seq": r.seq, "category": r.category, "type": r.type, "title": r.title,
        "status": r.status, "start": r.start.isoformat() if r.start else None,
        "end": r.end.isoformat() if r.end else None, "day": r.day.isoformat() if r.day else None,
        "in_calendar": bool(r.event_id), "calendar_off": r.calendar_off, "pending": r.pending, "warnings": r.warnings,
        "transcript": r.transcript, "source": r.source,
        "needs_category": r.needs_category, "ambiguous_hour": r.ambiguous_hour,
    }


def build_api(core: Core, runtime=None, notifier: Notifier | None = None,
              agent: Agent | None = None, operator: Operator | None = None) -> FastAPI:
    app = FastAPI(title="misecre", version="1.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    cfg = core.cfg.api
    tokens = TokenStore(core.db)
    limiter = AuthLimiter()
    nets = parse_networks(cfg.allowed_ips)
    notifier = notifier or (runtime.notifier if runtime else None)
    rem = runtime.reminders if runtime else None
    from .operator import MODES as operator_modes
    agent = agent or Agent(core)
    operator = operator or Operator(core, agent)

    def client_ip(request: Request) -> str:
        """IP real. Detrás de TU proxy se toma el valor que éste agregó (el N-ésimo desde la derecha):
        lo que el cliente haya puesto antes en X-Forwarded-For no se puede usar para falsificarla, ni con
        `$remote_addr` ni con `$proxy_add_x_forwarded_for`."""
        if cfg.trust_proxy:
            parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
            if parts:
                n = max(1, cfg.trusted_proxies)
                return parts[-n] if len(parts) >= n else parts[0]
        return request.client.host if request.client else "unknown"

    def proxied(request: Request) -> bool:
        """¿Llegó a través de un proxy? (entonces 'estar en loopback' no significa 'estar en tu PC')."""
        return cfg.trust_proxy or any(
            h in request.headers for h in ("x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host"))

    # ------------------------------------------------------ middleware
    def local_open_violation(request: Request) -> str | None:
        """Modo local sin tokens: la API está abierta a «cualquiera que llegue a 127.0.0.1», y eso incluye a
        una página web que visites (CSRF con POST «simples», o DNS rebinding). Se exige que el Host sea el
        propio y que un POST con Origin venga de la misma página."""
        host = request.headers.get("host", "")
        hostname = host.rsplit(":", 1)[0].strip("[]") if host.count(":") <= 1 or host.startswith("[") else host
        allowed = {"127.0.0.1", "localhost", "::1", cfg.host}
        if hostname not in allowed:
            return "Host no permitido (posible DNS rebinding): usá http://127.0.0.1:%d" % cfg.port
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != host:
                return "Origen no permitido (otra página intentó usar tu API local)"
        return None

    @app.middleware("http")
    async def guard_and_headers(request: Request, call_next):
        ip = client_ip(request)
        if not ip_allowed(ip, nets):
            return JSONResponse({"detail": "IP no permitida"}, status_code=403)
        if not tokens.configured() and not proxied(request):
            why = local_open_violation(request)
            if why:
                return JSONResponse({"detail": why}, status_code=403)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    def auth(request: Request, authorization: str = Header(default="")) -> str:
        ip = client_ip(request)
        if not tokens.configured():
            # Sin ningún token solo se permite uso local DIRECTO: ni red, ni a través de un proxy.
            if cfg.host not in LOOPBACK or proxied(request):
                raise HTTPException(
                    500, "Falta crear un token: en el servidor corré `misecre token create <dispositivo>`. "
                         "Sin tokens la API no responde a nada que llegue por la red o por un proxy.")
            return "local"
        if limiter.blocked(ip):
            raise HTTPException(429, "Demasiados intentos fallidos. Probá en unos minutos.")
        raw = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        name = tokens.verify(raw)
        if name is None:
            limiter.fail(ip)
            raise HTTPException(401, "token inválido")
        limiter.ok(ip)
        return name

    def guard(fn, *a, **k):
        async def run():
            try:
                return await asyncio.to_thread(fn, *a, **k)
            except KeyError as e:
                raise HTTPException(404, f"captura inexistente: {e.args[0]}")
            except (ValueError, LookupError) as e:
                raise HTTPException(422, str(e))
        return run()

    # ------------------------------------------------------------ público
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request):
        if not tokens.configured() and (cfg.host not in LOOPBACK or proxied(request)):
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8><title>misecre</title>"
                "<body style='font:16px system-ui;max-width:34em;margin:3em auto;padding:0 1em'>"
                "<h2>Falta crear un token</h2><p>En el servidor corré:</p>"
                "<pre>misecre token create celu</pre><p>Después recargá esta página y pegá el token.</p>",
                status_code=503)
        nonce = secrets.token_urlsafe(16)
        html = PAGE.replace("__NEEDS_TOKEN__", "true" if tokens.configured() else "false") \
                   .replace("__NONCE__", nonce)
        csp = (f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data:; connect-src 'self'; media-src 'self' blob:; "
               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return HTMLResponse(html, headers={"Content-Security-Policy": csp})

    @app.get("/health")
    def health():
        return {"ok": True}

    # ------------------------------------------------------------ captura
    @app.post("/capture/text")
    async def capture_text(body: TextIn, _: str = Depends(auth)):
        return _dump(await guard(core.capture_text, body.text, body.source))

    @app.post("/capture/audio")
    async def capture_audio(file: UploadFile = File(...), _: str = Depends(auth)):
        suffix = Path(file.filename or "audio.webm").suffix[:8] or ".webm"
        data = await file.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, f"El audio supera {MAX_AUDIO_BYTES // (1024 * 1024)} MB")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
        try:
            return _dump(await guard(core.capture_audio, tmp.name, "web-voz"))
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    # --------------------------------------------------------------- CRUD
    @app.get("/capture/{ref}")
    async def get_capture(ref: str, _: str = Depends(auth)):
        return _dump(await guard(core.get, ref))

    @app.patch("/capture/{ref}")
    async def patch_capture(ref: str, body: PatchIn, _: str = Depends(auth)):
        def apply():
            r = core.get(ref)
            if body.title is not None:
                r = core.update_title(ref, body.title)
            if body.when is not None:
                r = core.reschedule(ref, body.when)
            if body.duration_min is not None:
                r = core.set_duration(ref, body.duration_min)
            if body.category is not None:
                r = core.set_category(ref, body.category)
            if body.type is not None:
                r = core.set_type(ref, body.type)
            if body.status is not None:
                if body.status not in ("pendiente", "hecha"):
                    raise ValueError("status debe ser pendiente o hecha")
                r = core.complete(ref) if body.status == "hecha" else core.reopen(ref)
            return r
        return _dump(await guard(apply))

    @app.post("/capture/{ref}/category")
    async def set_category(ref: str, body: CategoryIn, _: str = Depends(auth)):
        return _dump(await guard(core.set_category, ref, body.category))

    @app.post("/capture/{ref}/hour")
    async def set_hour(ref: str, body: HourIn, _: str = Depends(auth)):
        return _dump(await guard(core.set_hour, ref, body.part))

    @app.post("/capture/{ref}/time")
    async def set_time(ref: str, body: TimeIn, _: str = Depends(auth)):
        return _dump(await guard(core.set_time, ref, body.text))

    @app.post("/capture/{ref}/all-day")
    async def all_day(ref: str, _: str = Depends(auth)):
        return _dump(await guard(core.skip_time, ref))

    @app.post("/capture/{ref}/snooze")
    async def snooze(ref: str, body: SnoozeIn, _: str = Depends(auth)):
        if rem is None:
            raise HTTPException(503, "avisos no disponibles")
        fire = await guard(rem.snooze, ref, body.minutes)
        return {"fire_at": fire.isoformat()}

    @app.delete("/capture/{ref}")
    async def delete(ref: str, _: str = Depends(auth)):
        await guard(core.delete, ref)
        return {"ok": True}

    @app.post("/capture/{ref}/restore")
    async def restore(ref: str, _: str = Depends(auth)):
        return _dump(await guard(core.restore, ref))

    @app.post("/undo")
    async def undo(_: str = Depends(auth)):
        action, r = await guard(core.undo)
        return {"undone": action, "capture": _dump(r) if r else None}

    # ----------------------------------------------------------- consultas
    @app.get("/agenda")
    def agenda(day: date | None = None, days: int = 1, _: str = Depends(auth)):
        d = day or core.today()
        return [_dump(core._from_row(r)) for r in core.agenda(d, days)]

    @app.get("/tasks")
    def tasks(_: str = Depends(auth)):
        return [_dump(r) for r in core.tasks()]

    @app.get("/inbox")
    def inbox(_: str = Depends(auth)):
        return [_dump(r) for r in core.inbox()]

    @app.get("/trash")
    def trash(_: str = Depends(auth)):
        return [_dump(r) for r in core.trash()]

    @app.get("/search")
    def search(q: str, _: str = Depends(auth)):
        return [_dump(r) for r in core.search(q)]

    @app.get("/digest/{kind}")
    def get_digest(kind: str, _: str = Depends(auth)):
        if kind == "pulse":
            return {"kind": kind, "text": digest.build_pulse(core)}
        if kind not in digest.BUILDERS:
            raise HTTPException(404, "kind debe ser daily, weekly, monthly o pulse")
        return {"kind": kind, "text": digest.BUILDERS[kind](core)}

    @app.get("/status")
    def status(name: str = Depends(auth)):
        acc = learning.accuracy(core)
        return {**core.stats(), "last_sync": core.db.kv_get("sync:last"),
                "ai": core.ai.available, "ai_calls_today": core.ai.calls_today(),
                "agent": agent.available, "agent_calls_today": agent.calls_today(),
                "agent_usage_today": agent.usage_today(),
                "routing_today": operator.stats_today(),
                "category_accuracy": list(acc) if acc else None,
                "suggestions": len(learning.suggest_rules(core)),
                "silenced": (rem.silenced(core.now()) if rem else None), "device": name,
                "categories": core.cfg.category_names}

    @app.post("/sync")
    async def sync(_: str = Depends(auth)):
        if runtime is None:
            raise HTTPException(503, "sincronización no disponible")
        rep = await asyncio.to_thread(runtime.syncer.run)
        return {"changes": rep.changes, "imported": rep.imported,
                "healed": rep.healed, "silenced": rep.silenced, "errors": rep.errors}

    # ------------------------------------------------------------ ajustes
    def _settings() -> dict:
        until = rem.paused_until(core.now()) if rem else None
        return {
            "policies": {c: {"before": rem.policy_for(c).before, "digest_only": rem.policy_for(c).digest_only}
                         for c in core.cfg.category_names} if rem else {},
            "quiet": rem.quiet_spec() if rem else core.cfg.notifications.quiet_hours,
            "paused_until": until.isoformat() if until else None,
            "snooze_minutes": core.cfg.notifications.snooze_minutes,
            "calendar": {c: core.calendar_wanted(c) for c in core.cfg.category_names},
            "mode": operator.mode, "modes": operator_modes,
            "periodic": core.cfg.digests.periodic,
            "rules": core.rules_view(),
            "suggestions": [s.__dict__ for s in learning.suggest_rules(core)],
        }

    @app.get("/settings")
    def get_settings(_: str = Depends(auth)):
        return _settings()

    @app.post("/settings/policy")
    async def set_policy(body: PolicyIn, _: str = Depends(auth)):
        if rem is None:
            raise HTTPException(503, "avisos no disponibles")
        await guard(rem.set_policy, body.category, body.before, body.digest_only)
        return _settings()

    @app.post("/settings/quiet")
    async def set_quiet(body: QuietIn, _: str = Depends(auth)):
        if rem is None:
            raise HTTPException(503, "avisos no disponibles")
        await guard(rem.set_quiet, body.spec)
        return _settings()

    @app.post("/settings/pause")
    async def set_pause(body: PauseIn, _: str = Depends(auth)):
        if rem is None:
            raise HTTPException(503, "avisos no disponibles")
        if body.minutes:
            await guard(rem.pause, body.minutes)
        else:
            rem.resume()
        return _settings()

    @app.post("/settings/mode")
    async def set_mode(body: ModeIn, _: str = Depends(auth)):
        await guard(operator.set_mode, body.mode)
        return _settings()

    @app.post("/settings/calendar")
    async def set_calendar(body: CalendarIn, _: str = Depends(auth)):
        await guard(core.set_calendar_for, body.category, body.on)
        return _settings()

    @app.post("/rules")
    async def rules(body: RuleIn, _: str = Depends(auth)):
        if body.action == "remove":
            await guard(core.remove_rule, body.category, body.word)
        else:
            await guard(core.add_rule, body.category, body.word)
        return _settings()

    @app.post("/suggestions")
    async def suggestions(body: SuggestionIn, _: str = Depends(auth)):
        if body.action == "accept":
            await guard(core.add_rule, body.category, body.word)
        elif body.action == "reject":
            learning.reject(core, body.category, body.word)
        else:
            raise HTTPException(422, "action debe ser accept o reject")
        return _settings()

    # ------------------------------------------------------ agente (chat)
    @app.post("/agent/chat")
    async def agent_chat(body: ChatIn, _: str = Depends(auth)):
        """Chat: reglas primero; la IA solo si hace falta (y solo si está activa)."""
        try:
            r = await asyncio.to_thread(operator.handle, "web", body.text, "web")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"reply": r.text, "source": r.source, "mutations": r.mutations, "refs": r.refs,
                "calls": r.calls, "learned": r.learned,
                "candidates": [{"ref": c.seq, "title": c.title} for c in r.candidates]}

    @app.delete("/agent/chat")
    async def agent_forget(_: str = Depends(auth)):
        return {"forgotten": agent.forget("web")}

    # ----------------------------------------------- eventos en tiempo real
    @app.get("/events")
    async def events(request: Request, _: str = Depends(auth)):
        """SSE: avisos, resúmenes y cambios de sincronización, para web y listener de escritorio."""
        if notifier is None:
            raise HTTPException(503, "eventos no disponibles")
        q = notifier.subscribe()

        async def stream():
            try:
                yield ": conectado\n\n"
                while not getattr(app.state, "stopping", False):
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=5)
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                notifier.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
