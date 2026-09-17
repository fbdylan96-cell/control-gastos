"""Finance query functions (the "tools portfolio").

Conventions
-----------
- Scope is given as exactly one of:
    individual_id=<uuid>  → a personal client (sentinel business)
    business_id=<uuid>    → a whole business (aggregates all its members)
- Amounts are always core.transactions_enriched.amount_local (CRC).
- Only real, settled transactions are counted:
    transaction_approval = 'Aprobada'
    transaction_status NOT IN ('unknown', 'Descartado', 'Duplicado')
    amount_local IS NOT NULL
  Gastos = transaction_type_guess 'debito'; Ingresos = 'credito'.
- Category attribution uses the user-confirmed ground truth in
  core.transactions_notifications (final_category / final_subcategory).
- Dates are filtered on core.transactions_raw.local_date.

All functions return raw colones; presentation layers divide by 1000 when a
"miles de colones" display is desired.
"""

from datetime import date, timedelta

INDIVIDUAL_BIZ_ID = "00000000-0000-0000-0000-000000009999"

# Rows that represent a real, settled transaction with a usable local amount.
_BASE_FILTERS = """
    e.transaction_approval = 'Aprobada'
    AND e.transaction_status NOT IN ('unknown', 'Descartado', 'Duplicado')
    AND e.amount_local IS NOT NULL
"""


# ---------------------------------------------------------------------------
# Scope helper
# ---------------------------------------------------------------------------

def _scope_filter(individual_id, business_id):
    """Return (sql_fragment, params) constraining transactions_enriched `e`."""
    if individual_id is not None:
        return "e.individual_id = %s", [str(individual_id)]
    if business_id is not None:
        return "e.business_id = %s", [str(business_id)]
    raise ValueError("Provide exactly one of individual_id or business_id")


# ---------------------------------------------------------------------------
# Date-range helpers
# ---------------------------------------------------------------------------

def last_full_year_range(today=None):
    """Trailing 12 *complete* months, excluding the current month.

    e.g. on 2026-05-31 → (2025-05-01, 2026-04-30).
    """
    today = today or date.today()
    first_this_month = today.replace(day=1)
    end = first_this_month - timedelta(days=1)          # last day of prev month
    start = date(first_this_month.year - 1, first_this_month.month, 1)
    return start, end


