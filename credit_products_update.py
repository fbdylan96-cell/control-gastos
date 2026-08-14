"""Fetch del comparador de productos crediticios (BCCR / MEIC).

Fuente: dashboard público de Power BI que el BCCR publica junto con el MEIC.
Está publicado con "Publish to web", que expone un endpoint de consulta
anónimo; la única credencial es un resource key público embebido en la URL
del reporte. No hay OAuth ni sesión — es HTTP plano.

Este módulo solo PIDE, DECODIFICA y FILTRA. No toca la base ni programa nada:
la carga y las alertas viven en rate_scheduler.run_credit_products_update(),
igual que el par exchange_rate_update.py / rate_scheduler.py.

Dos cosas no obvias:

1. La respuesta no es JSON tabular. Power BI la comprime con tres mecanismos
   simultáneos (diccionarios por columna, repetición de la fila anterior y
   máscara de nulos). Ver decode_dsr(); implementarlo mal produce filas
   silenciosamente incorrectas, no errores.
2. Un HTTP 200 no significa éxito: los errores vienen dentro del cuerpo como
   `odata.error`.

Especificación completa y constantes verificadas: extractorbccrmeic.md
"""
import hashlib
import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

# Constantes del reporte. Configurables porque el resource key cambia si el
# BCCR vuelve a publicar el reporte, y el host puede migrar de región.
ENDPOINT = os.environ.get(
    "BCCR_PRODUCTS_ENDPOINT",
    "https://wabi-paas-1-scus-api.analysis.windows.net"
    "/public/reports/querydata?synchronous=true",
)
RESOURCE_KEY = os.environ.get("BCCR_PRODUCTS_RESOURCE_KEY",
                              "15eb0011-0613-4d52-9a52-f8a18d8293fe")
MODEL_ID = int(os.environ.get("BCCR_PRODUCTS_MODEL_ID", "1625747"))
DATASET_ID = os.environ.get("BCCR_PRODUCTS_DATASET_ID",
                            "98b32c61-3e84-4993-a450-2a4064392a10")
REPORT_ID = os.environ.get("BCCR_PRODUCTS_REPORT_ID", "1223846")
ENTITY = "XML productos crediticios"

# Se piden las 36 columnas aunque solo se guarden 30: las 36 son el contrato
# verificado, y len(row) == 36 es el chequeo de que la fuente no cambió.
# `Indicador` aparece en el esquema pero NO es consultable (la API responde
# CouldNotResolveSemanticQueryDefinition si se incluye).
API_COLUMNS = (
    "TipoPersona", "IdOferente", "NombreOferente", "GrupoOferente", "Periodo",
    "TipoProducto", "Producto", "TipoUso", "Uso", "TipoGenerador", "Generador",
    "TipoCliente", "Cliente", "NombreProducto", "TipoMoneda", "Moneda", "Plazo",
    "Prima", "TipoTasa", "Tasa", "TasaNominal", "TasaMoratoria", "ObsTasa",
    "Beneficios", "TipoCargo", "Cargo", "ValorCargo", "TipoValorCargo",
    "Formato", "ObsCargo",
    # Variantes de presentación (minúsculas, coma decimal). Se piden para que
    # el conteo de 36 siga siendo el contrato, pero NO se guardan.
    "NombreProducto2", "ObsCargo2", "Beneficios2", "ObsTasa2",
    "TasaMoratoria2", "TasaNominal2",
)
N_API_COLUMNS = len(API_COLUMNS)          # 36
N_STORED_COLUMNS = 30                     # las primeras 30 de API_COLUMNS

# Columnas de core.credit_products, en el mismo orden que las 30 primeras de
# API_COLUMNS. create.sql es la fuente de verdad de estos nombres.
DB_COLUMNS = (
    "person_type", "provider_id", "provider_name", "provider_group", "period",
    "product_type", "product", "usage_type", "usage", "generator_type",
    "generator", "client_type", "client", "product_name", "currency_type",
    "currency", "term_months", "down_payment_pct", "rate_type", "rate_kind",
    "nominal_rate", "default_rate", "rate_notes", "benefits", "fee_type",
    "fee", "fee_value", "fee_value_type", "fee_format", "fee_notes",
)

