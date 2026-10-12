"""Extrae fecha/hora de frases en español rioplatense, sin librerías externas.

Convenciones (documentadas porque son decisiones, no verdades):
- "el jueves" dicho un jueves => el jueves de la semana siguiente. "hoy" para hoy.
- Horas 1..7 sin indicador (am/pm/"de la tarde") se asumen PM y se marcan
  `ambiguous_hour`, para que el bot lo avise. 8..11 se asumen AM.
- Solo hora sin día: hoy si todavía no pasó, sino mañana.
- Solo día sin hora: `at` queda en None (tarea/evento de todo el día).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

WEEKDAYS = {
    "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3,
    "viernes": 4, "sabado": 5, "domingo": 6,
}
MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
NUMWORDS = {
    "una": 1, "un": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
    "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
}
_WD = "|".join(WEEKDAYS)
_MO = "|".join(MONTHS)
_NW = "|".join(NUMWORDS)


def fold(s: str) -> str:
    """minúsculas y sin tildes, conservando el largo (los spans valen para el original)."""
    return "".join(unicodedata.normalize("NFD", c)[0] for c in s.lower())


@dataclass
class When:
    day: date | None = None
    at: time | None = None
    ambiguous_hour: bool = False
    date_inferred: bool = False
    duration: timedelta | None = None
    spans: list[tuple[int, int]] = field(default_factory=list)

    @property
    def start(self) -> datetime | None:
        if self.day and self.at:
            return datetime.combine(self.day, self.at)
        return None


# ---------------------------------------------------------------- hora

_TIME_PATTERNS = [
    re.compile(r"\ba\s+las?\s+(?P<h>\d{1,2})(?:[:.h](?P<m>\d{2}))?(?:\s*(?P<suf>hs|h|horas|am|pm))?\b"),
    re.compile(rf"\ba\s+las?\s+(?P<hw>{_NW})\b"),
    re.compile(r"\b(?P<h>\d{1,2}):(?P<m>\d{2})(?:\s*(?P<suf>hs|h|horas|am|pm))?\b"),
    re.compile(r"\b(?P<h>\d{1,2})\s*(?P<suf>hs|horas|am|pm)\b"),
]
_NOON = re.compile(r"\b(?:al\s+)?(?P<w>mediodia|medio\s+dia|medianoche)\b")
_TAIL = re.compile(r"\s+(?P<t>y\s+media|y\s+cuarto|menos\s+cuarto)\b")
_PART = re.compile(r"\s+(?:de|por|en)\s+la\s+(?P<p>manana|tarde|noche|madrugada)\b")
_DURATION = re.compile(rf"\b(?:durante|por)\s+(?:(?P<n>\d+|{_NW})|(?P<half>media))\s+(?P<u>horas?|minutos?)\b")
_REL_TIME = re.compile(r"\ben\s+(?:(?P<n>\d+|un|una|dos|tres|cuatro|cinco)|(?P<half>media))\s+(?P<u>horas?|minutos?|hora)\b")


def _resolve_hour(h: int, suf: str | None, part: str | None) -> tuple[int, bool]:
    """-> (hora 0-23, ambigua)"""
    if suf in ("hs", "h", "horas"):
        return h, False
    if suf == "pm" or part in ("tarde",):
        return (h + 12 if h < 12 else h), False
    if part == "noche":
        if h == 12:
            return 0, False
        return (h + 12 if 6 <= h <= 11 else h), False
    if suf == "am":
        return (0 if h == 12 else h), False                  # "12 am" = medianoche
    if part in ("manana", "madrugada"):
        return (0 if h == 12 and part == "madrugada" else h), False
    if h == 0 or h >= 12:
        return h, False
    if h <= 7:
        return h + 12, True
    return h, False


def _extract_time(t: str):
    """-> (time|None, ambiguous, span|None)"""
    m = hour = minute = suf = None
    for pat in _TIME_PATTERNS:
        m = pat.search(t)
        if m:
            g = m.groupdict()
            hour = NUMWORDS[g["hw"]] if g.get("hw") else int(g["h"])
            minute = int(g.get("m") or 0)
            suf = g.get("suf")
            break
    if not m:
        n = _NOON.search(t)
        if n:
            hh = 0 if "noche" in n.group("w") else 12
            return time(hh, 0), False, n.span()
        return None, False, None
    if hour > 23 or minute > 59:
        return None, False, None

    end = m.end()
    minus_quarter = False
    tail = _TAIL.match(t, end)
    if tail:
        end = tail.end()
        kind = tail.group("t").split()[-1]
        if kind == "media":
            minute = 30
        elif kind == "cuarto" and tail.group("t").startswith("y"):
            minute = 15
        else:                       # "menos cuarto": se resta DESPUÉS de resolver AM/PM de la hora dicha
            minus_quarter = True
    part = None
    pm = _PART.match(t, end)
    if pm:
        end = pm.end()
        part = pm.group("p")
    hour_resolved, amb = _resolve_hour(hour % 24 if hour != 24 else 0, suf, part)
    if minus_quarter:
        total = (hour_resolved * 60 + minute - 15) % (24 * 60)
        hour_resolved, minute = divmod(total, 60)
    return time(hour_resolved % 24, minute), amb, (m.start(), end)


# ---------------------------------------------------------------- fecha

_LEFT = re.compile(r"(?:\b(?:para|el|este|esta|proximo|la|a|en|del)\s+)+$")


def _extend_left(t: str, start: int) -> int:
    m = _LEFT.search(t[:start])
    return m.start() if m else start


def _next_weekday(today: date, wd: int) -> date:
    delta = (wd - today.weekday()) % 7
    return today + timedelta(days=delta or 7)


def _safe_date(y: int, mth: int, d: int) -> date | None:
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def _extract_date(t: str, today: date):
    """-> (date|None, span|None)"""
    if m := re.search(r"\bpasado\s+manana\b", t):
        return today + timedelta(days=2), m.span()
    # "mañana" = día siguiente, salvo que sea parte del día: "por/a/de la mañana", "esta mañana"
    if m := re.search(r"(?<!la )(?<!esta )\bmanana\b", t):
        return today + timedelta(days=1), m.span()
    if m := re.search(r"\bhoy\b", t):
        return today, m.span()
    if m := re.search(rf"\ben\s+(?P<n>\d+|{_NW}|una)\s+(?P<u>dias?|semanas?)\b", t):
        raw = m.group("n")
        n = int(raw) if raw.isdigit() else NUMWORDS[raw]
        days = n * (7 if m.group("u").startswith("semana") else 1)
        return today + timedelta(days=days), m.span()
    if m := re.search(rf"\b(?P<wd>{_WD})\b(?:\s+(?:que\s+viene|proximo|siguiente))?", t):
        s = _extend_left(t, m.start())
        return _next_weekday(today, WEEKDAYS[m.group("wd")]), (s, m.end())
    if m := re.search(rf"\b(?P<d>\d{{1,2}})\s+de\s+(?P<mo>{_MO})(?:\s+de(?:l)?\s+(?P<y>\d{{4}}))?\b", t):
        d, mo = int(m.group("d")), MONTHS[m.group("mo")]
        if m.group("y"):
            res = _safe_date(int(m.group("y")), mo, d)
        else:
            res = _safe_date(today.year, mo, d)
            if res and res < today:
                res = _safe_date(today.year + 1, mo, d)
        if res:
            return res, (_extend_left(t, m.start()), m.end())
    if m := re.search(r"\b(?P<d>\d{1,2})/(?P<mo>\d{1,2})(?:/(?P<y>\d{2,4}))?\b", t):
        d, mo = int(m.group("d")), int(m.group("mo"))
        if m.group("y"):
            y = int(m.group("y"))
            y += 2000 if y < 100 else 0
            res = _safe_date(y, mo, d)
        else:
            res = _safe_date(today.year, mo, d)
            if res and res < today:
                res = _safe_date(today.year + 1, mo, d)
        if res:
            return res, (_extend_left(t, m.start()), m.end())
    if m := re.search(r"\bel\s+(?P<d>\d{1,2})\b", t):
        d = int(m.group("d"))
        res = _safe_date(today.year, today.month, d)
        if res and res < today:
            ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            res = _safe_date(ny, nm, d)
        if res:
            return res, m.span()
    return None, None


# ---------------------------------------------------------------- API

def parse_when(text: str, now: datetime) -> When:
    t = fold(text)
    w = When()

    # duración: "durante tres horas", "por 30 minutos", "durante media hora"
    if m := _DURATION.search(t):
        if m.group("half"):
            w.duration = timedelta(minutes=30)
        else:
            raw = m.group("n")
            n = int(raw) if raw.isdigit() else NUMWORDS[raw]
            w.duration = timedelta(hours=n) if m.group("u").startswith("hora") else timedelta(minutes=n)
        s0 = _extend_left(t, m.start())
        w.spans.append((s0, m.end()))
        t = t[:m.start()] + " " * (m.end() - m.start()) + t[m.end():]

    # relativos de hora: "en 2 horas", "en media hora", "en 30 minutos"
    if m := _REL_TIME.search(t):
        if m.group("half"):
            delta = timedelta(minutes=30)
        else:
            raw = m.group("n")
            n = int(raw) if raw.isdigit() else NUMWORDS[raw]
            delta = timedelta(hours=n) if m.group("u").startswith("hora") else timedelta(minutes=n)
        target = (now + delta).replace(second=0, microsecond=0)
        w.day, w.at = target.date(), target.time()
        w.spans.append(m.span())
        return w

    at, amb, tspan = _extract_time(t)
    if tspan:
        w.at, w.ambiguous_hour = at, amb
        s = _extend_left(t, tspan[0]) if tspan[0] > 0 else tspan[0]
        w.spans.append((s, tspan[1]))
        t = t[: tspan[0]] + " " * (tspan[1] - tspan[0]) + t[tspan[1]:]

    day, dspan = _extract_date(t, now.date())
    if dspan:
        w.day = day
        w.spans.append((_extend_left(t, dspan[0]), dspan[1]))

    if w.at and not w.day:
        w.day = now.date() if w.at > now.time() else now.date() + timedelta(days=1)
        w.date_inferred = True
    return w
