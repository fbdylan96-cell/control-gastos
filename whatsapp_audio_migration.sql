-- whatsapp_audio_migration.sql — notas de voz en el chat de WhatsApp
--
-- Migración ADITIVA e IDEMPOTENTE (segura para prod):
--   media_id — id del media en la Cloud API de Meta cuando el mensaje entrante
--   fue una nota de voz. El worker lo descarga, lo transcribe con Whisper y
--   sobreescribe content con la transcripción. NULL para mensajes de texto.
--
-- create.sql (fuente de verdad) ya incluye la columna para instalaciones nuevas.

ALTER TABLE core.whatsapp_chat_messages ADD COLUMN IF NOT EXISTS media_id TEXT;
