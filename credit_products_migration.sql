-- Migración aditiva: comparador de productos crediticios (BCCR / MEIC).
--
-- Tabla de datos de mercado externos, hermana de core.exchange_rates: la
-- puebla un job del rate_scheduler los días 8 y 22. Ver extractorbccrmeic.md.
--
-- Guarda SOLO el último período extraído; cada corrida reemplaza el contenido
-- entero (DELETE + INSERT en una transacción). Por eso el tamaño es constante
-- (~6 400 filas, ~5 MB) y no hace falta particionar ni purgar.
--
-- El orden de las columnas espeja el orden de la fuente para que el mapeo
-- contra credit_products_update.DB_COLUMNS sea 1 a 1 y auditable de un vistazo.

CREATE TABLE IF NOT EXISTS core.credit_products (
    person_type       SMALLINT,
    provider_id       TEXT NOT NULL,        -- cédula jurídica: es texto, no numérico
    provider_name     TEXT,
    provider_group    TEXT,                 -- Bancos, Cooperativas, …
    period            DATE NOT NULL,        -- corte del dato en la fuente
    product_type      SMALLINT,
    product           TEXT,                 -- "Clasificación"; columna del filtro
    usage_type        SMALLINT,
    usage             TEXT,                 -- Nuevo / Usado / No aplica
    generator_type    SMALLINT,
    generator         TEXT,
    client_type       SMALLINT,
    client            TEXT,                 -- Cliente actual / nuevo
    product_name      TEXT,
    currency_type     SMALLINT,
    currency          TEXT,                 -- Colón / Dólar estadounidense / Euro
    term_months       INTEGER,
    down_payment_pct  NUMERIC(12,4),
    rate_type         SMALLINT,
    rate_kind         TEXT,                 -- "Tasa fija-variable", etc.
    nominal_rate      NUMERIC(12,4),
    default_rate      NUMERIC(12,4),        -- tasa moratoria
    rate_notes        TEXT,
    benefits          TEXT,
    fee_type          SMALLINT,
    fee               TEXT,                 -- Formalización, avalúo, …
    fee_value         NUMERIC(18,4),
    fee_value_type    SMALLINT,
    fee_format        TEXT,                 -- Porcentaje / Monto
    fee_notes         TEXT,

    -- El grano es producto × cargo y la fuente no trae clave estable: dos filas
    -- pueden coincidir en todo lo identificatorio y diferir solo en un texto.
    -- El hash cubre las 30 columnas de arriba.
    row_hash          BYTEA NOT NULL,
    extracted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (period, row_hash)
);

CREATE INDEX IF NOT EXISTS idx_credit_products_seg
    ON core.credit_products (product, currency, usage);
CREATE INDEX IF NOT EXISTS idx_credit_products_provider
    ON core.credit_products (provider_id);

COMMENT ON TABLE core.credit_products IS
    'Comparador de productos crediticios del BCCR/MEIC. Solo el último período extraído; cada corrida reemplaza el contenido. Ver extractorbccrmeic.md';

-- Un registro por producto, sin la explosión por cargo.
CREATE OR REPLACE VIEW core.v_credit_products_unique AS
SELECT DISTINCT period, provider_id, provider_name, provider_group,
       product, usage, product_name, currency, term_months, down_payment_pct,
       rate_kind, nominal_rate, default_rate
FROM core.credit_products;
