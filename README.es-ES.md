

<p align="center">
  <img src="./docs/assets/banner.png" alt="EchoVessel — a digital persona engine" width="640">
</p>

<p align="center">
  <a href="https://github.com/AlanY1an/echovessel/actions/workflows/ci.yml"><img src="https://github.com/AlanY1an/echovessel/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  &nbsp;·&nbsp;
  🌐 <a href="./README.zh.md">中文</a>
</p>

*Algunas personas dejan recuerdos detrás.  
Algunas dejan una voz.  
Algunas permanecen con nosotros en fragmentos: un tono, un ritmo, una forma de hablar que nunca desaparece por completo.*

**EchoVessel** es un motor de código abierto para crear personas digitales que pueden recordar, responder, evolucionar y mantenerse presentes a lo largo del tiempo.

Está diseñado para quienes deseen crear personajes, compañeros, personas ficticias, ecos personales o contrapartes digitales con consentimiento, con:

- identidad y estilo
- memoria a largo plazo
- evolución de relaciones
- interacción por voz
- privacidad primero local

EchoVessel no es un chatbot genérico.  
Es un recipiente para la presencia.

---

## Inicio Rápido (v0.0.1)

La versión v0.0.1 es una liberación etiquetada en fase alpha temprana. Incluye un daemon local-first construido sobre la pila completa de 5 módulos (memoria / voz / canales / proactivo / runtime), con un canal Web funcional (chat + administración de bloques de persona + activación de voz + orientación de primer uso) y un canal de DM de Discord funcional. Varias superficies de administración son marcadores de posición intencionales en esta versión; consulte las **Limitaciones Conocidas** en [`CHANGELOG.md`](./CHANGELOG.md) para ver la lista completa de lo que se ha diferido a v0.0.2+. Probado en macOS y Linux; Windows aún no es compatible.

### Instalación (desde el código fuente)

