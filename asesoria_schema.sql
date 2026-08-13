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
--   * payload_raw — el payload ANTES de convertir USD→CRC. La conversión es
--                   destructiva (reescribe montos y anota " [US$ 123.45]" en el
--                   nombre), así que para REEDITAR el diagnóstico se usa esta
--                   copia fiel. NULL en las filas anteriores a la corrección.
--   * corregido_de — id del diagnóstico que esta fila corrige. Cada corrección
--                   del asesor es una fila NUEVA, nunca un UPDATE: queda
--                   evidencia de lo que el cliente llenó originalmente.
CREATE TABLE IF NOT EXISTS diagnosticos (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- clock_timestamp(), no now(): now() es la hora de inicio de la transacción
    -- y empataría dos filas insertadas juntas, rompiendo el "última versión por
    -- correo" del que dependen /ruta y el editor del asesor.
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),

    nombre       TEXT NOT NULL,
    correo       TEXT NOT NULL,
    celular      TEXT NOT NULL,

    tc_usd       NUMERIC(14,6),
    tc_fecha     DATE,
    ip           TEXT,
    report_sent  BOOLEAN NOT NULL DEFAULT FALSE,

    payload      JSONB NOT NULL,
    payload_raw  JSONB,
    corregido_de UUID REFERENCES diagnosticos(id)
);

CREATE INDEX IF NOT EXISTS idx_diagnosticos_correo
    ON diagnosticos (correo, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_diagnosticos_corregido_de
    ON diagnosticos (corregido_de);
