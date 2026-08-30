"""Registro de transacciones manuales desde el chat de WhatsApp.

Primera (y única) superficie de ESCRITURA del agente de consultas. Disciplina:

- El alcance es siempre el cliente dueño del teléfono (resuelto server-side
  por el webhook): individual_id/business_id salen de su fila, nunca del modelo.
- La categoría/subcategoría debe existir EXACTA en la taxonomía del cliente
  (core.categories). El matching flexible ("restaurante" → "Alimentación /
  Fuera de casa") es trabajo del MODELO; esta función es estricta y ante un
  nombre desconocido devuelve la lista disponible para que el modelo pregunte.
- Reusa insert_manual_transaction de web-app/db.py — el mismo camino que el tab
  "Añadir transacciones" de los portales (4 tablas + notificación pre-marcada,
  conversión a colones incluida). Se carga con alias porque la raíz tiene su
  propio db.py.
"""

import importlib.util
import os
from datetime import date

from tools import finance

_WEBAPP_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web-app", "db.py")

# Topes de sanidad (el modelo pasa lo que el cliente dijo; esto es el respaldo).
MAX_AMOUNT = 100_000_000   # anti-error de dedo, en la moneda indicada
NOTE_MAX_LEN = 280         # mismo tope que client_notes en las apps
MERCHANT_MAX_LEN = 120

_TYPE_MAP = {"gasto": "debito", "ingreso": "credito"}

_webapp_db = None


def _db():
    """web-app/db.py bajo el alias webapp_db (es autocontenido: stdlib+psycopg2)."""
    global _webapp_db
    if _webapp_db is None:
        spec = importlib.util.spec_from_file_location("webapp_db", _WEBAPP_DB_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _webapp_db = mod
    return _webapp_db


def _category_catalog(conn, catalog_scope):
    """[(category, subcategory|None), ...] de la taxonomía del cliente."""
    return [(c["category"], c["subcategory"] or None)
            for c in finance.list_categories(conn, **catalog_scope)]


def _format_catalog(catalog):
    grouped = {}
    for cat, sub in catalog:
        grouped.setdefault(cat, [])
        if sub:
            grouped[cat].append(sub)
    lines = []
    for cat, subs in grouped.items():
        lines.append(f"{cat}" + (f" ({', '.join(subs)})" if subs else ""))
    return "; ".join(lines)


def register_transaction(conn, client_row, catalog_scope, *, amount,
                         transaction_type, category, subcategory=None,
                         currency=None, merchant=None, note=None, txn_date=None):
    """Valida y registra una transacción manual del cliente.

    Devuelve un dict-resumen para el tool_result. Cualquier problema de
    validación lanza ValueError con un mensaje que el modelo puede relatar
    (incluida la lista de categorías cuando el nombre no existe).
    """
    # Monto
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise ValueError("El monto debe ser un número.")
    if not (0 < amount < MAX_AMOUNT):
        raise ValueError(f"El monto debe ser mayor a 0 y menor a {MAX_AMOUNT:,.0f}.")

    # Tipo: el modelo habla en gasto/ingreso; la BD en debito/credito.
    txn_type = _TYPE_MAP.get((transaction_type or "").strip().lower())
    if not txn_type:
        raise ValueError("El tipo debe ser 'gasto' o 'ingreso'.")

    # Moneda (default colones)
    currency = (currency or "CRC").strip().upper() or "CRC"
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError(f"Moneda inválida: {currency!r} (código de 3 letras, ej. CRC, USD).")

    # Categoría/subcategoría: EXACTAS en la taxonomía del cliente.
    cat = (category or "").strip()
    sub = (subcategory or "").strip() or None
    catalog = _category_catalog(conn, catalog_scope)
    if (cat, sub) not in catalog:
        etiqueta = f"{cat} / {sub}" if sub else cat
        raise ValueError(
            f"La categoría '{etiqueta}' no existe en la taxonomía del cliente. "
            f"Disponibles: {_format_catalog(catalog)}")

    # Fecha (opcional, default hoy; nunca futura)
    parsed_date = None
    if txn_date:
        try:
            parsed_date = date.fromisoformat(str(txn_date).strip())
        except ValueError:
            raise ValueError(f"Fecha inválida: {txn_date!r} (se espera YYYY-MM-DD).")
        if parsed_date > _db().today_cr():
            raise ValueError("La fecha no puede ser futura.")

    merchant = (str(merchant).strip()[:MERCHANT_MAX_LEN] or None) if merchant else None
    note = (str(note).strip()[:NOTE_MAX_LEN] or None) if note else None

    _db().insert_manual_transaction(
        conn,
        individual_id=str(client_row["id"]),
        business_id=str(client_row["business_id"]),
        merchant=merchant,
        amount=amount,
        currency=currency,
        txn_type=txn_type,
        category=cat,
        subcategory=sub,
        txn_date=parsed_date,
        client_notes=note,
    )

    return {
        "registrada": True,
        "monto": amount,
        "moneda": currency,
        "tipo": "gasto" if txn_type == "debito" else "ingreso",
        "categoria": cat,
        "subcategoria": sub,
        "comercio": merchant,
        "nota": note,
        "fecha": (parsed_date or _db().today_cr()).isoformat(),
    }
