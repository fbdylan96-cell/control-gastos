"""
Bootstrap del catálogo de PayPal (correr UNA VEZ por ambiente, sandbox y live).

Crea el Product y un Billing Plan por cada fila activa de
core.subscription_plans que aún no tenga paypal_plan_id, y guarda el id
resultante. Idempotente: re-ejecutarlo no duplica nada (el Product usa id fijo
y los planes ya cableados se saltan).

Uso (desde web-app/, con el venv del webapp y el .env apuntando a la BD y a
las credenciales PayPal del ambiente deseado):

    python paypal_bootstrap.py            # muestra qué haría (dry-run)
    python paypal_bootstrap.py --ejecutar # crea los planes de verdad

⚠ PAYPAL_MODE decide el ambiente. Los plan ids de sandbox NO sirven en live:
al pasar a producción hay que vaciar paypal_plan_id y correr esto de nuevo.
"""

import argparse
import logging
import os
import sys

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

import paypal_client  # noqa: E402
from db import get_connection, list_subscription_plans, set_plan_paypal_id  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser(description="Crea Products/Plans de PayPal para los planes de neto")
    ap.add_argument("--ejecutar", action="store_true",
                    help="Crear de verdad (sin esto solo muestra qué haría)")
    args = ap.parse_args()

    if not paypal_client.is_configured():
        log.error("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET no están en el .env")
        return 1
    log.info(f"Ambiente PayPal: {os.environ.get('PAYPAL_MODE', 'sandbox')}")

    conn = get_connection()
    try:
        plans = list_subscription_plans(conn)  # activos, todos los tiers
        pending = [p for p in plans if not p["paypal_plan_id"]]
        done = [p for p in plans if p["paypal_plan_id"]]
        for p in done:
            log.info(f"  ya cableado: {p['name']} → {p['paypal_plan_id']}")
        if not pending:
            log.info("Nada que crear — todos los planes activos tienen paypal_plan_id.")
            return 0
        for p in pending:
            log.info(f"  por crear:   {p['name']} (${float(p['amount_usd']):.2f} USD / {p['modality']})")

        if not args.ejecutar:
            log.info("Dry-run. Repetir con --ejecutar para crear en PayPal.")
            return 0

        paypal_client.ensure_product()
        for p in pending:
            paypal_plan_id = paypal_client.create_plan(
                f"neto — {p['name']}", p["amount_usd"], p["modality"])
            set_plan_paypal_id(conn, p["id"], paypal_plan_id)
            log.info(f"  creado: {p['name']} → {paypal_plan_id}")
        log.info("Listo.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
