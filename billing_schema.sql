-- ============================================================================
-- billing_schema.sql — Motor de pagos (Fase 1)
--
-- Migración ADITIVA e IDEMPOTENTE. Segura para correr contra la base de datos
-- de producción: sólo crea tablas/filas que aún no existen y NUNCA borra ni
-- altera objetos existentes. Correr con:
--     psql "<DB_PROD_URL>" -f billing_schema.sql
--
-- La fuente de verdad de nombres de columnas sigue siendo create.sql (estas
-- mismas tablas están añadidas allí). PayPal NO está cableado en Fase 1: las
-- columnas paypal_* quedan en NULL hasta construir y probar la integración
-- viva (Fase 2) en sandbox.
-- ============================================================================

-- Catálogo de planes (una fila por tier × modalidad).
--   amount_crc → precio mostrado al cliente (lo que ve en la app).
--   amount_usd → monto que se cobrará en PayPal (PayPal no soporta CRC).
--   paypal_plan_id → se llena en Fase 2 al crear el Billing Plan en PayPal.
CREATE TABLE IF NOT EXISTS core.subscription_plans (
    id              UUID PRIMARY KEY,
    tier            TEXT NOT NULL
        CONSTRAINT chk_plan_tier CHECK (tier IN ('individual', 'familia', 'empresa')),
    modality        TEXT NOT NULL
        CONSTRAINT chk_plan_modality CHECK (modality IN ('mensual', 'anual')),
    name            TEXT NOT NULL,
    amount_crc      NUMERIC(12,2) NOT NULL,
    amount_usd      NUMERIC(12,2) NOT NULL,
    paypal_plan_id  TEXT,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_plan UNIQUE (tier, modality)
);

-- Códigos de descuento. discount_pct = 100 → cuenta de cortesía (no se cobra).
CREATE TABLE IF NOT EXISTS core.discount_codes (
    id               UUID PRIMARY KEY,
    code             TEXT NOT NULL UNIQUE,
    description      TEXT,
    discount_pct     INT NOT NULL DEFAULT 100
        CONSTRAINT chk_discount_pct CHECK (discount_pct BETWEEN 1 AND 100),
    active           BOOLEAN NOT NULL DEFAULT TRUE,
    max_redemptions  INT,                       -- NULL = ilimitado
    times_redeemed   INT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_redemptions_cap CHECK (max_redemptions IS NULL OR times_redeemed <= max_redemptions)
);

-- Suscripción por cliente (una fila por core.clients).
--   * status 'trial'  → prueba gratuita (trial_end marca el fin).
--   * status 'comp'   → cortesía (código 100% o marcada por el admin); sin cargos.
--   * current_period_end → próxima fecha de cobro (NULL en trial/cortesía).
--   * paypal_subscription_id → se llena en Fase 2.
-- ON DELETE CASCADE: el borrado de un cliente (administracion._wipe_*) limpia
-- esta fila automáticamente, sin tocar esa lógica de borrado.
CREATE TABLE IF NOT EXISTS core.client_subscriptions (
    id                      UUID PRIMARY KEY,
    client_id               UUID NOT NULL UNIQUE REFERENCES core.clients(id) ON DELETE CASCADE,
    plan_id                 UUID REFERENCES core.subscription_plans(id),
    status                  TEXT NOT NULL DEFAULT 'trial'
        CONSTRAINT chk_sub_status CHECK (status IN ('trial', 'active', 'past_due', 'suspended', 'cancelled', 'comp')),
    trial_start             TIMESTAMPTZ,
    trial_end               TIMESTAMPTZ,
    current_period_end      TIMESTAMPTZ,
    provider                TEXT NOT NULL DEFAULT 'paypal',
    paypal_subscription_id  TEXT,
    comp                    BOOLEAN NOT NULL DEFAULT FALSE,
    discount_code_id        UUID REFERENCES core.discount_codes(id),
    cancel_at_period_end    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Redenciones de códigos (un código como máximo una vez por cliente).
CREATE TABLE IF NOT EXISTS core.discount_redemptions (
    id            UUID PRIMARY KEY,
    code_id       UUID NOT NULL REFERENCES core.discount_codes(id),
    client_id     UUID NOT NULL REFERENCES core.clients(id) ON DELETE CASCADE,
    redeemed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_redemption UNIQUE (code_id, client_id)
);

-- ── Seeds ────────────────────────────────────────────────────────────────────
-- Planes Individuos (Fase 1). amount_usd = conversión propuesta desde ₡
-- (≈ ₡500/$), ajustable; NO se cobra nada en Fase 1.
INSERT INTO core.subscription_plans (id, tier, modality, name, amount_crc, amount_usd)
VALUES (gen_random_uuid(), 'individual', 'mensual', 'Individual mensual', 7500, 15.00)
ON CONFLICT ON CONSTRAINT uq_plan DO NOTHING;

INSERT INTO core.subscription_plans (id, tier, modality, name, amount_crc, amount_usd)
VALUES (gen_random_uuid(), 'individual', 'anual', 'Individual anual', 75000, 150.00)
ON CONFLICT ON CONSTRAINT uq_plan DO NOTHING;

-- Planes Familia. USD = misma conversión ≈ ₡500/$ (ajustable; no se cobra en Fase 1).
INSERT INTO core.subscription_plans (id, tier, modality, name, amount_crc, amount_usd)
VALUES (gen_random_uuid(), 'familia', 'mensual', 'Familia mensual', 10000, 20.00)
ON CONFLICT ON CONSTRAINT uq_plan DO NOTHING;

INSERT INTO core.subscription_plans (id, tier, modality, name, amount_crc, amount_usd)
VALUES (gen_random_uuid(), 'familia', 'anual', 'Familia anual', 100000, 200.00)
ON CONFLICT ON CONSTRAINT uq_plan DO NOTHING;

-- Códigos de cortesía 100% (socios).
INSERT INTO core.discount_codes (id, code, description, discount_pct)
VALUES (gen_random_uuid(), 'josemontero', 'Socio — cortesía 100%', 100)
ON CONFLICT (code) DO NOTHING;

INSERT INTO core.discount_codes (id, code, description, discount_pct)
VALUES (gen_random_uuid(), 'dylanmosquera', 'Socio — cortesía 100%', 100)
ON CONFLICT (code) DO NOTHING;
