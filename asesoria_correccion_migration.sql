-- Migración aditiva en asesoria_db: corrección de diagnósticos por el asesor.
--
-- Contexto: los prospectos llenan mal el diagnóstico. El asesor lo revisa con
-- el cliente, corrige y vuelve a enviar. Dos columnas nuevas lo soportan.
--
-- payload_raw — el payload ANTES de convertir dólares a colones. `payload`
--   guarda el resultado post-conversión (que es lo que consume /ruta y el
--   Excel), pero esa conversión es destructiva: reescribe los montos y le pega
--   al nombre un sufijo " [US$ 123.45]". Recargar esa versión en el formulario
--   mostraría colones donde el cliente escribió dólares y, al volver a guardar,
--   anotaría el sufijo dos veces. payload_raw es la copia fiel para reeditar.
--   NULL en las filas anteriores a este cambio (ahí se limpia el sufijo al
--   cargar y se trabaja en colones).
--
-- corregido_de — id del diagnóstico que esta fila corrige. Cada corrección es
--   una fila nueva, no un UPDATE: así queda evidencia de lo que el cliente
--   llenó originalmente frente a lo que el asesor corrigió, que en un servicio
--   pagado es respaldo. /ruta ya toma la más reciente por correo
--   (ORDER BY created_at DESC LIMIT 1), así que ve la corrección sin cambios.

-- created_at pasa de now() a clock_timestamp(). now() devuelve la hora de
-- INICIO DE LA TRANSACCIÓN: dos filas insertadas en la misma transacción
-- quedan con el mismo timestamp y el "ORDER BY created_at DESC LIMIT 1" que
-- usan /ruta y el editor para tomar la última versión resuelve el empate de
-- forma arbitraria. Con la corrección de diagnósticos eso importa: original y
-- corrección son la misma fila lógica en dos versiones. clock_timestamp() es
-- la hora real del insert. Solo afecta filas nuevas.
ALTER TABLE diagnosticos
    ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE diagnosticos
    ADD COLUMN IF NOT EXISTS payload_raw JSONB;

ALTER TABLE diagnosticos
    ADD COLUMN IF NOT EXISTS corregido_de UUID REFERENCES diagnosticos(id);

CREATE INDEX IF NOT EXISTS idx_diagnosticos_corregido_de
    ON diagnosticos (corregido_de);

COMMENT ON COLUMN diagnosticos.payload_raw IS
    'Payload tal como lo escribió el cliente, antes de la conversión USD→CRC. NULL en filas previas a la corrección de diagnósticos.';
COMMENT ON COLUMN diagnosticos.corregido_de IS
    'Diagnóstico que esta fila corrige. NULL = envío original del cliente.';
