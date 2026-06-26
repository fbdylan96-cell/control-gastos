-- ============================================================================
-- account_type_migration.sql — Distinción Familia / Empresa en core.businesses
--
-- Migración ADITIVA e IDEMPOTENTE. Segura para correr en producción:
--     psql "<DB_PROD_URL>" -f account_type_migration.sql
--
-- Agrega core.businesses.account_type ('individual' | 'familia' | 'empresa').
-- Distingue qué facturación aplicar a cada cuenta (el workflow de familia y
-- empresa es idéntico; sólo cambia el plan/precio).
--   * Negocio centinela (__individual__, ...9999) → 'individual'.
--   * Todo otro business existente → 'familia' (hoy todas las "empresas"
--     cargadas son en realidad familias).
--   * Nuevas cuentas: el panel de administración manda 'familia' o 'empresa'.
-- La misma columna queda en create.sql (fuente de verdad).
-- ============================================================================

ALTER TABLE core.businesses ADD COLUMN IF NOT EXISTS account_type TEXT;

UPDATE core.businesses
SET account_type = CASE
        WHEN id = '00000000-0000-0000-0000-000000009999' THEN 'individual'
        ELSE 'familia'
    END
WHERE account_type IS NULL;

ALTER TABLE core.businesses ALTER COLUMN account_type SET DEFAULT 'empresa';
ALTER TABLE core.businesses ALTER COLUMN account_type SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_business_account_type'
    ) THEN
        ALTER TABLE core.businesses
            ADD CONSTRAINT chk_business_account_type
            CHECK (account_type IN ('individual', 'familia', 'empresa'));
    END IF;
END$$;
