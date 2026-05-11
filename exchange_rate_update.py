from datetime import date, timedelta
from bccr import SW

code_list = {
    'JPY': 325,
    'CHF': 326,
    'CAD': 328,
    'MXN': 332,
    'SEK': 335,
    'KRW': 337,
    'GTQ': 338,
    'HNL': 339,
    'NIO': 340,
    'DKK': 342,
    'NOK': 343,
    'ARS': 344,
    'COP': 345,
    'BRL': 346,
    'DOP': 3043,
    'HKD': 3052,
    'TWD': 3053,
    'BOB': 3054,
    'CLP': 3055,
    'RUB': 3056,
    'PEN': 3057,
    'CNY': 3364,
    'PLN': 3430,
    'LKR': 20873,
    'BDT': 21251,
    'THB': 21262,
    'IDR': 21263,
    'AED': 21264,
    'MAD': 21265,
    'ILS': 21266,
    'INR': 21267,
    'EGP': 21268,
    'NZD': 21269,
    'SGD': 21270,
    'VND': 21766,
    'ZAR': 21881,
    'JOD': 22204,
    'MYR': 25067,
    'BHD': 41438,
    'VES': 60246,
    'UYU': 84857,
    'CRC': 318,
}

today = date.today()
start = today - timedelta(days=15)

exc_rate_daily = SW(
    **{code: indicator for code, indicator in code_list.items()},
    FechaInicio=str(start.strftime("%Y-%m-%d"))
)

latest_rates = {}
for code in code_list:
    if code not in exc_rate_daily.columns:
        latest_rates[code] = None
    else:
        col = exc_rate_daily[code].dropna()
        latest_rates[code] = col.iloc[-1] if not col.empty else None
