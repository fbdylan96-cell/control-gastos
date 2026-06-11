"""WhatsApp finance consultation agent.

Answers natural-language questions about a client's own transactions by
letting Claude call the read-only query functions in tools.finance through
the Messages API tool-use loop.

Security model
--------------
- The scope (individual_id / business_id) is resolved server-side from the
  client row that the webhook matched by phone. The model only chooses dates,
  categories and limits — it can never point a query at another client.
- Every tool is a parameterized SELECT; the agent has no write surface.

Called by whatsapp_agent_worker.py, never from inside a web request.
"""

import json
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import anthropic

from tools import finance

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"
MAX_TOKENS = 2048
MAX_TOOL_ITERATIONS = 8   # hard stop for the tool-use loop
REQUEST_TIMEOUT = 120.0   # seconds per API call
REPLY_MAX_CHARS = 4000    # Meta caps text messages at 4096 chars

_CR_TZ = ZoneInfo("America/Costa_Rica")

_api_client = None


def _client() -> anthropic.Anthropic:
    global _api_client
    if _api_client is None:
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            raise RuntimeError("ANTHROPIC_API_KEY env var is not set")
        _api_client = anthropic.Anthropic(timeout=REQUEST_TIMEOUT)
    return _api_client


# ---------------------------------------------------------------------------
# Tool definitions (schemas the model sees)
# ---------------------------------------------------------------------------

_DATE_PROP = {"type": "string", "description": "Fecha en formato YYYY-MM-DD"}

TOOL_DEFINITIONS = [
    {
        "name": "get_income_expense_summary",
        "description": (
            "Total de ingresos, total de gastos y tasa de ahorro (%) del cliente "
            "en un rango de fechas. Úsala para preguntas de resumen general."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"date_from": _DATE_PROP, "date_to": _DATE_PROP},
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_top_spending",
        "description": (
            "Las categorías/subcategorías con mayor gasto en un rango de fechas, "
            "con el porcentaje que cada una representa del gasto total."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": _DATE_PROP,
                "date_to": _DATE_PROP,
                "limit": {
                    "type": "integer",
                    "description": "Cuántas posiciones devolver (1-10, default 5)",
                },
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_monthly_category_spending",
        "description": (
            "Gasto mes a mes de una categoría/subcategoría específica en un rango "
            "de fechas. Los meses sin gasto vienen en 0."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Nombre exacto de la categoría (de la lista disponible)",
                },
                "subcategory": {
                    "type": "string",
                    "description": "Nombre exacto de la subcategoría; omitir si la categoría no tiene",
                },
                "date_from": _DATE_PROP,
                "date_to": _DATE_PROP,
            },
            "required": ["category", "date_from", "date_to"],
        },
    },
    {
        "name": "get_category_budget",
        "description": (
            "Presupuesto mensual (CRC) configurado para una categoría/subcategoría. "
            "Devuelve null cuando no hay presupuesto definido."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Nombre exacto de la categoría (de la lista disponible)",
                },
                "subcategory": {
                    "type": "string",
                    "description": "Nombre exacto de la subcategoría; omitir si la categoría no tiene",
                },
            },
            "required": ["category"],
        },
    },
]


# ---------------------------------------------------------------------------
# Server-side scope resolution and tool execution
# ---------------------------------------------------------------------------

def _resolve_scopes(client_row: dict) -> tuple[dict, dict]:
    """Return (spend_scope, catalog_scope) kwargs for tools.finance calls.

    - Individual client (sentinel business): everything scoped to the person.
    - Business admin: business-wide spending and business-level catalog.
    - Business member: only their own spending, but the business's category
      catalog and business-level budgets (members have no personal catalog).
    """
    cid = str(client_row["id"])
    bid = str(client_row["business_id"])
    if bid == finance.INDIVIDUAL_BIZ_ID:
        return {"individual_id": cid}, {"individual_id": cid}
    if client_row.get("business_admin"):
        return {"business_id": bid}, {"business_id": bid}
    return {"individual_id": cid}, {"business_id": bid}


def _parse_date(value, field: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field} inválida: {value!r} (se espera YYYY-MM-DD)")


