# Proyecto: Herramienta de control de gastos

## Descripción

Herramienta de seguimiento de gastos personales que procesa notificaciones bancarias recibidas por correo electrónico. El sistema lee correos de Gmail, extrae la información de cada transacción, la almacena en una base de datos PostgreSQL y la enriquece con datos parseados por banco (BAC, Promérica, DAVIbank) o mediante inteligencia artificial como respaldo.

El pipeline está compuesto por los siguientes pasos:
1. **Ingesta** — Lectura de correos desde el label `Finanzas Personales` en Gmail
2. **Parseo** — Extracción del cuerpo del correo (HTML → texto plano)
3. **Almacenamiento RAW** — Inserción en `core.transactions_raw`
4. **Enriquecimiento** — Detección de banco, extracción de monto/comercio/moneda → `core.transactions_enriched`
5. **Clasificación** — *(pendiente)*
6. **Notificación** — *(pendiente)*

## Tecnologías

- Python 3.11+
- PostgreSQL (esquema `core`)
- Gmail API (OAuth 2.0)
- OpenAI API (fallback de enriquecimiento)
- psycopg2
- python-dotenv
- pytz
- openpyxl

## Setup

### 1. Clonar repo

```bash
git clone <URL>
cd implementacion_formal
```

### 2. Crear entorno virtual

```bash
python -m venv venv
venv\Scripts\activate      # Windows
# source venv/bin/activate  # Linux / Mac
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 4. Configurar variables de entorno

Crear un archivo `.env` en la raíz del proyecto con el siguiente contenido:

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=gastos_db
DB_USER=gastos_user
DB_PASSWORD=tu_password

OPENAI_API_KEY=sk-...
AI_ASSISTANCE=0
```

> Las credenciales de OAuth de Google (`client_secret_*.json`) deben colocarse en la raíz del proyecto. El archivo `token.json` se genera automáticamente al autenticarse por primera vez.

### 5. Crear el esquema en PostgreSQL

```bash
psql -U gastos_user -d gastos_db -f create.sql
```

## Uso

### Ejecutar el poller principal

Lee correos nuevos del label `Finanzas Personales`, los almacena en `transactions_raw` y los enriquece en `transactions_enriched`. Se ejecuta en un loop cada 60 segundos.

```bash
python email_reader.py
```