def last_12_months_range(today=None):
    """Last 12 months including the current (partial) month.

    e.g. on 2026-05-15 → (2025-06-01, 2026-05-15): June 2025 through today.
    """
    today = today or date.today()
    first_this_month = today.replace(day=1)
    # Start 11 whole months before the current month's first day.
    month_index = (first_this_month.year * 12 + (first_this_month.month - 1)) - 11
    start = date(month_index // 12, month_index % 12 + 1, 1)
    return start, today


def resolve_range(option, today=None):
    """Map a UI option string to a (date_from, date_to) pair.

    Options: 'ultimo_anio', 'anio_actual', 'ultimos_30', 'mes_actual'.
    Unknown values fall back to 'ultimo_anio'.
    """
    today = today or date.today()
    if option == "anio_actual":
        return date(today.year, 1, 1), today
    if option == "ultimos_30":
        return today - timedelta(days=30), today
    if option == "mes_actual":
        return today.replace(day=1), today
    return last_full_year_range(today)


def month_buckets(date_from, date_to):
    """List of 'YYYY-MM' strings spanning the months of [date_from, date_to]."""
    buckets = []
    y, m = date_from.year, date_from.month
    while (y, m) <= (date_to.year, date_to.month):
        buckets.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return buckets


# ---------------------------------------------------------------------------
# Section 1: general summary
# ---------------------------------------------------------------------------

def get_income_expense_summary(conn, *, individual_id=None, business_id=None,
                               date_from, date_to):
    """Return {'ingresos', 'gastos', 'tasa_ahorro'} for the scope and range.

    tasa_ahorro = (ingresos - gastos) / ingresos * 100, or None when ingresos = 0.
    """
    scope_sql, params = _scope_filter(individual_id, business_id)
    sql = f"""
        SELECT
            COALESCE(SUM(e.amount_local) FILTER (WHERE e.transaction_type_guess = 'credito'), 0),
            COALESCE(SUM(e.amount_local) FILTER (WHERE e.transaction_type_guess = 'debito'), 0)
        FROM core.transactions_enriched e
        JOIN core.transactions_raw r ON r.id = e.raw_id
        WHERE {scope_sql} AND {_BASE_FILTERS}
          AND r.local_date::date BETWEEN %s AND %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params + [date_from, date_to])
        ingresos, gastos = cur.fetchone()
    ingresos = float(ingresos)
    gastos = float(gastos)
    tasa = ((ingresos - gastos) / ingresos * 100) if ingresos > 0 else None
    return {"ingresos": ingresos, "gastos": gastos, "tasa_ahorro": tasa}


def get_top_spending(conn, *, individual_id=None, business_id=None,
                     date_from, date_to, limit=5):
    """Top `limit` Categoría/Subcategoría pairs by gasto, with % share.

    share_pct is each pair's share of *total* spending in the range (not just
    of the returned top rows).
    """
    scope_sql, params = _scope_filter(individual_id, business_id)
    sql = f"""
        SELECT n.final_category, n.final_subcategory, SUM(e.amount_local) AS total
        FROM core.transactions_enriched e
        JOIN core.transactions_raw r ON r.id = e.raw_id
        JOIN core.transactions_classified c ON c.raw_id = e.raw_id
        JOIN core.transactions_notifications n ON n.classified_id = c.id
        WHERE {scope_sql} AND {_BASE_FILTERS}
          AND e.transaction_type_guess = 'debito'
          AND r.local_date::date BETWEEN %s AND %s
        GROUP BY n.final_category, n.final_subcategory
        ORDER BY total DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql, params + [date_from, date_to])
        rows = cur.fetchall()

    total_all = sum(float(r[2]) for r in rows) or 0.0
    out = []
    for cat, sub, total in rows[:limit]:
        total = float(total)
        out.append({
            "category": cat,
            "subcategory": sub,
            "total": total,
            "share_pct": (total / total_all * 100) if total_all > 0 else 0.0,
        })
    return out


# ---------------------------------------------------------------------------
# Section 2: monthly spend for one category/subcategory
# ---------------------------------------------------------------------------

def get_monthly_category_spending(conn, *, individual_id=None, business_id=None,
                                  category, subcategory, date_from, date_to):
    """Monthly gasto for a single Categoría/Subcategoría over the range.

    Returns a list of {'month': 'YYYY-MM', 'total': float} with every month in
    the range present (missing months filled with 0.0).
    """
    scope_sql, params = _scope_filter(individual_id, business_id)
    sql = f"""
        SELECT to_char(date_trunc('month', r.local_date), 'YYYY-MM') AS ym,
               SUM(e.amount_local) AS total
        FROM core.transactions_enriched e
        JOIN core.transactions_raw r ON r.id = e.raw_id
        JOIN core.transactions_classified c ON c.raw_id = e.raw_id
        JOIN core.transactions_notifications n ON n.classified_id = c.id
        WHERE {scope_sql} AND {_BASE_FILTERS}
          AND e.transaction_type_guess = 'debito'
          AND n.final_category = %s
          AND (n.final_subcategory = %s OR (n.final_subcategory IS NULL AND %s IS NULL))
          AND r.local_date::date BETWEEN %s AND %s
        GROUP BY ym
        ORDER BY ym
    """
    with conn.cursor() as cur:
        cur.execute(sql, params + [category, subcategory, subcategory, date_from, date_to])
        found = {ym: float(total) for ym, total in cur.fetchall()}

    return [{"month": m, "total": found.get(m, 0.0)} for m in month_buckets(date_from, date_to)]


