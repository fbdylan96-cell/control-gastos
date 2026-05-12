"""Daily FX rate updater.

Runs as a standalone process. Schedules a job for Mon-Fri at 23:30 (server local
time) that fetches the latest non-null BCCR rate per currency and upserts a row
into core.exchange_rates keyed on (today, currency).

Run with:  python rate_scheduler.py
"""
import logging
from datetime import date

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

from db import get_connection
from exchange_rate_update import get_latest_rates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rate_scheduler")


def run_rate_update():
    log.info("FX rate update: starting")
    try:
        rates = get_latest_rates()
    except Exception as e:
        log.error(f"FX rate update: failed to fetch rates from BCCR: {e}")
        return

    today = date.today()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for currency, rate in rates.items():
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
        null_count = sum(1 for r in rates.values() if r is None)
        log.info(
            f"FX rate update: upserted {len(rates)} rows for {today} "
            f"({null_count} null)"
        )
    except Exception as e:
        log.error(f"FX rate update: DB error: {e}")
        conn.rollback()
    finally:
        conn.close()


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
