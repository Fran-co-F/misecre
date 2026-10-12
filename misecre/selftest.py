"""Prueba de integración contra Google Calendar real (crea, edita, mueve y borra un evento propio).

Se ejecuta a mano: `misecre selftest-calendar`. El evento lleva el prefijo SELFTEST_PREFIX para que la
sincronización lo ignore, y se borra siempre al final.
"""
from __future__ import annotations

from datetime import timedelta

SELFTEST_PREFIX = "[misecre-selftest]"


def run_calendar_selftest(core) -> int:
    cal = core.calendar
    if cal is None:
        print("✗ calendar.enabled es false en config.yaml")
        return 1
    cats = core.cfg.category_names
    a, b = cats[0], cats[1]
    start = (core.now() + timedelta(days=2)).replace(hour=3, minute=0)
    end = start + timedelta(minutes=30)
    title = f"{SELFTEST_PREFIX} prueba"
    eid = cal_id = None
    failures = 0

    def step(name: str, ok: bool, extra: str = "") -> None:
        nonlocal failures
        print(("✓ " if ok else "✗ ") + name + (f" — {extra}" if extra else ""))
        failures += 0 if ok else 1

    def find(category: str, ev_id: str):
        return next((e for e in cal.list_changes(category, None, 5) if e.id == ev_id), None)

    try:
        eid, cal_id = cal.create_event(a, title, "autoprueba", start=start, end=end, day=None)
        step(f"crear evento con hora en “{a}”", True, eid)
        e = find(a, eid)
        step("leerlo de vuelta con la hora correcta", e is not None and e.start == start and e.end == end,
             f"{e.start}–{e.end}" if e else "no apareció")

        # un dato "ajeno" (lugar) que misecre NO administra: tiene que sobrevivir a las actualizaciones
        cal._service().events().patch(calendarId=cal_id, eventId=eid,
                                      body={"location": "misecre-selftest-lugar"}).execute()

        cal.update_event(eid, cal_id, title + " (todo el día)", "", start=None, end=None, day=start.date())
        e = find(a, eid)
        step("pasar a evento de todo el día", e is not None and e.start is None and e.day == start.date())
        raw = cal._service().events().get(calendarId=cal_id, eventId=eid).execute()
        step("conserva lo que misecre no administra (lugar y descripción)",
             raw.get("location") == "misecre-selftest-lugar" and raw.get("description") == "autoprueba",
             f"location={raw.get('location')!r} description={raw.get('description')!r}")

        cal.update_event(eid, cal_id, title, "", start=start, end=end, day=None)
        e = find(a, eid)
        step("volver a evento con hora", e is not None and e.start == start)

        eid, cal_id = cal.move_event(eid, cal_id, b)
        step(f"mover a “{b}”", find(b, eid) is not None)
        e = find(b, eid)
        step("el calendario de origen ya no lo tiene vivo",
             not any(x.id == eid and not x.deleted for x in cal.list_changes(a, None, 5)))
        raw = cal._service().events().get(calendarId=cal_id, eventId=eid).execute()
        rem = raw.get("reminders", {})
        step("sin avisos nativos de Google (los avisa misecre)",
             rem.get("useDefault") is False and not rem.get("overrides"), f"reminders={rem}")
    except Exception as ex:  # noqa: BLE001
        step("ejecución sin excepciones", False, repr(ex))
    finally:
        if eid and cal_id:
            try:
                cal.delete_event(eid, cal.calendar_id(b) if find_safe(cal, b, eid) else cal_id)
                print("✓ limpieza: evento de prueba borrado")
            except Exception as ex:  # noqa: BLE001
                print(f"✗ limpieza: no pude borrar {eid}: {ex!r}")
                failures += 1
    print("RESULTADO:", "OK" if failures == 0 else f"{failures} fallo(s)")
    return 0 if failures == 0 else 1


def find_safe(cal, category: str, ev_id: str) -> bool:
    try:
        return any(e.id == ev_id and not e.deleted for e in cal.list_changes(category, None, 5))
    except Exception:  # noqa: BLE001
        return False
