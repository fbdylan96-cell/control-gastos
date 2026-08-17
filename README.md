# Proyecto: Herramienta de control de gastos

## Descripción

Plataforma multi-tenant de seguimiento de gastos que procesa notificaciones bancarias recibidas por correo electrónico. El sistema lee correos de Gmail, extrae la información de cada transacción, la almacena en PostgreSQL, la enriquece con parsers por banco (BAC, BCR, Promérica, DAVIbank, Grupo Mutual, MUCAP) o con IA como respaldo, la clasifica en categorías, y notifica al cliente por correo y/o WhatsApp para que confirme o reclasifique. Incluye además un chat de consulta por WhatsApp con un agente de IA (Claude) que responde preguntas en lenguaje natural sobre los gastos del cliente, aplicaciones web para personas, empresas y administración, y un módulo opcional de portafolio de inversión (Alpaca, solo lectura).

## Pipeline de procesamiento

```
Gmail (label: 'Finanzas Personales')
  └── 1-3. Ingesta y parseo → core.transactions_raw
        └── 4. Enriquecimiento (banco + IA) → core.transactions_enriched
              └── 5. Clasificación (reglas + IA) → core.transactions_classified
                    └── 6. Notificación (email y/o WhatsApp) → core.transactions_notifications
                          └── 7. Reclasificación por el cliente (email, web o WhatsApp)
```

1. **Ingesta** — `email_reader.py` lee correos no leídos del label `Finanzas Personales` y enruta cada correo al cliente según su alias de reenvío (`email_forward`). Despierta cada 60 s y drena la bandeja en lotes de 5 hasta vaciarla (con tope de lotes y guarda contra mensajes que fallan repetidamente).
2. **Parseo** — `parser.py` extrae el cuerpo del correo (HTML → texto plano).
3. **Almacenamiento RAW** — Inserción en `core.transactions_raw`.
4. **Enriquecimiento** — `enricher.py` detecta el banco por palabras clave y extrae monto/comercio/moneda/tipo con el parser correspondiente (`banks/`); usa OpenAI como respaldo cuando `AI_ASSISTANCE=1`. Convierte a colones (`amount_local`) usando `core.exchange_rates` y detecta duplicados (misma persona, mismo monto, ventana de 2 minutos → marcado `Duplicado`).
5. **Clasificación** — `classifier.py` normaliza el comercio y busca en `core.category_rules`; si no hay regla, OpenAI elige entre las categorías del negocio (`core.categories`, con fallback a `Otros`).
6. **Notificación** — `notifier.py` envía email HTML con botones de reclasificación firmados (HMAC); `whatsapp_notifier.py` envía plantillas de Meta WhatsApp Cloud API (con o sin información de presupuesto) a clientes con `whatsapp_notification = TRUE`.
7. **Reclasificación** — El cliente corrige la categoría desde el email (`/reclassify`), desde la web app, o desde WhatsApp (botón *Reclasificar* → lista interactiva de categorías). La decisión final queda en `core.transactions_notifications` (`final_category` / `final_subcategory`) y alimenta la creación automática de reglas.

## Estructura del repositorio

