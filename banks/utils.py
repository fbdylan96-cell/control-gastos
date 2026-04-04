"""Shared text-parsing utilities for bank-specific parsers."""

import re



def normalize_whitespace(s):
    s = re.sub(r"[\u2000-\u200D\uFEFF]", " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def normalize_number(s):
    """
    Convert '1.234,56' or '1,234.56' or '41.000,00' → float or None.
    Returns a Python float rounded to 2 decimal places, or None on failure.
    """
    x = str(s or "").strip()
    if not x:
        return None

    has_comma = "," in x
    has_dot = "." in x

    if has_comma and has_dot:
        if x.rfind(".") > x.rfind(","):
            # 1,234.56 → US format
            x = x.replace(",", "")
        else:
            # 1.234,56 → European format
            x = x.replace(".", "").replace(",", ".")
    elif has_comma and not has_dot:
        if re.search(r",(\d{2})$", x):
            x = x.replace(",", ".")
        else:
            x = x.replace(",", "")
    else:
        if x.count(".") > 1:
            x = x.replace(".", "")

    try:
        return round(float(x), 2)
    except ValueError:
        return None


def parse_amount_currency(text):
    """
    Return (currency: str, amount: float|None) from a text snippet.
    currency is '' if not found. amount is None if not found.
    """
    t = normalize_whitespace(str(text or ""))

    # "por USD 20.00" / "por CRC 9,900.00"
    m = re.search(
        r"\bpor\s+(CRC|USD|EUR)\s*([\d]{1,3}(?:[.,][\d]{3})*(?:[.,][\d]{2})?)\b",
        t, re.IGNORECASE,
    )
    if m:
        return m.group(1).upper(), normalize_number(m.group(2))

    # "por un monto de 41.000,00 CRC"
    m = re.search(
        r"por\s+un\s+monto\s+de\s*[:\-]?\s*([\d]{1,3}(?:[.,][\d]{3})*(?:[.,][\d]{2})?)\s*(CRC|USD|EUR)\b",
        t, re.IGNORECASE,
    )
    if m:
        return m.group(2).upper(), normalize_number(m.group(1))

    # "Monto: CRC 3,578.00" or "Monto CRC: 2,999.00"
    m = re.search(r"\bMonto\b[\s:]*(CRC|USD|EUR)\b[\s:]*([\d.,]+)\b", t, re.IGNORECASE)
    if m:
        return m.group(1).upper(), normalize_number(m.group(2))

    # "CRC: 10,202.50" / "USD 99.00"
    m = re.search(
        r"\b(CRC|USD|EUR)\b\s*[:\-]?\s*([\d]{1,3}(?:[.,][\d]{3})*(?:[.,][\d]{2})?)\b",
        t, re.IGNORECASE,
    )
    if m:
        return m.group(1).upper(), normalize_number(m.group(2))

    # ₡
    m = re.search(r"₡\s*([\d]{1,3}(?:[.,][\d]{3})*(?:[.,][\d]{2})?)", t)
    if m:
        return "CRC", normalize_number(m.group(1))

    # $
    m = re.search(r"\$\s*([\d]{1,3}(?:[.,][\d]{3})*(?:[.,][\d]{2})?)", t)
    if m:
        return "USD", normalize_number(m.group(1))

    return "", None


def strip_city_country_suffix(s):
    t = re.sub(r"\s*Ciudad\s+y\s+pa[\s\S]*$", "", str(s or ""), flags=re.IGNORECASE)
    t = re.sub(
        r"Ciudad\s+y\s+pa[\s\S]*?(?=(Fecha|Monto|VISA|Autorizaci[oó]n|Referencia"
        r"|Tipo\s+de\s+Transacci[oó]n|$))",
        " ", t, flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", t).strip()


_ARTICLES = {"de", "la", "el", "y", "en", "del"}
_MERCHANT_EXCEPTIONS = {
    "uber": "Uber",
    "uber eats": "Uber Eats",
    "apple": "Apple",
    "apple.com": "Apple.com",
    "spotify": "Spotify",
    "amazon": "Amazon",
    "transferencia bac": "Transferencia BAC",
}


def smart_title_case(s):
    t = str(s or "").strip()
    if not t:
        return ""
    key = t.lower()
    if key in _MERCHANT_EXCEPTIONS:
        return _MERCHANT_EXCEPTIONS[key]
    result = []
    for w in t.lower().split():
        if not w:
            continue
        if len(w) <= 2 and re.match(r"^[a-z]{1,2}$", w):
            result.append(w.upper())
        elif w in _ARTICLES:
            result.append(w)
        else:
            result.append(w[0].upper() + w[1:])
    return " ".join(result).strip()
