"""WhatsApp AI consultation chat worker.

Standalone process (runs under systemd, like email_reader.py and
rate_scheduler.py): drains core.whatsapp_chat_messages rows queued by the
webhook, runs the finance agent (tools/agent.py) for each, and replies via
the Meta Cloud API.

Design notes
------------
- The webhook only INSERTs and returns 200 to Meta immediately, so slow LLM
  calls never occupy a gunicorn worker.
- Claims use FOR UPDATE SKIP LOCKED: starting a second worker process scales
  throughput horizontally with no code change.
- A failed message is marked 'failed' and the user gets a fallback reply —
  no automatic retry, so a persistent error can't burn API spend in a loop.
- Rows stuck in 'processing' (worker crashed mid-run) are reclaimed after
  STALE_PROCESSING_MINUTES. If the crash happened after send_text but before
  mark_done, the client may receive a second reply — rare and acceptable.

Run with:  python whatsapp_agent_worker.py
"""

import logging
import os
import re
import time
import uuid

from dotenv import load_dotenv

load_dotenv()

import db
import whatsapp_client
from tools import agent

POLL_INTERVAL = 2               # seconds between idle polls
RECLAIM_EVERY = 300             # seconds between stale-claim sweeps
STALE_PROCESSING_MINUTES = 10   # reclaim 'processing' rows older than this
HISTORY_LIMIT = 10              # prior messages given to the agent as context

# Notas de voz: tope de tamaño antes de transcribir (~5 min de opus). Acota el
# costo de Whisper (~$0.006/min) y evita audios-podcast.
MAX_AUDIO_BYTES = 3 * 1024 * 1024
TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1")

# Respuestas por audio (espejo): si el cliente mandó nota de voz Y el switch
# WHATSAPP_AUDIO_REPLIES=1, la respuesta va como texto + nota de voz (TTS).
# Apagar = poner el flag en 0 en .env y reiniciar este servicio.
TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("OPENAI_TTS_VOICE", "coral")
# Solo lo soporta gpt-4o-mini-tts (los modelos tts-1* lo rechazan). Dirige el
# estilo de habla; la lectura de ₡ además se fuerza en _speakable() porque las
# instrucciones son probabilísticas.
TTS_INSTRUCTIONS = os.environ.get(
    "OPENAI_TTS_INSTRUCTIONS",
    "Hablá en español latinoamericano neutro, con tono natural, cálido y "
    "conversacional, como un asesor financiero de confianza. Los montos son "
    "colones costarricenses: decí siempre 'colones', nunca 'pesos'. Leé los "
    "números como cantidades completas ('2000' es 'dos mil'), nunca dígito "
    "por dígito.",
)
# Respuestas más largas que esto solo van en texto: leídas en voz alta suenan
# mal (tablas/listas) y acota el costo del TTS.
TTS_MAX_CHARS = 1500

FALLBACK_REPLY = (
    "Lo siento, no pude procesar tu consulta en este momento. "
    "Intenta de nuevo en unos minutos."
)
AUDIO_FALLBACK_REPLY = (
    "No pude procesar tu nota de voz. ¿Me lo escribís en un mensaje de texto?"
)
AUDIO_TOO_LONG_REPLY = (
    "Tu nota de voz es muy larga para procesarla. Intentá con un audio más "
    "corto (menos de ~5 minutos) o escribime el mensaje."
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------

def claim_next(conn):
    """Atomically claim the oldest pending message; None when queue is empty."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.whatsapp_chat_messages
            SET status = 'processing', claimed_at = now()
            WHERE id = (
                SELECT id FROM core.whatsapp_chat_messages
                WHERE status = 'pending'
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, client_id, phone, content, media_id
            """
        )
        row = cur.fetchone()
        cols = [d[0] for d in cur.description] if row else None
    conn.commit()
    return dict(zip(cols, row)) if row else None


def reclaim_stale(conn):
    """Re-queue rows stuck in 'processing' by a crashed/restarted worker."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.whatsapp_chat_messages
            SET status = 'pending', claimed_at = NULL
            WHERE status = 'processing'
              AND claimed_at < now() - make_interval(mins => %s)
            """,
            (STALE_PROCESSING_MINUTES,),
        )
        reclaimed = cur.rowcount
    conn.commit()
    if reclaimed:
        log.warning(f"Reclaimed {reclaimed} stale processing message(s)")


