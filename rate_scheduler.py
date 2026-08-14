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

import psycopg2.extras
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

import credit_products_update
from db import get_connection
from exchange_rate_update import CRITICAL_CURRENCIES, get_latest_rates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rate_scheduler")


def _send_alert_email(subject: str, body: str, to_env: str = "FX_ALERT_EMAIL") -> None:
    """Best-effort alert via SMTP (neto). Never raises: a mail failure must not
    kill the rate update."""
    try:
        host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("SMTP_PORT", "587"))
        user = os.environ.get("SMTP_USER")
        password = os.environ.get("SMTP_PASSWORD")
        to = os.environ.get(to_env) or user
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


def _alerta_productos(subject: str, body: str) -> None:
    _send_alert_email(subject, body + "\n\nRevisar: sudo journalctl -u rate-scheduler",
                      to_env="BCCR_PRODUCTS_ALERT_EMAIL")


def run_credit_products_update():
    """Recarga core.credit_products desde el dashboard BCCR/MEIC.

    La tabla guarda SOLO el último período: cada corrida exitosa reemplaza el
    contenido entero. Por eso el orden importa — se descarga y valida TODO en
    memoria antes de tocar la tabla, y el DELETE + INSERT van en una sola
    transacción. Si algo falla no hay período anterior al que volver, porque no
    guardamos historia.
    """
    log.info("Productos crediticios: starting")
    try:
        rows, descargadas, paginas = credit_products_update.get_credit_products()
    except Exception as e:
        log.error(f"Productos crediticios: fallo descargando del BCCR: {e}")
        _alerta_productos(
            "⚠ Productos crediticios: no se pudo descargar del BCCR",
            "El job quincenal no pudo traer el comparador de productos "
            f"crediticios.\n\nError: {e}\n\n"
            "La tabla conserva la última extracción buena; el dato queda viejo, "
            "no perdido.",
        )
        return

    log.info(f"Productos crediticios: {descargadas} filas descargadas "
             f"({paginas} página(s)), {len(rows)} tras filtrar por producto")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM core.credit_products")
            previas = cur.fetchone()[0]

            # Guarda contra carga parcial: una caída fuerte contra la corrida
            # anterior es la fuente rota, no un mes flojo. Se aborta ANTES del
            # DELETE para no reemplazar datos buenos por datos a medias.
            if previas and len(rows) < previas * 0.8:
                conn.rollback()
                log.error(f"Productos crediticios: ABORTADO, {len(rows)} filas "
                          f"contra {previas} de la corrida anterior")
                _alerta_productos(
                    "⚠ Productos crediticios: descarga sospechosamente corta, no se cargó",
                    f"La descarga trajo {len(rows)} filas y la corrida anterior "
                    f"tenía {previas} (caída de más del 20%).\n\n"
                    "No se tocó la tabla: conserva la extracción anterior.",
                )
                return

            cur.execute("DELETE FROM core.credit_products")
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO core.credit_products (" +
                ", ".join(credit_products_update.DB_COLUMNS) +
                ", row_hash) VALUES %s ON CONFLICT (period, row_hash) DO NOTHING",
                [tuple(r) + (credit_products_update.row_hash(r),) for r in rows],
                page_size=500,
            )
            # Contar con un SELECT, no con cur.rowcount: execute_values manda
            # varios lotes y rowcount solo refleja el último.
            cur.execute("SELECT count(*) FROM core.credit_products")
            insertadas = cur.fetchone()[0]
        conn.commit()
        # La fuente trae filas exactamente duplicadas (verificado: un mismo
        # cargo listado dos veces para el mismo producto). El ON CONFLICT las
        # colapsa, que es lo correcto — no aportan información — pero se deja
        # constancia para que la diferencia no parezca un bug de carga.
        if insertadas != len(rows):
            log.info(f"Productos crediticios: {len(rows) - insertadas} fila(s) "
                     f"duplicada(s) en la fuente, colapsadas por el ON CONFLICT")
    except Exception as e:
        conn.rollback()
        log.error(f"Productos crediticios: error de BD: {e}")
        _alerta_productos(
            "⚠ Productos crediticios: error de base al cargar",
            f"La descarga funcionó pero la carga falló.\n\nError: {e}\n\n"
            "Se hizo rollback: la tabla conserva la extracción anterior.",
        )
        return
    finally:
        conn.close()

    log.info(f"Productos crediticios: {insertadas} filas cargadas "
             f"(reemplazaron {previas})")


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
    # Quincenal (días 8 y 22): el dato del BCCR es de corte mensual y se publica
    # alrededor del día 5. La segunda corrida acorta la ventana en que la tabla
    # muestra el mes anterior si el BCCR publica tarde. misfire_grace_time hace
    # las veces del Persistent=true de un systemd timer: un reinicio corto no
    # pierde la corrida.
    scheduler.add_job(
        run_credit_products_update,
        "cron",
        day="8,22",
        hour=6,
        minute=0,
        id="credit_products_update",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
    )
    log.info("FX rate scheduler started; next run: Mon-Fri 23:30 server time")
    log.info("Credit products job scheduled: days 8 and 22 at 06:00")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    main()
