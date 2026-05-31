"""Finance tools portfolio.

A collection of parameterized, side-effect-free query functions over the
core.* tables. Each function takes a live psycopg2 connection plus a scope
(either an individual_id for a personal client or a business_id for a whole
business) and returns plain Python dicts/lists — no Flask, no ORM.

These power the web dashboards today and are intended to be called later by an
AI agent answering personal-finance questions over WhatsApp.
"""

from tools.finance import (  # noqa: F401
    get_category_budget,
    get_income_expense_summary,
    get_monthly_category_spending,
    get_top_spending,
    last_full_year_range,
    list_categories,
    month_buckets,
    resolve_range,
)