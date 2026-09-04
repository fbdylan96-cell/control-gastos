-- whatsapp_actions_migration.sql — botones v2 de las plantillas de notificación
-- (Reclasificar / Añadir nota / Eliminar)
--
-- Migración ADITIVA e IDEMPOTENTE (segura para prod). Crea la tabla de
-- acciones pendientes: el tap en "Añadir nota" registra que el SIGUIENTE
-- mensaje de texto del cliente (ventana de 10 min, validada en código) es la
-- nota de esa transacción y no un mensaje para el chat AI.
-- create.sql (fuente de verdad) ya incluye la tabla para instalaciones nuevas.

CREATE TABLE IF NOT EXISTS core.whatsapp_pending_actions (
    id               UUID PRIMARY KEY,
    client_id        UUID NOT NULL UNIQUE REFERENCES core.clients(id) ON DELETE CASCADE,
    notification_id  UUID NOT NULL REFERENCES core.transactions_notifications(id) ON DELETE CASCADE,
    action           TEXT NOT NULL CHECK (action IN ('nota')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
