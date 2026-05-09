import os

import psycopg2
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())


def get_connection():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def get_notifications_past_window(conn):
    """Return notifications where the 24hr window is closed and rules not yet ingested."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                n.id            AS notification_id,
                n.individual_id,
                n.business_id,
                n.final_category,
                n.final_subcategory,
                n.reclassified_by,
                cl.merchant,
                cl.classified_by,
                e.merchant_guess
            FROM core.transactions_notifications n
            JOIN core.transactions_classified cl ON cl.id  = n.classified_id
            JOIN core.transactions_enriched   e  ON e.raw_id = cl.raw_id
            WHERE n.email_notified     = TRUE
              AND n.email_notified_at  < NOW() - INTERVAL '24 hours'
              AND n.rule_processing    = FALSE
              AND n.final_category     IS NOT NULL
            ORDER BY n.email_notified_at ASC
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def upsert_category_rule(conn, row):
    """Upsert one row into core.category_rules. Updates category/subcategory on conflict."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.category_rules (
                id, business_id, individual_id,
                merchant_key, merchant_raw,
                category, subcategory,
                source, confidence
            ) VALUES (
                %(id)s, %(business_id)s, %(individual_id)s,
                %(merchant_key)s, %(merchant_raw)s,
                %(category)s, %(subcategory)s,
                %(source)s, %(confidence)s
            )
            ON CONFLICT ON CONSTRAINT uq_rule DO UPDATE
                SET category     = EXCLUDED.category,
                    subcategory  = EXCLUDED.subcategory,
                    merchant_raw = EXCLUDED.merchant_raw,
                    source       = EXCLUDED.source,
                    confidence   = EXCLUDED.confidence,
                    updated_at   = now()
            """,
            row,
        )
    conn.commit()


def mark_rule_processed(conn, notification_id: str):
    """Mark a notification as ingested into category_rules."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.transactions_notifications SET rule_processing = TRUE WHERE id = %s",
            (notification_id,),
        )
    conn.commit()


def update_reclassification(conn, notification_id: str, category: str, subcategory: str):
    action_value = f"{category} / {subcategory}" if subcategory else category
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.transactions_notifications
            SET final_category     = %s,
                final_subcategory  = %s,
                reclassified_by    = 'user',
                reclassified_at    = now(),
                email_action_at    = now(),
                email_action_value = %s
            WHERE id = %s
            """,
            (category, subcategory or None, action_value, notification_id),
        )
    conn.commit()
