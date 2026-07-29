"""Budget threshold alerts for advisory clients (Asesoría, Fase 4).

Called from email_reader.run_once right after the transaction notifications:
whenever new transactions were classified, every advisory-eligible client's
budgeted categories are re-evaluated against their month-to-date spending.
Crossing 80% sends alerta_presupuesto; crossing 100% sends
alerta_presupuesto_superado. See PLAN_asesoria.md.

Eligibility mirrors advisory_scheduler: plan enabled + budget_alerts_enabled +
tracking_start reached (NULL = baseline month) + program not ended + client
active with whatsapp_notification and phone.

Budget/spending semantics mirror db.get_budget_and_spending: individuals use
their personal category budgets (individual_id = client) and their own
spending; business members use business-level budgets and business-wide
spending. Only 'Aprobada' + 'debito' rows count, in CRC (amount_local).

Idempotency: one row per (client, alert type, month:category) in
core.advisory_message_log — max two alerts per category per month (one 80%,
one 100%). Sending the 100% alert also claims the 80% row so a client who
jumps straight past 100% never gets the (now stale) 80% alert afterwards.
Claim + send + commit share one transaction; a failed send rolls the claim
back and retries on the next pipeline cycle.

This module must never break the pipeline: run_budget_alerts catches all
errors internally.
"""
import calendar
import logging
import os
from datetime import datetime

import pytz

import whatsapp_client

log = logging.getLogger(__name__)

INDIVIDUAL_BIZ_ID = "00000000-0000-0000-0000-000000009999"
CR_TZ = pytz.timezone("America/Costa_Rica")


def _template_alert80() -> str:
    return os.environ.get("META_WA_TEMPLATE_ADVISORY_ALERT80",
                          "alerta_presupuesto").strip()


def _template_alert100() -> str:
    return os.environ.get("META_WA_TEMPLATE_ADVISORY_ALERT100",
                          "alerta_presupuesto_superado").strip()


def _template_lang() -> str:
    return os.environ.get("META_WA_TEMPLATE_ADVISORY_LANG", "es").strip()


def _fmt_crc(amount) -> str:
    if amount is None:
        return "—"
    return f"₡{float(amount):,.0f}"


def _category_label(category, subcategory) -> str:
    return f"{category} / {subcategory}" if subcategory else category


