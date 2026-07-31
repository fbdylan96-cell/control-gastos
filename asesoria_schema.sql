-- ============================================================================
-- asesoria_schema.sql — Base de datos de la asesoría (asesoria_db)
--
-- Base de datos SEPARADA de la de Neto app (gastos_db) por diseño: los
-- diagnósticos son datos de PROSPECTOS (no de clientes) y alimentan los
-- reportes del servicio de asesoría. Vive en la misma instancia RDS pero es
-- un catálogo aparte — el código de Neto no puede tocarla por accidente.
--
-- Primera vez (crear la base y luego el esquema):
--     psql "<DB_PROD_URL>" -c "CREATE DATABASE asesoria_db"
--     psql "<ASESORIA_DB_URL>" -f asesoria_schema.sql
--
-- Migraciones posteriores: este archivo es ADITIVO e IDEMPOTENTE (seguro de
-- re-correr). La conexión de la app usa ASESORIA_DB_URL (prod) o
-- ASESORIA_DB_NAME (dev local) — ver web-app/db.py get_asesoria_connection.
-- ============================================================================

-- Una fila por envío del formulario público /diagnostico/.
--   * payload     — el JSON completo sanitizado (post-conversión USD→CRC, el
--                   mismo del que se genera el Excel). JSONB para que el
--                   formulario pueda evolucionar sin migraciones; los reportes
--                   de la asesoría leen de aquí.
--   * tc_usd/tc_fecha — tipo de cambio aplicado (NULL si todo vino en CRC).
--   * report_sent — TRUE cuando el correo con el Excel salió bien; una fila
--                   en FALSE indica que el envío falló pero los datos se
--                   conservaron.
--   * ip          — origen del envío (forense / dedup del endpoint público).
CREATE TABLE IF NOT EXISTS diagnosticos (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    nombre       TEXT NOT NULL,
    correo       TEXT NOT NULL,
    celular      TEXT NOT NULL,

    tc_usd       NUMERIC(14,6),
    tc_fecha     DATE,
    ip           TEXT,
    report_sent  BOOLEAN NOT NULL DEFAULT FALSE,

    payload      JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diagnosticos_correo
    ON diagnosticos (correo, created_at DESC);