def _execute_tool(conn, client_row: dict, name: str, tool_input: dict):
    spend_scope, catalog_scope = _resolve_scopes(client_row)
    subcategory = (tool_input.get("subcategory") or "").strip() or None

    if name == "get_income_expense_summary":
        return finance.get_income_expense_summary(
            conn, **spend_scope,
            date_from=_parse_date(tool_input.get("date_from"), "date_from"),
            date_to=_parse_date(tool_input.get("date_to"), "date_to"),
        )
    if name == "get_top_spending":
        limit = min(max(int(tool_input.get("limit") or 5), 1), 10)
        return finance.get_top_spending(
            conn, **spend_scope,
            date_from=_parse_date(tool_input.get("date_from"), "date_from"),
            date_to=_parse_date(tool_input.get("date_to"), "date_to"),
            limit=limit,
        )
    if name == "get_monthly_category_spending":
        return finance.get_monthly_category_spending(
            conn, **spend_scope,
            category=(tool_input.get("category") or "").strip(),
            subcategory=subcategory,
            date_from=_parse_date(tool_input.get("date_from"), "date_from"),
            date_to=_parse_date(tool_input.get("date_to"), "date_to"),
        )
    if name == "get_category_budget":
        return finance.get_category_budget(
            conn, **catalog_scope,
            category=(tool_input.get("category") or "").strip(),
            subcategory=subcategory,
        )
    raise ValueError(f"Herramienta desconocida: {name}")


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _category_lines(conn, catalog_scope: dict) -> str:
    grouped: dict[str, list[str]] = {}
    for c in finance.list_categories(conn, **catalog_scope):
        grouped.setdefault(c["category"], [])
        if c["subcategory"]:
            grouped[c["category"]].append(c["subcategory"])
    lines = []
    for cat, subs in grouped.items():
        lines.append(f"- {cat}" + (f": {', '.join(subs)}" if subs else ""))
    return "\n".join(lines) if lines else "- (sin categorías configuradas)"


def _scope_description(client_row: dict) -> str:
    name = client_row.get("client_name") or "el cliente"
    if str(client_row["business_id"]) == finance.INDIVIDUAL_BIZ_ID:
        return f"los movimientos personales de {name}"
    if client_row.get("business_admin"):
        return f"los movimientos de toda la empresa ({name} es administrador)"
    return (
        f"los movimientos personales de {name} (miembro de una empresa; "
        "las categorías y presupuestos son los de su empresa)"
    )


def _build_system_prompt(conn, client_row: dict) -> str:
    _spend_scope, catalog_scope = _resolve_scopes(client_row)
    today = datetime.now(_CR_TZ).date().isoformat()
    return f"""Eres el asistente financiero por WhatsApp de la herramienta de control de gastos. \
Respondes preguntas sobre {_scope_description(client_row)}.

Hoy es {today} (hora de Costa Rica).

Datos y convenciones:
- Todos los montos están en colones costarricenses (CRC). Formatéalos como ₡1 234 567, sin decimales salvo que aporten.
- "Gastos" son débitos aprobados; "ingresos" son créditos aprobados. La tasa de ahorro es (ingresos - gastos) / ingresos.
- Si el usuario no indica período, usa el mes actual y di explícitamente qué período usaste.
- Obtén toda cifra con las herramientas; nunca inventes números. Si una herramienta devuelve vacío o cero, dilo claramente.

Categorías disponibles (usa estos nombres exactos al llamar herramientas):
{_category_lines(conn, catalog_scope)}

Formato de respuesta (es un chat de WhatsApp):
- Responde la pregunta primero, breve y directo; máximo ~10 líneas.
- Usa *negrita* para las cifras clave y guiones para listas. Nada de tablas ni encabezados markdown.
- Responde siempre en español.

Límites:
- Solo respondes sobre los datos financieros de este usuario dentro de esta herramienta. \
Si preguntan cualquier otra cosa (temas generales, datos de otras personas, asesoría de inversión específica), \
decláralo amablemente fuera de tu alcance y ofrece ayudar con sus gastos, ingresos o presupuestos."""


# ---------------------------------------------------------------------------
# Tool-use loop
# ---------------------------------------------------------------------------

def answer_query(conn, client_row: dict, history: list[dict], question: str) -> str:
    """Run the agent and return the reply text to send back over WhatsApp.

    history: prior turns as [{"role": "user"|"assistant", "content": str}, ...],
    oldest first. The current question is appended as the final user turn.
    """
    system = _build_system_prompt(conn, client_row)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": question})

    response = None
    for _ in range(MAX_TOOL_ITERATIONS):
        response = _client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=system,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )
        if response.stop_reason != "tool_use":
            break

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                value = _execute_tool(conn, client_row, block.name, block.input or {})
                content = json.dumps(value, ensure_ascii=False, default=str)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": content}
                )
            except Exception as e:
                log.warning(f"Tool {block.name} failed: {e}")
                # A failed SELECT aborts the transaction; clear it so the
                # remaining tools (and the worker's bookkeeping) still work.
                conn.rollback()
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Error: {e}",
                        "is_error": True,
                    }
                )
        messages.append({"role": "user", "content": results})

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        text = "No pude generar una respuesta para esa consulta. ¿Puedes reformularla?"
    return text[:REPLY_MAX_CHARS]