def _eligible_plans(cur, today):
    cur.execute(
        """
        SELECT c.id AS client_id, c.client_name, c.phone_number, c.business_id
        FROM core.client_advisory_plans ap
        JOIN core.clients c ON c.id = ap.client_id
        WHERE ap.enabled
          AND ap.budget_alerts_enabled
          AND ap.tracking_start IS NOT NULL AND ap.tracking_start <= %s
          AND (ap.program_end IS NULL OR ap.program_end >= %s)
          AND c.active
          AND c.whatsapp_notification
          AND c.phone_number IS NOT NULL
        ORDER BY c.created_at
        """,
        (today, today),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _budgeted_categories(cur, business_id, client_id):
    """(category, subcategory, monthly_budget) rows for the client's scope."""
    if str(business_id) == INDIVIDUAL_BIZ_ID:
        cur.execute(
            """
            SELECT category, subcategory, monthly_budget
            FROM core.categories
            WHERE business_id = %s AND individual_id = %s
              AND monthly_budget IS NOT NULL
            """,
            (INDIVIDUAL_BIZ_ID, str(client_id)),
        )
    else:
        cur.execute(
            """
            SELECT category, subcategory, monthly_budget
            FROM core.categories
            WHERE business_id = %s AND individual_id IS NULL
              AND monthly_budget IS NOT NULL
            """,
            (str(business_id),),
        )
    return cur.fetchall()


def _month_spending(cur, business_id, client_id, category, subcategory) -> float:
    """Month-to-date CRC spending for the category, scoped like
    db.get_budget_and_spending (own rows for individuals, business-wide for
    business members)."""
    is_individual = str(business_id) == INDIVIDUAL_BIZ_ID
    scope_sql = "e.individual_id = %s" if is_individual else "e.business_id = %s"
    scope_val = str(client_id) if is_individual else str(business_id)
    cur.execute(
        f"""
        SELECT COALESCE(SUM(e.amount_local), 0)
        FROM core.transactions_enriched e
        JOIN core.transactions_raw r ON r.id = e.raw_id
        JOIN core.transactions_classified cl ON cl.raw_id = e.raw_id
        JOIN core.transactions_notifications n ON n.classified_id = cl.id
        WHERE {scope_sql}
          AND n.final_category = %s
          AND (n.final_subcategory = %s OR (n.final_subcategory IS NULL AND %s IS NULL))
          AND date_trunc('month', r.local_date) = date_trunc('month', now())
          AND e.transaction_approval = 'Aprobada'
          AND e.transaction_type_guess = 'debito'
        """,
        (scope_val, category, subcategory, subcategory),
    )
    return float(cur.fetchone()[0])


def _claim(cur, client_id, message_type, period_key) -> bool:
    cur.execute(
        """
        INSERT INTO core.advisory_message_log (client_id, message_type, period_key)
        VALUES (%s, %s, %s)
        ON CONFLICT (client_id, message_type, period_key) DO NOTHING
        """,
        (str(client_id), message_type, period_key),
    )
    return bool(cur.rowcount)


def run_budget_alerts(conn) -> int:
    """Evaluate 80%/100% thresholds for advisory clients; returns alerts sent.

    Never raises: any error is logged and the pipeline continues.
    """
    sent = 0
    try:
        today = datetime.now(CR_TZ).date()
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        dias_rest = days_in_month - today.day

        with conn.cursor() as cur:
            plans = _eligible_plans(cur, today)
        conn.rollback()  # plain SELECT; keep claim transactions clean
        if not plans:
            return 0

        for plan in plans:
            cid = str(plan["client_id"])
            to = whatsapp_client.normalize_phone(plan["phone_number"] or "")
            if not to:
                log.warning(f"Advisory alerts: invalid phone for {plan['client_name']}, skipping")
                continue

            try:
                with conn.cursor() as cur:
                    categories = _budgeted_categories(cur, plan["business_id"], cid)
                conn.rollback()
            except Exception as e:
                conn.rollback()
                log.error(f"Advisory alerts: category lookup failed for {plan['client_name']}: {e}")
                continue

            for category, subcategory, monthly_budget in categories:
                label = _category_label(category, subcategory)
                period_key = f"{today:%Y-%m}:{category}|{subcategory or ''}"
                try:
                    with conn.cursor() as cur:
                        spent = _month_spending(cur, plan["business_id"], cid,
                                                category, subcategory)
                        budget = float(monthly_budget)
                        if budget <= 0:
                            conn.rollback()
                            continue
                        pct = spent / budget * 100

                        if pct >= 100:
                            if not _claim(cur, cid, "budget_100", period_key):
                                conn.rollback()
                                continue
                            # A fresh 80% alert after this one would be stale
                            # noise — claim it too (no-op if already sent).
                            _claim(cur, cid, "budget_80", period_key)
                            whatsapp_client.send_template(
                                to=to,
                                template_name=_template_alert100(),
                                language_code=_template_lang(),
                                header_params=[label],
                                body_params=[
                                    _fmt_crc(spent),    # {{1}} gasto actual
                                    label,              # {{2}} categoría
                                    str(dias_rest),     # {{3}} días restantes
                                ],
                            )
                        elif pct >= 80:
                            if not _claim(cur, cid, "budget_80", period_key):
                                conn.rollback()
                                continue
                            # Run-rate projection: spending pace so far applied
                            # to the whole month.
                            proyeccion = (spent / max(today.day, 1)) * days_in_month
                            whatsapp_client.send_template(
                                to=to,
                                template_name=_template_alert80(),
                                language_code=_template_lang(),
                                header_params=[label],
                                body_params=[
                                    label,                  # {{1}} categoría
                                    _fmt_crc(spent),        # {{2}} gasto acumulado
                                    f"{pct:.0f}",           # {{3}} % del presupuesto
                                    _fmt_crc(budget),       # {{4}} presupuesto
                                    str(dias_rest),         # {{5}} días restantes
                                    _fmt_crc(proyeccion),   # {{6}} proyección cierre
                                ],
                            )
                        else:
                            conn.rollback()
                            continue
                    conn.commit()
                    sent += 1
                    tipo = "100%" if pct >= 100 else "80%"
                    log.info(f"Advisory alert {tipo} sent → {to} | "
                             f"{plan['client_name']} | {label} | {pct:.0f}%")
                except Exception as e:
                    conn.rollback()
                    log.error(f"Advisory alert failed for {plan['client_name']} / {label}: {e}")
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.error(f"Advisory alerts pass failed: {e}")
    return sent
