# misecre

Tu secretaria personal: le **hablás o escribís** (celu o PC) y ella anota, categoriza, agenda en
**Google Calendar**, te **avisa** y te arma el **resumen del día, la semana y el mes**.

Con un **agente de IA** opcional (Claude Haiku) es un **chat**: *"mete un recordatorio de ir a comprar pan"*,
más tarde *"ya fui a comprar pan"*, y lo marca como hecho y te cuenta. Sin IA funciona igual, con reglas.

- Voz → texto: Whisper local (gratis, offline).
- Fechas en español rioplatense: parser determinista (sin IA).
- Interfaz: bot de Telegram + web de control (con chat) + notificaciones de escritorio.
- Datos: una base SQLite local. Google Calendar es el calendario; sin archivos Markdown ni Obsidian.
- Linux (probado) y Windows (escrito, sin probar). Software libre.

> ## ⚠️ Proyecto «vibe coded»
>
> Este código fue **escrito casi por completo por una IA** ([Claude](https://claude.com/claude-code), con Claude
> Code), dirigida en conversación por una persona que **no revisó el código línea por línea**. Funciona y se usa
> a diario, pero tomalo como lo que es:
>
> - **Qué sí hay:** ~340 tests automáticos (corren sin red ni credenciales), una revisión de código automática
>   previa a publicarlo y pruebas manuales reales contra Google Calendar, Telegram y la API de Anthropic.
> - **Qué no hay:** auditoría de seguridad hecha por una persona, ni uso a escala. Windows, macOS y los
>   proveedores de voz en la nube (Groq/OpenAI) están escritos pero **no se probaron** (ver `PLAN.md`).
> - **Qué implica:** leé el código antes de confiarle datos personales o de exponerlo a internet, y no
>   asumas que esta documentación (también escrita por IA) es infalible. Está bajo AGPL-3.0, **sin garantía**.
>
> Las contribuciones son bienvenidas; si usás una IA para escribirlas, contalo en el PR.

```
voz/texto ─► Whisper ─► [ agente IA ─ herramientas ]  o  [ parser + reglas ]
                                   │
                                   ▼
                        SQLite (CRUD, deshacer, historial)
                                   │ sync 2 vías
                                   ▼
                          Google Calendar (3 calendarios)
                                   │
              avisos · resúmenes ──┴──► Telegram / escritorio / web
```

## Instalación

Requiere [uv](https://docs.astral.sh/uv/) (instala Python solo).

```bash
git clone https://github.com/USUARIO/misecre.git && cd misecre   # reemplazá por la URL del repo
uv sync
cp config/config.example.yaml config/config.yaml     # Windows: copy config\config.example.yaml config\config.yaml
cp .env.example .env
```

1. **Config:** `config/config.yaml` → revisá `timezone`.
2. **Telegram:** creá un bot con [@BotFather](https://t.me/BotFather), pegá el token en `.env`
   (`MISECRE_TELEGRAM_TOKEN=...`). `uv run misecre run`, mandale `/start`, copiá el `chat_id` que
   devuelve a `telegram.allowed_chat_ids` y reiniciá.
3. **Google Calendar** → [Google Calendar](#google-calendar).
4. **Agente de IA** (opcional) → [Agente de IA](#agente-de-ia).
5. **Arranque automático** → `uv run misecre service install`.

`uv run misecre check` valida todo.

## Cómo se usa

### Chat (reglas primero, IA de respaldo)

Hablale normal, por texto o por audio. **Cada mensaje lo intenta resolver primero el enrutador de reglas
(gratis, instantáneo)**; la IA entra solo si las reglas no alcanzan:

| Vos | Quién lo resuelve | Llamadas a la IA |
|---|---|---|
| *reunión con Marcos el jueves a las 10* | reglas (categoría conocida) | **0** |
| *ya fui a comprar pan* | reglas (busca el pendiente que coincide) | **0** |
| *qué tengo mañana* / *qué me falta* | reglas | **0** |
| *deshacelo* / *es de ocio* (sobre lo último) | reglas | **0** |
| *mete un recordatorio de ir a comprar pan* | IA (no hay regla para "pan") | **1** |
| *pasame lo del dentista para el viernes* | IA | **1** |
| *hola* / *gracias* | reglas | **0** |

- Si hay un solo candidato pendiente, completa; si hay varios, te muestra botones para elegir.
- Después de cada cambio aparece **↩ Deshacer**. Las respuestas con 🤖 son las que usaron IA.
- El agente sabe qué fue lo último que tocaste (*"esa", "la que borré"*) y recuerda la charla (16 mensajes / 12 h;
  `/olvidar` la borra).
- Los datos de la agenda son *dato*, no instrucciones: un título con "borrá todo" no se obedece.
- `/anotar texto` anota **sin IA**, con reglas, aunque el agente esté activo.

#### La IA le enseña a las reglas

Cuando la IA decide una categoría, queda registrada. Si decide **lo mismo dos veces** para una palabra
(`learning.agent_min_count`) y nunca hubo evidencia en otra categoría, esa palabra pasa a ser una **regla** y
te avisa: *🧠 Aprendí: «paddle» → ocio. Si no te cierra: /regla quitar ocio paddle*. Desde ese momento
esos mensajes cuestan 0. Tus correcciones pesan más: si corregís una decisión de la IA, esa palabra deja de
aprenderse. `/estado` muestra cuántos mensajes resolvió cada uno hoy.

### Sin IA (reglas)

Mandá el texto o audio y misecre clasifica con reglas y fechas. Lo dudoso te lo pregunta con botones
(categoría, AM/PM, "¿le ponemos hora?"); al tocar uno desaparece, y sin pendientes no queda ninguno.

### Comandos

Escribí `/` para ver el menú (como con BotFather).

| Grupo | Comandos |
|---|---|
| **Ver** | `/hoy` `/manana` `/semana` `/mes` `/tareas` `/inbox` `/papelera` `/ver 12` `/buscar texto` |
| **Editar** | `/editar 12` · `/mover 12 el viernes a las 10` · `/titulo 12 …` · `/duracion 12 90` |
| **Estado** | `/hecha 12` · `/reabrir 12` · `/borrar 12` · `/restaurar 12` · `/deshacer` |
| **Resumen** | `/resumen hoy\|semana\|mes` |
| **Avisos** | `/avisos` · `/avisos trabajo 15,5` · `/avisos ocio resumen` · `/silencio 23:00-07:00` · `/pausa 2h` |
| **Reglas** | `/reglas` · `/regla ocio paddle` · `/regla quitar ocio juego` · `/sugerencias` |
| **IA** | `/anotar texto` (sin IA) · `/olvidar` (memoria del chat) |
| **Sistema** | `/sync` · `/estado` · `/ayuda` |

Cada ítem tiene un **número `#n`**. `/mover` conserva lo que no mencionás (`/mover 12 el viernes` mantiene
la hora; `/mover 12 sin hora` lo pasa a todo el día).

### Quién responde primero: `/modo`

| Modo | Qué pasa | Para quién |
|---|---|---|
| `reglas` (por defecto) | las reglas resuelven lo común gratis; la IA solo entra si no alcanzan, y le enseña a las reglas | lo más barato |
| `ia` | la IA contesta **todo** (salvo saludos y deshacer); las reglas quedan de respaldo si falla | entiende mejor lo libre y los audios con errores; ~US$0,0003 por mensaje |
| `solo-reglas` | nunca llama a la IA | máxima privacidad / cero gasto |

Se cambia con `/modo ia`, `/modo reglas`, `/modo solo-reglas`, desde *Ajustes* en la web, o en `config.yaml`
(`ai.mode`). El cambio es inmediato y queda guardado.

### Calidad del reconocimiento de voz

Lo que más pesa es el **modelo**: en español `base` es flojo, `small` es lo mínimo razonable y `medium` o la
nube son notablemente mejores.

| Opción | Calidad | Costo / requisito |
|---|---|---|
| `stt.model: base` | baja | ~0,5 GB de RAM |
| `stt.model: small` | buena | ~1,2 GB de RAM (con swap en un VPS chico) |
| `stt.model: medium` | muy buena | ~2,8 GB de RAM |
| **`stt.provider: groq`** | **excelente** (large-v3-turbo) | `GROQ_API_KEY`; centavos por hora de audio; **el audio sale de tu servidor** |
| `stt.provider: openai` | excelente | `OPENAI_API_KEY`; más caro |

Con la nube, si la API falla se usa el modelo local (`stt.fallback_local`). Además, sin cambiar de modelo:

- **Vocabulario propio:** a Whisper se le pasa un vocabulario con **tus** nombres, lugares y proyectos (sacados
  de tus últimos ítems) y las palabras clave de las reglas con su ortografía. Cuanto más uses misecre, mejor
  reconoce tus palabras.
- **Con IA activa**, el agente sabe que el texto viene de un audio y lo interpreta por contexto (nombres,
  números, palabras parecidas), diciéndote qué entendió.
- Siempre ves la transcripción (`🗣 “…”`): si está mal, decile al bot qué era y lo corrige.

### Convenciones de fechas

- "el jueves" dicho un jueves → el de la semana siguiente (`hoy` para hoy).
- Hora 1–7 sin "de la mañana/tarde" → se asume PM y se avisa.
- Solo hora → hoy si no pasó, si no mañana. Solo día → evento **de todo el día**.
- "durante tres horas", "por 30 minutos" → duración (si no, `default_event_minutes`).

## Agente de IA

Apagado por defecto. Para activarlo: `ai.enabled: true` en `config.yaml` y `ANTHROPIC_API_KEY` en `.env`.

- **Qué puede hacer:** crear, buscar, listar, editar, mover, recategorizar, marcar hecho/reabrir,
  borrar/restaurar y deshacer. **Nada más**: no ejecuta código, no navega, no toca archivos.
- **Modelo:** `claude-haiku-5-5`, con esfuerzo bajo.
- **Costo:** Haiku 5.5 cuesta US$0,10 por millón de tokens de entrada y US$0,50 de salida. Una llamada son
  ~2,5 mil tokens (**~US$0,0003**) y un mensaje que la necesita hace **1** (o 2 si debe leer algo antes de
  actuar). Medido con la API real: 8 mensajes variados = 4 llamadas, ~US$0,001. `ai.agent_max_calls_per_day: 300`
  pone el techo.
- **Plan Pro/Max ≠ API:** la suscripción de Claude **no incluye** la API. Se factura aparte en
  [console.anthropic.com](https://console.anthropic.com) con créditos prepagos (mínimo US$5): el uso los va
  descontando y no se cobra de más salvo que actives la recarga automática (revisalo en *Billing*).
- **Privacidad:** se envía a Anthropic el texto de tus mensajes y los datos de la agenda que consulta el
  agente (títulos, fechas). Con la IA apagada no sale nada.
- **Si la IA falla** (sin red, sin créditos, tope diario): el bot avisa y anota con reglas; no pierde la nota.
- `uv run misecre chat` (terminal) y `uv run misecre ai-test "texto"` para probarla.
- `uv run misecre check` te dice si está activo y, si no, **por qué** (`ai.enabled`, falta la clave…).
  `/estado` en el bot muestra llamadas y gasto estimado del día.
- **YAML:** la sección va **anidada** (`ai:` y debajo `  enabled: true`). Una línea suelta `ai.enabled: true`
  también se acepta, y cualquier clave que no se reconoce se avisa en `check` y al arrancar.

## Google Calendar

1. [Google Cloud Console](https://console.cloud.google.com) → proyecto nuevo → habilitar **Google Calendar API**.
2. Credenciales → **ID de cliente OAuth → Aplicación de escritorio** → descargar el JSON y guardarlo como
   `<data_dir>/credentials.json` (`~/.misecre/credentials.json`).
3. Pantalla de consentimiento (Google Auth Platform → Público): agregá tu mail como **usuario de prueba**.
4. `calendar.enabled: true` y `uv run misecre google-auth` (abre el navegador; crea los 3 calendarios y
   los deja visibles). Aviso "Google no verificó esta app" → *Avanzado → Ir a misecre*.
5. `uv run misecre selftest-calendar` prueba contra Google real: crear, editar, pasar a todo el día, mover
   entre calendarios, que no queden avisos nativos y borrar.

**Modo Testing:** el permiso vence a los 7 días (borrá `token.json` y repetí `google-auth`).

<details><summary>Publicar la app de Google (opcional, para que no venza)</summary>

Google Auth Platform → *Branding*: nombre `misecre`, correo de asistencia, una URL principal pública (repo de
GitHub, Notion…), una URL de política de privacidad y el correo de contacto. Texto mínimo de privacidad:

> misecre es una herramienta personal que crea eventos en el Google Calendar del propio usuario. Accede
> únicamente al calendario de la cuenta que la autoriza, solo para crear, mover y borrar los eventos que el
> usuario le pide. No comparte, vende ni transmite datos a terceros. Todo se procesa y guarda en la computadora
> del usuario; los tokens quedan en un archivo local que el usuario puede borrar en cualquier momento, y puede
> revocar el acceso desde https://myaccount.google.com/permissions.

Después: *Público → Publicar app*. No hace falta verificación formal para uso personal. Si Google rechaza las
URLs por dominio no autorizado, quedate en modo Testing (no se pudo comprobar de antemano).
</details>

### Una sola fuente de avisos

Los eventos se crean **sin recordatorios de Google**: los avisos los emite misecre (Telegram y escritorio),
con política por categoría (`/avisos`). misecre además *silencia* los eventos que creás a mano en esos
calendarios (`sync.silence_google_reminders`). Hay un calendario por categoría ("Misecre · Trabajo", …).

## Sincronización con Google Calendar (dos vías)

| Qué cambiás en Google | Resultado en misecre |
|---|---|
| Mover / cambiar duración | se actualiza la fecha y los avisos se reprograman |
| Renombrar | se actualiza el título |
| Pasar a otro calendario | cambia la categoría |
| Borrar el evento | la captura va a la papelera (`/restaurar`) |
| Crear un evento | se **importa** como evento |

Y lo que hacés en misecre (bot, web, agente) se publica en Google. Corre cada 5 min y con `/sync`.
Todo lo categorizado y con fecha tiene su evento: si falta, se repara. Los eventos recurrentes de Google se
ignoran (todavía).

## Avisos y resúmenes

- **Avisos** por categoría: `trabajo` 15 min antes, `personal` 60 y 10, `ocio` solo en el resumen. Lo de
  todo el día avisa a las 09:00. Horas de silencio, `/pausa`, botón **⏰ posponer** y **✅**.
- **Sin repetidos**; si la PC estuvo apagada, al prender llega **un único** resumen de lo perdido.
- **Resúmenes:** diario 08:00, semanal domingo 20:00, mensual el día 1 (`/resumen hoy|semana|mes`).
- **Pulso periódico** ("cómo está todo" a horas que elijas): `digests.periodic: ["08:00","10:00","14:00","18:00"]`.
  Muestra el próximo evento, lo que falta hoy, tareas, atrasadas y sin categoría. Una vez por hora y por día;
  si la PC prendió tarde manda solo el último vigente; si no hay nada, no molesta
  (`periodic_skip_empty`). `/pulso` lo muestra ahora, para probar.

## Web de control

`http://127.0.0.1:8787/` (o `uv run misecre open`): chat con el agente, agenda de hoy y de la semana,
tareas, sin categoría y papelera; editar, hecha, borrar, restaurar, **deshacer**; captura por **voz** (🎙);
resolver ambigüedades; ajustes de avisos, silencio, pausa, reglas y sugerencias; avisos en vivo.
HTML + JS sin dependencias; el contenido se inserta con `textContent` y el script lleva `nonce` (CSP).

## Calendario, avisos y datos: tres cosas independientes

| Capa | Qué es | Dónde vive |
|---|---|---|
| **Datos** | tus ítems (título, fecha, estado, categoría) | SQLite local: la fuente de verdad |
| **Avisos** | quién te avisa y cuándo | los calcula misecre **desde la base**, no desde Google |
| **Calendario** | un espejo en Google Calendar (por categoría) | opcional: se puede apagar por categoría |

Por eso un ítem puede **avisar sin estar en el calendario**. Ejemplo (ocio con avisos pero sin Google):

```
/calendario ocio off        # ocio deja de publicarse en Google (y se retiran sus eventos de allá)
/avisos ocio 30,10          # y avisa 30 y 10 min antes, por Telegram/escritorio
```

o en `config.yaml`: `calendar.skip_categories: [ocio]` y `notifications.categories.ocio.before: [30, 10]`.
Las capturas quedan guardadas igual; en las listas aparecen con 🔔 (solo avisos). `/calendario` muestra el estado
de cada categoría.

Lo que sí va a Google: todo lo que tiene **fecha** y cuya categoría no esté apagada (con hora, o de todo el día).
Si algo con fecha no aparece en Google, `/estado` y las listas lo marcan (`⚠️ sin calendario`).

## Cómo llegan los avisos (por sistema)

El que decide es siempre el proceso `misecre run`. Después entrega por estos canales:

| Canal | Cómo | Funciona en |
|---|---|---|
| **Telegram** | llamada saliente a la API de Telegram | todos, y llega al celu aunque no haya escritorio |
| **Notificación nativa** (misma máquina) | Linux: `notify-send` (habla con el demonio de notificaciones por D-Bus) · Windows: toast de WinRT vía PowerShell · macOS: `osascript` | donde corre `misecre run` |
| **`misecre listen`** | proceso en segundo plano **sin ventana**: mantiene una conexión (SSE) con el servidor y dispara la notificación nativa de *su* PC. No depende de que abras la web | cualquier PC que apunte al servidor |
| **Pestaña web abierta** | `Notification` del navegador | solo mientras la pestaña esté abierta |

No hace falta ningún servicio especial en Windows: `misecre service install --listener` crea una tarea
programada que lanza ese proceso al iniciar sesión.

## Qué funciona fuera de casa

El core corre en **una** máquina (tu PC o un VPS). Telegram es una conexión *saliente* de esa máquina: desde el
celu, con datos o wifi, escribirle al bot funciona **mientras esa máquina esté prendida y con internet**.

| Si la PC está… | Capturas / chat | Avisos | Calendario en el celu |
|---|---|---|---|
| Prendida | ✅ | ✅ | ✅ |
| Apagada | ⏳ Telegram las guarda ~24 h | ❌ (Google está en silencio a propósito) | ✅ sin alarma |

Para no depender de la PC: un VPS (abajo) o una máquina siempre prendida.

## Acceso remoto, tokens y VPS

**Por defecto la API escucha solo en `127.0.0.1`**. Para usarla desde el celu o un VPS:

| Capa | Qué hace |
|---|---|
| **Token por dispositivo** | `misecre token create celu` lo muestra **una vez**; en la DB queda solo su hash SHA-256. `token list`, `token revoke celu` |
| **Límite de intentos** | 10 fallos desde una IP → bloqueo 5 min (HTTP 429), aun con el token correcto |
| **Lista blanca de IPs** | `api.allowed_ips: ["203.0.113.7", "10.0.0.0/8"]`, antes del token y también para la página |
| **Cabeceras** | CSP con nonce, `X-Frame-Options: DENY`, `nosniff`, sin caché; `/docs` desactivado |
| **Fail-closed** | sin token y fuera de loopback, la API se niega a responder |

Siempre detrás de **HTTPS**: **Tailscale** (`tailscale serve 8787`, lo más simple y seguro) o dominio + **Caddy**
(`deploy/Caddyfile.example`; `api.trust_proxy: true` solo si el proxy es tuyo).

**Avisos con el core en un VPS** (no tiene escritorio): Telegram (siempre), `misecre listen` en cada PC
(notificación nativa; ~30 MB, sin GUI, con su propio token) y la pestaña web abierta.

```bash
echo "MISECRE_SERVER=https://misecre.tudominio.com" >> ~/.misecre/.env
echo "MISECRE_TOKEN=msc_..."                       >> ~/.misecre/.env
uv run misecre listen                      # prueba
uv run misecre service install --listener  # arranque automático
```

**Checklist VPS:** (1) apagá el bot en tu PC: un token de Telegram admite un solo proceso; (2) autorizá Google
en una PC y copiá `credentials.json` y `token.json` a `~/.misecre/` del VPS; (3) `stt.model: base` o `tiny`
en servidores chicos; (4) `misecre token create <dispositivo>` y `misecre service install`.

## Publicar con nginx en un subdominio (sin Tailscale)

La opción más simple si ya tenés nginx y un dominio: HTTPS real (el 🎙 del navegador funciona), nada que
instalar en los dispositivos. La seguridad la dan los tokens por dispositivo, el límite de intentos y la
API que solo escucha en `127.0.0.1`.

1. **DNS:** registro `A` de `misecre.tudominio.com` → la IP del VPS.
2. **nginx:** copiá `deploy/nginx.conf.example` a `/etc/nginx/sites-available/misecre`, cambiá el dominio,
   activalo (`ln -s` a `sites-enabled`), `sudo nginx -t && sudo systemctl reload nginx`.
3. **Certificado:** `sudo certbot --nginx -d misecre.tudominio.com` (agrega 443 y la redirección).
4. **misecre** (`config/config.yaml` del VPS): `api.host: 127.0.0.1` y `api.trust_proxy: true`.
   (`trust_proxy` es seguro porque solo nginx puede alcanzar el puerto; nginx pisa `X-Forwarded-For`.)
5. **Verificar:**
   ```bash
   ss -ltnp | grep 8787                      # debe decir 127.0.0.1:8787, NO 0.0.0.0
   curl -s https://misecre.tudominio.com/health        # {"ok":true}
   curl -si https://misecre.tudominio.com/agenda | head -1   # HTTP/2 401 (sin token no entra)
   ```
6. **Tokens:** `uv run misecre token create celu` y entrás desde cualquier navegador. En cada PC con
   escritorio: `MISECRE_SERVER=https://misecre.tudominio.com` + `misecre service install --listener`.

Opcional: `api.allowed_ips` si entrás siempre desde IPs fijas (en el celu con datos no sirve), y `fail2ban`
sobre los 401 del log de nginx. El firewall solo necesita 80/443 (y tu SSH): el 8787 no se expone.

**Elegir:** nginx+subdominio = lo más cómodo, expuesto a internet (protegido por token). Tailscale/WireGuard =
nada expuesto, pero hay que tener el cliente en cada dispositivo.

### Con una interfaz web de nginx (Nginx Proxy Manager)

*Proxy Hosts → Add Proxy Host*:

| Campo | Valor |
|---|---|
| Domain Names | `misecre.tudominio.com` |
| Scheme | `http` |
| Forward Hostname / IP | **NPM instalado directo en el servidor:** `127.0.0.1` · **NPM en Docker:** la IP del host vista desde el contenedor (ver abajo) |
| Forward Port | `8787` |
| Block Common Exploits | ✅ · Websockets Support: da igual (se usa SSE) |
| SSL → Request a new certificate | ✅ **Force SSL** ✅ (HTTP/2 ✅) |
| Advanced → Custom Nginx Configuration | `proxy_buffering off;` `proxy_read_timeout 3600s;` `client_max_body_size 10m;` |

En `config.yaml` del VPS: `api.trust_proxy: true` (misecre toma la IP que agregó el proxy, no la del cliente).

**Si NPM corre en Docker, `127.0.0.1` es el propio contenedor, no el servidor.** Hay que apuntar a la IP del
host y hacer que misecre escuche ahí (solo en la red de Docker, no en internet):

```bash
docker exec <contenedor-npm> sh -c "ip route | awk '/default/ {print \$3}'"    # p. ej. 172.17.0.1
```

Esa IP va en *Forward Hostname*, y en `config.yaml`: `api.host: "172.17.0.1"`. Con eso el token es obligatorio
(misecre no responde sin él). Si la red de Docker aún no existe al arrancar, misecre reintenta cada 15 s.

### Si no anda: qué mirar según lo que ves

Primero, desde el VPS: `curl -s http://127.0.0.1:8787/health` → debe dar `{"ok":true}`.

| Lo que ves en el navegador | Causa probable |
|---|---|
| **502 Bad Gateway** | nginx no llega a misecre: misecre no está corriendo (`systemctl --user status misecre`), el puerto no es 8787, o es el caso Docker de arriba (usa `127.0.0.1`) |
| **504 Gateway Timeout** | el firewall del host bloquea al contenedor, o IP equivocada en *Forward* |
| **503 «Falta crear un token»** | todo conecta bien: en el servidor, `uv run misecre token create celu` |
| **Pantalla de entrada con un campo de token** | todo bien: pegá el token que creaste (se muestra una sola vez; si lo perdiste, creá otro) |
| **«token inválido» / 401** | token mal copiado, o de otro servidor, o revocado (`misecre token list`) |
| **429** | demasiados intentos fallidos desde esa IP: esperá 5 min |
| Carga pero **no hay avisos en vivo / el 🎙 no anda** | falta HTTPS (Force SSL) o `proxy_buffering off` |
| Certificado SSL falla | el DNS del subdominio aún no apunta al VPS, o el puerto 80 está cerrado |

## Mudarse a un VPS con Tailscale

Tailscale es una malla WireGuard: cada dispositivo recibe una IP `100.x.y.z` y un nombre
(`mi-vps.tu-tailnet.ts.net`), y **la conexión es bidireccional sin configurar ruteo ni abrir puertos**
(atraviesa NAT; si no puede conectar directo, relaya). El VPS solo necesita salida a internet.

**Lo que NO depende de Tailscale:** el bot de Telegram (el VPS le habla a Telegram por su cuenta) y Google
Calendar. Tailscale solo sirve para que *vos* llegues a la web/API del VPS y para `misecre listen`.

```bash
# --- en tu PC: apagar el servicio local (un token de Telegram admite UN solo proceso escuchando)
uv run misecre service uninstall            # o Ctrl+C si lo corrés a mano

# --- copiar el proyecto y los datos (sin .venv: es de esta máquina)
rsync -av --exclude .venv --exclude __pycache__ ~/baxico/ vps:~/misecre/
rsync -av ~/.misecre/ vps:~/.misecre/       # base, token.json, credentials.json, reglas, logs

# --- en el VPS
curl -LsSf https://astral.sh/uv/install.sh | sh
cd ~/misecre && uv sync
uv run misecre check                         # valida config, token, calendario e IA
uv run misecre token create celu             # un token por dispositivo (se muestra una vez)
uv run misecre service install && sudo loginctl enable-linger $USER

# --- Tailscale en el VPS: HTTPS con tu nombre de tailnet, sin exponer nada a internet
sudo tailscale up
tailscale serve --bg 8787                    # https://mi-vps.tu-tailnet.ts.net  ->  127.0.0.1:8787
```

- Dejá `api.host: 127.0.0.1`: la API **no** escucha en la red; `tailscale serve` la publica solo en tu tailnet,
  con HTTPS (necesario para que el navegador deje usar el 🎙: `http://100.x…` no es un contexto seguro).
- En el celu y en cada PC: instalá Tailscale con la misma cuenta y abrí `https://mi-vps.tu-tailnet.ts.net`.
  Entrás con el token de ese dispositivo.
- En cada PC con escritorio: `MISECRE_SERVER=https://mi-vps.tu-tailnet.ts.net` y `MISECRE_TOKEN=msc_…` en
  `~/.misecre/.env`, y `misecre service install --listener`.
- **Whisper en el VPS:** descarga su propio modelo (el caché de Hugging Face no está en `~/.misecre`). Con 1–2
  vCPU usá `stt.model: base` o `tiny`.
- **Zona horaria:** el VPS suele estar en UTC; misecre usa `timezone` del `config.yaml` para todo, así que no
  hace falta cambiar el reloj del servidor.
- **Google:** `token.json` copiado sigue valiendo (misma cuenta). Si vence, se autoriza en una PC con navegador
  y se vuelve a copiar.
- **Seguridad:** el token de Telegram y `ANTHROPIC_API_KEY` viajan en `.env` (`chmod 600`). Si el VPS es
  compartido o público, mejor un usuario dedicado.

## Actualizar el servidor

El código va por **git**; la configuración y los datos **no** (`config/config.yaml`, `.env` y `~/.misecre/`
están fuera del repositorio), así que una actualización nunca pisa tu config, tu token ni tu base.

**Opción A: repo remoto (GitHub, Gitea, lo que uses) + `git pull` en el VPS**

```bash
# una vez, en el VPS (si lo copiaste con rsync, convertilo en repo):
cd ~/misecre && git init -b main && git remote add origin <url-del-repo> && git fetch && git reset --hard origin/main
# cada actualización, en el VPS:
~/misecre/deploy/update.sh        # git pull + uv sync + check + reinicio + doctor
```

**Opción B: `git push` directo al VPS (sin servicio externo)** — con `deploy/post-receive.example`:

```bash
# en tu PC, cada vez:
git push vps main                  # el hook hace checkout, uv sync y reinicia el servicio
```

Sin git, a mano: `rsync -av -e "ssh -p PUERTO" --exclude .venv --exclude config/config.yaml --exclude .env ~/baxico/ USUARIO@IP:misecre/`
y después `uv sync && systemctl --user restart misecre` en el VPS.

### VPS con poca RAM: agregar swap

Si `doctor` marca "margen justo" (o el sistema mata el proceso: `dmesg | grep -i "killed process"`), un swap de
2 GB lo evita sin gastar nada:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## Si algo no anda: `misecre doctor`

Imprime un diagnóstico de solo lectura (config, Telegram, Google, IA, RAM vs modelo de voz, API, tokens,
notificaciones de escritorio, servicio systemd y `linger`, último error del log). Es lo que hay que pegar
cuando algo falla. Otros dos lugares útiles: `journalctl --user -u misecre -n 80 --no-pager` y
`~/.misecre/logs/misecre.log`.

## Arranque automático

```bash
uv run misecre service install     # crea e inicia el servicio
uv run misecre service status
uv run misecre service uninstall
```

- **Linux:** `systemd --user`; para que arranque sin iniciar sesión: `sudo loginctl enable-linger $USER`.
  Logs en `~/.misecre/logs/misecre.log`.
- **Windows:** tarea programada al iniciar sesión. ⚠️ *Escrito pero no probado en Windows por el autor.*

## Comandos de terminal

| Comando | Qué hace |
|---|---|
| `misecre run` | API + bot + avisos + resúmenes + sincronización |
| `misecre check` | valida config, token, calendario, IA |
| `misecre chat` | chatea con el agente desde la terminal |
| `misecre sync` | sincroniza con Google ahora |
| `misecre capture "texto"` | anota con reglas (prueba sin Telegram) |
| `misecre transcribe audio.ogg` | transcribe y anota un audio |
| `misecre digest [daily\|weekly\|monthly]` | imprime un resumen |
| `misecre google-auth` · `selftest-calendar` | autoriza Google · prueba la integración |
| `misecre token create\|list\|revoke <nombre>` | tokens por dispositivo |
| `misecre listen` | avisos de un servidor remoto en este escritorio |
| `misecre service install\|uninstall\|status [--listener]` | arranque automático |
| `misecre open` | abre la web de control |

## Aprendizaje

Cada corrección tuya queda en la tabla `feedback` (lo que eligió el sistema vs. lo que elegiste).
`/sugerencias` propone reglas tras N correcciones de la misma palabra; **nunca las aplica sola**. Tus reglas
viven en `<data_dir>/rules.user.yaml` y `config/rules.yaml` queda intacto. Lo que decide el agente no cuenta
como corrección humana.

## Desarrollo

```bash
uv sync && uv run pytest        # ~210 tests, sin red ni credenciales
```

`tests/fakes.py` trae un Google Calendar en memoria, un cliente de Anthropic guionado (tool use incluido) y
uno simple. Módulos: `core.py` (CRUD + publish), `agent.py`, `sync.py`, `reminders.py`, `digest.py`,
`runtime.py`, `telegram_bot.py`, `api.py`, `web.py`, `gcal.py`, `ai.py`, `parser.py`, `classifier.py`, `db.py`.

## Seguridad

- El token del bot vive solo en `.env` (ignorado por git). **No pegues logs sin revisarlos**; si se filtra:
  @BotFather → `/revoke`.
- `telegram.allowed_chat_ids` limita quién puede escribirle al bot (por defecto, nadie).
- Lo borrado va a la papelera y `/deshacer` revierte las últimas 50 acciones.

## Licencia

misecre es software libre bajo la **GNU Affero General Public License v3.0 o posterior** (AGPL-3.0-or-later):
podés usarlo, estudiarlo, modificarlo y compartirlo. Si lo modificás y ofrecés esa versión como servicio a
terceros, tenés que ofrecerles también el código fuente de tus cambios. Texto completo en [`LICENSE`](LICENSE).

## Contribuir

Los `issues` y los pull requests son bienvenidos (si los escribió una IA, aclaralo en el PR). Antes de enviar cambios: `uv sync && uv run pytest`
(los tests no necesitan red ni credenciales). El proyecto prioriza que **lo que no se pudo verificar se diga**:
si probaste algo contra un servicio real (Google, Telegram, Groq, Anthropic), anotalo en el PR.
