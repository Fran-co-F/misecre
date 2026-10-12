#!/usr/bin/env bash
# Actualiza misecre en el servidor: trae el código, sincroniza dependencias y reinicia.
# Uso (en el VPS):  ~/misecre/deploy/update.sh
# No toca config/config.yaml, .env ni ~/.misecre (están fuera de git).
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/.."

echo "▶ trayendo cambios…"
git pull --ff-only

echo "▶ sincronizando dependencias…"
uv sync --frozen

echo "▶ validando configuración…"
uv run misecre check

echo "▶ reiniciando el servicio…"
systemctl --user restart misecre
sleep 4
systemctl --user --no-pager --lines=12 status misecre || true

echo "▶ diagnóstico…"
uv run misecre doctor || true
