import logging
import time

from dotenv import load_dotenv

import classifier
import db
import enricher
import gmail_client
import parser as email_parser

load_dotenv()

GMAIL_LABEL = "Finanzas Personales"
MAX_PER_RUN = 5
POLL_INTERVAL = 60  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def process_one_message(service, msg_meta, clients, conn):
    msg_id = msg_meta["id"]
    full_msg = gmail_client.get_message_full(service, msg_id)
    headers = email_parser.read_headers(full_msg)

    log.info(f"  Subject: [{headers['subject']}]  From: [{headers['from']}]")

    client = email_parser.detect_client(headers, clients)
    if not client:
        log.warning(f"  No active client matched for To: [{headers['to']}] — skipping")
        gmail_client.mark_as_read(service, msg_id)
        return

    if db.message_exists(conn, client["id"], headers["message_id"]):
        log.info(f"  Duplicate {msg_id} — skipping")
        gmail_client.mark_as_read(service, msg_id)
        return

    row = email_parser.build_raw_row(full_msg, client, GMAIL_LABEL)
    db.insert_raw_transaction(conn, row)
    log.info(f"  Inserted raw row | year_month={row['year_month']} | client={client['email_forward']}")

    enriched_row = enricher.enrich_raw(row, conn=conn, client=client)
    db.insert_enriched_transaction(conn, enriched_row)
    log.info(f"  Inserted enriched row | status={enriched_row['transaction_status']} | bank={enriched_row['bank']}")

    if enriched_row.get("transaction_approval") != "Denegada":
        classifier.classify_transaction(conn, enriched_row)
        log.info(f"  Classification done")

    gmail_client.mark_as_read(service, msg_id)


def run_once(service, conn):
    clients = db.get_active_clients(conn)
    if not clients:
        log.warning("No active clients in DB.")
        return

    messages = gmail_client.find_unread_label_messages(service, GMAIL_LABEL, MAX_PER_RUN)
    if not messages:
        return

    log.info(f"Found {len(messages)} unread message(s) to process.")
    for msg_meta in messages:
        try:
            process_one_message(service, msg_meta, clients, conn)
        except Exception as e:
            log.error(f"Error processing message {msg_meta['id']}: {e}")


def main():
    log.info("Authenticating with Gmail...")
    service = gmail_client.build_service()
    log.info("Authenticated.")

    conn = db.get_connection()
    log.info("Connected to Postgres. Starting poller.\n")

    while True:
        try:
            run_once(service, conn)
        except Exception as e:
            log.error(f"Poller error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