def get_category_budget(conn, *, individual_id=None, business_id=None,
                        category, subcategory):
    """Return the monthly_budget (CRC) for a Categoría/Subcategoría, or None.

    For an individual, a personal budget (individual_id = user) takes precedence
    over a shared one (individual_id IS NULL). For a business, only the
    business-level row (individual_id IS NULL) is considered.
    """
    with conn.cursor() as cur:
        if individual_id is not None:
            cur.execute(
                """
                SELECT monthly_budget FROM core.categories
                WHERE business_id = %s
                  AND (individual_id = %s OR individual_id IS NULL)
                  AND category = %s
                  AND (subcategory = %s OR (subcategory IS NULL AND %s IS NULL))
                ORDER BY individual_id NULLS LAST
                LIMIT 1
                """,
                (INDIVIDUAL_BIZ_ID, str(individual_id), category, subcategory, subcategory),
            )
        else:
            cur.execute(
                """
                SELECT monthly_budget FROM core.categories
                WHERE business_id = %s
                  AND individual_id IS NULL
                  AND category = %s
                  AND (subcategory = %s OR (subcategory IS NULL AND %s IS NULL))
                LIMIT 1
                """,
                (str(business_id), category, subcategory, subcategory),
            )
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def list_categories(conn, *, individual_id=None, business_id=None):
    """Return the selectable Categoría/Subcategoría pairs for the scope."""
    with conn.cursor() as cur:
        if individual_id is not None:
            cur.execute(
                """
                SELECT DISTINCT category, subcategory FROM core.categories
                WHERE business_id = %s AND (individual_id = %s OR individual_id IS NULL)
                ORDER BY category, subcategory NULLS FIRST
                """,
                (INDIVIDUAL_BIZ_ID, str(individual_id)),
            )
        else:
            cur.execute(
                """
                SELECT category, subcategory FROM core.categories
                WHERE business_id = %s AND individual_id IS NULL
                ORDER BY category, subcategory NULLS FIRST
                """,
                (str(business_id),),
            )
        return [{"category": c, "subcategory": s} for c, s in cur.fetchall()]


# ---------------------------------------------------------------------------
# Section 3: dashboard de inicio (mes en curso + histórico)
# ---------------------------------------------------------------------------

def get_first_transaction_date(conn, *, individual_id=None, business_id=None):
    """Date of the scope's earliest usable transaction, or None.

    The history charts start here instead of a fixed 12 months back: a client
    who joined in May would otherwise stare at seven empty months.
    """
    scope_sql, params = _scope_filter(individual_id, business_id)
    sql = f"""
        SELECT MIN(r.local_date::date)
        FROM core.transactions_enriched e
        JOIN core.transactions_raw r ON r.id = e.raw_id
        WHERE {scope_sql} AND {_BASE_FILTERS}
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def get_monthly_income_expense(conn, *, individual_id=None, business_id=None,
                               date_from, date_to):
    """Per-month {'month', 'ingresos', 'gastos', 'tasa_ahorro'} over the range.

    Every month of the range is present (zeros where nothing happened);
    tasa_ahorro is None for months without income, same rule as the summary.
    """
    scope_sql, params = _scope_filter(individual_id, business_id)
    sql = f"""
        SELECT to_char(date_trunc('month', r.local_date), 'YYYY-MM') AS ym,
               COALESCE(SUM(e.amount_local) FILTER (WHERE e.transaction_type_guess = 'credito'), 0),
               COALESCE(SUM(e.amount_local) FILTER (WHERE e.transaction_type_guess = 'debito'), 0)
        FROM core.transactions_enriched e
        JOIN core.transactions_raw r ON r.id = e.raw_id
        WHERE {scope_sql} AND {_BASE_FILTERS}
          AND r.local_date::date BETWEEN %s AND %s
        GROUP BY ym
    """
    with conn.cursor() as cur:
        cur.execute(sql, params + [date_from, date_to])
        found = {ym: (float(i), float(g)) for ym, i, g in cur.fetchall()}

    out = []
    for m in month_buckets(date_from, date_to):
        ingresos, gastos = found.get(m, (0.0, 0.0))
        tasa = ((ingresos - gastos) / ingresos * 100) if ingresos > 0 else None
        out.append({"month": m, "ingresos": ingresos, "gastos": gastos, "tasa_ahorro": tasa})
    return out


def get_top_merchants(conn, *, individual_id=None, business_id=None,
                      category, subcategory, date_from, date_to, limit=3):
    """Top merchants by gasto inside one Categoría/Subcategoría.

    merchant is None for movements the bank reports without one (SINPE,
    transferencias); the caller shows those as a bucket, not as a gap.
    """
    scope_sql, params = _scope_filter(individual_id, business_id)
    sql = f"""
        SELECT c.merchant, SUM(e.amount_local) AS total, COUNT(*) AS n
        FROM core.transactions_enriched e
        JOIN core.transactions_raw r ON r.id = e.raw_id
        JOIN core.transactions_classified c ON c.raw_id = e.raw_id
        JOIN core.transactions_notifications n ON n.classified_id = c.id
        WHERE {scope_sql} AND {_BASE_FILTERS}
          AND e.transaction_type_guess = 'debito'
          AND n.final_category = %s
          AND (n.final_subcategory = %s OR (n.final_subcategory IS NULL AND %s IS NULL))
          AND r.local_date::date BETWEEN %s AND %s
        GROUP BY c.merchant
        ORDER BY total DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params + [category, subcategory, subcategory, date_from, date_to, limit])
        return [{"merchant": m, "total": float(t), "count": n} for m, t, n in cur.fetchall()]


