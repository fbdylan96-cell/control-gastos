CREATE SCHEMA IF NOT EXISTS core;

CREATE SCHEMA IF NOT EXISTS audit;

-- Sentinel business for individual clients (not tied to any organization)
-- business_id '00000000-0000-0000-0000-000000009999' means "individual"
DROP TABLE IF EXISTS core.businesses CASCADE;

CREATE TABLE core.businesses (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed the sentinel individual business
INSERT INTO core.businesses (id, name)
VALUES ('00000000-0000-0000-0000-000000009999', '__individual__');

DROP TABLE IF EXISTS core.clients CASCADE;

CREATE TABLE core.clients (
    id UUID PRIMARY KEY,
    -- Use '00000000-0000-0000-0000-000000009999' for individual clients
    business_id UUID NOT NULL REFERENCES core.businesses(id),
    business_admin BOOLEAN NOT NULL DEFAULT FALSE,

    client_name TEXT NOT NULL,
    email_address TEXT NOT NULL,
    phone_number TEXT,

    email_forward TEXT NOT NULL UNIQUE,

    username TEXT,
    password_hash TEXT,

    active BOOLEAN NOT NULL DEFAULT FALSE,
    whatsapp_notification BOOLEAN NOT NULL DEFAULT FALSE,
    email_notification BOOLEAN NOT NULL DEFAULT FALSE,

    -- Consent (required before the client can use the web app)
    -- data_privacy_approval: client confirmed reading the data privacy policy
    -- messaging_approval: client opted in to WhatsApp/email notifications
    -- approval_date: date both consents were granted (NULL until then)
    data_privacy_approval BOOLEAN NOT NULL DEFAULT FALSE,
    messaging_approval BOOLEAN NOT NULL DEFAULT FALSE,
    approval_date DATE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Investment / brokerage (Alpaca) access, one row per client.
-- A row is created only for clients flagged as investment clients.
--   * enabled            — the gate that makes the web app show Alpaca info.
--                          When FALSE / no row, the Inversión tab keeps its
--                          marketing placeholder.
--   * api_key_cipher     — the client's Alpaca API key id, encrypted at rest
--                          with AES-256-GCM as a self-contained blob
--                          (12-byte nonce || ciphertext; client_id bound as
--                          AAD). The master key lives OUTSIDE the database
--                          (env ENCRYPTION_KEY / KMS) and is NEVER stored
--                          here. NULL until the administrator loads it.
--   * api_secret_cipher  — the matching API secret, same encryption scheme.
-- WARNING: Alpaca API keys are full-permission credentials (they CAN place
-- orders). The app's read-only behaviour is enforced in code: alpaca_client
-- only ever issues GET requests. Credentials are loaded exclusively from the
-- Administración panel and are never returned by any endpoint.
DROP TABLE IF EXISTS core.client_investment CASCADE;

CREATE TABLE core.client_investment (
    client_id          UUID PRIMARY KEY REFERENCES core.clients(id),

    enabled            BOOLEAN NOT NULL DEFAULT FALSE,
    provider           TEXT NOT NULL DEFAULT 'alpaca',

    api_key_cipher     BYTEA,
    api_secret_cipher  BYTEA,
    key_version        INT NOT NULL DEFAULT 1,

    connected_at       TIMESTAMPTZ,
    revoked_at         TIMESTAMPTZ,
    last_used_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TABLE IF EXISTS core.transactions_raw CASCADE;
 
CREATE TABLE core.transactions_raw (
    id UUID PRIMARY KEY,
    -- Both ids stored directly to allow filtering by client or by business
    -- without needing to JOIN through core.clients
    individual_id UUID NOT NULL REFERENCES core.clients(id),
    business_id UUID NOT NULL REFERENCES core.businesses(id),
 
    -- Identificadores del email
    message_id TEXT NOT NULL,
    thread_id TEXT,
    other_transaction_id TEXT,
 
    -- Metadata email
    from_email TEXT,
    to_email TEXT,
    subject TEXT,
 
    -- Contenido
    body_text_full TEXT NOT NULL,
    body_condensed TEXT,
 
    -- Fechas
    ms_date BIGINT,          -- internalDate from Gmail (epoch ms)
    local_date TIMESTAMPTZ,  -- ms_date converted to America/Costa_Rica

 
    -- Fuente
    -- label_source stores the Gmail label string, e.g. 'Finanzas Personales'
    label_source TEXT,
 
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 
    -- Dates broken out for easy filtering/grouping
    -- year_month must be zero-padded: 'YYYY-MM' (e.g. '2026-04', never '2026-4')
    month INT,
    year INT,
    year_month TEXT,
 
    CONSTRAINT uq_message UNIQUE (individual_id, message_id)
);
 

-- Speed up business-level dashboards
CREATE INDEX idx_transactions_raw_business_yearmonth
    ON core.transactions_raw (business_id, year_month);
 
DROP TABLE IF EXISTS core.transactions_enriched CASCADE;
 
CREATE TABLE core.transactions_enriched (
    id UUID PRIMARY KEY,
    raw_id UUID NOT NULL REFERENCES core.transactions_raw(id),
    individual_id UUID NOT NULL REFERENCES core.clients(id),
    business_id UUID NOT NULL REFERENCES core.businesses(id),
 	
    bank TEXT,
    merchant_guess TEXT,
    amount_guess NUMERIC(14,2),
    currency_guess TEXT,
    desc_guess TEXT,
 
    -- Transaction direction parsed from email body
    -- 'debito'  → money leaving the account (COMPRA, PAGO, etc.)
    -- 'credito' → money entering the account (recibido, acreditó, etc.)
    -- 'unknown' → could not be determined from the email
    transaction_type_guess TEXT NOT NULL DEFAULT 'unknown'
        CONSTRAINT chk_transaction_type CHECK (transaction_type_guess IN ('debito', 'credito', 'unknown')),
    
    -- FX conversion to local currency (CRC)
    -- Populated at ingest time using core.exchange_rates (latest available rate_date).
    -- If currency_guess is already 'CRC', amount_local = amount_guess and fx_rate = 1.
    -- amount_local is NULL when conversion is not possible (currency not in exchange_rates,
    --   missing rate on the latest date, or amount_guess itself is NULL).
    -- NOTE: enrichment uses the latest available rate_date in core.exchange_rates,
    --   not the transaction's own date — fine while emails arrive in near-real-time. If we
    --   ever start backfilling old emails, switch to "latest rate on or before local_date".
    amount_local NUMERIC(14,2),   -- amount_guess converted to CRC
    currency_local TEXT NOT NULL DEFAULT 'CRC'
        CONSTRAINT chk_currency_local CHECK (currency_local = 'CRC'),
    fx_rate NUMERIC(14,6),        -- composite rate so amount_guess * fx_rate = amount_local
    fx_rate_date DATE,            -- core.exchange_rates.rate_date used for the lookup

    -- Approval state — 'Denegada' when "deneg" is found in subject or body; skips all further steps
    transaction_approval TEXT NOT NULL DEFAULT 'Aprobada'
        CONSTRAINT chk_transaction_approval CHECK (transaction_approval IN ('Aprobada', 'Denegada')),

    -- Pipeline state
    -- 'Duplicado' = a near-identical transaction (same individual + amount [+ currency]
    --   within 2 minutes) already arrived and was notified; this row is suppressed and
    --   never classified/notified. Its data may have been used to backfill the original.
    transaction_status TEXT NOT NULL DEFAULT 'unknown'
        CONSTRAINT chk_transaction_status CHECK (transaction_status IN ('unknown', 'Procesado', 'Procesado parcialmente', 'Descartado', 'Duplicado')),

    -- Whether OpenAI was invoked to fill missing fields
    ai_assistance BOOLEAN NOT NULL DEFAULT FALSE,

    -- Business member detection: TRUE when a member name was found in the email body
    -- and the transaction was reassigned away from the admin recipient
    member_detected BOOLEAN NOT NULL DEFAULT FALSE,
    -- Who this transaction actually belongs to (may differ from individual_id when member_detected = TRUE)
    assigned_individual_id UUID NOT NULL REFERENCES core.clients(id),

    errors TEXT,
 
    -- Always set to now() on insert; NOT NULL to match pipeline behaviour
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Speed up the polling query for unprocessed rows
CREATE INDEX idx_transactions_enriched_partially_processed
    ON core.transactions_enriched (individual_id, transaction_status)
    WHERE transaction_status = 'Procesado parcialmente';
 
 
DROP TABLE IF EXISTS core.transactions_classified CASCADE;

CREATE TABLE core.transactions_classified (
    id UUID PRIMARY KEY,
    raw_id UUID NOT NULL REFERENCES core.transactions_raw(id),
    individual_id UUID NOT NULL REFERENCES core.clients(id),
    business_id UUID NOT NULL REFERENCES core.businesses(id),

    merchant TEXT,
    category TEXT,
    subcategory TEXT,

    classified_by TEXT              -- 'rules' | 'openai'
        CONSTRAINT chk_classified_by CHECK (classified_by IN ('rules', 'openai')),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TABLE IF EXISTS core.transactions_notifications CASCADE;

-- One row per classified transaction.
-- Tracks notification delivery and the user's final classification decision.
-- final_category / final_subcategory = user-confirmed ground truth:
--   copied from transactions_classified on send, overwritten if user reclassifies.
-- reclassified_by: 'confirmed' = user accepted the guess, 'user' = user overrode it.
CREATE TABLE core.transactions_notifications (
    id UUID PRIMARY KEY,
    classified_id UUID NOT NULL REFERENCES core.transactions_classified(id),
    individual_id UUID NOT NULL REFERENCES core.clients(id),
    business_id UUID NOT NULL REFERENCES core.businesses(id),

    -- Final classification (ground truth after user interaction)
    final_category TEXT,  -- equal to transactions_classified.category value if user doesn't reclassify
    final_subcategory TEXT, -- equal to transactions_classified.subcategory value if user doesn't reclassify
    reclassified_by TEXT             -- 'confirmed' | 'user'
        CONSTRAINT chk_reclassified_by CHECK (reclassified_by IN ('confirmed', 'user')),
    reclassified_at TIMESTAMPTZ,

    -- Email notification
    email_notified BOOLEAN NOT NULL DEFAULT FALSE,
    email_notified_at TIMESTAMPTZ,
    email_action_at TIMESTAMPTZ,
    email_action_value TEXT,

    -- WhatsApp notification
    whatsapp_notified BOOLEAN NOT NULL DEFAULT FALSE,
    whatsapp_notified_at TIMESTAMPTZ,
    whatsapp_action_at TIMESTAMPTZ,
    whatsapp_action_value TEXT,

    rule_processing BOOLEAN NOT NULL DEFAULT FALSE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TABLE IF EXISTS core.categories CASCADE;

-- Categories are defined at the business level and apply to all individuals
-- within that business. individual_id is NULL for business-level categories.
-- When individual_id is set, that category/subcategory is specific to that individual.
-- NOTE: application must enforce that individual_id.business_id == business_id.
CREATE TABLE core.categories (
    id UUID PRIMARY KEY,
    business_id UUID NOT NULL REFERENCES core.businesses(id),
    individual_id UUID REFERENCES core.clients(id), -- NULL = applies to all in business

    category TEXT NOT NULL,
    subcategory TEXT,

    -- Optional monthly budget in CRC (NULL = user not tracking a budget for this category)
    monthly_budget NUMERIC(14,2),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_cat UNIQUE (business_id, individual_id, category, subcategory)
);


DROP TABLE IF EXISTS core.category_rules CASCADE;

-- Learned classification rules: maps a normalized merchant key to a category/subcategory.
-- Acts as a self-improving rule engine — checked first at Step 5 before falling back to AI.
-- Business-level rules (individual_id NULL) apply to all individuals in the business.
-- Individual-level rules (individual_id NOT NULL) take precedence over business-level ones.
CREATE TABLE core.category_rules (
    id UUID PRIMARY KEY,

    business_id UUID NOT NULL REFERENCES core.businesses(id),
    individual_id UUID REFERENCES core.clients(id),  -- NULL = applies to all in business

    merchant_key TEXT NOT NULL,   -- normalized form used for lookup, e.g. 'walmart'
    merchant_raw TEXT,            -- original merchant string before normalization

    category TEXT NOT NULL, -- Learn from the final result transactions_notifications.final_category
    subcategory TEXT, -- Learn from the final result transactions_notifications.final_subategory

    -- Origin of the rule
    -- 'user'   → created/confirmed by user feedback (highest trust)
    -- 'ai'     → created from an AI classification result
    -- 'system' → seeded by the system at setup time
    source TEXT NOT NULL DEFAULT 'ai'
        CONSTRAINT chk_rule_source CHECK (source IN ('user', 'ai', 'system')),

    -- 0.0–1.0: starts lower for AI rules, raised to 1.0 when user confirms
    confidence NUMERIC(4,3) NOT NULL DEFAULT 1.0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_rule UNIQUE NULLS NOT DISTINCT (business_id, individual_id, merchant_key)
);

-- Fast merchant lookup during Step 5 classification
CREATE INDEX idx_category_rules_merchant_key
    ON core.category_rules (business_id, merchant_key);


DROP TABLE IF EXISTS core.exchange_rates CASCADE;

-- Daily snapshot of foreign exchange rates relative to USD.
-- Populated by rate_scheduler.py every business day at 23:30 (server local time).
-- rate_date is always the date the scheduler ran (today), regardless of when the
-- underlying BCCR observation was published — this keeps every currency aligned
-- on the same row date so lookups by date return a complete cross-section.
-- rate_vs_usd is "units of `currency` per 1 USD" (e.g. CRC=531.25 means 1 USD = 531.25 CRC).
-- USD itself is stored with rate_vs_usd = 1.0.
-- rate_vs_usd is NULL when BCCR has no value within the 15-day lookback window
-- (or the currency code is absent from the response).
CREATE TABLE core.exchange_rates (
    rate_date     DATE NOT NULL,
    currency      TEXT NOT NULL,
    rate_vs_usd   NUMERIC(14,6),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rate_date, currency)
);


DROP TABLE IF EXISTS core.whatsapp_chat_messages CASCADE;

-- WhatsApp consultation chat (AI agent): message queue + conversation history.
-- The webhook (web-app/whatsapp_webhook.py) INSERTs inbound free-text messages
-- with status 'pending' and returns 200 to Meta immediately (no LLM work inside
-- the request). whatsapp_agent_worker.py claims pending rows with
-- FOR UPDATE SKIP LOCKED (safe to run multiple workers), answers them via
-- tools/agent.py and stores each reply as a 'done' assistant row.
-- wamid is Meta's message id — the UNIQUE constraint absorbs webhook
-- redeliveries (Meta retries) as no-op inserts. NULL on assistant rows.
CREATE TABLE core.whatsapp_chat_messages (
    id            UUID PRIMARY KEY,
    client_id     UUID NOT NULL REFERENCES core.clients(id) ON DELETE CASCADE,
    phone         TEXT NOT NULL,        -- digits-only, as Meta reports the sender
    role          TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content       TEXT NOT NULL,
    wamid         TEXT UNIQUE,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'processing', 'done', 'failed')),
    error         TEXT,
    claimed_at    TIMESTAMPTZ,          -- set on claim; drives stale-claim reclaim
    processed_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_wa_chat_pending ON core.whatsapp_chat_messages (created_at)
    WHERE status = 'pending';
CREATE INDEX idx_wa_chat_client_history ON core.whatsapp_chat_messages (client_id, created_at);


-- TRUNCATE core.transactions_raw CASCADE;
-- ALTER TABLE core.transactions_enriched DROP COLUMN bank_email_adress;


-- ============================================================================
-- Billing / Motor de pagos (Fase 1)
-- Ver billing_schema.sql para la migración aditiva e idempotente que se corre
-- sobre la base existente. PayPal no está cableado en Fase 1 (columnas paypal_*
-- en NULL). amount_crc = precio mostrado; amount_usd = lo que cobra PayPal
-- (PayPal no soporta CRC).
-- ============================================================================

DROP TABLE IF EXISTS core.discount_redemptions CASCADE;
DROP TABLE IF EXISTS core.client_subscriptions CASCADE;
DROP TABLE IF EXISTS core.discount_codes CASCADE;
DROP TABLE IF EXISTS core.subscription_plans CASCADE;

-- Catálogo de planes (una fila por tier × modalidad).
CREATE TABLE core.subscription_plans (
    id              UUID PRIMARY KEY,
    tier            TEXT NOT NULL
        CONSTRAINT chk_plan_tier CHECK (tier IN ('individual', 'familia', 'empresa')),
    modality        TEXT NOT NULL
        CONSTRAINT chk_plan_modality CHECK (modality IN ('mensual', 'anual')),
    name            TEXT NOT NULL,
    amount_crc      NUMERIC(12,2) NOT NULL,   -- precio mostrado al cliente (CRC)
    amount_usd      NUMERIC(12,2) NOT NULL,   -- monto cobrado en PayPal (USD)
    paypal_plan_id  TEXT,                     -- NULL hasta Fase 2
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_plan UNIQUE (tier, modality)
);

-- Códigos de descuento. discount_pct = 100 → cuenta de cortesía (no se cobra).
CREATE TABLE core.discount_codes (
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
-- ON DELETE CASCADE para que el borrado de un cliente la limpie sin tocar la
-- lógica de borrado de administracion.
CREATE TABLE core.client_subscriptions (
    id                      UUID PRIMARY KEY,
    client_id               UUID NOT NULL UNIQUE REFERENCES core.clients(id) ON DELETE CASCADE,
    plan_id                 UUID REFERENCES core.subscription_plans(id),
    status                  TEXT NOT NULL DEFAULT 'trial'
        CONSTRAINT chk_sub_status CHECK (status IN ('trial', 'active', 'past_due', 'suspended', 'cancelled', 'comp')),
    trial_start             TIMESTAMPTZ,
    trial_end               TIMESTAMPTZ,
    current_period_end      TIMESTAMPTZ,        -- próxima fecha de cobro (NULL en trial/cortesía)
    provider                TEXT NOT NULL DEFAULT 'paypal',
    paypal_subscription_id  TEXT,               -- NULL hasta Fase 2
    comp                    BOOLEAN NOT NULL DEFAULT FALSE,
    discount_code_id        UUID REFERENCES core.discount_codes(id),
    cancel_at_period_end    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Redenciones de códigos (un código como máximo una vez por cliente).
CREATE TABLE core.discount_redemptions (
    id            UUID PRIMARY KEY,
    code_id       UUID NOT NULL REFERENCES core.discount_codes(id),
    client_id     UUID NOT NULL REFERENCES core.clients(id) ON DELETE CASCADE,
    redeemed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_redemption UNIQUE (code_id, client_id)
);

-- Seeds: planes Individuos + códigos de cortesía 100% de los socios.
INSERT INTO core.subscription_plans (id, tier, modality, name, amount_crc, amount_usd)
VALUES (gen_random_uuid(), 'individual', 'mensual', 'Individual mensual', 7500, 15.00)
ON CONFLICT ON CONSTRAINT uq_plan DO NOTHING;

INSERT INTO core.subscription_plans (id, tier, modality, name, amount_crc, amount_usd)
VALUES (gen_random_uuid(), 'individual', 'anual', 'Individual anual', 75000, 150.00)
ON CONFLICT ON CONSTRAINT uq_plan DO NOTHING;

INSERT INTO core.discount_codes (id, code, description, discount_pct)
VALUES (gen_random_uuid(), 'josemontero', 'Socio — cortesía 100%', 100)
ON CONFLICT (code) DO NOTHING;

INSERT INTO core.discount_codes (id, code, description, discount_pct)
VALUES (gen_random_uuid(), 'dylanmosquera', 'Socio — cortesía 100%', 100)
ON CONFLICT (code) DO NOTHING;
