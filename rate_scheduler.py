"""Daily FX rate updater.

Runs as a standalone process. Schedules a job for Mon-Fri at 23:30 (server local
time) that fetches the latest non-null BCCR rate per currency and upserts a row
into core.exchange_rates keyed on (today, currency).

Currencies whose BCCR value is missing are SKIPPED (never upserted as NULL) —
get_fx_conversion falls back to the previous date with valid rates. Any fetch
failure or skipped currency triggers an alert email via SMTP (neto).

Run with:  python rate_scheduler.py
"""
import logging
import os
import smtplib
from datetime import date
from email.message import EmailMessage

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

from db import get_connection
from exchange_rate_update import CRITICAL_CURRENCIES, get_latest_rates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rate_scheduler")


def _send_alert_email(subject: str, body: str) -> None:
    """Best-effort alert via SMTP (neto). Never raises: a mail failure must not
    kill the rate update."""
    try:
        host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("SMTP_PORT", "587"))
        user = os.environ.get("SMTP_USER")
        password = os.environ.get("SMTP_PASSWORD")
        to = os.environ.get("FX_ALERT_EMAIL") or user
        if not user or not password:
            log.warning("FX alert email skipped: SMTP_USER/SMTP_PASSWORD not set")
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
        log.info(f"FX alert email sent to {to}")
    except Exception as e:
        log.error(f"FX alert email failed: {e}")


def run_rate_update():
    log.info("FX rate update: starting")
    today = date.today()
    try:
        rates = get_latest_rates()
    except Exception as e:
        log.error(f"FX rate update: failed to fetch rates from BCCR: {e}")
        _send_alert_email(
            f"⚠ Tipos de cambio: fallo total del BCCR ({today})",
            "El job diario de tipos de cambio no pudo descargar datos del BCCR.\n\n"
            f"Error: {e}\n\n"
            "No se insertó ninguna tasa hoy; las conversiones usarán la última "
            "fecha con tasas válidas. Revisar: sudo journalctl -u rate-scheduler",
        )
        return

    # Never upsert NULL rates: a NULL 'latest' set breaks every conversion
    # (incidente 2026-07-09/10). Missing currencies simply keep their previous date.
    missing = sorted(c for c, r in rates.items() if r is None)
    valid = {c: r for c, r in rates.items() if r is not None}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for currency, rate in valid.items():
                cur.execute(
                    """
                    INSERT INTO core.exchange_rates (rate_date, currency, rate_vs_usd, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (rate_date, currency)
                    DO UPDATE SET rate_vs_usd = EXCLUDED.rate_vs_usd,
                                  updated_at  = now()
                    """,
                    (today, currency, rate),
                )
        conn.commit()
        log.info(
            f"FX rate update: upserted {len(valid)} rows for {today} "
            f"({len(missing)} sin dato, omitidas: {', '.join(missing) or '-'})"
        )
    except Exception as e:
        log.error(f"FX rate update: DB error: {e}")
        conn.rollback()
        _send_alert_email(
            f"⚠ Tipos de cambio: error de BD al guardar tasas ({today})",
            f"El job descargó las tasas pero falló al guardarlas.\n\nError: {e}",
        )
        return
    finally:
        conn.close()

    if missing:
        # Solo alertar por correo cuando falta una moneda CRÍTICA (CRC/EUR):
        # el servicio legacy del BCCR está caído desde 2026-07-20 y las otras
        # 41 monedas faltan TODOS los días — alertar por ellas es puro ruido.
        # La lista completa de faltantes queda en el log de arriba.
        critical_missing = sorted(c for c in missing if c in CRITICAL_CURRENCIES)
        if critical_missing:
            _send_alert_email(
                f"⚠ Tipos de cambio: monedas CRÍTICAS sin dato ({today}): "
                f"{', '.join(critical_missing)}",
                "Ni el BCCR ni el fallback de Hacienda devolvieron tasa para "
                "estas monedas críticas (NO se insertaron como NULL; las "
                "conversiones usan la última fecha con datos válidos):\n\n"
                f"Críticas faltantes: {', '.join(critical_missing)}\n"
                f"Faltantes totales: {', '.join(missing)}\n\n"
                f"Se insertaron correctamente {len(valid)} de {len(rates)} monedas.\n"
                "Revisar: sudo journalctl -u rate-scheduler",
            )


def main():
    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_rate_update,
        "cron",
        day_of_week="mon-fri",
        hour=23,
        minute=30,
        id="fx_rate_update",
        replace_existing=True,
    )
    log.info("FX rate scheduler started; next run: Mon-Fri 23:30 server time")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    main()
