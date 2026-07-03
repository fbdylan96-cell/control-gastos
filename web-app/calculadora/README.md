# 💰 Calculadora de Monto Final para Retiro

App de Streamlit para proyectar el monto final de una cartera de
inversión en dólares, con dos vistas:

1. **Proyección teórica** — rendimiento fijo, aportes de monto y
   frecuencia variables, y un costo fijo por transferencia (p.ej.
   comisión SWIFT, editable, default $65).
2. **Simulación histórica real** — aplica el mismo plan de aportes
   sobre precios reales de QQQ, SPY, QLD y TQQQ descargados de
   Yahoo Finance. Para QLD y TQQQ, cuyo historial real es corto
   (desde 2006 y 2010), el tramo anterior se simula sobre el retorno
   diario de QQQ/SPY (ver `src/investment/leveraged_simulation.py`).

## 🛠️ Tech Stack
- Python, Streamlit, Pandas
- `yfinance` para descargar precios de Yahoo Finance (sin API key)

## 📦 Instalación

```bash
pip install -r requirements.txt
streamlit run calculadora_retiro.py
```

Los precios descargados se cachean en `data_cache/` (no se sube a git)
para no tener que volver a descargarlos en cada ejecución.