| Componente | Descripción |
|---|---|
| `email_reader.py` | Poller principal: orquesta ingesta → enriquecimiento → clasificación → notificaciones (email + WhatsApp) |
| `gmail_client.py` | Cliente Gmail API (OAuth 2.0): lectura de correos y envío de notificaciones |
| `parser.py` | Extracción del cuerpo del correo (HTML → texto) |
| `enricher.py` | Detección de banco, extracción de campos, FX a colones, detección de duplicados |
| `banks/` | Parsers por banco: `bac.py`, `bcr.py`, `davibank.py`, `grupomutual.py`, `mucap.py`, `promerica.py` + utilidades de normalización de comercios |
| `classifier.py` | Motor de clasificación: reglas en BD + OpenAI con catálogo global de pistas comercio→categoría |
| `notifier.py` | Notificaciones por email con enlaces de reclasificación firmados (HMAC-SHA256) |
| `whatsapp_client.py` | Wrapper de Meta WhatsApp Cloud API: plantillas, mensajes de lista, texto libre, verificación de firma de webhooks |
| `whatsapp_notifier.py` | Envío de notificaciones de transacción por WhatsApp |
| `advisory_alerts.py` | Alertas de presupuesto (80%/100%) por WhatsApp para clientes con plan de asesoría; se evalúan tras clasificar cada lote de transacciones |
| `db.py` | Helpers de acceso a PostgreSQL del pipeline |
| `tools/finance.py` | Portafolio de funciones de consulta financiera parametrizadas (resúmenes de ingresos/gastos, top de gastos, gasto mensual por categoría, presupuestos). Sin efectos secundarios; alimentan los dashboards web y el agente de WhatsApp |
| `tools/agent.py` | Agente de IA (Claude, tool-use sobre `tools/finance.py`) que responde consultas financieras en lenguaje natural. El scope se inyecta server-side: el modelo nunca puede consultar datos de otro cliente |
| `whatsapp_agent_worker.py` | Proceso worker (systemd) del chat de consulta: reclama mensajes encolados por el webhook (`FOR UPDATE SKIP LOCKED`), corre el agente y responde por WhatsApp. Ver `WHATSAPP_AI_CHAT_SETUP.md` |
| `rate_scheduler.py` / `exchange_rate_update.py` | Actualización diaria (L-V 23:30) de tipos de cambio → `core.exchange_rates`. CRC y EUR del Ministerio de Hacienda (referencia oficial del BCCR); las otras 41 monedas de open.er-api |
| `credit_products_update.py` | Comparador de productos crediticios del BCCR/MEIC (dashboard público de Power BI) → `core.credit_products`. Corre como job quincenal (días 8 y 22, 06:00) dentro de `rate_scheduler.py`. Guarda solo el último período: cada corrida reemplaza la tabla. Ver `extractorbccrmeic.md` y `test_credit_products.py` |
| `advisory_scheduler.py` | Seguimiento de asesoría por WhatsApp: resumen semanal de presupuesto y seguimiento mensual del fondo de emergencia para clientes con plan (`core.client_advisory_plans`); idempotente vía `core.advisory_message_log`. Ver `PLAN_asesoria.md` |
| `create.sql` | Esquema completo de la base de datos (fuente de verdad de nombres de columnas) |
| `web-app/` | Aplicación Flask (ver abajo) |
| `META_WHATSAPP_SETUP.md` | Guía de configuración de Meta WhatsApp Cloud API en producción |
| `ALPACA_SETUP.md` | Guía de configuración de la integración de inversión con Alpaca |

### Aplicación web (`web-app/`)

Flask app única (`run.py`) con blueprints:

| Ruta | Blueprint | Uso |
|---|---|---|
| `/persona` | `persona/` | Cliente individual: dashboards de gastos y portafolio de inversión |
| `/empresa` | `empresa/` | Cliente empresarial: miembros, categorías, reportes, reclasificación |
| `/administracion` | `administracion/` | Administrador: alta de negocios/clientes, activación, edición de datos |
| `/whatsapp/webhook` | `whatsapp_webhook.py` | Webhook de Meta: verificación (GET) y callbacks de mensajes entrantes — taps de botones, selecciones de lista, y texto libre del chat de consulta, que se encola para el worker sin bloquear el request (POST, firma verificada) |
| `/reclassify` | `run.py` | Reclasificación desde enlaces firmados en el email |

Componentes adicionales:
- `scheduler.py` — Job en background (APScheduler + advisory lock de PostgreSQL) que convierte reclasificaciones confirmadas en reglas de `core.category_rules`.
- `alpaca_client.py` — OAuth con Alpaca **sin scope de trading** (solo lectura) para el tab "Portafolio de Inversión".
- `crypto.py` — Cifrado AES-256-GCM de los tokens de Alpaca en reposo (`ENCRYPTION_KEY` en `.env`, nunca en la BD).

## Base de datos (esquema `core`)

| Tabla | Contenido |
|---|---|
| `businesses` | Agrupación multi-tenant. Registro centinela `…9999` (`__individual__`) para clientes individuales |
| `clients` | Una fila por persona: alias de reenvío, teléfono, flags de notificación y activación |
| `client_investment` | Clientes con servicio de inversión; token OAuth de Alpaca cifrado |
| `categories` | Taxonomía de categorías/subcategorías por negocio (con presupuesto mensual opcional). `('Otros', NULL)` siempre existe y está protegida |
| `category_rules` | Reglas comercio→categoría del motor de clasificación |
| `exchange_rates` | Tipos de cambio diarios (44 monedas): CRC/EUR del Ministerio de Hacienda, el resto de open.er-api |
| `transactions_raw` | Correo crudo, una fila por notificación |
| `transactions_enriched` | Campos extraídos: monto, moneda, comercio, tipo, monto en colones |
| `transactions_classified` | Categoría/subcategoría asignada y método (`rules` / `openai`) |
| `transactions_notifications` | Ciclo de notificación y la determinación final del cliente (`final_category` / `final_subcategory`), incluyendo acciones vía WhatsApp |
| `whatsapp_chat_messages` | Chat de consulta por WhatsApp: cola de mensajes entrantes (`pending`/`processing`/`done`/`failed`) e historial de conversación para contexto multi-turno |

## Tecnologías

