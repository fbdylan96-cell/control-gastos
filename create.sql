--CREATE SCHEMA core;

--CREATE SCHEMA audit;

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

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
    -- Populated at ingest time via BCCR API.
    -- If currency_guess is already 'CRC', amount_local = amount_guess and fx_rate = 1.
    amount_local NUMERIC(14,2),   -- amount_guess converted to CRC
    currency_local TEXT,          -- always 'CRC' for now
    fx_rate NUMERIC(10,4),        -- rate used (e.g. 531.2500 means 1 USD = 531.25 CRC)
    fx_rate_date DATE,            -- date the rate was fetched from BCCR

    -- Approval state — 'Denegada' when "deneg" is found in subject or body; skips all further steps
    transaction_approval TEXT NOT NULL DEFAULT 'Aprobada'
        CONSTRAINT chk_transaction_approval CHECK (transaction_approval IN ('Aprobada', 'Denegada')),

    -- Pipeline state
    transaction_status TEXT NOT NULL DEFAULT 'unknown'
        CONSTRAINT chk_transaction_status CHECK (transaction_status IN ('unknown', 'Procesado', 'Procesado parcialmente', 'Descartado')),

    -- Whether OpenAI was invoked to fill missing fields
    ai_assistance BOOLEAN NOT NULL DEFAULT FALSE,

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

    -- Mirrors '💾 Crear Regla Permanente' checkbox in the sheet
    permanent_rule BOOLEAN NOT NULL DEFAULT FALSE,
    logic TEXT,           -- free-text reasoning (Razonamiento)
    classified_by TEXT,   -- 'rules' | 'openai'

    -- Notification / action tracking (mirrors sheet columns)
    email_notified BOOLEAN NOT NULL DEFAULT FALSE,
    email_notified_at TIMESTAMPTZ,
    email_action_at TIMESTAMPTZ,
    email_action_value TEXT,

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

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_cat UNIQUE (business_id, individual_id, category, subcategory)
);


-- TRUNCATE core.transactions_raw CASCADE;
-- ALTER TABLE core.transactions_enriched DROP COLUMN bank_email_adress;
