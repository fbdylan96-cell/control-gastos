-- Migración: auditoría de cambios de contraseña (2026-07-05)
-- Idempotente: se puede correr varias veces sin efecto adicional.
--
--   psql -h <RDS> -U <user> -d gastos_db -f password_change_migration.sql

ALTER TABLE core.clients
    ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;

COMMENT ON COLUMN core.clients.password_changed_at IS
    'Última vez que el cliente cambió su contraseña (web app o reset por email). NULL = nunca la cambió (sigue con la inicial).';
