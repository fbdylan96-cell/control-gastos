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
        # 1-2 trailing digits after the comma → decimal comma ("164,5" / "152,63");
        # 3 digits → thousands separator ("41,000")
        if re.search(r",(\d{1,2})$", x):
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

    # "por USD 20.00" / "por CRC 9,900.00" / "por CRC 110,510.4" (1-2 decimals)
    m = re.search(
        r"\bpor\s+(CRC|USD|EUR)\s*([\d]{1,3}(?:[.,][\d]{3})*(?:[.,][\d]{1,2})?)\b",
        t, re.IGNORECASE,
    )
    if m:
        return m.group(1).upper(), normalize_number(m.group(2))

    # "por un monto de 41.000,00 CRC" / "por un monto de 169.5 USD"
    m = re.search(
        r"por\s+un\s+monto\s+de\s*[:\-]?\s*([\d]{1,3}(?:[.,][\d]{3})*(?:[.,][\d]{1,2})?)\s*(CRC|USD|EUR)\b",
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
        r"\b(CRC|USD|EUR)\b\s*[:\-]?\s*([\d]{1,3}(?:[.,][\d]{3})*(?:[.,][\d]{1,2})?)\b",
        t, re.IGNORECASE,
    )
    if m:
        return m.group(1).upper(), normalize_number(m.group(2))

    # ₡
    m = re.search(r"₡\s*([\d]{1,3}(?:[.,][\d]{3})*(?:[.,][\d]{1,2})?)", t)
    if m:
        return "CRC", normalize_number(m.group(1))

    # $
    m = re.search(r"\$\s*([\d]{1,3}(?:[.,][\d]{3})*(?:[.,][\d]{1,2})?)", t)
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


# ---------------------------------------------------------------------------
# Merchant normalization for classification (ported from Classify.js)
# ---------------------------------------------------------------------------

import unicodedata as _unicodedata

_CR_LOCATION_STOP = {
    "san", "jose", "sanjose", "escazu", "santa", "ana", "curridabat",
    "moravia", "heredia", "alajuela", "cartago", "montes", "oca",
    "barva", "belen", "tibas", "desamparados", "guadalupe", "pavas",
    "lindora", "costa", "rica", "cr", "cri", "multiplaza", "oxigeno",
    "terramall", "lincoln", "plaza", "city", "mall", "ocn", "oc", "crl",
}

_PAYMENT_PREFIX_RE = re.compile(r"^\s*(dlc\*|dl\*|payu\*|stripe\*|sq\*)\s*", re.IGNORECASE)

_DISPLAY_EXCEPTIONS = {
    "uber eats": "Uber Eats",
    "uber": "Uber",
    "pricesmart": "PriceSmart",
    "price smart": "PriceSmart",
    "icloud": "iCloud",
    "amazon marketplace": "Amazon",
}

_TITLE_STOP = {"y", "de", "la", "el", "los", "las", "del", "al", "and", "of", "the"}


def clean_merchant_key(name: str) -> str:
    """
    Deterministic lookup key: lowercase, no accents, no symbols, collapsed spaces.
    e.g. 'WALMART OXÍGENO' → 'walmart oxigeno'
    """
    if not name:
        return ""
    s = str(name).lower().strip()
    s = _unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if _unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_cr_location_suffix(s: str) -> str:
    """
    Remove trailing Costa Rica location tokens from a merchant name.
    Works token-by-token from the right, stopping at the first non-location word.
    e.g. 'Walmart Oxigeno San Jose' → 'Walmart'
    """
    if not s:
        return ""
    s = str(s)
    s = re.sub(r"\b(ciudad\s*y\s*pa[íi]s?|ciudad\s*y\s*pa)\b.*$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    tokens = s.split(" ")
    while len(tokens) > 1:
        last_key = clean_merchant_key(tokens[-1])
        if not last_key or last_key in _CR_LOCATION_STOP or re.match(r"^\d+$", last_key):
            tokens.pop()
        else:
            break
    return " ".join(tokens).strip()


def _title_case_smart(s: str) -> str:
    parts = str(s or "").strip().split()
    out = []
    for i, word in enumerate(parts):
        w = word.lower()
        # preserve short all-caps tokens (e.g. 'KFC', 'AM PM')
        if len(word) <= 5 and re.match(r"^[A-Z0-9]+$", word) and re.search(r"[A-Z]", word):
            out.append(word)
        elif i > 0 and w in _TITLE_STOP:
            out.append(w)
        else:
            out.append(w[0].upper() + w[1:] if w else w)
    return " ".join(out)


def format_merchant_display(name: str) -> str:
    """
    Clean display name for a merchant:
    strips CR location suffixes, removes payment processor prefixes, applies title case.
    e.g. 'DLC* UBER EATS SAN JOSE CRI' → 'Uber Eats'
    """
    if not name:
        return ""
    t = str(name).strip()
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = strip_cr_location_suffix(t)
    t = _PAYMENT_PREFIX_RE.sub("", t).strip()
    t = _title_case_smart(t)
    lower = t.lower()
    if lower in _DISPLAY_EXCEPTIONS:
        return _DISPLAY_EXCEPTIONS[lower]
    return t