def get_budget_status(conn, *, individual_id=None, business_id=None, date_from, date_to):
    """Every Categoría/Subcategoría that has a monthly_budget, with its gasto
    in the range: [{'category', 'subcategory', 'budget', 'spent'}]."""
    spent = {(r["category"], r["subcategory"]): r["total"]
             for r in get_top_spending(conn, individual_id=individual_id, business_id=business_id,
                                       date_from=date_from, date_to=date_to, limit=10_000)}
    out = []
    for c in list_categories(conn, individual_id=individual_id, business_id=business_id):
        budget = get_category_budget(conn, individual_id=individual_id, business_id=business_id,
                                     category=c["category"], subcategory=c["subcategory"])
        if not budget:
            continue
        out.append({
            "category": c["category"],
            "subcategory": c["subcategory"],
            "budget": budget,
            "spent": spent.get((c["category"], c["subcategory"]), 0.0),
        })
    return out


def get_category_month_matrix(conn, *, individual_id=None, business_id=None,
                              date_from, date_to):
    """Ingresos and gastos per Categoría/Subcategoría per month, for export.

    Returns [{'tipo': 'credito'|'debito', 'category', 'subcategory',
    'by_month': {'YYYY-MM': total}}], ingresos first, then gastos, each
    alphabetical by category.
    """
    scope_sql, params = _scope_filter(individual_id, business_id)
    sql = f"""
        SELECT e.transaction_type_guess, n.final_category, n.final_subcategory,
               to_char(date_trunc('month', r.local_date), 'YYYY-MM') AS ym,
               SUM(e.amount_local)
        FROM core.transactions_enriched e
        JOIN core.transactions_raw r ON r.id = e.raw_id
        JOIN core.transactions_classified c ON c.raw_id = e.raw_id
        JOIN core.transactions_notifications n ON n.classified_id = c.id
        WHERE {scope_sql} AND {_BASE_FILTERS}
          AND e.transaction_type_guess IN ('credito', 'debito')
          AND r.local_date::date BETWEEN %s AND %s
        GROUP BY 1, 2, 3, 4
        ORDER BY (e.transaction_type_guess = 'credito') DESC, 2, 3 NULLS FIRST, 4
    """
    rows = {}
    with conn.cursor() as cur:
        cur.execute(sql, params + [date_from, date_to])
        for tipo, cat, sub, ym, total in cur.fetchall():
            rows.setdefault((tipo, cat, sub), {})[ym] = float(total)
    return [{"tipo": t, "category": c, "subcategory": s, "by_month": bm}
            for (t, c, s), bm in rows.items()]