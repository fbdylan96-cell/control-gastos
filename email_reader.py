import logging
import time

from dotenv import load_dotenv

import classifier
import db
import enricher
import gmail_client
import notifier
import parser as email_parser
import whatsapp_notifier

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


def _nz(value):
    """Normalize blanks to None so `a or b` fills only genuinely empty fields."""
    if isinstance(value, str):
        value = value.strip()
    return value or None


def _handle_possible_duplicate(conn, enriched_row):
    """If the just-inserted enriched row duplicates a recently-notified transaction,
    mark it 'Duplicado', backfill the original's blank fields, and return True
    (so the caller skips classification/notification for this row).

    Only runs for approved, processed rows. A duplicate is an earlier row for the same
    individual with the same amount (and currency, when both have one) within 2 minutes.
    """
    if enriched_row.get("transaction_approval") != "Aprobada":
        return False
    if enriched_row.get("transaction_status") not in ("Procesado", "Procesado parcialmente"):
        return False

    original = db.find_recent_duplicate_original(
        conn,
        enriched_row["individual_id"],
        enriched_row.get("amount_guess"),
        _nz(enriched_row.get("currency_guess")),
        enriched_row["id"],
    )
    if not original:
        return False

    # Fill the original's blanks from the new row; never overwrite existing values.
    merged_merchant = _nz(original.get("merchant_guess")) or _nz(enriched_row.get("merchant_guess"))
    merged_currency = _nz(original.get("currency_guess")) or _nz(enriched_row.get("currency_guess"))
    merged_desc     = _nz(original.get("desc_guess")) or _nz(enriched_row.get("desc_guess"))
    orig_amount = original.get("amount_guess")

    # Currency may have just been filled in — recompute FX for the original.
    amount_local, fx_rate, fx_rate_date = enricher._compute_fx(conn, orig_amount, merged_currency)

    complete = all([merged_merchant, orig_amount is not None, merged_currency, merged_desc])
    new_status = "Procesado" if complete else original.get("transaction_status")

    db.backfill_enriched_original(
        conn, original["id"], merged_merchant, merged_currency, merged_desc,
        amount_local, fx_rate, fx_rate_date, new_status,
    )
    db.mark_enriched_duplicate(conn, enriched_row["id"])

    log.info(
        f"  Duplicate of original={original['id']} — marked Duplicado; "
        f"original backfilled (status={new_status})"
    )
    return True


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

    if enriched_row["transaction_status"] == "Descartado":
        log.info("  Status=Descartado — pipeline stopped, no classification or notification")
        gmail_client.mark_as_read(service, msg_id)
        return

    if _handle_possible_duplicate(conn, enriched_row):
        gmail_client.mark_as_read(service, msg_id)
        return

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

    notifier.run_notifications(conn, service)
    whatsapp_notifier.run_whatsapp_notifications(conn)


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
