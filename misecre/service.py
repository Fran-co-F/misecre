"""Instalación como servicio de usuario: arranca solo al iniciar sesión y se reinicia si cae.

Linux   -> unidad `systemd --user` (probado)
Windows -> tarea programada de Task Scheduler "misecre" (escrito, SIN probar en Windows)
macOS   -> no soportado todavía
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "misecre.service"
TASK_NAME = "misecre"
LISTENER_UNIT = "misecre-listener.service"
LISTENER_TASK = "misecre-listener"


def _project_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _command(config: str | None, listener: bool = False) -> list[str]:
    cmd = [sys.executable, "-m", "misecre"]
    if config and not listener:
        cmd += ["-c", str(Path(config).resolve())]
    return cmd + ["listen" if listener else "run"]


def unit_text(config: str | None, listener: bool = False) -> str:
    exec_start = " ".join(shlex.quote(c) for c in _command(config, listener))
    desc = ("misecre — avisos de escritorio (listener)" if listener
            else "misecre — captura por voz, notas y calendario")
    return f"""[Unit]
Description={desc}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={_project_dir()}
ExecStart={exec_start}
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def install(config: str | None = None, listener: bool = False) -> str:
    unit = LISTENER_UNIT if listener else UNIT_NAME
    task = LISTENER_TASK if listener else TASK_NAME
    what = "listener de avisos" if listener else "misecre"
    if sys.platform.startswith("linux"):
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / unit).write_text(unit_text(config, listener), encoding="utf-8")
        _run("systemctl", "--user", "daemon-reload")
        r = _run("systemctl", "--user", "enable", "--now", unit)
        if r.returncode != 0:
            raise RuntimeError(f"systemctl falló: {r.stderr.strip() or r.stdout.strip()}")
        return (f"Servicio ({what}) instalado y arrancado ({unit_dir / unit}).\n"
                f"Ver logs:  journalctl --user -u {unit} -f\n"
                "Para que arranque al prender la PC aunque no inicies sesión: sudo loginctl enable-linger $USER")
    if sys.platform == "win32":
        py = Path(sys.executable)
        pyw = py.with_name("pythonw.exe")
        exe = str(pyw if pyw.exists() else py)
        args = " ".join(f'"{a}"' if " " in a else a for a in _command(config, listener)[1:])
        r = _run("schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED", "/TN", task,
                 "/TR", f'"{exe}" {args}')
        if r.returncode != 0:
            raise RuntimeError(f"schtasks falló: {r.stderr.strip() or r.stdout.strip()}")
        _run("schtasks", "/Run", "/TN", task)
        return f"Tarea programada creada: {what} arranca al iniciar sesión (y se lanzó ahora)."
    raise RuntimeError("Instalación de servicio no soportada en este sistema operativo")


def uninstall(listener: bool = False) -> str:
    unit = LISTENER_UNIT if listener else UNIT_NAME
    task = LISTENER_TASK if listener else TASK_NAME
    if sys.platform.startswith("linux"):
        _run("systemctl", "--user", "disable", "--now", unit)
        (Path.home() / ".config" / "systemd" / "user" / unit).unlink(missing_ok=True)
        _run("systemctl", "--user", "daemon-reload")
        return "Servicio desinstalado."
    if sys.platform == "win32":
        _run("schtasks", "/End", "/TN", task)
        r = _run("schtasks", "/Delete", "/F", "/TN", task)
        return "Tarea eliminada." if r.returncode == 0 else f"No se pudo eliminar: {r.stderr.strip()}"
    raise RuntimeError("No soportado en este sistema operativo")


def status(listener: bool = False) -> str:
    unit = LISTENER_UNIT if listener else UNIT_NAME
    task = LISTENER_TASK if listener else TASK_NAME
    if sys.platform.startswith("linux"):
        r = _run("systemctl", "--user", "status", unit, "--no-pager", "-n", "15")
        return r.stdout or r.stderr
    if sys.platform == "win32":
        r = _run("schtasks", "/Query", "/TN", task, "/V", "/FO", "LIST")
        return r.stdout or r.stderr
    return "No soportado en este sistema operativo"


def can_manage() -> bool:
    return sys.platform.startswith("linux") or sys.platform == "win32"
