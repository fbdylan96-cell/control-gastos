-- Migration: add consent columns to core.clients without dropping data.
-- Run this on the live DB instead of recreating from create.sql.
ALTER TABLE core.clients
    ADD COLUMN IF NOT EXISTS data_privacy_approval BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS messaging_approval    BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS approval_date         DATE;
