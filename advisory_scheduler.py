"""Advisory follow-up scheduler (Asesoría Financiera Personal).

Runs as a standalone process (systemd: advisory-scheduler.service). Two jobs,
both in America/Costa_Rica time:

  * Daily 17:00  — weekly budget summaries: clients whose plan has
                   weekly_send_dow = today receive seguimiento_semanal_presupuesto.
  * Day 1, 09:00 — monthly emergency-fund follow-up over the CLOSED previous
                   month: seguimiento_fondo_emergencia when the month had a
                   surplus (ingresos > gastos), otherwise the
                   _noexcedente twin.

Eligibility (see PLAN_asesoria.md): plan enabled + module flag + tracking_start
reached (NULL = baseline month, nothing is sent) + program not ended + client
active with whatsapp_notification and a phone number.

Idempotency: every send INSERTs into core.advisory_message_log first
(ON CONFLICT DO NOTHING) and only sends if the row was inserted; the send and
the log row commit together, so a failed send rolls the row back and can retry
on the next run. A client failure never stops the batch; failures are reported
in one alert email via SMTP (neto), mirroring rate_scheduler.py.

Run with:  python advisory_scheduler.py
"""
import calendar
import logging
import os
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

import whatsapp_client
from db import get_connection
from tools.finance import get_income_expense_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("advisory_scheduler")

CR_TZ = pytz.timezone("America/Costa_Rica")

MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
            "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
MESES_ABBR = ["ene", "feb", "mar", "abr", "may", "jun", "jul",
              "ago", "sep", "oct", "nov", "dic"]


def _template_weekly() -> str:
    return os.environ.get("META_WA_TEMPLATE_ADVISORY_WEEKLY",
                          "seguimiento_semanal_presupuesto").strip()


def _template_fund() -> str:
    return os.environ.get("META_WA_TEMPLATE_ADVISORY_FUND",
                          "seguimiento_fondo_emergencia").strip()


def _template_fund_neg() -> str:
    return os.environ.get("META_WA_TEMPLATE_ADVISORY_FUND_NEG",
                          "seguimiento_fondo_emergencia_noexcedente").strip()


def _template_lang() -> str:
    return os.environ.get("META_WA_TEMPLATE_ADVISORY_LANG", "es").strip()


# ── Formatting (mirrors whatsapp_notifier conventions; Meta rejects empty
#    params, so None always becomes a visible dash) ───────────────────────────

def _fmt_crc(amount) -> str:
    if amount is None:
        return "—"
    return f"₡{float(amount):,.0f}"


def _fmt_pct(value, decimals=1) -> str:
    if value is None:
        return "—"
    s = f"{float(value):.{decimals}f}".rstrip("0").rstrip(".")
    return (s or "0").replace(".", ",")


def _cr_today():
    return datetime.now(CR_TZ).date()


def _week_label(today) -> str:
    """Header {{1}} of the weekly template: '22–28 jul' (last 7 days)."""
    start = today - timedelta(days=6)
    if start.month == today.month:
        return f"{start.day}–{today.day} {MESES_ABBR[today.month - 1]}"
    return (f"{start.day} {MESES_ABBR[start.month - 1]} – "
            f"{today.day} {MESES_ABBR[today.month - 1]}")


# ── Alert email (best-effort, never raises) ──────────────────────────────────

def _send_alert_email(subject: str, body: str) -> None:
    try:
        host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("SMTP_PORT", "587"))
        user = os.environ.get("SMTP_USER")
        password = os.environ.get("SMTP_PASSWORD")
        to = os.environ.get("ADVISORY_ALERT_EMAIL") or user
        if not user or not password:
            log.warning("Advisory alert email skipped: SMTP_USER/SMTP_PASSWORD not set")
            return
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"neto <{user}>"
        msg["To"] = to
        msg.set_content(body)
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        log.info(f"Advisory alert email sent to {to}")
    except Exception as e:
        log.error(f"Advisory alert email failed: {e}")


# ── Data helpers ─────────────────────────────────────────────────────────────

