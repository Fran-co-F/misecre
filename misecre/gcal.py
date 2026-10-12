"""Google Calendar: un calendario por categoría, eventos SIN avisos nativos.

Los avisos los emite el scheduler propio (fase 4); acá se crean con
`reminders.useDefault=false` y `overrides=[]` para que Google quede en silencio.
"""
from __future__ import annotations

import os

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .config import Config
from .db import DB

SCOPES = ["https://www.googleapis.com/auth/calendar"]


@dataclass
class CalEvent:
    """Evento normalizado (lo que ve la sincronización; independiente de la API de Google)."""
    id: str
    calendar_id: str
    category: str
    title: str
    start: datetime | None      # naive, en la zona horaria configurada (None si es de todo el día)
    end: datetime | None
    day: date | None            # solo para eventos de todo el día
    deleted: bool = False
    recurring: bool = False
    updated: str = ""
    reminders_default: bool = False   # True => Google avisaría por su cuenta (queremos que no)


class GoogleCalendar:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db
        self._svc = None

    # -- auth -----------------------------------------------------------
    def _service(self):
        if self._svc is not None:
            return self._svc
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        token_path = self.cfg.data_dir / "token.json"
        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                cred_file = self.cfg.data_dir / self.cfg.calendar.credentials_file
                if not cred_file.exists():
                    raise FileNotFoundError(
                        f"Falta {cred_file}. Bajalo desde Google Cloud Console (OAuth client 'Desktop app')."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(cred_file), SCOPES)
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            if os.name == "posix":
                token_path.chmod(0o600)
        import google_auth_httplib2
        import httplib2

        # Sin timeout, una conexión colgada bloquea el hilo (y el cierre del programa) indefinidamente.
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
        self._svc = build("calendar", "v3", http=http, cache_discovery=False)
        return self._svc

    def authorize(self) -> None:
        """Fuerza el flujo OAuth (abre el navegador) y asegura los 3 calendarios."""
        svc = self._service()
        for cat in self.cfg.category_names:
            cal_id = self.calendar_id(cat)
            # los calendarios creados por API quedan ocultos en la UI de Google: se los marca visibles
            svc.calendarList().patch(calendarId=cal_id, body={"selected": True, "hidden": False}).execute()

    # -- calendarios ----------------------------------------------------
    def calendar_id(self, category: str) -> str:
        cached = self.db.get_calendar_id(category)
        if cached:
            return cached
        name = self.cfg.calendar.names.get(category) or f"Misecre · {category.capitalize()}"
        svc = self._service()
        page = None
        while True:
            res = svc.calendarList().list(pageToken=page).execute()
            for item in res.get("items", []):
                if item.get("summary") == name:
                    self.db.set_calendar_id(category, item["id"])
                    return item["id"]
            page = res.get("nextPageToken")
            if not page:
                break
        created = svc.calendars().insert(body={"summary": name, "timeZone": self.cfg.timezone}).execute()
        self.db.set_calendar_id(category, created["id"])
        svc.calendarList().patch(calendarId=created["id"], body={"selected": True, "hidden": False}).execute()
        return created["id"]

    # -- eventos --------------------------------------------------------
    def _body(self, title: str, description: str, start: datetime | None, end: datetime | None, day: date | None):
        body: dict = {
            "summary": title,
            "description": description,
            "reminders": {"useDefault": False, "overrides": []},
        }
        if start:
            end = end or start + timedelta(minutes=self.cfg.default_event_minutes)
            body["start"] = {"dateTime": start.isoformat(), "timeZone": self.cfg.timezone}
            body["end"] = {"dateTime": end.isoformat(), "timeZone": self.cfg.timezone}
        else:
            assert day is not None
            body["start"] = {"date": day.isoformat()}
            body["end"] = {"date": (day + timedelta(days=1)).isoformat()}
        return body

    def create_event(self, category: str, title: str, description: str, *,
                     start: datetime | None, end: datetime | None, day: date | None) -> tuple[str, str]:
        cal_id = self.calendar_id(category)
        ev = self._service().events().insert(
            calendarId=cal_id, body=self._body(title, description, start, end, day)
        ).execute()
        return ev["id"], cal_id

    def update_event(self, event_id: str, calendar_id: str, title: str, description: str, *,
                     start: datetime | None, end: datetime | None, day: date | None) -> None:
        """Actualiza SOLO lo que misecre administra: título, cuándo y avisos (PATCH).

        No toca descripción, lugar, invitados ni videollamada: un evento creado a mano en Google e importado
        conserva todo lo suyo. Los eventos de varios días mantienen su duración."""
        svc = self._service()
        body: dict = {"summary": title, "reminders": {"useDefault": False, "overrides": []}}
        if start:
            end = end or start + timedelta(minutes=self.cfg.default_event_minutes)
            body["start"] = {"dateTime": start.isoformat(), "timeZone": self.cfg.timezone, "date": None}
            body["end"] = {"dateTime": end.isoformat(), "timeZone": self.cfg.timezone, "date": None}
        else:
            assert day is not None
            span = 1
            try:
                cur = svc.events().get(calendarId=calendar_id, eventId=event_id, fields="start,end").execute()
                cs, ce = cur.get("start", {}), cur.get("end", {})
                if "date" in cs and "date" in ce:
                    span = max(1, (date.fromisoformat(ce["date"]) - date.fromisoformat(cs["date"])).days)
            except Exception:  # noqa: BLE001
                span = 1
            body["start"] = {"date": day.isoformat(), "dateTime": None, "timeZone": None}
            body["end"] = {"date": (day + timedelta(days=span)).isoformat(), "dateTime": None, "timeZone": None}
        svc.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()

    def move_event(self, event_id: str, from_cal: str, to_category: str) -> tuple[str, str]:
        to_cal = self.calendar_id(to_category)
        if to_cal != from_cal:
            self._service().events().move(calendarId=from_cal, eventId=event_id, destination=to_cal).execute()
            # Google resetea los recordatorios al mover (verificado con `selftest-calendar`)
            self.silence_event(event_id, to_cal)
        return event_id, to_cal

    def silence_event(self, event_id: str, calendar_id: str) -> None:
        """Quita los avisos de Google del evento: los emite misecre, una sola vez."""
        self._service().events().patch(
            calendarId=calendar_id, eventId=event_id,
            body={"reminders": {"useDefault": False, "overrides": []}},
        ).execute()

    def delete_event(self, event_id: str, calendar_id: str) -> None:
        self._service().events().delete(calendarId=calendar_id, eventId=event_id).execute()

    # -- lectura para sincronizar -----------------------------------------
    def _to_local(self, iso: str) -> datetime:
        from zoneinfo import ZoneInfo

        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo(self.cfg.timezone)).replace(tzinfo=None)
        return dt

    def _normalize(self, item: dict, category: str, calendar_id: str) -> CalEvent:
        deleted = item.get("status") == "cancelled"
        start = end = day = None
        st, en = item.get("start") or {}, item.get("end") or {}
        if "dateTime" in st:
            start = self._to_local(st["dateTime"])
            end = self._to_local(en["dateTime"]) if "dateTime" in en else None
        elif "date" in st:
            day = date.fromisoformat(st["date"])
        return CalEvent(
            id=item["id"], calendar_id=calendar_id, category=category,
            title=(item.get("summary") or "").strip(), start=start, end=end, day=day,
            deleted=deleted, recurring=bool(item.get("recurrence") or item.get("recurringEventId")),
            updated=item.get("updated", ""),
            reminders_default=bool((item.get("reminders") or {}).get("useDefault")),
        )

    def list_changes(self, category: str, updated_min: str | None, past_days: int = 1) -> list[CalEvent]:
        """Eventos modificados (o cancelados) desde `updated_min`. Sin `updated_min`: los de
        los últimos `past_days` días en adelante."""
        cal_id = self.calendar_id(category)
        svc = self._service()
        params: dict = {"calendarId": cal_id, "showDeleted": True, "singleEvents": False, "maxResults": 250}
        if updated_min:
            params["updatedMin"] = updated_min
        else:
            since = datetime.now(timezone.utc) - timedelta(days=past_days)
            params["timeMin"] = since.isoformat().replace("+00:00", "Z")
        out: list[CalEvent] = []
        token = None
        while True:
            res = svc.events().list(pageToken=token, **params).execute()
            out += [self._normalize(i, category, cal_id) for i in res.get("items", [])]
            token = res.get("nextPageToken")
            if not token:
                break
        return out