- Python 3.11+
- PostgreSQL (esquema `core`)
- Gmail API (OAuth 2.0)
- OpenAI API (respaldo de enriquecimiento y clasificación)
- Meta WhatsApp Cloud API (notificaciones y webhook de respuestas)
- Flask + APScheduler (web app y jobs en background)
- Alpaca API (portafolio de inversión, OAuth solo lectura)
- Ministerio de Hacienda (`api.hacienda.go.cr`) para CRC/EUR y open.er-api para el resto de monedas
- psycopg2, python-dotenv, pytz, openpyxl, lxml, requests, cryptography

## Setup

### 1. Clonar repo

```bash
git clone <URL>
cd control-gastos
```

### 2. Crear entorno virtual

```bash
python -m venv venv
venv\Scripts\activate      # Windows
# source venv/bin/activate  # Linux / Mac
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt          # pipeline
pip install -r web-app/requirements.txt  # web app
```

### 4. Configurar variables de entorno

Crear un archivo `.env` en la raíz del proyecto (el pipeline y la web app comparten el mismo archivo):

```env
# Base de datos
DB_HOST=localhost
DB_PORT=5432
DB_NAME=gastos_db
DB_USER=gastos_user
DB_PASSWORD=tu_password

# IA (enriquecimiento / clasificación)
OPENAI_API_KEY=sk-...
AI_ASSISTANCE=0

# IA (chat de consulta por WhatsApp — ver WHATSAPP_AI_CHAT_SETUP.md)
ANTHROPIC_API_KEY=sk-ant-...

# Notificaciones por email
NOTIFICATION_SECRET=cadena-aleatoria-larga
WEBAPP_URL=https://tu-dominio.com

# Web app
SECRET_KEY=otra-cadena-aleatoria
ADMIN_PASSWORD=password-del-panel-admin

# Asesoría (base de datos separada de Neto app — ver asesoria_schema.sql)
# En prod (IS_PROD_DB=1): URL completa a asesoria_db en la misma instancia RDS.
# En dev local se usa ASESORIA_DB_NAME (default asesoria_db) con DB_HOST/USER/PASSWORD.
ASESORIA_DB_URL=postgresql://usuario:password@host:5432/asesoria_db

# Meta WhatsApp Cloud API (ver META_WHATSAPP_SETUP.md)
META_WA_PHONE_ID=...
META_WA_TOKEN=...
META_WA_APP_SECRET=...
META_WA_VERIFY_TOKEN=...
META_WA_TEMPLATE_SIMPLE=gasto_detectado_simple
META_WA_TEMPLATE_BUDGET=gasto_detectado_presupuesto
META_WA_TEMPLATE_SIMPLE_LANG=es
META_WA_TEMPLATE_BUDGET_LANG=es_ES

# Inversión — Alpaca (ver ALPACA_SETUP.md)
ALPACA_CLIENT_ID=...
ALPACA_CLIENT_SECRET=...
ENCRYPTION_KEY=clave-aes-256-en-base64
```

> Las credenciales de OAuth de Google (`client_secret_*.json`) deben colocarse en la raíz del proyecto. El archivo `token.json` se genera automáticamente al autenticarse por primera vez.

### 5. Crear el esquema en PostgreSQL

```bash
psql -U gastos_user -d gastos_db -f create.sql
```

## Uso

### Poller principal del pipeline

Lee correos nuevos, los enriquece, clasifica y dispara las notificaciones por email y WhatsApp. Loop cada 60 segundos.

```bash
python email_reader.py
```

### Web app

```bash
cd web-app
python run.py        # desarrollo (en producción: gunicorn detrás de nginx con TLS)
```

### Worker del chat de consulta por WhatsApp

Proceso independiente que atiende la cola de mensajes del chat de IA (correr bajo systemd; ver `WHATSAPP_AI_CHAT_SETUP.md`).

```bash
python whatsapp_agent_worker.py
```

### Actualizador de tipos de cambio

Proceso independiente que corre de lunes a viernes a las 23:30.

```bash
python rate_scheduler.py
```

### Scheduler de seguimiento de asesoría

Proceso independiente (correr bajo systemd; ver `PLAN_asesoria.md`). Envía por
WhatsApp el resumen semanal de presupuesto (diario 17:00 CR, filtrado por el día
configurado en cada plan) y el seguimiento mensual del fondo de emergencia
(día 1, 9:00 CR, sobre el mes anterior cerrado).

```bash
python advisory_scheduler.py
```

## Documentación adicional

- `web-app/CLAUDE.md` — Modelo de datos detallado, reglas de negocio y orden de onboarding de clientes.
- `META_WHATSAPP_SETUP.md` — Checklist completo de WhatsApp: credenciales de Meta, plantillas, webhook y troubleshooting.
- `WHATSAPP_AI_CHAT_SETUP.md` — Despliegue y operación del chat de consulta con IA: SQL, systemd, parámetros y troubleshooting.
- `ALPACA_SETUP.md` — Configuración de la integración de inversión.