EchoVessel está diseñado para Python **3.11+**. **Aún no hay una versión en PyPI** — clone el repositorio y ejecute desde el código fuente usando [`uv`](https://github.com/astral-sh/uv):

```bash
git clone https://github.com/AlanY1an/echovessel.git
cd echovessel
uv sync --all-extras
```

`--all-extras` descarga todas las pilas opcionales de una sola vez. Si desea mantener la instalación ligera, seleccione solo lo que use:

```bash
uv sync --extra embeddings --extra llm --extra voice --extra discord
```

- `embeddings` — incrustador local sentence-transformers
- `llm` — SDKs de OpenAI / Anthropic
- `voice` — SDK de FishAudio TTS
- `discord` — `discord.py` para el canal de DM de Discord

Todos los comandos siguientes se ejecutan dentro del repositorio con `uv run …`.

### Recorrido arquitectónico de 5 minutos

Antes de ejecutar nada, ayuda ver cómo encajan las piezas. Tres visualizaciones HTML escritas a mano se encuentran en `docs/`; ábralas localmente en un navegador.

- 🗺 [**`docs/architecture.html`**](https://alanyian.com/projects/echovessel/docs/architecture.html) — anatomía estática de una página. Capas de módulos, pila de memoria L1–L4, flujo de mensajes, SSE entre canales, superficie HTTP completa, reglas de hierro, cronograma de lanzamiento.
- 🧠 [**`docs/memory/layers.html`**](https://alanyian.com/projects/echovessel/docs/memory/layers.html) — modelo mental de memoria más simple posible. Una figura SVG, cuatro capas, cómo se conectan, créditos al paper Generative Agents de Stanford por la fórmula de puntuación de recuperación.
- 🔄 [**`docs/architecture-flow.html`**](https://alanyian.com/projects/echovessel/docs/architecture-flow.html) — compañero del "sistema nervioso" en tiempo de ejecución. Secuencia de activación por turno, traza de historia real, reglas de destilación L1–L4 (citando los prompts reales de extracción/reflexión), matemáticas de recuperación, puertas de política.

Si solo tiene 60 segundos, abra el del medio.

### Primer Inicio

EchoVessel lee `~/.echovessel/config.toml` para la configuración y `./.env` (el directorio de trabajo actual en tiempo de ejecución) para las claves API. Cree ambos archivos de inicio de una sola vez:

```bash
uv run echovessel init
```

`init` escribe `~/.echovessel/config.toml` **y** una plantilla `.env` comentada en el directorio actual (permisos 0600). El daemon carga automáticamente `./.env` al ejecutar `uv run echovessel run`, así que mantenga `.env` en el directorio desde el que inicia — típicamente la raíz del proyecto. Descomente las claves que necesite:

```
OPENAI_API_KEY=sk-...
FISH_AUDIO_KEY=...              # opcional · FishAudio TTS
ECHOVESSEL_DISCORD_TOKEN=...    # opcional · token de bot de Discord
```

Edite `~/.echovessel/config.toml` para elegir un proveedor de LLM: la configuración cero funciona con cualquier endpoint compatible con OpenAI (establezca `OPENAI_API_KEY`), o cambie a `anthropic` + `ANTHROPIC_API_KEY`, o `ollama` (local, sin clave). Consulte el ejemplo para ver todas las opciones.

**Prueba de humo sin ninguna clave API**: establezca `[llm].provider = "stub"` en la configuración para iniciar el daemon con respuestas simuladas predefinidas — útil para verificar la instalación.

### Ejecutar el Daemon

```bash
uv run echovessel run
```

El primer inicio descarga el incrustador sentence-transformers (~90MB, una sola vez). Los arranques posteriores son instantáneos.

Registro esperado en un arranque limpio:
```
schema migration: created table core_block_appends
voice service: <enabled | disabled> (config.voice.enabled=...)
proactive scheduler: <enabled | disabled> (config.proactive.enabled=...)
importer facade: built
static frontend: mounted from .../channels/web/static
web channel: serving on http://127.0.0.1:7777 (debounce_ms=2000)
memory observer: registered
EchoVessel runtime started | data_dir=... persona=... llm_provider=... channels=...
local-first disclosure: outbound = only <llm endpoint>; embedder runs locally; no telemetry; logs stay in <data_dir>/logs
first launch: opened browser at http://127.0.0.1:7777/
```

Esa última línea significa que el daemon **abre automáticamente su navegador predeterminado** en el primer uso; debería llegar a la pantalla de orientación sin tener que pegar la URL usted mismo.

Los datos se almacenan en `~/.echovessel/memory.db` (SQLite + sqlite-vec). Los registros en `~/.echovessel/logs/`.

### Canal Web

El daemon sirve la interfaz React directamente en `http://127.0.0.1:7777/` (host/puerto configurables bajo `[channels.web]` en `config.toml`). Ábrala en un navegador; eso es todo. Sin `npm`, sin servidor de desarrollo separado.

Si desea reconstruir el frontend desde el código fuente (solo para colaboradores), las fuentes se encuentran en `src/echovessel/channels/web/frontend/`. Ejecute:

```bash
cd src/echovessel/channels/web/frontend
npm install
npm run build
```

El gancho de compilación hatch copia la salida en `src/echovessel/channels/web/static/`, que se incluye en la rueda (wheel).

### Canal de Discord

EchoVessel puede comunicarse con usted a través de DMs de Discord: respuestas de texto más mensajes de voz nativos en OGG Opus cuando la voz está habilitada.

1. Cree una aplicación + bot en <https://discord.com/developers/applications>. Bajo **Bot → Privileged Gateway Intents**, active **MESSAGE CONTENT INTENT**.
2. Copie el token del bot en `.env`:
   ```
   ECHOVESSEL_DISCORD_TOKEN=...
   ```
3. En `~/.echovessel/config.toml`:
   ```toml
   [channels.discord]
   enabled = true
   token_env = "ECHOVESSEL_DISCORD_TOKEN"
   debounce_ms = 2000
   # allowed_user_ids = [123456789012345678]   # lista de permitidos opcional
   ```
4. Invite al bot a su cuenta (generador de URL OAuth2 → alcance `bot` + permisos de DM), luego envíele un DM. Los mensajes entrantes se agrupan por debounce (2s por defecto) y se despachan como un solo turno.
5. Los mensajes de voz se envían como burbujas de voz nativas de Discord cuando `[persona].voice_enabled = true` **y** `ffmpeg` está en PATH: el canal convierte la salida MP3 de FishAudio a OGG Opus sobre la marcha. Instálelo con `brew install ffmpeg` (macOS) o `apt install ffmpeg` (Debian/Ubuntu). Sin ffmpeg, el canal de Discord vuelve al texto.
6. Todo lo que envíe por DM a través de Discord también aparece en vivo en la página de chat Web en `http://127.0.0.1:7777/`, etiquetado con una etiqueta `📱 Discord`. Los mensajes históricos de Discord se cargan al montar Web mediante `/api/chat/history`. La misma memoria de persona respalda ambos canales (regla de hierro D4).

### Voz

EchoVessel utiliza [FishAudio](https://fish.audio) para TTS. Coloque `FISH_AUDIO_KEY` en `.env` y elija un `voice_id` bajo `[persona]` en `config.toml`. Establezca `[persona].voice_enabled = true` para emitir voz junto con texto. La ruta de mensajes de voz de Discord requiere además `ffmpeg` (conversión MP3 → OGG Opus).

### Ejecutar Pruebas

```bash
uv run pytest tests/ -q                # 916 pruebas en memoria / runtime / voz / proactivo / canales / importación / integración
uv run ruff check src/ tests/          # lint
uv run lint-imports                    # contratos de arquitectura por capas
```

### Estructura del Proyecto

```
src/echovessel/
├── core/            — tipos compartidos, enums, utilidades
├── memory/          — memoria L1-L4 · SQLite + sqlite-vec · observadores + migraciones
├── voice/           — TTS + STT + clonación de voz (FishAudio + Whisper + simulado)
├── proactive/       — mensajería autónoma · puertas de política · entrega
├── channels/        — Protocolo de Canales + adaptadores por canal (web + discord)
│   ├── web/         — rutas FastAPI + SSE + paquete React incrustado
│   │   ├── frontend/ — código fuente React 19 + Vite + TS (colaboradores)
│   │   └── static/  — paquete compilado servido por el daemon
│   └── discord/     — bot discord.py · ingestión de DM · voz OGG Opus
├── import_/         — pipeline universal de importador LLM (texto → memoria)
├── prompts/         — prompts del sistema para extracción / reflexión / interacción
├── resources/       — config.toml.sample empaquetado
└── runtime/         — daemon · despachador de turnos · proveedores LLM · CLI
```

### Estado Actual (v0.0.1)

- ✅ **Daemon**: arranque de extremo a extremo, toda la conexión de inicio verificada en el registro, 916 pruebas pasando (3 omitidas)
- ✅ **Línea de tiempo unificada entre canales**: la página de chat Web transmite en vivo eventos de turno de cada canal (Web + Discord hoy, listo para iMessage) con una etiqueta de origen `📱 Discord` / `💬 iMessage`. Un nuevo endpoint `/api/chat/history` rellena los últimos 50 mensajes entre canales al montar.
- ✅ **Memoria**: jerarquía L1–L4, migración de esquema idempotente, hooks de observador, 4/4 métricas de evaluación MVP pasando (Tasa de FP de sobre-recuperación 0.08 ≤ objetivo 0.15)
- ✅ **Voz**: TTS de FishAudio + proveedor TTS simulado · fachada `VoiceService.generate_voice()` · `voice_id` por persona · caché MP3 en disco
- ✅ **Proactivo**: motor de políticas · cuatro puertas incluyendo `no_in_flight_turn` · la entrega hereda `persona.voice_enabled`
- ✅ **Runtime**: ciclo de turno con transmisión (IncomingTurn + delta de texto) · alternancia atómica de voz por persona · recarga en caliente `SIGHUP` · cableado del observador de memoria
- ✅ **Canal Web** (rutas de producción): transmisión FastAPI + SSE · paquete React 19 incrustado · flujo de orientación · chat con transmisión de tokens · admin → edición de bloques principales de persona · admin → alternancia de voz
- ✅ **Canal Web** (orientación): ambas rutas de entrada funcionan — escritura en blanco (complete los 5 bloques de persona a mano) y carga de material (pegue una biografía/diario, el LLM redacta los 5 bloques para su revisión)
- 🚧 **Canal Web** (marcadores de posición en esta versión): admin → lista de eventos / lista de pensamientos / asistente de clonación de voz / pestañas de configuración existen, pero la mayoría solo renderizan el marco de la sección; algunas están completamente cableadas (Bloques de persona · Alternancia de voz · Búsqueda de memoria · Desglose de costos · etc. — consulte CHANGELOG para el mapa exacto)
- ✅ **Canal de Discord**: ingestión de DM con debounce · respuestas de texto · mensajes de voz nativos OGG Opus (requiere ffmpeg)
- ✅ **Pipeline de importación** (solo biblioteca): importador LLM universal · clasificación de cinco tipos de contenido · ruta lateral `self_block` · paso de incrustación obligatorio — *aún no se expone ninguna ruta HTTP, por lo que ni el SPA Web ni la CLI pueden impulsar una importación real en esta versión*
- ⚠️ **Plataforma**: probado en macOS y Linux; Windows aún no es compatible
- 🔜 **Objetivos v0.0.2**: cablear rutas `/api/admin/import/*` + asistente de importación Web · Vistas de lista de eventos/pensamientos de Admin · feed SSE en vivo de estado/límites de sesión en el chat Web

---

## Continuar leyendo

La documentación completa módulo por módulo se encuentra en **[`docs/`](./docs/)** (inglés + 中文). Comience en la página de inicio de su idioma y siga los enlaces cruzados:

- 🇬🇧 [**docs/en/README.md**](./docs/en/README.md) · 🇨🇳 [**docs/zh/README.md**](./docs/zh/README.md)

Las páginas de módulos cubren [memoria](./docs/en/memory.md), [voz](./docs/en/voice.md), [canales](./docs/en/channels.md), [proactivo](./docs/en/proactive.md), [runtime](./docs/en/runtime.md) e [importación](./docs/en/import.md), además de [configuración](./docs/en/configuration.md) y [colaboración](./docs/en/contributing.md). Las tres visualizaciones HTML anteriores son la forma más rápida de ver el sistema en una sola página.

## Nombre

**EchoVessel** significa llevar un eco el tiempo suficiente para que se convierta en presencia.
