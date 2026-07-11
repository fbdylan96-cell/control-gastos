# Calculadoras financieras — guía de la carpeta

Esta carpeta agrupa **todo lo relacionado con las calculadoras de inversión y retiro**.
Son tres piezas con roles distintos que comparten un solo conjunto de motores.

```
web-app/calculadora/
├── calculadora-core/          ★ ÚNICA FUENTE DE VERDAD de los motores
│   ├── src/
│   │   ├── data/              Clientes de datos (Yahoo Finance, CBOE) + caché
│   │   ├── finance/           Métricas (CAGR, Sharpe, drawdown, XIRR) y anualidades
│   │   ├── investment/        Backtests y estrategias (momentum, VIX, DCA, leverage…)
│   │   └── pension/           Reglas CCSS: IVM y ROP (fuente de verdad regulatoria)
│   ├── assets/                Logos de marca + CSVs de datos (SP500 histórico, spread HY)
│   └── data_cache/            Caché de precios (committeado; las apps lo refrescan solo)
│
├── calculadora-estrategia/    App del asesor: "Estrategias de Inversión"
│   └── calculadora_retiro.py  Streamlit · URL /calculadora/ · puerto 8501 · acceso con contraseña
│
└── calculadora-financiera/    Wizard público "Tu brecha de retiro" (lead magnet)
    └── retiro_wizard.py       Streamlit · URL /brecha/ · puerto 8502 · sin login
```

## La regla de oro: un solo `src/`

Los motores viven **solo** en `calculadora-core/src/`. Las dos apps lo importan así:

```python
_CORE_DIR = Path(__file__).resolve().parent.parent / "calculadora-core"
sys.path.insert(0, str(_CORE_DIR))
from src.investment import backtest_v2 as btv2   # ejemplo
```

**Nunca copies `src/` (ni un archivo suyo) dentro de una app.** Si una estrategia nueva
necesita algo, se agrega en `calculadora-core/src/` y ambas apps lo ven de inmediato.
En julio 2026 hubo dos copias divergentes de `src/` y costó una fusión manual completa
(commit `280fa72`); no repitamos eso.

## Cómo agregar una estrategia nueva

1. El motor (cálculo puro, sin Streamlit) va en `calculadora-core/src/investment/<nombre>.py`.
   Sin `print`, sin UI; funciones que reciben parámetros y devuelven DataFrames/dataclasses.
2. Si necesita datos de mercado, usa `src.data.yahoo_client.get_daily_closes()` /
   `get_daily_ohlc()` — cachean solos en `calculadora-core/data_cache/`.
3. Si necesita un archivo de datos estático (CSV), va en `calculadora-core/assets/` y se
   resuelve con rutas relativas al módulo (ver `sp500_momentum.py` como ejemplo).
4. La UI (tab de Streamlit) va en la app que corresponda (`calculadora_retiro.py` o
   `retiro_wizard.py`).
5. **Cambios de firma en funciones existentes**: revisa antes quién las llama
   (`grep -rn "nombre_funcion" web-app/calculadora/`) — las dos apps consumen el mismo core.
   Prefiere parámetros nuevos con default para no romper llamadas existentes.

## Qué NO se commitea

- `__pycache__/`, `*.pyc` (ya están en el `.gitignore` raíz)
- `venv/` (cada app tiene el suyo en el servidor, fuera de git)
- Binarios pesados (PPT, ZIPs, videos). Los PNG de marca ya están en `core/assets/`.
- `.env`, tokens, credenciales

## Datos regulatorios (CCSS)

`src/pension/ivm.py` y `rop.py` son la fuente de verdad de montos y reglas (pensión
máxima/mínima IVM, etc.). Si la CCSS actualiza un monto, se cambia **ahí** con un
comentario que cite la fuente y la fecha del acuerdo.

## Despliegue (referencia)

| App | Servicio systemd | Puerto | URL pública |
|---|---|---|---|
| calculadora-estrategia | `calculadora.service` | 8501 | `/calculadora/` (auth vía nginx) |
| calculadora-financiera | `brecha.service` | 8502 | `/brecha/` (público) |

Ambos servicios corren en el EC2 desde `/srv/control-gastos/web-app/calculadora/…` con un
venv propio dentro de cada carpeta de app. Tras un `git pull` que toque `calculadora-core/`,
reiniciar **ambos**: `sudo systemctl restart calculadora brecha`.
