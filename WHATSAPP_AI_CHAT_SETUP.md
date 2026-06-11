# WhatsApp AI Consultation Chat — Setup & Operations

Cómo desplegar y operar el chat de consulta financiera por WhatsApp: el cliente
escribe una pregunta en lenguaje natural ("¿cuánto gasté en restaurantes este
mes?") y un agente de IA (Claude) responde usando las funciones de solo lectura
de `tools/finance.py`, siempre limitado a los datos del propio cliente.

Asume que el flujo de notificaciones de WhatsApp ya está funcionando según
`META_WHATSAPP_SETUP.md` (webhook verificado, suscrito a `messages`).

---

## Arquitectura

```
Cliente escribe por WhatsApp
  └── Meta POST → /whatsapp/webhook  (web-app/whatsapp_webhook.py)
        ├── verifica firma, identifica cliente por teléfono, aplica rate limit
        └── INSERT en core.whatsapp_chat_messages (status='pending') → 200 OK inmediato
              └── whatsapp_agent_worker.py (proceso systemd independiente)
                    ├── reclama el mensaje (FOR UPDATE SKIP LOCKED)
                    ├── tools/agent.py: loop de tool-use de Claude sobre tools/finance.py
                    ├── responde vía whatsapp_client.send_text (ventana de 24h)
                    └── guarda la respuesta como historial (multi-turno, 24h)
```

Decisiones clave (estabilidad primero):

- **El LLM nunca corre dentro del request del webhook** — el webhook solo hace
  un INSERT, así que los workers de gunicorn nunca se bloquean.
- **Dedupe de reintentos de Meta** por constraint `UNIQUE(wamid)`.
- **Un fallo no se reintenta**: el mensaje queda `failed`, el cliente recibe un
  texto de disculpa, y no se quema gasto de API en loops.
- **Scope inyectado server-side**: el modelo solo elige fechas/categorías;
  nunca puede apuntar a datos de otro cliente. Todas las tools son SELECTs.
- **Escala horizontal lista**: los claims usan `FOR UPDATE SKIP LOCKED`, así
  que se puede correr un segundo worker idéntico sin cambios de código.

Quién puede usar el chat: clientes con `active = TRUE` y `phone_number`
registrado (mismo requisito de formato que las notificaciones: inicia con `+`).
Números desconocidos se ignoran en silencio.

Scope según tipo de cliente:

| Cliente | Gastos/ingresos consultados | Categorías y presupuestos |
|---|---|---|
| Individual (negocio centinela) | Los propios | Los propios |
| Admin de empresa (`business_admin`) | Toda la empresa | Los de la empresa |
| Miembro de empresa | Solo los propios | Los de la empresa |

---

## 1. SQL en producción

`create.sql` ya incluye la tabla para instalaciones nuevas. En la BD existente,
ejecutar una vez:

```sql
CREATE TABLE core.whatsapp_chat_messages (
    id            UUID PRIMARY KEY,
    client_id     UUID NOT NULL REFERENCES core.clients(id) ON DELETE CASCADE,
    phone         TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content       TEXT NOT NULL,
    wamid         TEXT UNIQUE,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'processing', 'done', 'failed')),
    error         TEXT,
    claimed_at    TIMESTAMPTZ,
    processed_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_wa_chat_pending ON core.whatsapp_chat_messages (created_at)
    WHERE status = 'pending';
CREATE INDEX idx_wa_chat_client_history ON core.whatsapp_chat_messages (client_id, created_at);
```

## 2. Variable de entorno

Agregar a `/srv/control-gastos/.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

(Se crea en <https://platform.claude.com/> → API Keys.)

## 3. Dependencia

```bash
cd /srv/control-gastos
sudo -u ubuntu pip install -r requirements.txt   # agrega 'anthropic'
```

## 4. Servicio systemd del worker

`/etc/systemd/system/whatsapp-agent-worker.service`:

```ini
[Unit]
Description=WhatsApp AI consultation chat worker (control-gastos)
After=network-online.target

[Service]
WorkingDirectory=/srv/control-gastos
ExecStart=/usr/bin/python3 whatsapp_agent_worker.py
Restart=always
RestartSec=5
User=ubuntu

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now whatsapp-agent-worker
sudo journalctl -u whatsapp-agent-worker -f
```

## 5. Reiniciar el web app

El webhook nuevo (encolado de mensajes de texto) corre dentro del web app:

```bash
cd /srv/control-gastos && git pull
sudo systemctl restart <web-app-service-name>
```

No hay que tocar nada en Meta: el webhook ya recibe los mensajes de texto
(antes se ignoraban).

---

## 6. Prueba end-to-end

1. Desde el teléfono registrado de un cliente activo, enviar:
   *"¿Cuánto he gastado este mes?"*
2. En `journalctl -u <web-app>`: `WA chat message queued for client=...`
3. En `journalctl -u whatsapp-agent-worker`: `Answered chat message ... (N chars)`
4. La respuesta llega por WhatsApp en ~10–30 segundos.
5. Verificar en BD:
   ```sql
   SELECT role, status, left(content, 60), created_at
   FROM core.whatsapp_chat_messages
   ORDER BY created_at DESC LIMIT 6;
   ```
6. Hacer una pregunta de seguimiento ("¿y el mes pasado?") — el agente usa el
   historial de las últimas 24 horas.

---

## 7. Operación

| Parámetro | Dónde | Default |
|---|---|---|
| Rate limit por cliente | `CHAT_RATE_LIMIT_PER_HOUR` en `web-app/whatsapp_webhook.py` | 20 mensajes/hora |
| Modelo | `MODEL` en `tools/agent.py` | `claude-opus-4-8` |
| Tope de iteraciones de tools | `MAX_TOOL_ITERATIONS` en `tools/agent.py` | 8 |
| Timeout por llamada al API | `REQUEST_TIMEOUT` en `tools/agent.py` | 120 s |
| Historial de contexto | `HISTORY_LIMIT` en `whatsapp_agent_worker.py` | 10 mensajes / 24 h |
| Reclamo de claims huérfanos | `STALE_PROCESSING_MINUTES` en el worker | 10 min |

**Escalar throughput**: correr una segunda instancia del worker (otro unit
systemd apuntando al mismo script). Los claims con `SKIP LOCKED` reparten la
cola sin coordinación extra.

### Troubleshooting

| Síntoma | Causa probable |
|---|---|
| El mensaje queda `pending` para siempre | El worker no está corriendo — `systemctl status whatsapp-agent-worker` |
| `ANTHROPIC_API_KEY env var is not set` en el log del worker | Falta la variable en `.env` |
| El cliente escribe y no pasa nada (ni log de queued) | El teléfono no coincide con ningún cliente `active` — verificar `phone_number` en `core.clients` (los dígitos deben coincidir; el `+` y los espacios se ignoran) |
| Mensajes `failed` con error de Anthropic | Revisar `error` en la fila; rate limit o caída del API → el cliente ya recibió el fallback, basta reintentar manualmente o esperar |
| Respuestas duplicadas tras un reinicio | Crash entre `send_text` y `mark_done` + reclamo de claim huérfano — raro y benigno |
| `(#131026) Re-engagement message` al responder | Pasaron >24 h desde el último mensaje del usuario — no debería ocurrir (el worker responde en segundos); indica backlog enorme o worker caído por horas |