def _eligible_plans(cur, today, module_flag: str, weekly_dow=None):
    """Plans whose module is on, tracking has started and the client is
    reachable by WhatsApp. module_flag is one of the fixed column names
    (never user input)."""
    dow_sql = "AND ap.weekly_send_dow = %s" if weekly_dow is not None else ""
    params = [today, today]
    if weekly_dow is not None:
        params.append(weekly_dow)
    cur.execute(
        f"""
        SELECT c.id AS client_id, c.client_name, c.phone_number, c.business_id,
               ap.target_savings_rate, ap.emergency_fund_goal,
               ap.emergency_fund_current, ap.declared_monthly_income
        FROM core.client_advisory_plans ap
        JOIN core.clients c ON c.id = ap.client_id
        WHERE ap.enabled
          AND ap.{module_flag}
          AND ap.tracking_start IS NOT NULL AND ap.tracking_start <= %s
          AND (ap.program_end IS NULL OR ap.program_end >= %s)
          {dow_sql}
          AND c.active
          AND c.whatsapp_notification
          AND c.phone_number IS NOT NULL
        ORDER BY c.created_at
        """,
        params,
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _claim_send(cur, client_id, message_type: str, period_key: str) -> bool:
    """Insert the idempotency row; False when this period was already sent."""
    cur.execute(
        """
        INSERT INTO core.advisory_message_log (client_id, message_type, period_key)
        VALUES (%s, %s, %s)
        ON CONFLICT (client_id, message_type, period_key) DO NOTHING
        """,
        (str(client_id), message_type, period_key),
    )
    return bool(cur.rowcount)


def _reference_income(conn, plan, today) -> float | None:
    """Monthly income used as the base of the global spending budget:
    declared_monthly_income when configured, else the income of the previous
    CLOSED month (month-to-date income is useless early in the month — a
    salary deposited at month end would make the budget collapse to ~0)."""
    if plan["declared_monthly_income"] is not None:
        return float(plan["declared_monthly_income"])
    prev_end = today.replace(day=1) - timedelta(days=1)
    summary = get_income_expense_summary(
        conn, individual_id=str(plan["client_id"]),
        date_from=prev_end.replace(day=1), date_to=prev_end,
    )
    return summary["ingresos"] if summary["ingresos"] > 0 else None


# ── Job: weekly budget summary ───────────────────────────────────────────────

def run_weekly_summaries():
    today = _cr_today()
    iso_year, iso_week, _ = today.isocalendar()
    period_key = f"{iso_year}-W{iso_week:02d}"
    dow = (today.weekday() + 1) % 7  # plan convention: 0=domingo … 6=sábado
    log.info(f"Weekly summaries: starting ({today}, dow={dow}, period={period_key})")

    failures = []
    sent = 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            plans = _eligible_plans(cur, today, "weekly_summary_enabled", weekly_dow=dow)
        conn.rollback()  # plain SELECTs; keep the claim transaction clean
        log.info(f"Weekly summaries: {len(plans)} eligible client(s)")

        for plan in plans:
            cid = str(plan["client_id"])
            try:
                with conn.cursor() as cur:
                    if not _claim_send(cur, cid, "weekly_summary", period_key):
                        conn.rollback()
                        log.info(f"  {plan['client_name']}: already sent {period_key}, skipping")
                        continue

                    to = whatsapp_client.normalize_phone(plan["phone_number"] or "")
                    if not to:
                        raise ValueError(f"invalid phone_number {plan['phone_number']!r}")

                    summary = get_income_expense_summary(
                        conn, individual_id=cid,
                        date_from=today.replace(day=1), date_to=today,
                    )
                    ingresos = (float(plan["declared_monthly_income"])
                                if plan["declared_monthly_income"] is not None
                                else summary["ingresos"])
                    gastos = summary["gastos"]

                    # Global monthly budget = reference income × (1 − meta de
                    # tasa de ahorro). The target rate is set for every client
                    # at the advisory onboarding; without it (or without a
                    # reference income) the % falls back to the dash
                    # placeholder.
                    rate = plan["target_savings_rate"]
                    income_ref = _reference_income(conn, plan, today)
                    if rate is not None and income_ref:
                        budget = income_ref * (1 - float(rate) / 100)
                        pct = _fmt_pct(gastos / budget * 100, 0) if budget > 0 else "—"
                    else:
                        pct = "—"
                    dias_rest = calendar.monthrange(today.year, today.month)[1] - today.day

                    whatsapp_client.send_template(
                        to=to,
                        template_name=_template_weekly(),
                        language_code=_template_lang(),
                        header_params=[_week_label(today)],
                        body_params=[
                            _fmt_crc(ingresos),   # {{1}} ingresos del mes
                            _fmt_crc(gastos),     # {{2}} gasto acumulado
                            pct,                  # {{3}} % del presupuesto
                            str(dias_rest),       # {{4}} días restantes
                        ],
                        # Fase 5: the webhook resolves this payload to the
                        # spending breakdown inside the 24 h window.
                        button_payloads=[f"ad|cid={cid}"],
                    )
                conn.commit()
                sent += 1
                log.info(f"  WA weekly sent → {to} | {plan['client_name']}")
            except Exception as e:
                conn.rollback()
                failures.append(f"{plan['client_name']}: {e}")
                log.error(f"  Failed weekly for {plan['client_name']}: {e}")
    finally:
        conn.close()

    log.info(f"Weekly summaries complete: {sent} sent, {len(failures)} failed")
    if failures:
        _send_alert_email(
            f"⚠ Asesoría: {len(failures)} resumen(es) semanal(es) fallidos ({today})",
            "Fallaron estos envíos del resumen semanal de presupuesto "
            "(se reintentan en la próxima corrida del día configurado):\n\n"
            + "\n".join(f"  - {f}" for f in failures)
            + "\n\nRevisar: sudo journalctl -u advisory-scheduler",
        )


# ── Job: monthly emergency-fund follow-up ────────────────────────────────────

def run_monthly_fund():
    today = _cr_today()
    month_end = today.replace(day=1) - timedelta(days=1)   # closed previous month
    month_start = month_end.replace(day=1)
    period_key = f"{month_start:%Y-%m}"
    month_name = MESES_ES[month_start.month - 1]
    log.info(f"Monthly fund follow-up: starting (reporting {period_key})")

    failures = []
    sent = 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            plans = _eligible_plans(cur, today, "fund_tracking_enabled")
        conn.rollback()
        log.info(f"Monthly fund: {len(plans)} eligible client(s)")

        for plan in plans:
            cid = str(plan["client_id"])
            try:
                with conn.cursor() as cur:
                    if not _claim_send(cur, cid, "monthly_fund", period_key):
                        conn.rollback()
                        log.info(f"  {plan['client_name']}: already sent {period_key}, skipping")
                        continue

                    to = whatsapp_client.normalize_phone(plan["phone_number"] or "")
                    if not to:
                        raise ValueError(f"invalid phone_number {plan['phone_number']!r}")

                    summary = get_income_expense_summary(
                        conn, individual_id=cid,
                        date_from=month_start, date_to=month_end,
                    )
                    ingresos = (float(plan["declared_monthly_income"])
                                if plan["declared_monthly_income"] is not None
                                else summary["ingresos"])
                    gastos = summary["gastos"]
                    excedente = ingresos - gastos
                    tasa = (excedente / ingresos * 100) if ingresos > 0 else None

                    common = [
                        _fmt_crc(ingresos),                        # {{1}} ingresos
                        _fmt_crc(gastos),                          # {{2}} gastos
                        _fmt_pct(tasa),                            # {{3}} tasa de ahorro
                        _fmt_pct(plan["target_savings_rate"]),     # {{4}} meta tasa
                        _fmt_crc(plan["emergency_fund_current"]),  # {{5}} fondo actual
                        _fmt_crc(plan["emergency_fund_goal"]),     # {{6}} meta fondo
                    ]
                    if excedente > 0:
                        template = _template_fund()
                        body_params = common + [_fmt_crc(excedente)]  # {{7}} disponible
                    else:
                        template = _template_fund_neg()
                        body_params = common

                    whatsapp_client.send_template(
                        to=to,
                        template_name=template,
                        language_code=_template_lang(),
                        header_params=[month_name],
                        body_params=body_params,
                    )
                conn.commit()
                sent += 1
                log.info(f"  WA monthly sent → {to} | {plan['client_name']} | template={template}")
            except Exception as e:
                conn.rollback()
                failures.append(f"{plan['client_name']}: {e}")
                log.error(f"  Failed monthly for {plan['client_name']}: {e}")
    finally:
        conn.close()

    log.info(f"Monthly fund complete: {sent} sent, {len(failures)} failed")
    if failures:
        _send_alert_email(
            f"⚠ Asesoría: {len(failures)} seguimiento(s) de fondo fallidos ({period_key})",
            "Fallaron estos envíos del seguimiento mensual del fondo de emergencia "
            "(se reintentan si el job vuelve a correr dentro del mes):\n\n"
            + "\n".join(f"  - {f}" for f in failures)
            + "\n\nRevisar: sudo journalctl -u advisory-scheduler",
        )


def main():
    scheduler = BlockingScheduler(timezone=CR_TZ)
    scheduler.add_job(
        run_weekly_summaries,
        "cron",
        hour=17,
        minute=0,
        id="advisory_weekly_summaries",
        replace_existing=True,
    )
    scheduler.add_job(
        run_monthly_fund,
        "cron",
        day=1,
        hour=9,
        minute=0,
        id="advisory_monthly_fund",
        replace_existing=True,
    )
    log.info("Advisory scheduler started; weekly daily 17:00 CR, monthly day 1 09:00 CR")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    main()
