-- Migración aditiva: notas del cliente por transacción.
--
-- Los clientes pidieron poder anotar el contexto de un gasto ("regalo de
-- cumpleaños de Ana", "reembolsable por la empresa"). La nota vive en
-- transactions_notifications porque es la fila que ya guarda la decisión
-- final del usuario sobre la transacción (final_category / reclassified_by);
-- las tablas raw/enriched/classified son del pipeline y no se editan.
--
-- Límite de 280 caracteres (tamaño de un tweet): suficiente para contexto,
-- corto como para caber en una celda de tabla y en el Excel de reportes.
-- NULL = sin nota. Es aditiva y anulable, así que ninguna lectura ni inserción
-- existente se ve afectada.

ALTER TABLE core.transactions_notifications
    ADD COLUMN IF NOT EXISTS client_notes VARCHAR(280);

COMMENT ON COLUMN core.transactions_notifications.client_notes IS
    'Nota libre escrita por el cliente sobre la transacción (máx. 280 caracteres). NULL = sin nota.';