def mark_done(conn, msg_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.whatsapp_chat_messages
            SET status = 'done', processed_at = now()
            WHERE id = %s
            """,
            (msg_id,),
        )
    conn.commit()


def mark_failed(conn, msg_id, error: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.whatsapp_chat_messages
            SET status = 'failed', processed_at = now(), error = left(%s, 500)
            WHERE id = %s
            """,
            (error, msg_id),
        )
    conn.commit()


def update_content(conn, msg_id, content):
    """Reemplaza el placeholder de una nota de voz con su transcripción, para
    que load_history dé al agente el texto real en turnos futuros."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.whatsapp_chat_messages SET content = %s WHERE id = %s",
            (content, msg_id),
        )
    conn.commit()


def insert_reply(conn, client_id, phone, content):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.whatsapp_chat_messages
                (id, client_id, phone, role, content, status, processed_at)
            VALUES (%s, %s, %s, 'assistant', %s, 'done', now())
            """,
            (str(uuid.uuid4()), client_id, phone, content),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------

def fetch_client(conn, client_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, business_id, business_admin, client_name
            FROM core.clients
            WHERE id = %s AND active = TRUE
            """,
            (client_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def load_history(conn, client_id, exclude_id):
    """Last completed turns within 24h, oldest first, for multi-turn context."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT role, content
            FROM core.whatsapp_chat_messages
            WHERE client_id = %s AND id <> %s AND status = 'done'
              AND created_at > now() - interval '24 hours'
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (client_id, exclude_id, HISTORY_LIMIT),
        )
        rows = cur.fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


# ---------------------------------------------------------------------------
# Transcripción de notas de voz
# ---------------------------------------------------------------------------

class AudioTooLong(Exception):
    pass


def transcribe_audio(media_id: str) -> str:
    """Descarga la nota de voz de la Cloud API y la transcribe con Whisper.

    Lanza AudioTooLong si supera MAX_AUDIO_BYTES; cualquier otra excepción la
    maneja el llamador con AUDIO_FALLBACK_REPLY.
    """
    import openai

    audio_bytes, mime = whatsapp_client.get_media(media_id)
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise AudioTooLong(f"{len(audio_bytes)} bytes")

    # Los audios de WhatsApp llegan como OGG/Opus; la extensión del nombre es
    # lo que la API usa para detectar el formato.
    ext = "mp3" if "mpeg" in mime else "ogg"
    result = openai.OpenAI().audio.transcriptions.create(
        model=TRANSCRIBE_MODEL,
        file=(f"nota_de_voz.{ext}", audio_bytes),
        language="es",
    )
    text = (result.text or "").strip()
    if not text:
        raise ValueError("transcripción vacía")
    return text


# ---------------------------------------------------------------------------
# Respuestas por audio (TTS)
# ---------------------------------------------------------------------------

def _audio_replies_enabled() -> bool:
    return os.environ.get("WHATSAPP_AUDIO_REPLIES", "0").strip() == "1"


# Montos en colones tal como los escribe el agente: "₡2 000" (espacio de miles,
# también NBSP/narrow-NBSP), "₡50,000.00", "₡2.000", "₡2000".
_CRC_AMOUNT_RE = re.compile(
    r"₡\s*("
    "\d{1,3}(?:[   ]\d{3})+(?:[.,]\d{1,2})?"  # 2 000 / 1 234 567,50
    r"|\d(?:[\d.,]*\d)?"                                 # 2000 / 50,000.00 / 2.000
    r")"
)


def _spoken_amount(num: str) -> str:
    """Normaliza un monto a cantidad plana para que el TTS lo lea como número
    ('2000' → 'dos mil') y no dígito por dígito ('2 000' → 'dos cero cero cero')."""
    compact = re.sub("[   ]", "", num)
    m = re.match(r"^(\d[\d.,]*?)[.,](\d{1,2})$", compact)  # 1-2 decimales al final
    integer, dec = (m.group(1), m.group(2)) if m else (compact, "")
    integer = re.sub(r"[.,]", "", integer)  # el resto de [.,] es agrupación de miles
    if dec and int(dec) != 0:
        return f"{integer}.{dec} colones"
    return f"{integer} colones"


