# -*- coding: utf-8 -*-
"""
Funciones financieras genéricas compartidas por los módulos de proyección
de inversión.
"""
from __future__ import annotations

FREQ_TO_MONTHS = {
    "Mensual": 1,
    "Trimestral": 3,
    "Semestral": 6,
    "Anual": 12,
    "Cada 2 años": 24,
    "Cada 3 años": 36,
}


def monthly_rate_from_annual(annual_rate: float) -> float:
    """Convierte una tasa anual efectiva a su equivalente mensual compuesta."""
    return (1.0 + annual_rate) ** (1.0 / 12.0) - 1.0
