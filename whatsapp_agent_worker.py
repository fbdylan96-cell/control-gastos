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

FALLBACK_REPLY = (
    "Lo siento, no pude procesar tu consulta en este momento. "
    "Intenta de nuevo en unos minutos."
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
            RETURNING id, client_id, phone, content
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
# Main loop
# ---------------------------------------------------------------------------

def process_message(conn, row):
    client = fetch_client(conn, row["client_id"])
    if not client:
        mark_failed(conn, row["id"], "client not found or inactive")
        return

    history = load_history(conn, row["client_id"], row["id"])
    reply = agent.answer_query(conn, client, history, row["content"])

    whatsapp_client.send_text(row["phone"], reply, preview_url=False)
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