_I_PERIODO = API_COLUMNS.index("Periodo")
_I_PRODUCTO = API_COLUMNS.index("Producto")

# Alcance del negocio: 3 de las 9 clasificaciones. Configurable para poder
# agregar una sin tocar lógica.
_FILTER_DEFAULT = ("Actividades inmobiliarias y construcción|Vehículos|Consumo")
PRODUCT_FILTER = tuple(
    p.strip() for p in os.environ.get("BCCR_PRODUCTS_FILTER", _FILTER_DEFAULT).split("|")
    if p.strip()
)

PAGE_SIZE = 5000
MAX_PAGES = 100          # tope de seguridad por si el restart token deja de avanzar
# Piso de cordura sobre la descarga cruda: el dataset traía 9 974 filas el
# 2026-08-14. Muy por debajo de esto es la fuente rota, no un mes flojo.
MIN_RAW_ROWS = 3000


class SourceError(RuntimeError):
    """La fuente respondió algo que no se puede usar."""


def _build_payload(restart_tokens=None):
    select = [{"Column": {"Expression": {"SourceRef": {"Source": "p"}},
                          "Property": c}, "Name": f"p.{c}"}
              for c in API_COLUMNS]
    window = {"Count": PAGE_SIZE}
    if restart_tokens:
        window["RestartTokens"] = restart_tokens
    return {
        "version": "1.0.0",
        "modelId": MODEL_ID,
        "cancelQueries": [],
        "queries": [{
            "CacheKey": "",
            "QueryId": "",
            "ApplicationContext": {"DatasetId": DATASET_ID,
                                   "Sources": [{"ReportId": REPORT_ID}]},
            "Query": {"Commands": [{"SemanticQueryDataShapeCommand": {
                "Query": {
                    "Version": 2,
                    "From": [{"Name": "p", "Entity": ENTITY, "Type": 0}],
                    "Select": select,
                    # OrderBy es obligatorio para que la paginación sea
                    # determinista.
                    "OrderBy": [{"Direction": 1, "Expression": {"Column": {
                        "Expression": {"SourceRef": {"Source": "p"}},
                        "Property": "IdOferente"}}}],
                },
                "Binding": {
                    "Primary": {"Groupings": [
                        {"Projections": list(range(N_API_COLUMNS)), "Subtotal": 0}]},
                    "DataReduction": {"DataVolume": 3,
                                      "Primary": {"Window": window}},
                    "Version": 1,
                },
                "ExecutionMetricsKind": 1,
            }}]},
        }],
    }


def decode_dsr(ds, ncols, schema=None):
    """Decodifica un DataSet de la respuesta. Devuelve (filas, restart, schema).

    Tres mecanismos simultáneos:
      * ValueDicts — diccionarios de strings POR COLUMNA (no globales): el
        índice 2 significa cosas distintas en D0 y en D1.
      * R — bitmask: el bit i encendido significa "la columna i repite el valor
        de la fila anterior" y no viene en C.
      * Ø — bitmask de nulos (la clave es literalmente el carácter U+00D8).

    `schema` solo llega en la primera fila de la primera página; hay que
    persistirlo entre filas Y entre páginas.

    Ojo: con 36 columnas los bitmask superan los 32 bits. En Python no hay
    problema (enteros arbitrarios); no portar esto a un entero de 32 bits.
    """
    dicts = ds.get("ValueDicts", {})
    rows = []
    prev = [None] * ncols
    for item in ds.get("PH", [{}])[0].get("DM0", []):
        if "S" in item:
            schema = item["S"]
        repeat = item.get("R", 0)
        nulls = item.get("Ø", 0)
        values = item.get("C", [])
        row = [None] * ncols
        vi = 0
        for i in range(ncols):
            if (nulls >> i) & 1:
                row[i] = None
            elif (repeat >> i) & 1:
                row[i] = prev[i]
            else:
                if vi >= len(values):
                    raise SourceError(
                        f"DSR inconsistente: la fila declara más columnas que "
                        f"valores trae (columna {i}, {len(values)} valores)")
                value = values[vi]
                vi += 1
                dn = (schema[i] or {}).get("DN") if schema else None
                if dn and isinstance(value, int) and dn in dicts:
                    value = dicts[dn][value]
                row[i] = value
        # prev se actualiza con la fila YA RESUELTA, incluidos los valores que
        # vinieron por repetición: si no, las cadenas largas de R se corrompen.
        prev = row
        rows.append(row)
    return rows, ds.get("RT"), schema


