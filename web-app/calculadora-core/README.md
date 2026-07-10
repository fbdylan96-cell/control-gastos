# calculadora-core — módulos compartidos

Motores de cálculo y assets de marca **compartidos** por las apps Streamlit de inversión:

| Consumidor | App | Acceso |
|---|---|---|
| `calculadora/calculadora_retiro.py` | Estrategias de Inversión (`/calculadora/`) | Exclusivo del asesor (contraseña admin vía nginx `auth_request`) |
| `calculadora-financiera/retiro_wizard.py` | Tu brecha de retiro (`/brecha/`) | Público (lead magnet) |

## Contenido

- `src/pension/` — reglas del IVM (CCSS) y proyección del ROP. **Única fuente de
  verdad regulatoria** (parámetros 2026); no duplicar estas reglas en las apps.
- `src/investment/` — proyecciones y backtests (rendimiento fijo, DCA histórico,
  SMA/Faber, decumulación, etc.).
- `src/finance/` — utilidades financieras (anualidades, métricas).
- `src/data/` — clientes de datos de mercado (Yahoo, CBOE). Solo los usa la
  calculadora completa, no el wizard.
- `assets/` — logo y marca Empowered Investor.

## Cómo importar desde una app

Cada app agrega esta carpeta al `sys.path` antes de sus imports:

```python
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent / "calculadora-core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from src.pension.ivm import calcular_pension_ivm  # etc.
```

En el server cada app corre con su **propio venv y servicio systemd**
(`calculadora.service` :8501, `brecha.service` :8502); esta carpeta no tiene
venv propio — es solo código importable.
