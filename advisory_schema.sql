-- ============================================================================
-- advisory_schema.sql — Asesoría Financiera Personal (seguimiento, Fase 1)
--
-- Migración ADITIVA e IDEMPOTENTE. Segura para correr contra la base de datos
-- de producción: sólo crea tablas que aún no existen y NUNCA borra ni altera
-- objetos existentes. Correr con:
--     psql "<DB_PROD_URL>" -f advisory_schema.sql
--
-- La fuente de verdad de nombres de columnas sigue siendo create.sql (estas
-- mismas tablas están añadidas allí). Diseño completo en PLAN_asesoria.md.
-- ============================================================================

-- Plan de asesoría, una fila por cliente (patrón client_investment).
--   * enabled                — interruptor maestro; los tres módulos filtran
--                              además por su propio flag.
--   * tracking_start         — NULL durante el mes baseline: ningún job envía
--                              nada hasta la reunión de activación.
--   * emergency_fund_current — saldo actual del fondo; lo actualiza el asesor
--                              (el fondo vive en una cuenta que Neto no ve).
--   * declared_monthly_income— sustituye a los ingresos capturados cuando el
--                              banco no notifica créditos. NULL = usar lo
--                              capturado.
--   * weekly_send_dow        — día del envío semanal: 0=domingo … 6=sábado.
CREATE TABLE IF NOT EXISTS core.client_advisory_plans (
    client_id               UUID PRIMARY KEY REFERENCES core.clients(id) ON DELETE CASCADE,

    enabled                 BOOLEAN NOT NULL DEFAULT FALSE,

    program_start           DATE NOT NULL,
    tracking_start          DATE,
    program_end             DATE,

    objective               TEXT
        CONSTRAINT chk_advisory_objective CHECK (objective IN
            ('control_gasto', 'fondo_emergencia', 'pago_deuda', 'compra_activo', 'inversion')),
    target_savings_rate     NUMERIC(5,2),
    emergency_fund_goal     NUMERIC(14,2),
    emergency_fund_current  NUMERIC(14,2) NOT NULL DEFAULT 0,
    declared_monthly_income NUMERIC(14,2),

    weekly_send_dow         SMALLINT NOT NULL DEFAULT 1
        CONSTRAINT chk_weekly_send_dow CHECK (weekly_send_dow BETWEEN 0 AND 6),
    weekly_summary_enabled  BOOLEAN NOT NULL DEFAULT TRUE,
    fund_tracking_enabled   BOOLEAN NOT NULL DEFAULT TRUE,
    budget_alerts_enabled   BOOLEAN NOT NULL DEFAULT TRUE,

    notes                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Log de envíos: la PK compuesta ES la garantía de idempotencia (disparo único
-- por período). Todo envío hace INSERT ... ON CONFLICT DO NOTHING y solo manda
-- el WhatsApp si la fila se insertó.
-- period_key: weekly_summary '2026-W30' | monthly_fund '2026-07' |
--             budget_80/100 '2026-07:<categoria>'
CREATE TABLE IF NOT EXISTS core.advisory_message_log (
    client_id     UUID NOT NULL REFERENCES core.clients(id) ON DELETE CASCADE,
    message_type  TEXT NOT NULL
        CONSTRAINT chk_advisory_msg_type CHECK (message_type IN
            ('weekly_summary', 'monthly_fund', 'budget_80', 'budget_100')),
    period_key    TEXT NOT NULL,
    sent_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, message_type, period_key)
);