def fetch_raw_rows():
    """Descarga TODAS las filas del reporte (sin filtrar). Devuelve (filas, páginas)."""
    headers = {"X-PowerBI-ResourceKey": RESOURCE_KEY,
               "Content-Type": "application/json;charset=UTF-8"}
    rows, restart, schema, pages = [], None, None, 0
    while pages < MAX_PAGES:
        resp = requests.post(ENDPOINT, json=_build_payload(restart),
                             headers=headers, timeout=120)
        resp.raise_for_status()
        # Un HTTP 200 no significa éxito: los errores viajan en el cuerpo.
        if "odata.error" in resp.text:
            raise SourceError(f"La API devolvió un error: {resp.text[:800]}")
        try:
            ds = resp.json()["results"][0]["result"]["data"]["dsr"]["DS"][0]
        except (KeyError, IndexError, ValueError) as e:
            raise SourceError(f"Respuesta con forma inesperada: {e}")
        page_rows, restart, schema = decode_dsr(ds, N_API_COLUMNS, schema)
        rows.extend(page_rows)
        pages += 1
        if not restart:
            break
    else:
        raise SourceError(f"La paginación no terminó en {MAX_PAGES} páginas")
    return rows, pages


def _to_date(epoch_ms):
    """Periodo llega como epoch en milisegundos UTC: 1785888000000 → 2026-08-05."""
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(float(epoch_ms) / 1000, tz=timezone.utc).date()


def row_hash(stored_row):
    """Hash de las 30 columnas GUARDADAS, no de las 36 que llegan.

    El grano producto × cargo no tiene clave estable en la fuente: dos filas
    pueden coincidir en oferente, producto, moneda y cargo y diferir solo en
    observaciones de texto. Si el hash incluyera las variantes *2 (puro
    formato), un cambio cosmético en la fuente generaría filas duplicadas
    indistinguibles.
    """
    NULO = "\x00"   # NULL y cadena vacía deben dar hashes DISTINTOS: si no,
                    # dos filas que solo difieren en eso colisionan y el
                    # ON CONFLICT DO NOTHING descarta una en silencio.
    joined = "\x1f".join(NULO if v is None else str(v) for v in stored_row)
    return hashlib.sha256(joined.encode("utf-8")).digest()


def to_storage_row(api_row):
    """Recorta a las 30 columnas guardadas y convierte el período a date."""
    row = list(api_row[:N_STORED_COLUMNS])
    row[_I_PERIODO] = _to_date(row[_I_PERIODO])
    return row


def get_credit_products():
    """Devuelve (filas_listas_para_insertar, total_descargado, páginas).

    Cada fila es la tupla de 30 columnas de DB_COLUMNS. El total descargado
    (pre-filtro) es el chequeo de salud de la fuente: si el filtro se midiera
    después, el propio filtro se vería como pérdida de datos.
    """
    raw, pages = fetch_raw_rows()

    for row in raw:
        if len(row) != N_API_COLUMNS:
            raise SourceError(
                f"Se esperaban {N_API_COLUMNS} columnas por fila y llegaron "
                f"{len(row)}: la fuente cambió de forma")
    if len(raw) < MIN_RAW_ROWS:
        raise SourceError(
            f"Solo llegaron {len(raw)} filas (mínimo esperado {MIN_RAW_ROWS}): "
            f"descarga incompleta o fuente rota")

    selected = [r for r in raw if r[_I_PRODUCTO] in PRODUCT_FILTER]

    # Un producto esperado con cero filas es un cambio de la fuente (renombre),
    # no un dato vacío.
    encontrados = {r[_I_PRODUCTO] for r in selected}
    faltantes = [p for p in PRODUCT_FILTER if p not in encontrados]
    if faltantes:
        raise SourceError(
            "Estos productos del filtro no aparecen en la fuente (¿los "
            f"renombraron?): {', '.join(faltantes)}")

    return [to_storage_row(r) for r in selected], len(raw), pages