def _speakable(text: str) -> str:
    """Ajusta el texto SOLO para el audio (el mensaje de texto va intacto):
    '₡2 000' → '2000 colones', porque el TTS lee ₡ como 'pesos' y los números
    con separadores los deletrea."""
    return _CRC_AMOUNT_RE.sub(lambda m: _spoken_amount(m.group(1)), text)


def synthesize_speech(text: str) -> bytes:
    """Convierte la respuesta del agente a voz (OGG/Opus) con TTS de OpenAI."""
    import openai

    kwargs = {}
    if not TTS_MODEL.startswith("tts-"):
        kwargs["instructions"] = TTS_INSTRUCTIONS
    resp = openai.OpenAI().audio.speech.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=_speakable(text),
        response_format="opus",
        **kwargs,
    )
    return resp.content


def send_voice_reply(phone: str, reply: str) -> None:
    """Best-effort: sintetiza y envía la respuesta como nota de voz.

    Nunca lanza — el texto ya fue entregado; el audio es un extra y su fallo
    solo se registra en el log.
    """
    try:
        audio = synthesize_speech(reply)
        media_id = whatsapp_client.upload_media(audio, "audio/ogg", "respuesta.ogg")
        whatsapp_client.send_audio(phone, media_id)
        log.info(f"Voice reply sent ({len(audio)} bytes)")
    except Exception as e:
        log.error(f"Voice reply failed (text already sent): {e}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def process_message(conn, row):
    client = fetch_client(conn, row["client_id"])
    if not client:
        mark_failed(conn, row["id"], "client not found or inactive")
        return

    content = row["content"]
    if row.get("media_id"):
        try:
            content = transcribe_audio(row["media_id"])
        except AudioTooLong as e:
            log.warning(f"Audio too long for message {row['id']}: {e}")
            whatsapp_client.send_text(row["phone"], AUDIO_TOO_LONG_REPLY, preview_url=False)
            mark_failed(conn, row["id"], f"audio too long: {e}")
            return
        except Exception as e:
            log.error(f"Transcription failed for message {row['id']}: {e}")
            whatsapp_client.send_text(row["phone"], AUDIO_FALLBACK_REPLY, preview_url=False)
            mark_failed(conn, row["id"], f"transcription failed: {e}")
            return
        update_content(conn, row["id"], content)
        log.info(f"Transcribed voice note {row['id']} ({len(content)} chars)")

    history = load_history(conn, row["client_id"], row["id"])
    reply = agent.answer_query(conn, client, history, content)

    whatsapp_client.send_text(row["phone"], reply, preview_url=False)
    # Espejo: el cliente habló en audio → respondemos también en audio (además
    # del texto, que conserva montos/listas legibles).
    if row.get("media_id") and _audio_replies_enabled() and len(reply) <= TTS_MAX_CHARS:
        send_voice_reply(row["phone"], reply)
    insert_reply(conn, row["client_id"], row["phone"], reply)
    mark_done(conn, row["id"])
    log.info(
        f"Answered chat message {row['id']} for client={row['client_id']} "
        f"({len(reply)} chars)"
    )


def _recover_connection(conn):
    """Return a usable connection after a cycle failed (same as email_reader)."""
    try:
        conn.rollback()
        return conn
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass
    try:
        new_conn = db.get_connection()
        log.warning("DB connection was broken — reconnected.")
        return new_conn
    except Exception as e:
        log.error(f"DB reconnect failed (will retry next cycle): {e}")
        return conn


def main():
    conn = db.get_connection()
    reclaim_stale(conn)
    log.info("WhatsApp agent worker started.")

    last_reclaim = time.monotonic()
    while True:
        try:
            row = claim_next(conn)
            if row is None:
                if time.monotonic() - last_reclaim > RECLAIM_EVERY:
                    reclaim_stale(conn)
                    last_reclaim = time.monotonic()
                time.sleep(POLL_INTERVAL)
                continue

            try:
                process_message(conn, row)
            except Exception as e:
                log.error(f"Agent failed for message {row['id']}: {e}")
                try:
                    conn.rollback()
                except Exception:
                    conn = _recover_connection(conn)
                mark_failed(conn, row["id"], str(e))
                try:
                    whatsapp_client.send_text(row["phone"], FALLBACK_REPLY, preview_url=False)
                except Exception as send_err:
                    log.error(f"Fallback reply failed for {row['phone']}: {send_err}")

        except Exception as e:
            log.error(f"Worker loop error: {e}")
            conn = _recover_connection(conn)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
