import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def get_connection():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def get_active_clients(conn):
    """Return all active clients as a list of dicts."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, business_id, email_forward, business_admin, client_name
            FROM core.clients
            WHERE active = TRUE
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_business_members(conn, business_id, exclude_id):
    """Return all active members of a business except the given client (the admin)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, client_name
            FROM core.clients
            WHERE business_id = %s
              AND active = TRUE
              AND id != %s
            """,
            (str(business_id), str(exclude_id)),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def message_exists(conn, individual_id, message_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM core.transactions_raw
            WHERE individual_id = %s AND message_id = %s
            LIMIT 1
            """,
            (str(individual_id), message_id),
        )
        return cur.fetchone() is not None


def get_unenriched_raws(conn):
    """Return transactions_raw rows that have no corresponding enriched row yet."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.individual_id, r.business_id, r.subject,
                   r.from_email, r.body_text_full, r.body_condensed
            FROM core.transactions_raw r
            WHERE NOT EXISTS (
                SELECT 1 FROM core.transactions_enriched e WHERE e.raw_id = r.id
            )
            ORDER BY r.created_at ASC
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def insert_enriched_transaction(conn, row):
    """Insert one row into core.transactions_enriched. Skips if raw_id already exists."""
    sql = """
        INSERT INTO core.transactions_enriched (
            id,
            raw_id,
            individual_id,
            business_id,
            bank,
            merchant_guess,
            amount_guess,
            currency_guess,
            desc_guess,
            transaction_type_guess,
            transaction_approval,
            transaction_status,
            ai_assistance,
            member_detected,
            assigned_individual_id,
            errors
        ) VALUES (
            %(id)s,
            %(raw_id)s,
            %(individual_id)s,
            %(business_id)s,
            %(bank)s,
            %(merchant_guess)s,
            %(amount_guess)s,
            %(currency_guess)s,
            %(desc_guess)s,
            %(transaction_type_guess)s,
            %(transaction_approval)s,
            %(transaction_status)s,
            %(ai_assistance)s,
            %(member_detected)s,
            %(assigned_individual_id)s,
            %(errors)s
        )
        ON CONFLICT DO NOTHING
    """
    with conn.cursor() as cur:
        cur.execute(sql, row)
    conn.commit()


def get_unclassified_enriched(conn):
    """Return approved, non-discarded enriched rows that have no classified row yet."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.raw_id, e.individual_id, e.business_id,
                   e.merchant_guess, e.amount_guess, e.currency_guess,
                   e.desc_guess, e.transaction_type_guess
            FROM core.transactions_enriched e
            WHERE NOT EXISTS (
                SELECT 1 FROM core.transactions_classified c WHERE c.raw_id = e.raw_id
            )
            AND e.transaction_approval = 'Aprobada'
            AND e.transaction_status != 'Descartado'
            AND e.amount_guess IS NOT NULL
            ORDER BY e.created_at ASC
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_categories(conn, business_id, individual_id):
    """Return all categories available for this business/individual as list of dicts."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT category, subcategory
            FROM core.categories
            WHERE business_id = %s
              AND (individual_id = %s OR individual_id IS NULL)
            ORDER BY category, subcategory NULLS LAST
            """,
            (str(business_id), str(individual_id)),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def find_category_rule(conn, business_id, individual_id, merchant_key):
    """
    Return the best matching rule for a merchant_key, or None.
    Individual-level rules take precedence over business-level rules.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT category, subcategory, source, confidence
            FROM core.category_rules
            WHERE business_id = %s
              AND merchant_key = %s
              AND (individual_id = %s OR individual_id IS NULL)
            ORDER BY individual_id NULLS LAST
            LIMIT 1
            """,
            (str(business_id), merchant_key, str(individual_id)),
        )
        row = cur.fetchone()
        if row:
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        return None


def insert_classified_transaction(conn, row):
    """Insert one row into core.transactions_classified. Skips if raw_id already exists."""
    sql = """
        INSERT INTO core.transactions_classified (
            id, raw_id, individual_id, business_id,
            merchant, category, subcategory, classified_by
        ) VALUES (
            %(id)s, %(raw_id)s, %(individual_id)s, %(business_id)s,
            %(merchant)s, %(category)s, %(subcategory)s, %(classified_by)s
        )
        ON CONFLICT DO NOTHING
    """
    with conn.cursor() as cur:
        cur.execute(sql, row)
    conn.commit()


def insert_notification_row(conn, row):
    """Insert one row into core.transactions_notifications."""
    sql = """
        INSERT INTO core.transactions_notifications (
            id, classified_id, individual_id, business_id,
            final_category, final_subcategory
        ) VALUES (
            %(id)s, %(classified_id)s, %(individual_id)s, %(business_id)s,
            %(final_category)s, %(final_subcategory)s
        )
        ON CONFLICT DO NOTHING
    """
    with conn.cursor() as cur:
        cur.execute(sql, row)
    conn.commit()


def get_pending_email_notifications(conn):
    """
    Return notification rows that need an email sent.
    Joins enriched + classified + notifications + clients.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                n.id                AS notification_id,
                n.classified_id,
                n.individual_id,
                n.business_id,
                n.final_category,
                n.final_subcategory,
                c.client_name,
                c.email_address,
                cl.merchant,
                e.amount_guess,
                e.currency_guess,
                e.desc_guess,
                r.local_date
            FROM core.transactions_notifications n
            JOIN core.clients              c  ON c.id  = n.individual_id
            JOIN core.transactions_classified cl ON cl.id = n.classified_id
            JOIN core.transactions_enriched   e  ON e.raw_id = cl.raw_id
            JOIN core.transactions_raw        r  ON r.id = cl.raw_id
            WHERE n.email_notified = FALSE
              AND c.email_notification = TRUE
              AND c.active = TRUE
            ORDER BY n.created_at ASC
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def mark_email_sent(conn, notification_id):
    """Mark a notification as emailed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.transactions_notifications
            SET email_notified = TRUE, email_notified_at = now()
            WHERE id = %s
            """,
            (str(notification_id),),
        )
    conn.commit()


def update_reclassification(conn, notification_id, category, subcategory):
    """Record a user reclassification received via email click."""
    action_value = f"{category} / {subcategory}" if subcategory else category
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.transactions_notifications
            SET final_category    = %s,
                final_subcategory = %s,
                reclassified_by   = 'user',
                reclassified_at   = now(),
                email_action_at   = now(),
                email_action_value = %s
            WHERE id = %s
            """,
            (category, subcategory, action_value, str(notification_id)),
        )
    conn.commit()


def insert_raw_transaction(conn, row):
    """
    Insert one row into core.transactions_raw.
    ON CONFLICT (individual_id, message_id) DO NOTHING handles duplicates.
    """
    sql = """
        INSERT INTO core.transactions_raw (
            id,
            individual_id,
            business_id,
            message_id,
            thread_id,
            from_email,
            to_email,
            subject,
            body_text_full,
            body_condensed,
            ms_date,
            local_date,
            label_source,
            month,
            year,
            year_month
        ) VALUES (
            %(id)s,
            %(individual_id)s,
            %(business_id)s,
            %(message_id)s,
            %(thread_id)s,
            %(from_email)s,
            %(to_email)s,
            %(subject)s,
            %(body_text_full)s,
            %(body_condensed)s,
            %(ms_date)s,
            %(local_date)s,
            %(label_source)s,
            %(month)s,
            %(year)s,
            %(year_month)s
        )
        ON CONFLICT (individual_id, message_id) DO NOTHING
    """
    with conn.cursor() as cur:
        cur.execute(sql, row)
    conn.commit()
