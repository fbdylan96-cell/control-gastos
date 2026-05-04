import logging
import uuid

from apscheduler.schedulers.background import BackgroundScheduler

from db import (get_connection, get_notifications_past_window,
                mark_rule_processed, upsert_category_rule)
from utils import clean_merchant_key

log = logging.getLogger(__name__)


def run_rule_ingestion():
    log.info("Rule ingestion: starting")
    conn = get_connection()
    try:
        rows = get_notifications_past_window(conn)
        log.info(f"Rule ingestion: {len(rows)} notification(s) to process")
        ingested = 0

        for row in rows:
            nid = str(row["notification_id"])
            try:
                merchant_raw = row["merchant"] or row["merchant_guess"] or ""
                merchant_key = clean_merchant_key(merchant_raw)
                if not merchant_key:
                    log.warning(f"  notification_id={nid} — empty merchant key, skipping")
                    mark_rule_processed(conn, nid)
                    continue

                if row["reclassified_by"] == "user":
                    source, confidence = "user", 1.0
                elif row["classified_by"] == "openai":
                    source, confidence = "ai", 0.85
                else:
                    source, confidence = "system", 1.0

                rule = {
                    "id": str(uuid.uuid4()),
                    "business_id": str(row["business_id"]),
                    "individual_id": str(row["individual_id"]),
                    "merchant_key": merchant_key,
                    "merchant_raw": merchant_raw,
                    "category": row["final_category"],
                    "subcategory": row["final_subcategory"],
                    "source": source,
                    "confidence": confidence,
                }
                upsert_category_rule(conn, rule)
                mark_rule_processed(conn, nid)
                log.info(f"  {merchant_key} → {row['final_category']} [{source}]")
                ingested += 1

            except Exception as e:
                log.error(f"  Failed notification_id={nid}: {e}")

        log.info(f"Rule ingestion: {ingested}/{len(rows)} rules upserted")
    except Exception as e:
        log.error(f"Rule ingestion error: {e}")
    finally:
        conn.close()


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    # Runs at 00:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00 every day
    scheduler.add_job(
        run_rule_ingestion,
        "cron",
        hour="0,6,9,12,15,18,21",
        minute=0,
        id="rule_ingestion",
        replace_existing=True,
    )
    return scheduler
