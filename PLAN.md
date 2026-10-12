# misecre — estado

Actualizado: 2026-10-12 · ~210 tests · linter limpio.

## Qué es hoy

Captura por voz/texto → SQLite → Google Calendar, con avisos propios, resúmenes y un **agente de IA opcional**
que opera la agenda por chat (bot y web). Sin Markdown ni Obsidian (eliminados: la web alcanza).

## Estado por componente

| Componente | Estado | Verificado |
|---|---|---|
| Captura (Whisper local, parser es-AR, reglas) | ✅ | tests + voz real en Telegram |
| CRUD completo (`#n`, editar, mover, hecha, borrar, restaurar, deshacer, buscar) | ✅ | tests |
| Google Calendar (3 calendarios, sin avisos nativos) | ✅ | `selftest-calendar` contra Google real |
| Sincronización de dos vías con Google | ✅ | tests + `misecre sync` real |
| Avisos (silencio, pausa, posponer, dedupe, catch-up) | ✅ | tests + SSE→listener real |
| Resúmenes diario/semanal/mensual | ✅ | tests |
| Web de control (CRUD, voz, ajustes, chat) | ✅ | jsdom contra la API real |
| Acceso remoto (tokens por dispositivo, límite, IPs, CSP) | ✅ | tests |
| Listener de escritorio (`misecre listen`) | ✅ Linux | e2e real · ⚠️ Windows sin probar |
| Menú `/` del bot | ✅ | `getMyCommands` contra Telegram |
| **Enrutador de reglas** (completar, consultar, deshacer, recategorizar, crear) | ✅ | tests + API real: 4 de 8 mensajes sin IA |
| **Aprendizaje desde la IA** (auto-reglas, reversibles) | ✅ | tests + API real: aprendió «paddle» |
| **Agente de IA (operador por chat)** | ✅ | tests con cliente guionado **y contra la API real** (Haiku 5.5, 2 mensajes del ejemplo) |
| Aprendizaje (`/sugerencias`, reglas de usuario) | ✅ | tests |

## Voz y modos

- `/modo reglas|ia|solo-reglas` (y `ai.mode`): quién responde primero.
- Voz: `stt.provider` local | groq | openai (con respaldo local), vocabulario dinámico con TUS palabras y tildes
  correctas, parámetros de decodificación afinados. **No medido con audios reales**: la mejora esperada viene
  sobre todo de usar un modelo mayor o la nube.

## Calendario por categoría y pulsos

- `calendar.skip_categories` / `/calendario ocio off`: la categoría avisa pero no se publica en Google
  (al apagarla se retiran sus eventos; al prenderla, la sync los recrea).
- `digests.periodic` + `/pulso`: resumen de "cómo está todo" a horas configurables, sin ráfagas ni vacíos.
- Voz: modelo desde caché local sin consultar la red, precarga al arrancar y tiempos en `/estado`.
- Diagnóstico de cuelgues: `kill -USR1 <pid>` vuelca las pilas de los hilos en `~/.misecre/logs/stacks.log`.

## El agente

- **Herramientas** (las mismas operaciones que bot y web): `create_item`, `search_items`, `list_items`,
  `get_item`, `update_item`, `set_category`, `set_status` (done/reopen/delete/restore), `undo_last`.
- **Flujo del ejemplo pedido:** *"mete un recordatorio de ir a comprar pan"* → `create_item`;
  *"ya fui a comprar pan"* → `search_items` + `set_status(done)`. Verificado con un modelo guionado.
- **Salvaguardas:** botón ↩ Deshacer tras cada cambio; borrar es a papelera; los datos de la agenda viajan
  como `tool_result`, nunca en el prompt del sistema; tope de rondas y de llamadas por día; si la API falla
  antes de actuar se cae a reglas sin perder la nota; lo que decide el agente no es "corrección humana".
- **Reglas primero, IA de respaldo.** El enrutador (`intent.py`) resuelve sin IA lo común; el agente solo
  entra con categoría dudosa, modificaciones y preguntas libres.
- **1 llamada por mensaje** (antes 3): `create_item` recibe la categoría, `complete_matching` busca y completa en
  un paso, y si el modelo solo escribió y todo salió bien, el sistema arma la confirmación sin 2ª llamada.
- **Costo:** ~2,5 mil tokens por llamada ≈ US$0,0003; 8 mensajes reales = 4 llamadas ≈ US$0,001.

## Qué NO está verificado

| | Riesgo |
|---|---|
| Clasificación de respaldo con Haiku (`/anotar` sin chat) | solo con cliente simulado; el agente sí se verificó con la API real |
| Web en un **navegador real** (layout, micrófono, notificaciones) | jsdom no renderiza ni graba audio |
| Windows | servicio, toast y rutas escritos, no ejecutados |
| Whisper con audios reales | solo se probó la carga y el decodificado |


## Pendiente

1. Parser v2: repetición ("todos los lunes", `RRULE`), rangos ("de 10 a 12"), varias cosas por audio.
   Los eventos recurrentes de Google se ignoran al sincronizar.
2. Guía de despliegue en VPS más detallada (la base está en el README).
3. Afinar el prompt del agente con conversaciones reales.

## Hallazgos que cambiaron el diseño

- `events.move` de Google resetea los recordatorios → se re-silencian al mover y la sync silencia lo creado a mano.
- El menú de `/` no se registraba (`post_init` no corre con ciclo de vida manual).
- El puerto 8765 lo usa AnkiConnect → 8787 por defecto y la API ya no tumba el bot si no puede abrirse.
- "recordatorio: ir a comprar pan" conservaba la etiqueta en el título → limpiador de etiquetas.

## Límites conocidos

- Sin la PC/VPS prendida no hay avisos (Google está en silencio a propósito).
- Las notificaciones del navegador solo funcionan con la pestaña abierta (para lo demás: Telegram o `listen`).
- Las columnas `note_*` de la DB son legado del vault y no se usan.
