# -*- coding: utf-8 -*-
"""
Calculadora de Monto Final para Retiro.

Única app del proyecto — enfocada solo en:

  1) Proyección teórica con rendimiento fijo, con aportes de monto y
     frecuencia variables, costos del servicio (setup + SWIFT + management
     fee), graficada por año calendario real y edad real del cliente.
     Incluye la meta de retiro (25x ingreso anual), el ingreso implícito
     con un método porcentual de retiro, y una simulación de cuántos años
     resiste el portafolio en el retiro.
  2) Simulación con datos históricos reales (Yahoo Finance) de QQQ, QLD,
     TQQQ o SPY desde el 1999-03-10 (fecha de listado de QQQ, usada
     como inception de referencia para toda la data de esta app),
     aplicando el mismo plan de aportes y costos para ver "cómo hubiera
     sido" invertir de verdad en el pasado. Los precios se bajan y se
     guardan en caché local (`data_cache/`).

Se ejecuta con:
    streamlit run calculadora_retiro.py
"""
import base64
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.finance.metrics import max_drawdown
from src.investment.decumulation import simular_decumulacion
from src.investment.drawdown_recovery import (
    HORIZONTES_DIAS_HABILES,
    backtest_buy_the_dip,
    backtest_filtro_vix,
    fechas_disparo_caida,
    retornos_individuales_por_evento,
    retornos_post_caida_vs_promedio,
    sweep_buy_the_dip,
    sweep_filtro_vix,
)
from src.investment.fixed_return_projection import proyectar_rendimiento_fijo
from src.investment.historical_dca import (
    TICKERS_DISPONIBLES,
    obtener_serie_diaria,
    obtener_vix_diario,
    simular_dca_historico,
)
from src.investment.tiered_leverage_backtest import (
    TIER_NOMBRES,
    backtest_tiered_leverage,
    sweep_tiered_leverage,
)
from src.investment.fractional_allocation_backtest import (
    ConfigFraccion,
    backtest_fracciones_capital,
    generar_conclusion_barrido,
    sweep_fracciones_capital,
)
from src.investment import backtest_v2 as btv2
from src.investment.leveraged_simulation import (
    DEFAULT_EXPENSE_RATIO_ANUAL_PCT,
    DEFAULT_FINANCING_RATE_ANUAL_PCT,
    FECHA_INCEPTION_GLOBAL,
)
from src.investment.risk_analysis import mejores_y_peores_anios, peores_drawdowns
from src.pension.ivm import MONTO_MAXIMO_DEFAULT, MONTO_MINIMO_DEFAULT, calcular_pension_ivm
from src.pension.rop import proyectar_rop

# ----------------------------------------------------------
# CACHÉ DE CÓMPUTO — Streamlit re-ejecuta TODO el script en cada interacción
# (incluidas las pestañas que no están a la vista); sin esto, cada input
# re-simulaba décadas de datos y reintentaba el refresco de Yahoo Finance.
# Con el caché, una combinación de parámetros ya calculada responde al
# instante. max_entries acota la memoria del proceso (el servicio corre con
# MemoryMax en systemd); ttl deja refrescar precios durante el día.
# ----------------------------------------------------------
# hash_funcs: fechas_disparo_caida devuelve un DatetimeIndex, que st.cache_data
# no sabe hashear por sí solo cuando se pasa como argumento a un backtest
# cacheado; ConfigFraccion es un dataclass simple → repr determinístico.
# Los sweeps NO se cachean: reciben progreso_callback (no hasheable) y ya
# están detrás de botones "▶️ Ejecutar barrido" explícitos.
_CACHE_KW = dict(
    show_spinner=False, ttl=6 * 3600, max_entries=24,
    hash_funcs={
        pd.DatetimeIndex: lambda idx: idx.asi8.tobytes(),
        ConfigFraccion: repr,
    },
)
obtener_serie_diaria = st.cache_data(**_CACHE_KW)(obtener_serie_diaria)
obtener_vix_diario = st.cache_data(**_CACHE_KW)(obtener_vix_diario)
simular_dca_historico = st.cache_data(**_CACHE_KW)(simular_dca_historico)
fechas_disparo_caida = st.cache_data(**_CACHE_KW)(fechas_disparo_caida)
backtest_buy_the_dip = st.cache_data(**_CACHE_KW)(backtest_buy_the_dip)
backtest_filtro_vix = st.cache_data(**_CACHE_KW)(backtest_filtro_vix)
retornos_individuales_por_evento = st.cache_data(**_CACHE_KW)(retornos_individuales_por_evento)
retornos_post_caida_vs_promedio = st.cache_data(**_CACHE_KW)(retornos_post_caida_vs_promedio)
backtest_tiered_leverage = st.cache_data(**_CACHE_KW)(backtest_tiered_leverage)
backtest_fracciones_capital = st.cache_data(**_CACHE_KW)(backtest_fracciones_capital)
# backtest_v2 se consume vía el módulo (btv2.f(...)), así que se parchean los
# atributos del módulo; sus llamadas internas también aprovechan el caché.
btv2.analizar_caidas = st.cache_data(**_CACHE_KW)(btv2.analizar_caidas)
btv2.backtest_sma_trend = st.cache_data(**_CACHE_KW)(btv2.backtest_sma_trend)
btv2.tabla_sensibilidad_trend = st.cache_data(**_CACHE_KW)(btv2.tabla_sensibilidad_trend)
btv2.backtest_dual_momentum = st.cache_data(**_CACHE_KW)(btv2.backtest_dual_momentum)
btv2.backtest_senal_compuesta = st.cache_data(**_CACHE_KW)(btv2.backtest_senal_compuesta)
btv2.contribucion_señales = st.cache_data(**_CACHE_KW)(btv2.contribucion_señales)
btv2.episodios_para_grafico = st.cache_data(**_CACHE_KW)(btv2.episodios_para_grafico)

# ----------------------------------------------------------
# IDENTIDAD DE MARCA — Empowered Investor
# ----------------------------------------------------------
ASSETS_DIR = Path(__file__).parent / "assets"
LOGO_PATH = ASSETS_DIR / "logo.png"

BRAND_BG = "#14161c"
BRAND_BG_SOFT = "#1c1f27"
BRAND_TEXT = "#f5f1e8"
BRAND_GREEN = "#34d399"   # acento verde del logo (la "W")
BRAND_BLUE = "#3b5bdb"    # acento azul del logo (el "in")
BRAND_MUTED = "#9aa0a6"
PLOTLY_TEMPLATE = "plotly_dark"
PLOTLY_GRIDCOLOR = "rgba(245,241,232,0.10)"

st.set_page_config(
    page_title="Retiro Empoderado",
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else "💰",
    layout="wide",
)
if LOGO_PATH.exists():
    st.logo(str(LOGO_PATH), size="large")
    st.markdown(
        """
        <style>
        [data-testid="stLogo"] { height: 4.5rem !important; width: auto !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )

PLOTLY_CONFIG = {"displayModeBar": False, "scrollZoom": False}
PLOTLY_CONFIG_ZOOM = {"displayModeBar": False, "scrollZoom": True}

DISCLAIMER_V2 = (
    "Backtest hipotético con fines educativos. No se garantizan retornos. "
    "Los resultados pasados no garantizan resultados futuros."
)


def _edad_en_anio(anio: int, edad_actual: int, anio_actual: int) -> str:
    edad = edad_actual + (anio - anio_actual)
    return str(edad) if edad >= 0 else "–"


def _con_inflacion(valor_real: float, anios_transcurridos: float, inflacion_pct: float) -> float:
    """Convierte un monto REAL (poder de compra de hoy) a su equivalente NOMINAL ese año futuro."""
    return valor_real * (1.0 + inflacion_pct / 100.0) ** anios_transcurridos


def _ticks_anio_edad(
    anio_min: int, anio_max: int, edad_actual: int, anio_actual: int, max_ticks: int = 40
) -> tuple[list, list]:
    """
    Un tick por año calendario, con la edad del cliente debajo en la misma etiqueta.
    Con etiquetas cortas de 2 líneas (sin rotar) caben bastantes sin traslaparse — solo
    se espacian (cada 2, 3, 4... años) cuando el rango es tan largo que ya no entrarían.
    """
    rango = max(1, anio_max - anio_min)
    paso = max(1, -(-rango // max_ticks))  # división hacia arriba (ceil) sin importar
    tickvals = list(range(anio_min, anio_max + 1, paso))
    if tickvals[-1] != anio_max:
        tickvals.append(anio_max)
    ticktext = [f"{anio}<br>{_edad_en_anio(anio, edad_actual, anio_actual)}" for anio in tickvals]
    return tickvals, ticktext


def _eje_x_anio_edad(anio_min: int, anio_max: int, edad_actual: int, anio_actual: int) -> dict:
    tickvals, ticktext = _ticks_anio_edad(anio_min, anio_max, edad_actual, anio_actual)
    return dict(
        title="Año / Edad", tickmode="array", tickvals=tickvals, ticktext=ticktext,
        fixedrange=True, showgrid=True, gridcolor="rgba(0,0,0,0.08)",
    )


def _eje_x_anio_edad_fechas(anio_min: int, anio_max: int, edad_actual: int, anio_actual: int) -> dict:
    """Igual que _eje_x_anio_edad, pero para un eje X de fechas reales (datos con granularidad mensual)."""
    tickvals_anios, ticktext = _ticks_anio_edad(anio_min, anio_max, edad_actual, anio_actual)
    tickvals = [pd.Timestamp(year=a, month=1, day=1) for a in tickvals_anios]
    return dict(
        title="Año / Edad", tickmode="array", tickvals=tickvals, ticktext=ticktext,
        fixedrange=True, showgrid=True, gridcolor="rgba(0,0,0,0.08)",
    )


def _agregar_marcas_aportes(fig: go.Figure, x_aportes: list, y_referencia: list, max_marcas: int = 60) -> None:
    """
    Seña discreta (💰) arriba del gráfico, en cada momento en el que hubo un aporte real.
    Si hay demasiados aportes (p.ej. plan mensual por 30 años), se omiten — dejarían de ser
    discretos y se verían como una franja sólida, no como una señal puntual.
    """
    if not x_aportes or not y_referencia or len(x_aportes) > max_marcas:
        return
    y_top = max(y_referencia) * 1.06
    fig.add_trace(
        go.Scatter(
            x=x_aportes, y=[y_top] * len(x_aportes), mode="text", text=["💰"] * len(x_aportes),
            textfont=dict(size=10), showlegend=False, hoverinfo="skip",
        )
    )


def _agregar_marcadores_entrada_salida(
    fig: go.Figure, fechas_entrada: list, fechas_salida: list, precios_senal: pd.Series,
    row: int | None = None, col: int | None = None, max_marcas: int = 250,
) -> None:
    """
    Flecha verde (entrada) y flecha roja (salida) tocando la línea del activo de
    señal, en cada apertura/cierre real de posición — para ver de un vistazo qué
    tan profunda estaba la caída cuando la estrategia entró y dónde estaba la
    señal cuando salió.
    """
    idx = precios_senal.index

    def _alinear(fechas: list) -> list:
        out = []
        for f in fechas:
            pos = int(idx.searchsorted(f))
            if pos < len(idx):
                out.append(idx[pos])
        return out

    entradas = _alinear(fechas_entrada)
    salidas = _alinear(fechas_salida)

    if entradas and len(entradas) <= max_marcas:
        trace_in = go.Scatter(
            x=entradas, y=[precios_senal.loc[f] for f in entradas], mode="markers", name="Entrada",
            marker=dict(symbol="triangle-up", color=BRAND_GREEN, size=10, line=dict(width=1, color="#f5f1e8")),
            showlegend=False, hovertemplate="Entrada: %{x|%d %b %Y}<extra></extra>",
        )
        fig.add_trace(trace_in, row=row, col=col) if row is not None else fig.add_trace(trace_in)

    if salidas and len(salidas) <= max_marcas:
        trace_out = go.Scatter(
            x=salidas, y=[precios_senal.loc[f] for f in salidas], mode="markers", name="Salida",
            marker=dict(symbol="triangle-down", color="#e0525a", size=10, line=dict(width=1, color="#f5f1e8")),
            showlegend=False, hovertemplate="Salida: %{x|%d %b %Y}<extra></extra>",
        )
        fig.add_trace(trace_out, row=row, col=col) if row is not None else fig.add_trace(trace_out)


def _grafico_lineas(
    anios_x, series: dict, edad_actual: int, anio_actual: int, y_label: str = "$", anios_aporte: list | None = None
) -> go.Figure:
    """Línea(s) vs. año calendario, con un solo eje X (Año / Edad) y el valor + edad exactos al hacer hover."""
    fig = go.Figure()
    edades_x = [_edad_en_anio(a, edad_actual, anio_actual) for a in anios_x]
    todos_los_valores: list = []
    for nombre, (y, color, dash) in series.items():
        todos_los_valores.extend(y)
        fig.add_trace(
            go.Scatter(
                x=anios_x, y=y, mode="lines+markers", name=nombre,
                line=dict(color=color, width=2.5, dash=dash), marker=dict(size=5),
                customdata=edades_x,
                hovertemplate=f"Año %{{x}} (edad %{{customdata}})<br>{nombre}: %{{y:$,.0f}}<extra></extra>",
            )
        )
    _agregar_marcas_aportes(fig, anios_aporte or [], todos_los_valores)
    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
        dragmode="pan",
        xaxis=_eje_x_anio_edad(min(anios_x), max(anios_x), edad_actual, anio_actual),
        yaxis=dict(
            title=y_label, fixedrange=False, showgrid=True, gridcolor=PLOTLY_GRIDCOLOR,
            tickprefix="$", separatethousands=True, rangemode="tozero", minallowed=0,
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=10, r=10, t=40, b=10),
    )
    fig.update_xaxes(fixedrange=False)  # _eje_x_anio_edad trae fixedrange=True, hay que revertirlo
    return fig


def _tabla_peores_drawdowns(drawdowns: list[dict]) -> pd.DataFrame:
    """
    Convierte la lista de episodios de `peores_drawdowns` en una tabla lista
    para mostrar, con fecha del pico, del valle, de la recuperación y cuántos
    días pasó el precio "bajo el agua" (del pico hasta recuperar ese máximo).
    Todas las fechas se guardan como texto para que la columna sea de un solo
    tipo (evita el error de Arrow al mezclar fechas con el texto "Sin recuperar").
    """
    filas = []
    for d in drawdowns:
        fecha_rec = d.get("fecha_recuperacion")
        dias_rec = d.get("dias_recuperacion")
        filas.append(
            {
                "Pico": d["fecha_pico"].strftime("%Y-%m-%d"),
                "Valle": d["fecha_valle"].strftime("%Y-%m-%d"),
                "Caída (%)": d["drawdown_pct"],
                "Recuperación": fecha_rec.strftime("%Y-%m-%d") if fecha_rec is not None else "Sin recuperar",
                "Días bajo el agua (pico→recuperación)": float(dias_rec) if dias_rec is not None else float("nan"),
            }
        )
    return pd.DataFrame(filas)


def _formatear_dinero(valor: float, simbolo: str = "$") -> str:
    """Símbolo de moneda + separador de miles; solo muestra decimales si el valor realmente los tiene."""
    if valor == int(valor):
        return f"{simbolo}{valor:,.0f}"
    return f"{simbolo}{valor:,.2f}"


def _parsear_dinero(texto: str) -> float:
    limpio = "".join(c for c in texto if c.isdigit() or c in ".,-").replace(",", "").strip()
    if not limpio:
        return 0.0
    try:
        return max(0.0, float(limpio))
    except ValueError:
        return 0.0


def _on_change_dinero(key: str, simbolo: str = "$") -> None:
    st.session_state[key] = _formatear_dinero(_parsear_dinero(st.session_state[key]), simbolo)


def money_input(
    contenedor, label: str, key: str, value: float, help: str | None = None,
    simbolo: str = "$", en_form: bool = False,
) -> float:
    """Campo de dinero: muestra el símbolo antes del número, sin decimales por defecto (solo si el usuario los escribe).

    en_form=True para usarlo dentro de un st.form: ahí Streamlit prohíbe callbacks
    en widgets (solo el submit button puede tenerlos), así que el reformateo bonito
    se hace al enviar el formulario (ver _reformatear_dinero_hist)."""
    if key not in st.session_state:
        st.session_state[key] = _formatear_dinero(value, simbolo)
    if en_form:
        contenedor.text_input(label, key=key, help=help)
    else:
        contenedor.text_input(label, key=key, on_change=_on_change_dinero, args=(key, simbolo), help=help)
    return _parsear_dinero(st.session_state[key])


def _reformatear_dinero_hist() -> None:
    """Al enviar el form de la simulación histórica, deja los campos de dinero con formato."""
    for k in ("aporte_inicial_hist_txt", "aporte_periodico_hist_txt"):
        if k in st.session_state:
            st.session_state[k] = _formatear_dinero(_parsear_dinero(st.session_state[k]))


def colon_input(contenedor, label: str, key: str, value: float, help: str | None = None) -> float:
    return money_input(contenedor, label, key, value, help=help, simbolo="₡")


def _sumar_anios(fecha: date, anios: int) -> date:
    try:
        return fecha.replace(year=fecha.year + anios)
    except ValueError:
        # 29 de febrero cayendo en un año no bisiesto
        return fecha.replace(month=2, day=28, year=fecha.year + anios)


if LOGO_PATH.exists():
    _logo_b64 = base64.b64encode(LOGO_PATH.read_bytes()).decode()
    st.markdown(
        f"""
        <div style="display:flex;align-items:center;gap:1rem;margin-bottom:0.5rem;">
            <img src="data:image/png;base64,{_logo_b64}" style="height:72px;width:auto;" />
            <h1 style="margin:0;font-size:2.5rem;">Retiro Empoderado</h1>
        </div>
        """,
        unsafe_allow_html=True,
    )
else:
    st.title("💰 Retiro Empoderado")

# ==========================================================
# SIDEBAR — DATOS DEL CLIENTE, PLAN DE APORTES Y COSTOS (compartidos por ambas pestañas)
# ==========================================================
st.sidebar.header("👤 Datos personales")
edad_actual = st.sidebar.number_input("Edad hoy", min_value=1, max_value=100, value=35)
anio_actual = date.today().year

st.sidebar.header("📥 Plan de aportes")
aporte_inicial = money_input(st.sidebar, "Aporte inicial", "aporte_inicial_txt", 10_000.0)
aporte_periodico = money_input(st.sidebar, "Aporte periódico", "aporte_periodico_txt", 5_000.0)
frecuencia_aporte = st.sidebar.selectbox("Frecuencia del aporte", ["Mensual", "Trimestral", "Semestral", "Anual", "Cada 2 años", "Cada 3 años"], index=2)
st.sidebar.caption(
    "⚠️ Para que el monto de retiro sea real (que de verdad conserve poder de compra), "
    "el aporte periódico también debería ir subiendo cada año con la inflación, no quedarse fijo."
)
st.sidebar.caption(
    "💡 El objetivo es destinar un 15% de tus ingresos a inversión — pero solo después de tener un "
    "fondo de emergencia de 3 meses de ingresos y de haber eliminado deudas de alto interés "
    "(como tarjetas de crédito)."
)

st.sidebar.header("💼 Costos del servicio")
setup_fee = money_input(
    st.sidebar, "Fee de apertura (único) — incluye el manejo del 1er año", "setup_fee_txt", 1500.0,
    help="Se cobra una sola vez, al inicio, y se descuenta del aporte inicial. Cubre el manejo del "
    "portafolio durante el primer año; a partir del segundo año aplica el management fee de abajo.",
)
management_fee_anual_pct = st.sidebar.number_input(
    "Management fee anual — manejo del portafolio, del 2º año en adelante (%)", min_value=0.0, value=1.0, step=0.1,
    help="El costo anual de administrar tu portafolio, cobrado mes a mes sobre el saldo (proporcional: "
    "%/12 por mes). No se cobra el primer año, porque ese año ya lo cubre el fee de apertura de arriba.",
)
costo_swift = money_input(
    st.sidebar, "Costo por enviar dinero a la cuenta de inversión", "costo_swift_txt", 65.0,
    help="Lo que cuesta transferir el dinero al extranjero cada vez que aportás (una transferencia "
    "internacional, tipo SWIFT). ⚠️ No es un cobro nuestro: es lo que cobra el banco por mover la plata. "
    "Se descuenta de cada aporte (el inicial y cada periódico) antes de invertir.",
)

tab_pension, tab_teorica, tab_historica, tab_avanzado, tab_avanzado_v2 = st.tabs(
    [
        "🏛️ Pensión del Estado (IVM + ROP)",
        "📐 Proyección de Inversión con Empowered Investor",
        "📊 Simulación con Datos Históricos",
        "🧪 Backtesting Avanzado",
        "🔬 Backtesting Avanzado V2",
    ]
)

# ----------------------------------------------------------
# TAB 1 — PROYECCIÓN TEÓRICA CON RENDIMIENTO FIJO
# ----------------------------------------------------------
with tab_teorica:
    st.subheader("Supuestos de la proyección")
    c1, c2 = st.columns(2)
    anios = c1.number_input("Años de acumulación", min_value=1, max_value=60, value=20, key="anios_teorico")
    rendimiento_anual_pct = c2.number_input(
        "Rendimiento NOMINAL anual esperado (%)", min_value=0.0, value=8.0, step=0.5,
        format="%0.1f", key="rend_teorico",
        help="Este número es NOMINAL — el rendimiento 'bruto' que normalmente se anuncia, antes de "
        "descontar inflación. Con la inflación que pongas abajo, la calculadora obtiene el rendimiento "
        "REAL equivalente (nominal − inflación), que es el que de verdad importa: cuánto crece tu poder "
        "de compra cada año. Todos los cálculos de esta calculadora (metas, ingreso de retiro, etc.) usan "
        "ese rendimiento real.",
    )
    inflacion_pct = st.number_input(
        "Inflación anual esperada (%)", min_value=0.0, value=3.0, step=0.5, format="%0.1f",
        help="Cuánto suben los precios en promedio cada año en EE.UU. (si invertís en dólares). Se usa "
        "para convertir tu rendimiento nominal de arriba al rendimiento real (nominal − inflación), y "
        "para mostrarte el equivalente nominal de los montos en dólares de hoy.",
    )
    rendimiento_real_pct = rendimiento_anual_pct - inflacion_pct

    rr1, rr2, rr3 = st.columns(3)
    rr1.metric("Rendimiento nominal", f"{rendimiento_anual_pct:.1f}%", help="Lo que pusiste arriba: el rendimiento 'bruto' antes de inflación.")
    rr2.metric("− Inflación esperada", f"{inflacion_pct:.1f}%", help="Lo que pusiste arriba: cuánto suben los precios cada año.")
    rr3.metric(
        "= Rendimiento REAL anual", f"{rendimiento_real_pct:.1f}%",
        help="Rendimiento real = nominal − inflación. Es tu ganancia verdadera en poder de compra, y es "
        "el número que usa TODA esta calculadora: metas, ingreso de retiro y los gráficos (línea 'real').",
    )
    st.caption(
        f"🔢 **Real = nominal − inflación**: {rendimiento_anual_pct:.1f}% − {inflacion_pct:.1f}% = "
        f"**{rendimiento_real_pct:.1f}%** anual. Todos los montos de esta calculadora ya están en dólares "
        "de hoy, calculados con ese rendimiento real, comparables directamente con tu salario o gastos "
        "actuales. En los gráficos, la línea **real** (verde, gruesa) es la que importa; la **nominal** "
        "(punteada) es solo la cifra literal que verás en la cuenta, inflada."
    )

    st.caption(
        "📊 Referencia histórica de mercado: a muy largo plazo, el S&P 500 ha rendido aproximadamente "
        "6.5%–7% anual en términos **reales** (unos 9.5%–10% **nominal**, sumando ~3% de inflación "
        "histórica promedio); el Nasdaq-100 ha rendido aproximadamente 9%–11% real (unos 12%–14% "
        "nominal), con bastante más volatilidad en el camino. Son promedios de varias décadas — no "
        "garantizan lo que pase en tu horizonte específico, pero te dan una idea de qué tan realista es "
        "el número nominal que pusiste arriba. Nuestras estrategias sistemáticas buscan superar estos "
        "promedios; resultados anteriores no son garantía de resultados futuros, pero el crecimiento de "
        "capital es nuestra especialidad."
    )

    resultado = proyectar_rendimiento_fijo(
        anios=anios,
        rendimiento_anual_pct=rendimiento_real_pct,
        aporte_inicial=aporte_inicial,
        aporte_periodico=aporte_periodico,
        frecuencia=frecuencia_aporte,
        edad_actual=edad_actual,
        setup_fee=setup_fee,
        costo_swift=costo_swift,
        management_fee_anual_pct=management_fee_anual_pct,
        anio_actual=anio_actual,
        meses_sin_management=12,  # el 1er año lo cubre el fee de apertura
    )

    m1, m2, m3 = st.columns(3)
    m1.metric("Dinero Aportado", f"${resultado.aportado_bruto_total:,.0f}")
    m2.metric(
        "Valor Final", f"${resultado.valor_final:,.0f}",
        help="En poder de compra de HOY (dólares reales). Es lo que ese dinero te alcanzaría a comprar "
        "si lo tuvieras ahora mismo, no la cifra que literalmente aparecería en tu cuenta en el futuro.",
    )
    m3.metric("La inversión generó", f"${resultado.rendimiento_generado:,.0f}")

    valor_final_nominal = resultado.valor_final * (1.0 + inflacion_pct / 100.0) ** anios
    st.caption(
        f"💵 Dato curioso: en dólares **nominales** — lo que literalmente diría tu cuenta en el año "
        f"{anio_actual + anios}, sin descontar inflación — sería \\${valor_final_nominal:,.0f}, un número "
        f"más grande, pero que compra lo mismo que los \\${resultado.valor_final:,.0f} de hoy.\n\n"
        "A eso se refiere un monto **'real'**: un número que sí contempla la inflación. Es el que nos "
        "interesa. El **nominal** es solo una cifra que no representa lo que realmente vas a poder "
        "comprar en el futuro. El **real** es el que refleja lo que ese dinero compra hoy, en tu estilo "
        "de vida actual — por eso es el que usamos en toda esta calculadora. El gráfico de abajo muestra "
        "ambas líneas para que veas la diferencia de un vistazo."
    )
    st.caption(":green[Resultados incluyen los costos del servicio: fee de apertura (manejo del 1er año), management fee del 2º año en adelante, y el costo de enviar cada aporte a la cuenta de inversión]")

    anios_cal = [p.anio_calendario for p in resultado.puntos]
    aportado_serie = [p.aportado_bruto_cum for p in resultado.puntos]
    balance_real_serie = [p.balance for p in resultado.puntos]
    balance_nominal_serie = [_con_inflacion(p.balance, p.anio_index, inflacion_pct) for p in resultado.puntos]
    anios_con_aporte = [
        p.anio_calendario for p, p_prev in zip(resultado.puntos[1:], resultado.puntos[:-1])
        if p.aportado_bruto_cum > p_prev.aportado_bruto_cum
    ]

    vista_acumulacion = st.radio(
        "🔍 Ver el gráfico en términos:", ["Real (recomendado)", "Nominal"],
        horizontal=True, key="vista_acumulacion",
        help="**Real** = poder de compra de hoy — lo que ese dinero te alcanzaría a comprar ahora mismo; "
        "es la línea que de verdad importa para planificar, y la que usa el resto de la calculadora. "
        "**Nominal** = la cifra literal que va a aparecer en tu cuenta en el futuro, sin descontar "
        "inflación — un número más grande, pero que compra lo mismo. Las dos líneas siempre se muestran "
        "juntas; este botón solo decide cuál se resalta como la principal.",
    )
    es_vista_real_acum = vista_acumulacion.startswith("Real")
    if es_vista_real_acum:
        nombre_primaria, serie_primaria = "Valor de la cartera (real)", balance_real_serie
        nombre_secundaria, serie_secundaria = "Valor de la cartera (nominal)", balance_nominal_serie
    else:
        nombre_primaria, serie_primaria = "Valor de la cartera (nominal)", balance_nominal_serie
        nombre_secundaria, serie_secundaria = "Valor de la cartera (real)", balance_real_serie

    edades_x_acum = [_edad_en_anio(a, edad_actual, anio_actual) for a in anios_cal]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=anios_cal, y=aportado_serie, mode="lines+markers", name="Dinero aportado",
        line=dict(color=BRAND_MUTED, width=2.5, dash="dash"), marker=dict(size=5),
        customdata=edades_x_acum,
        hovertemplate="Año %{x} (edad %{customdata})<br>Dinero aportado: %{y:$,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=anios_cal, y=serie_secundaria, mode="lines+markers", name=nombre_secundaria,
        line=dict(color=BRAND_BLUE, width=1.5, dash="dot"), marker=dict(size=4),
        customdata=edades_x_acum,
        hovertemplate=f"Año %{{x}} (edad %{{customdata}})<br>{nombre_secundaria}: %{{y:$,.0f}}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=anios_cal, y=serie_primaria, mode="lines+markers", name=nombre_primaria,
        line=dict(color=BRAND_GREEN, width=3.5, dash="solid"), marker=dict(size=6),
        customdata=edades_x_acum,
        hovertemplate=f"Año %{{x}} (edad %{{customdata}})<br>{nombre_primaria}: %{{y:$,.0f}}<extra></extra>",
    ))
    _agregar_marcas_aportes(fig, anios_con_aporte, aportado_serie + balance_real_serie + balance_nominal_serie)
    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
        dragmode="pan",
        xaxis=_eje_x_anio_edad(min(anios_cal), max(anios_cal), edad_actual, anio_actual),
        yaxis=dict(
            title="$", fixedrange=False, showgrid=True, gridcolor=PLOTLY_GRIDCOLOR,
            tickprefix="$", separatethousands=True, rangemode="tozero", minallowed=0,
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=10, r=10, t=40, b=10),
    )
    fig.update_xaxes(fixedrange=False)
    st.plotly_chart(fig, config=PLOTLY_CONFIG_ZOOM, width="stretch")
    st.caption(
        f"💰 = año en el que hubo al menos un aporte periódico (\\${aporte_periodico:,.0f} c/u, según tu "
        "plan de aportes). El primer punto puede incluir además el aporte inicial. 🔍 Podés hacer scroll "
        "para zoom y clic + arrastrar para moverte en el tiempo."
    )

    st.divider()
    st.subheader("🎯 Meta de retiro (25 años de ingreso asegurado)")
    modo_ingreso = st.radio(
        "¿Cómo preferís poner tu ingreso actual?", ["Mensual", "Anual"], horizontal=True,
        help="En Costa Rica solemos pensar el salario por mes, no por año — elegí lo que te sea más fácil, "
        "el otro se calcula solo.",
    )
    if modo_ingreso == "Mensual":
        ingreso_mensual_actual = money_input(st, "Ingreso mensual actual", "ingreso_mensual_txt", 5_000.0)
        ingreso_anual_actual = ingreso_mensual_actual * 12.0
    else:
        ingreso_anual_actual = money_input(st, "Ingreso anual actual", "ingreso_anual_txt", 60_000.0)
        ingreso_mensual_actual = ingreso_anual_actual / 12.0

    meta_25x = ingreso_anual_actual * 25
    pct_meta = (resultado.valor_final / meta_25x * 100.0) if meta_25x > 0 else 0.0

    st.caption(
        f"🧮 Cómo se arma la meta: tu ingreso mensual (\\${ingreso_mensual_actual:,.0f}) **× 12** = tu "
        f"ingreso anual (\\${ingreso_anual_actual:,.0f}). Y ese ingreso anual **× 25** = tu meta de retiro "
        f"(\\${meta_25x:,.0f}). ¿Por qué 25 veces? Porque si cada año en el retiro solo sacás el 4% de esa "
        "meta, el dinero está calibrado para durar mucho tiempo sin acabarse — y 1 ÷ 4% = 25. Es la misma "
        "idea que la sección de 'método porcentual' de abajo, vista desde otro ángulo."
    )

    g1, g2, g3 = st.columns(3)
    g1.metric("Meta (25x el ingreso anual)", f"${meta_25x:,.0f}")
    g2.metric("Valor final proyectado", f"${resultado.valor_final:,.0f}")
    g3.metric("% de la meta alcanzado", f"{pct_meta:,.0f}%")

    st.divider()
    st.subheader("💵 Ingreso mensual con método porcentual")
    tasa_retiro_pct = st.slider(
        "Tasa de retiro anual REAL (%)", 1.0, 10.0, 4.0, 0.5,
        help="Cuánto retiras del portafolio cada año, como % de su valor. Es una tasa REAL: el monto que "
        "sacás se mantiene constante en poder de compra (se ajusta por inflación cada año para que "
        "siempre te compre lo mismo). 4% es la regla clásica ('regla del 4%') de planificación de retiro. "
        "OJO: para que el portafolio no se agote, esta tasa se compara contra el crecimiento REAL (no el "
        "nominal) — lo vas a ver en la sección de abajo.",
    )

    ingreso_mensual_real = resultado.valor_final * tasa_retiro_pct / 100.0 / 12.0
    ingreso_anual_real = ingreso_mensual_real * 12.0
    ingreso_mensual_nominal_retiro = ingreso_mensual_real * (1.0 + inflacion_pct / 100.0) ** anios

    i1, i2 = st.columns(2)
    i1.metric(
        f"Ingreso mensual con retiro del {tasa_retiro_pct:.1f}%", f"${ingreso_mensual_real:,.0f}",
        help="En poder de compra de hoy — igual que 'Valor Final' arriba, esto ya está ajustado por "
        "inflación, así que se puede comparar directo con tus gastos actuales.",
    )
    i2.metric("Ingreso anual equivalente", f"${ingreso_anual_real:,.0f}")
    st.caption(
        f":green[✅ Este ingreso mensual (\\${ingreso_mensual_real:,.0f}) es el número que se suma a tu "
        "monto de retiro mensual total en la pestaña 'Pensión del Estado'. Si cambiás cualquier dato de "
        "esta sección (o de la barra lateral), ese total también cambia.]"
    )
    st.caption(
        f"💡 Esos \\${ingreso_mensual_real:,.0f}/mes están en poder de compra de **hoy**. En dólares "
        f"nominales del año {anio_actual + anios} (lo que literalmente vas a ver depositado, sin ajustar "
        f"por inflación), sería un número más alto: **\\${ingreso_mensual_nominal_retiro:,.0f}/mes** — "
        "pero compra exactamente lo mismo. Usamos siempre la cifra real para poder sumarla, más abajo en "
        "la pestaña de Pensión, con tu IVM y ROP (que también están en colones de hoy)."
    )

    st.divider()
    st.subheader("📉 ¿Cuántos años resiste el portafolio en el retiro?")
    st.caption(
        "Simulación en términos **reales** (poder de compra de hoy): cada año el portafolio crece a su "
        f"tasa **real** y, al mismo tiempo, se retira un monto **constante en poder de compra** (el "
        f"{tasa_retiro_pct:.1f}% del valor inicial del retiro, calculado arriba). Como todo está en la "
        "misma moneda —dólares de hoy—, la comparación justa es tasa de retiro **real** vs. crecimiento "
        "**real**."
    )
    c1, c2 = st.columns(2)
    crecimiento_retiro_pct = c1.slider(
        "Crecimiento NOMINAL anual asumido durante el retiro (%)", 0.0, 15.0, 7.0, 0.5,
        help="Igual que el rendimiento de arriba: este número es NOMINAL. Con la inflación esperada, la "
        "calculadora obtiene el crecimiento real equivalente, que es el que usa la simulación (el saldo y "
        "el retiro anual están en dólares de hoy).",
    )
    horizonte_anios = c2.number_input("Horizonte a simular (años)", min_value=10, max_value=80, value=50, step=5)
    crecimiento_retiro_real_pct = crecimiento_retiro_pct - inflacion_pct
    break_even_nominal_pct = tasa_retiro_pct + inflacion_pct

    gr1, gr2, gr3 = st.columns(3)
    gr1.metric("Crecimiento nominal", f"{crecimiento_retiro_pct:.1f}%")
    gr2.metric("− Inflación esperada", f"{inflacion_pct:.1f}%")
    gr3.metric(
        "= Crecimiento REAL", f"{crecimiento_retiro_real_pct:.1f}%",
        help="Crecimiento real = nominal − inflación. Es este número el que se compara contra la tasa de "
        "retiro para saber si el portafolio aguanta.",
    )
    if crecimiento_retiro_real_pct > tasa_retiro_pct + 1e-9:
        st.caption(
            f"⚖️ **Regla del punto de equilibrio:** para que el portafolio se mantenga **estático** (ni "
            f"crece ni se agota, en poder de compra), el crecimiento **real** debe igualar la tasa de "
            f"retiro real ({tasa_retiro_pct:.1f}%) — o, en términos nominales, el crecimiento nominal debe "
            f"ser al menos **tasa de retiro + inflación = {break_even_nominal_pct:.1f}%**. Ahora mismo tu "
            f"crecimiento real ({crecimiento_retiro_real_pct:.1f}%) **supera** la tasa de retiro, así que el "
            "portafolio crece."
        )
    elif abs(crecimiento_retiro_real_pct - tasa_retiro_pct) <= 1e-9:
        st.caption(
            f"⚖️ **Punto de equilibrio exacto:** tu crecimiento real ({crecimiento_retiro_real_pct:.1f}%) "
            f"iguala la tasa de retiro real ({tasa_retiro_pct:.1f}%), así que el portafolio se mantiene "
            f"estático (nominalmente equivale a un crecimiento de tasa de retiro + inflación = "
            f"{break_even_nominal_pct:.1f}%)."
        )
    else:
        st.caption(
            f"⚖️ **Por qué se agota aunque los números 'parezcan' iguales:** retirás un "
            f"{tasa_retiro_pct:.1f}% real, pero tu crecimiento **real** es solo {crecimiento_retiro_real_pct:.1f}% "
            f"(porque el {crecimiento_retiro_pct:.1f}% nominal pierde {inflacion_pct:.1f}% por inflación). "
            f"Como {crecimiento_retiro_real_pct:.1f}% < {tasa_retiro_pct:.1f}%, el poder de compra del "
            f"portafolio baja cada año. Para que se mantenga estático necesitarías un crecimiento **nominal** "
            f"de al menos **tasa de retiro + inflación = {break_even_nominal_pct:.1f}%** (o bajar la tasa de "
            f"retiro a {crecimiento_retiro_real_pct:.1f}% real)."
        )

    anio_retiro = anios_cal[-1]
    edad_retiro = edad_actual + anios
    decum = simular_decumulacion(
        valor_inicial=resultado.valor_final,
        tasa_retiro_anual_pct=tasa_retiro_pct,
        crecimiento_anual_pct=crecimiento_retiro_real_pct,
        edad_inicio=edad_retiro,
        anio_calendario_inicio=anio_retiro,
        horizonte_anios=horizonte_anios,
    )

    anios_decum = [p.anio_calendario for p in decum.puntos]
    balance_decum_real = [p.balance for p in decum.puntos]
    balance_decum_nominal = [
        _con_inflacion(p.balance, p.anio_calendario - anio_actual, inflacion_pct) for p in decum.puntos
    ]

    vista_decum = st.radio(
        "🔍 Ver el gráfico en términos:", ["Real (recomendado)", "Nominal"],
        horizontal=True, key="vista_decum",
        help="Misma idea que en el gráfico de arriba: **real** es lo que ese saldo compra hoy; **nominal** "
        "es la cifra literal que va a aparecer en la cuenta ese año futuro.",
    )
    es_vista_real_decum = vista_decum.startswith("Real")
    if es_vista_real_decum:
        nombre_primaria_decum, serie_primaria_decum = "Saldo del portafolio (real)", balance_decum_real
        nombre_secundaria_decum, serie_secundaria_decum = "Saldo del portafolio (nominal)", balance_decum_nominal
    else:
        nombre_primaria_decum, serie_primaria_decum = "Saldo del portafolio (nominal)", balance_decum_nominal
        nombre_secundaria_decum, serie_secundaria_decum = "Saldo del portafolio (real)", balance_decum_real

    edades_x_decum = [_edad_en_anio(a, edad_actual, anio_actual) for a in anios_decum]
    fig_decum = go.Figure()
    fig_decum.add_trace(go.Scatter(
        x=anios_decum, y=serie_secundaria_decum, mode="lines+markers", name=nombre_secundaria_decum,
        line=dict(color=BRAND_BLUE, width=1.5, dash="dot"), marker=dict(size=4),
        customdata=edades_x_decum,
        hovertemplate=f"Año %{{x}} (edad %{{customdata}})<br>{nombre_secundaria_decum}: %{{y:$,.0f}}<extra></extra>",
    ))
    fig_decum.add_trace(go.Scatter(
        x=anios_decum, y=serie_primaria_decum, mode="lines+markers", name=nombre_primaria_decum,
        line=dict(color="#d62728", width=3.5, dash="solid"), marker=dict(size=6),
        customdata=edades_x_decum,
        hovertemplate=f"Año %{{x}} (edad %{{customdata}})<br>{nombre_primaria_decum}: %{{y:$,.0f}}<extra></extra>",
    ))
    fig_decum.update_layout(
        template=PLOTLY_TEMPLATE,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
        dragmode="pan",
        xaxis=_eje_x_anio_edad(min(anios_decum), max(anios_decum), edad_actual, anio_actual),
        yaxis=dict(
            title="$", fixedrange=False, showgrid=True, gridcolor=PLOTLY_GRIDCOLOR,
            tickprefix="$", separatethousands=True, rangemode="tozero", minallowed=0,
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=10, r=10, t=40, b=10),
    )
    fig_decum.update_xaxes(fixedrange=False)
    st.plotly_chart(fig_decum, config=PLOTLY_CONFIG_ZOOM, width="stretch")

    if decum.se_agota:
        st.warning(
            f"⚠️ Con un retiro de \\${decum.retiro_anual:,.0f}/año y un crecimiento asumido del "
            f"{crecimiento_retiro_pct:.1f}% nominal ({crecimiento_retiro_real_pct:.1f}% real), el portafolio "
            f"se agotaría en **{decum.anio_agotamiento} años** (a los {edad_retiro + decum.anio_agotamiento} "
            f"años de edad), porque la tasa de retiro ({tasa_retiro_pct:.1f}%) supera el crecimiento real "
            f"asumido ({crecimiento_retiro_real_pct:.1f}%). Para que no se agotara, necesitarías un "
            f"crecimiento nominal de al menos **{break_even_nominal_pct:.1f}%** (tasa de retiro + inflación)."
        )
    elif decum.tendencia == "crece":
        st.success(
            f"✅ Con un retiro de \\${decum.retiro_anual:,.0f}/año y un crecimiento asumido del "
            f"{crecimiento_retiro_pct:.1f}% nominal ({crecimiento_retiro_real_pct:.1f}% real), el portafolio "
            f"**nunca se agota** — de hecho sigue creciendo, porque el crecimiento real "
            f"({crecimiento_retiro_real_pct:.1f}%) supera la tasa de retiro ({tasa_retiro_pct:.1f}%)."
        )
    elif decum.tendencia == "estable":
        st.info(
            f"➡️ Con un retiro de \\${decum.retiro_anual:,.0f}/año igual al crecimiento real asumido "
            f"({crecimiento_retiro_real_pct:.1f}%), el portafolio **se mantiene estable**: ni crece ni se "
            f"agota, se queda en \\${resultado.valor_final:,.0f} indefinidamente."
        )
    else:  # decrece, pero no llegó a $0 dentro del horizonte simulado
        st.warning(
            f"⚠️ Con un retiro de \\${decum.retiro_anual:,.0f}/año, el portafolio **va en descenso** (la tasa "
            f"de retiro de {tasa_retiro_pct:.1f}% supera el crecimiento real asumido de "
            f"{crecimiento_retiro_real_pct:.1f}%) y no alcanza a agotarse dentro de los {horizonte_anios} años "
            f"simulados, pero la tendencia es a la baja — se agotaría en algún año más allá de ese horizonte. "
            f"Prueba un horizonte más largo para ver el año exacto."
        )

# ----------------------------------------------------------
# TAB 2 — SIMULACIÓN CON DATOS HISTÓRICOS
# ----------------------------------------------------------
with tab_historica:
    st.subheader("¿Cómo hubiera sido con datos históricos reales?")
    st.caption("Precios reales de Yahoo Finance (splits y dividendos incluidos), cacheados localmente.")

    st.divider()
    st.markdown("##### 📥 Plan de aportes para esta simulación")
    st.caption(
        "Estos datos son independientes de la pestaña 'Proyección de Inversión con Empowered Investor' "
        "— podés usar un plan distinto para ver cómo le hubiera ido en el pasado. Los costos del "
        "servicio (fee de apertura, management fee y el costo de enviar cada aporte) sí se toman del "
        "panel de la izquierda, compartidos con las demás pestañas, tal como estén al momento de "
        "recalcular. Ajustá todo con calma: **nada se recalcula hasta que presionés 🔄 Recalcular "
        "simulación**."
    )
    with st.form("form_simulacion_historica"):
        with st.expander("ℹ️ Detalles técnicos"):
            st.caption(
                "TQQQ (desde 2010) y QLD (desde 2006) no existían en años más antiguos. Antes de su fecha "
                "real de listado, su precio se simula con la fórmula típica de un ETF apalancado sobre el "
                "retorno diario de QQQ, anclada para calzar exactamente con su primer precio real. QQQ en sí "
                "(listado real desde 1999-03-10) se puede extender hacia atrás hasta 1985 usando el índice "
                "Nasdaq-100 (^NDX) como proxy — un índice de precio que no incluye dividendos, así que ese "
                "tramo simulado subestima levemente el retorno total real."
            )
            c1, c2 = st.columns(2)
            expense_ratio_anual_pct = c1.number_input(
                "Gasto anual del fondo apalancado (%)", 0.0, 5.0, DEFAULT_EXPENSE_RATIO_ANUAL_PCT, 0.05
            )
            financing_rate_anual_pct = c2.number_input(
                "Costo de financiamiento anual (%)", 0.0, 15.0, DEFAULT_FINANCING_RATE_ANUAL_PCT, 0.25
            )

        c1, c2 = st.columns(2)
        edad_actual_hist = c1.number_input("Edad hoy", min_value=1, max_value=100, value=35, key="edad_hist")
        frecuencia_aporte_hist = c2.selectbox(
            "Frecuencia del aporte", ["Mensual", "Trimestral", "Semestral", "Anual", "Cada 2 años", "Cada 3 años"], index=2, key="frecuencia_hist"
        )
        c1, c2 = st.columns(2)
        aporte_inicial_hist = money_input(c1, "Aporte inicial", "aporte_inicial_hist_txt", 10_000.0, en_form=True)
        aporte_periodico_hist = money_input(c2, "Aporte periódico", "aporte_periodico_hist_txt", 5_000.0, en_form=True)

        c1, c2 = st.columns(2)
        fecha_inicio_hist = c1.date_input(
            "Fecha de inicio de la simulación", value=FECHA_INCEPTION_GLOBAL,
            min_value=date(1985, 10, 1), max_value=date.today() - timedelta(days=31),
            help="Desde cuándo empezar a simular los aportes. Cada ticker igual muestra datos reales solo "
            "desde su propia fecha de listado (p.ej. TQQQ arranca hasta 2010, QLD hasta 2006). Si elegís "
            "una fecha antes del 1999-03-10 para **QQQ**, el tramo previo se simula sobre el índice "
            "Nasdaq-100 (disponible desde 1985) — así podés capturar la subida y caída de la burbuja "
            "puntocom completa.",
        )
        anios_max_disponibles_hist = max(1, date.today().year - fecha_inicio_hist.year)
        anios_aportes_hist = c2.number_input(
            "Años de aportes a simular", min_value=1, max_value=anios_max_disponibles_hist,
            value=anios_max_disponibles_hist, step=1,
            help="Cuántos años, desde la fecha de inicio, se simulan los aportes. Por defecto llega hasta hoy.",
        )

        tickers_elegidos = st.multiselect(
            "Tickers a simular", TICKERS_DISPONIBLES, default=["QQQ", "SPY", "TQQQ", "QLD"]
        )

        recalcular_hist = st.form_submit_button(
            "🔄 Recalcular simulación", type="primary", on_click=_reformatear_dinero_hist
        )

    fecha_fin_hist = min(date.today(), _sumar_anios(fecha_inicio_hist, anios_aportes_hist))

    # "Foto" de parámetros: solo se actualiza al presionar el botón. Incluye los
    # costos del sidebar para que editar el panel izquierdo tampoco dispare la
    # simulación pesada — todo se aplica junto en el próximo recálculo.
    if recalcular_hist:
        st.session_state["params_hist"] = {
            "edad": edad_actual_hist,
            "frecuencia": frecuencia_aporte_hist,
            "aporte_inicial": aporte_inicial_hist,
            "aporte_periodico": aporte_periodico_hist,
            "fecha_inicio": fecha_inicio_hist,
            "fecha_fin": fecha_fin_hist,
            "tickers": list(tickers_elegidos),
            "expense_ratio": expense_ratio_anual_pct,
            "financing_rate": financing_rate_anual_pct,
            "setup_fee": setup_fee,
            "costo_swift": costo_swift,
            "management_fee": management_fee_anual_pct,
        }
    params_hist = st.session_state.get("params_hist")

    if params_hist is None:
        st.info("👆 Configurá tu plan de aportes y presioná **🔄 Recalcular simulación** para correr la simulación histórica.")
    elif not params_hist["tickers"]:
        st.warning("Elige al menos un ticker y volvé a presionar 🔄 Recalcular simulación.")
    else:
        # Todo lo de aquí para abajo usa la foto del último recálculo, no los
        # valores "en vivo" de los widgets. (Las pestañas avanzadas, que
        # comparten estos nombres, también quedan ancladas a la foto.)
        edad_actual_hist = params_hist["edad"]
        frecuencia_aporte_hist = params_hist["frecuencia"]
        aporte_inicial_hist = params_hist["aporte_inicial"]
        aporte_periodico_hist = params_hist["aporte_periodico"]
        fecha_inicio_hist = params_hist["fecha_inicio"]
        fecha_fin_hist = params_hist["fecha_fin"]
        tickers_elegidos = params_hist["tickers"]
        expense_ratio_anual_pct = params_hist["expense_ratio"]
        financing_rate_anual_pct = params_hist["financing_rate"]
        setup_fee = params_hist["setup_fee"]
        costo_swift = params_hist["costo_swift"]
        management_fee_anual_pct = params_hist["management_fee"]
        try:
            with st.spinner("Descargando / leyendo precios históricos de Yahoo Finance..."):
                resultados = simular_dca_historico(
                    tickers=tickers_elegidos,
                    fecha_inicio=fecha_inicio_hist,
                    fecha_fin=fecha_fin_hist,
                    aporte_inicial=aporte_inicial_hist,
                    aporte_periodico=aporte_periodico_hist,
                    frecuencia=frecuencia_aporte_hist,
                    setup_fee=setup_fee,
                    costo_swift=costo_swift,
                    management_fee_anual_pct=management_fee_anual_pct,
                    expense_ratio_anual_pct=expense_ratio_anual_pct,
                    financing_rate_anual_pct=financing_rate_anual_pct,
                    meses_sin_management=12,  # el 1er año lo cubre el fee de apertura
                )
        except Exception as e:
            resultados = {}
            st.error(f"Error consultando Yahoo Finance: {e}")

        if not resultados:
            st.warning("Yahoo Finance no devolvió datos para los tickers elegidos.")
        else:
            def _cagr_activo(r) -> float:
                """CAGR del precio del ticker solo (sin aportes) entre su primer y último dato."""
                precios = r.serie_precio
                if len(precios) < 2:
                    return 0.0
                p0, p1 = float(precios.iloc[0]), float(precios.iloc[-1])
                anios_p = (precios.index[-1] - precios.index[0]).days / 365.25
                if p0 <= 0 or anios_p <= 0:
                    return 0.0
                return (p1 / p0) ** (1.0 / anios_p) - 1.0

            def _tasa_real(tasa_nominal: float) -> float:
                """Fisher: tasa real aproximada a partir de una tasa nominal y la inflación asumida."""
                return (1.0 + tasa_nominal) / (1.0 + inflacion_pct / 100.0) - 1.0

            # Ancla del "real": el poder de compra del INICIO de la simulación. Deflactamos cada
            # flujo hacia atrás por la inflación, así que los montos reales quedan MENORES que los
            # nominales (la inflación encoge el valor del dinero con el tiempo) — como uno espera.
            base_real = fecha_inicio_hist
            factor_infl = 1.0 + inflacion_pct / 100.0
            filas_nominal = []
            filas_real = []
            for r in resultados.values():
                xirr_nominal = r.retorno_anualizado_pct
                cagr_nominal = _cagr_activo(r)

                aportado_real = 0.0
                for i, fecha_aporte in enumerate(r.fechas_aportes):
                    # El primer registro solo corresponde al aporte inicial si ese aporte
                    # existió (monto > 0); si no, incluso el primer registro es periódico.
                    monto = aporte_inicial_hist if (i == 0 and aporte_inicial_hist > 0) else aporte_periodico_hist
                    anios_desde_base = max(0.0, (fecha_aporte.date() - base_real).days / 365.25)
                    aportado_real += monto / (factor_infl ** anios_desde_base)

                anios_final_desde_base = max(0.0, (r.fecha_fin_real.date() - base_real).days / 365.25)
                valor_final_real = r.valor_final / (factor_infl ** anios_final_desde_base)

                filas_nominal.append(
                    {
                        "Ticker": r.ticker,
                        "Dinero Aportado ($)": r.aportado_bruto_total,
                        "Valor Final ($)": r.valor_final,
                        "La inversión generó ($)": r.rendimiento_generado,
                        "Retorno anualizado de tus aportes — XIRR (%)": xirr_nominal * 100,
                        "CAGR del activo, sin aportes (%)": cagr_nominal * 100,
                        "Max drawdown (%)": r.max_drawdown_pct * 100,
                    }
                )
                filas_real.append(
                    {
                        "Ticker": r.ticker,
                        "Dinero Aportado real ($)": aportado_real,
                        "Valor Final real ($)": valor_final_real,
                        "La inversión generó real ($)": valor_final_real - aportado_real,
                        "Retorno anualizado de tus aportes — XIRR real (%)": _tasa_real(xirr_nominal) * 100,
                        "CAGR del activo real, sin aportes (%)": _tasa_real(cagr_nominal) * 100,
                        "Max drawdown (%)": r.max_drawdown_pct * 100,
                    }
                )

            resumen_real = pd.DataFrame(filas_real).sort_values(
                "Valor Final real ($)", ascending=False
            ).reset_index(drop=True)
            resumen = pd.DataFrame(filas_nominal).sort_values(
                "Valor Final ($)", ascending=False
            ).reset_index(drop=True)

            st.caption(
                f"💡 Igual que en la pestaña 'Proyección de Inversión', acá también distinguimos **nominal** "
                f"vs. **real**. **Nominal** = los dólares literales de cada fecha (lo que pusiste y lo que "
                f"llegó a valer, sin ajustar). **Real** = todo expresado en **dólares constantes del inicio "
                f"de la simulación ({fecha_inicio_hist.year})**, quitándole el efecto de la inflación "
                f"({inflacion_pct:.1f}% anual, el mismo supuesto de la otra pestaña). Como la inflación hace "
                "que el dinero valga menos con el tiempo, los montos **reales son menores que los "
                "nominales** — y esa diferencia es justo lo que la inflación se 'come'. El **real es el que "
                "importa** para saber cuánto creció de verdad tu poder de compra; usalo como la vista por "
                "defecto."
            )
            vista_tabla_hist = st.radio(
                "🔍 Ver la tabla en términos:", ["Real (recomendado)", "Nominal"],
                horizontal=True, key="vista_tabla_hist",
            )
            if vista_tabla_hist.startswith("Real"):
                st.dataframe(
                    resumen_real.style.format(
                        {
                            "Dinero Aportado real ($)": "{:,.0f}",
                            "Valor Final real ($)": "{:,.0f}",
                            "La inversión generó real ($)": "{:,.0f}",
                            "Retorno anualizado de tus aportes — XIRR real (%)": "{:.2f}%",
                            "CAGR del activo real, sin aportes (%)": "{:.2f}%",
                            "Max drawdown (%)": "{:.2f}%",
                        }
                    ),
                    width="stretch",
                )
            else:
                st.dataframe(
                    resumen.style.format(
                        {
                            "Dinero Aportado ($)": "{:,.0f}",
                            "Valor Final ($)": "{:,.0f}",
                            "La inversión generó ($)": "{:,.0f}",
                            "Retorno anualizado de tus aportes — XIRR (%)": "{:.2f}%",
                            "CAGR del activo, sin aportes (%)": "{:.2f}%",
                            "Max drawdown (%)": "{:.2f}%",
                        }
                    ),
                    width="stretch",
                )
            st.caption(
                f"Vista **{'real — dólares constantes del inicio (' + str(fecha_inicio_hist.year) + ')' if vista_tabla_hist.startswith('Real') else 'nominal — dólares de cada fecha'}**. "
                "La conversión a real usa una tasa fija de inflación (no una serie histórica real), "
                "deflactando cada flujo desde su fecha hasta el inicio de la simulación. Costos del servicio "
                f"incluidos: \\${setup_fee:,.0f} de apertura (cubre el manejo del 1er año) + "
                f"\\${costo_swift:,.0f} por enviar cada aporte; a partir del 2º año, el management fee "
                f"({management_fee_anual_pct:.1f}% anual) también ya está reflejado.\n\n"
                "**XIRR** (\"Extended Internal Rate of Return\"): la tasa anual que, aplicada a cada uno de "
                "tus aportes según su fecha y monto exactos, hace que el valor presente de todos ellos "
                "cuadre exactamente con el valor final — o sea, tu retorno como inversionista, tomando en "
                "cuenta que no todo tu dinero estuvo invertido desde el día uno. El **CAGR del activo** es "
                "distinto: es el crecimiento anual del precio del ticker solo, sin importar cuándo aportaste "
                "— sirve para comparar qué tan bien se portó el activo en sí, separado de tu plan de "
                "aportes. En la vista **real**, ambas tasas ya restan el efecto de la inflación asumida (vía "
                "la ecuación de Fisher); en la vista **nominal** son las tasas brutas, antes de inflación."
            )

            st.markdown("**Evolución del saldo por ticker**")
            anio_min_hist = min(r.serie_balance.index.year.min() for r in resultados.values())
            anio_max_hist = max(r.serie_balance.index.year.max() for r in resultados.values())

            fig_hist = go.Figure()

            # El mejor "Valor Final real" del resumen (ya ordenado descendente) se resalta en el
            # color de marca; el resto usa una paleta neutra para no competir visualmente.
            mejor_ticker_hist = resumen_real.iloc[0]["Ticker"]
            paleta_otros = [BRAND_BLUE, "#a78bfa", "#c2856b", "#7c8591"]

            # Referencia siempre visible: el dinero aportado (neto de costos) del ticker con el
            # historial más largo entre los elegidos, para que se pueda comparar de un vistazo.
            ticker_referencia = min(resultados.values(), key=lambda r: r.fecha_inicio_real)
            edades_aportado = [
                _edad_en_anio(f.year, edad_actual_hist, anio_actual)
                for f in ticker_referencia.serie_invertido_neto.index
            ]
            fig_hist.add_trace(
                go.Scatter(
                    x=ticker_referencia.serie_invertido_neto.index,
                    y=ticker_referencia.serie_invertido_neto.values,
                    mode="lines",
                    name="Dinero aportado (neto de costos)",
                    line=dict(color=BRAND_MUTED, dash="dash", width=2),
                    customdata=edades_aportado,
                    hovertemplate="%{x|%b %Y} (edad %{customdata})<br>Aportado neto: %{y:$,.0f}<extra></extra>",
                )
            )

            todos_los_valores_hist: list = list(ticker_referencia.serie_invertido_neto.values)
            idx_otros = 0
            for r in resultados.values():
                edades_x = [_edad_en_anio(f.year, edad_actual_hist, anio_actual) for f in r.serie_balance.index]
                todos_los_valores_hist.extend(r.serie_balance.values)
                if r.ticker == mejor_ticker_hist:
                    color_linea, ancho_linea = BRAND_GREEN, 3.5
                else:
                    color_linea, ancho_linea = paleta_otros[idx_otros % len(paleta_otros)], 2
                    idx_otros += 1
                fig_hist.add_trace(
                    go.Scatter(
                        x=r.serie_balance.index,
                        y=r.serie_balance.values,
                        mode="lines",
                        name=r.ticker,
                        line=dict(color=color_linea, width=ancho_linea),
                        customdata=edades_x,
                        hovertemplate=f"%{{x|%b %Y}} (edad %{{customdata}})<br>{r.ticker}: %{{y:$,.0f}}<extra></extra>",
                    )
                )
            _agregar_marcas_aportes(fig_hist, ticker_referencia.fechas_aportes, todos_los_valores_hist)
            fig_hist.update_layout(
                template=PLOTLY_TEMPLATE,
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                hovermode="x unified",
                dragmode="pan",
                xaxis=_eje_x_anio_edad_fechas(int(anio_min_hist), int(anio_max_hist), edad_actual_hist, anio_actual),
                yaxis=dict(
                    title="$", fixedrange=False, showgrid=True, gridcolor=PLOTLY_GRIDCOLOR,
                    tickprefix="$", separatethousands=True, rangemode="tozero", minallowed=0,
                ),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                margin=dict(l=10, r=10, t=40, b=10),
            )
            fig_hist.update_xaxes(fixedrange=False)
            st.plotly_chart(fig_hist, config=PLOTLY_CONFIG_ZOOM, width="stretch")
            if 0 < len(ticker_referencia.fechas_aportes) <= 60:
                st.caption(
                    f"💰 = mes en el que hubo un aporte (\\${aporte_periodico_hist:,.0f} c/u, según tu aporte "
                    "periódico; el primer punto puede incluir además el aporte inicial). Con aportes muy "
                    "frecuentes (mensual/trimestral) no se marcan, para no saturar el gráfico."
                )

            st.divider()
            st.markdown("**📉 Riesgo histórico por ticker**")
            st.caption(
                "Basado en el precio del ticker (sin el efecto de cuándo se aportó): los peores "
                "derrumbes de un máximo a un mínimo, y los mejores/peores años calendario, con fechas. "
                "Un drawdown se cuenta como un solo episodio hasta que el precio alcanza un nuevo máximo "
                "histórico — por eso una caída larga (p.ej. 2000–2014 en QQQ) puede incluir varias crisis "
                "de por medio sin que cada una aparezca como una fila separada; se reporta el punto más "
                "profundo de todo ese tramo."
            )
            st.caption(
                "🕳️ La columna **Días bajo el agua (pico→recuperación)** cuenta desde el máximo previo a la "
                "caída hasta el día en que el precio recupera **ese mismo máximo** — es decir, cuánto "
                "tiempo real estuviste en pérdida antes de volver a estar completo. Si dice **'Sin "
                "recuperar'**, el precio todavía no había vuelto a ese máximo al final del período — sigue "
                "bajo el agua. Estas caídas son sobre el **precio del activo** (nominal); un drawdown es un "
                "cociente pico/valle, así que se ve prácticamente igual en términos nominales o reales."
            )
            ticker_riesgo = st.selectbox("Ticker a analizar", list(resultados.keys()), key="ticker_riesgo_hist")
            r_riesgo = resultados[ticker_riesgo]
            drawdowns_riesgo = peores_drawdowns(r_riesgo.serie_precio, top_n=10)
            mejores_anios_riesgo, peores_anios_riesgo = mejores_y_peores_anios(r_riesgo.serie_precio, top_n=10)

            st.caption("10 peores drawdowns (con recuperación)")
            if drawdowns_riesgo:
                df_dd = _tabla_peores_drawdowns(drawdowns_riesgo)
                st.dataframe(
                    df_dd.style.format(
                        {"Caída (%)": "{:.1f}%", "Días bajo el agua (pico→recuperación)": "{:,.0f}"},
                        na_rep="—",
                    ),
                    width="stretch", hide_index=True,
                )
            else:
                st.caption("Sin suficientes datos.")

            rc2, rc3 = st.columns(2)
            with rc2:
                st.caption("10 mejores años")
                if mejores_anios_riesgo:
                    df_mejores = pd.DataFrame(
                        [{"Año": a["anio"], "Retorno (%)": a["retorno_pct"]} for a in mejores_anios_riesgo]
                    )
                    st.dataframe(df_mejores.style.format({"Retorno (%)": "{:.1f}%"}), width="stretch", hide_index=True)
                else:
                    st.caption("Sin suficientes datos.")
            with rc3:
                st.caption("10 peores años")
                if peores_anios_riesgo:
                    df_peores = pd.DataFrame(
                        [{"Año": a["anio"], "Retorno (%)": a["retorno_pct"]} for a in peores_anios_riesgo]
                    )
                    st.dataframe(df_peores.style.format({"Retorno (%)": "{:.1f}%"}), width="stretch", hide_index=True)
                else:
                    st.caption("Sin suficientes datos.")

            st.divider()
            st.markdown("**📈 Comportamiento de los activos (sin aportes)**")
            st.caption(
                "Crecimiento de $100 invertidos una sola vez al inicio del período elegido en el plan de "
                "aportes, sin aportes adicionales — para comparar el desempeño puro de cada activo entre "
                f"{fecha_inicio_hist:%b %Y} y {fecha_fin_hist:%b %Y}, aislado de cuándo y cuánto aportaste."
            )
            mejor_ticker_activo = max(
                resultados.values(), key=lambda r: float(r.serie_precio.iloc[-1]) / float(r.serie_precio.iloc[0])
            ).ticker

            fig_activos = go.Figure()
            idx_otros_activo = 0
            for r in resultados.values():
                precio_norm = (r.serie_precio / float(r.serie_precio.iloc[0])) * 100.0
                edades_precio = [_edad_en_anio(f.year, edad_actual_hist, anio_actual) for f in precio_norm.index]
                if r.ticker == mejor_ticker_activo:
                    color_linea, ancho_linea = BRAND_GREEN, 3.5
                else:
                    color_linea, ancho_linea = paleta_otros[idx_otros_activo % len(paleta_otros)], 2
                    idx_otros_activo += 1
                fig_activos.add_trace(
                    go.Scatter(
                        x=precio_norm.index, y=precio_norm.values, mode="lines", name=r.ticker,
                        line=dict(color=color_linea, width=ancho_linea),
                        customdata=edades_precio,
                        hovertemplate=f"%{{x|%b %Y}} (edad %{{customdata}})<br>{r.ticker}: %{{y:,.0f}}<extra></extra>",
                    )
                )
            fig_activos.update_layout(
                template=PLOTLY_TEMPLATE,
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                hovermode="x unified",
                dragmode="pan",
                xaxis=_eje_x_anio_edad_fechas(int(anio_min_hist), int(anio_max_hist), edad_actual_hist, anio_actual),
                yaxis=dict(
                    title="Índice (100 = inicio)", fixedrange=False, showgrid=True, gridcolor=PLOTLY_GRIDCOLOR,
                    rangemode="tozero", minallowed=0,
                ),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                margin=dict(l=10, r=10, t=40, b=10),
            )
            fig_activos.update_xaxes(fixedrange=False)
            st.plotly_chart(fig_activos, config=PLOTLY_CONFIG_ZOOM, width="stretch")

            st.markdown("**📊 Estadísticas del activo (sin aportes)**")
            st.caption(
                "Retorno/riesgo de cada activo en el mismo período, calculado sobre el precio puro (sin "
                "aportes) — para comparar qué tan bien compensa cada uno el riesgo que exige. El **CAGR "
                "nominal** es el crecimiento anual del precio tal cual (antes de inflación); el **CAGR "
                f"real** le resta la inflación asumida ({inflacion_pct:.1f}%, vía la ecuación de Fisher) — "
                "es tu crecimiento verdadero en poder de compra, la misma lógica del resto de la "
                "calculadora. El **Max Drawdown** es un cociente pico/valle, así que es igual en términos "
                "nominales o reales."
            )
            filas_stats_activos = []
            for r in resultados.values():
                cagr_nominal_r = _cagr_activo(r) * 100.0
                cagr_real_r = _tasa_real(_cagr_activo(r)) * 100.0
                dd_r = max_drawdown(r.serie_precio) * 100.0
                return_dd_r = cagr_real_r / abs(dd_r) if dd_r != 0 else None
                filas_stats_activos.append(
                    {
                        "Ticker": r.ticker,
                        "CAGR nominal (%)": cagr_nominal_r,
                        "CAGR real (%)": cagr_real_r,
                        "Max Drawdown (%)": dd_r,
                        "Return/DD (real)": return_dd_r,
                    }
                )
            df_stats_activos = pd.DataFrame(filas_stats_activos).sort_values(
                "Return/DD (real)", ascending=False
            ).reset_index(drop=True)
            st.dataframe(
                df_stats_activos.style.format(
                    {
                        "CAGR nominal (%)": "{:.2f}%",
                        "CAGR real (%)": "{:.2f}%",
                        "Max Drawdown (%)": "{:.1f}%",
                        "Return/DD (real)": "{:.2f}",
                    },
                    na_rep="—",
                ),
                width="stretch", hide_index=True,
            )
            st.caption(
                "Return/DD (real) = CAGR **real** ÷ |Max Drawdown| (similar al ratio de Calmar): cuánto "
                "retorno anualizado real generó el activo por cada punto de riesgo (caída máxima) que "
                "tuviste que aguantar — más alto es mejor. Usamos el CAGR real para que el ratio no se "
                "infle con la parte del retorno que solo compensa la inflación."
            )

            ticker_stats_activos = st.selectbox(
                "Ver detalle de caídas y años de", list(resultados.keys()), key="ticker_stats_activos",
            )
            r_stats_activos = resultados[ticker_stats_activos]
            drawdowns_stats_activos = peores_drawdowns(r_stats_activos.serie_precio, top_n=10)
            mejores_anios_stats_activos, peores_anios_stats_activos = mejores_y_peores_anios(
                r_stats_activos.serie_precio, top_n=10
            )

            st.caption("10 peores drawdowns (con recuperación)")
            if drawdowns_stats_activos:
                df_dd_stats_activos = _tabla_peores_drawdowns(drawdowns_stats_activos)
                st.dataframe(
                    df_dd_stats_activos.style.format(
                        {"Caída (%)": "{:.1f}%", "Días bajo el agua (pico→recuperación)": "{:,.0f}"},
                        na_rep="—",
                    ),
                    width="stretch", hide_index=True,
                )
            else:
                st.caption("Sin suficientes datos.")

            dsa2, dsa3 = st.columns(2)
            with dsa2:
                st.caption("10 mejores años")
                if mejores_anios_stats_activos:
                    df_mejores_stats_activos = pd.DataFrame(
                        [{"Año": a["anio"], "Retorno (%)": a["retorno_pct"]} for a in mejores_anios_stats_activos]
                    )
                    st.dataframe(
                        df_mejores_stats_activos.style.format({"Retorno (%)": "{:.1f}%"}),
                        width="stretch", hide_index=True,
                    )
                else:
                    st.caption("Sin suficientes datos.")
            with dsa3:
                st.caption("10 peores años")
                if peores_anios_stats_activos:
                    df_peores_stats_activos = pd.DataFrame(
                        [{"Año": a["anio"], "Retorno (%)": a["retorno_pct"]} for a in peores_anios_stats_activos]
                    )
                    st.dataframe(
                        df_peores_stats_activos.style.format({"Retorno (%)": "{:.1f}%"}),
                        width="stretch", hide_index=True,
                    )
                else:
                    st.caption("Sin suficientes datos.")

# ----------------------------------------------------------
# TAB 3 — PENSIÓN DEL ESTADO (IVM + ROP)
# ----------------------------------------------------------
with tab_pension:
    st.subheader("Pensión del régimen básico (IVM) y complementario (ROP)")
    st.caption(
        "En Costa Rica, cuando te pensionás normalmente recibís **dos pagos separados del Estado**: el "
        "**IVM** (la pensión 'de toda la vida' que administra la CCSS, financiada con lo que vos y tu "
        "patrono han cotizado) y el **ROP** (un ahorro personal obligatorio, como una cuenta de banco a "
        "tu nombre que también se llena con cada salario). Esta sección estima cuánto te podrían dar esos "
        "dos, para sumarlo después a lo que proyectás en la pestaña 'Retiro Empoderado'."
    )
    st.caption(
        "Estimación educativa basada en el Reglamento del Seguro de IVM (CCSS, arts. 5, 6, 23, 24, 25, "
        "27 y 29) y en la Ley de Protección al Trabajador / normativa SUPEN del ROP. No sustituye la "
        "proyección oficial de la CCSS ni el estado de cuenta de tu operadora de pensiones (OPC)."
    )
    st.caption(
        "🔄 **Este cálculo se actualiza solo.** No hay que darle a ningún botón: apenas cambiás cualquier "
        "número de esta pestaña, todos los resultados de abajo se recalculan automáticamente."
    )

    with st.expander("💡 ¿Dónde consigo estos datos? (guía paso a paso)"):
        st.markdown(
            """
No hace falta que adivines nada — la CCSS y SUPEN tienen esta información gratis en línea:

- **Tus cuotas del IVM y tu historial de salarios**: entrá a la Oficina Virtual de la CCSS
  (o la app CCSSmóvil) con tu usuario. Ahí hay un reporte que se llama algo como "Estudio de Cuotas" o
  "Proyección de Pensión" que te dice exactamente cuántos meses has cotizado.
- **El saldo y las cuotas de tu ROP**: entrá al "Expediente Único" de SUPEN (necesitás firma digital,
  la misma que usás para trámites del Banco Central) o pedile el estado de cuenta a tu operadora de
  pensiones (OPC) — es la empresa que administra tu ROP (BAC, BCR, Vida Plena, IBP, Popular, etc.).
- **Si no sabés en cuál OPC está tu ROP**: normalmente aparece en tu "orden patronal" (el papel/PDF que
  te da tu trabajo con el desglose de deducciones), o lo podés confirmar en la Oficina Virtual del
  SICERE, o pidiéndolo directamente a SUPEN.
- **Ojo:** el número de cuotas del IVM y del ROP **no siempre es el mismo** — mirá la explicación al
  lado de esos dos campos más abajo.
"""
        )

    st.caption(
        f"📅 Se usa el mismo horizonte que en la pestaña 'Retiro Empoderado': **{anios} años** a partir de "
        f"hoy, es decir hasta que tengas **{edad_retiro} años**. Si querés cambiar ese número de años, "
        "hacelo en la pestaña 'Proyección de Inversión con Empowered Investor' (campo 'Años de acumulación'), y se actualiza aquí también."
    )

    c1, c2 = st.columns(2)
    salario_actual_crc = colon_input(
        c1, "Salario bruto mensual actual", "salario_actual_crc_txt", 2_000_000.0,
        help="Tu salario ANTES de que le quiten cargas sociales, impuesto de renta, etc. — el que aparece "
        "en tu contrato o en la parte de arriba de tu comprobante de pago (no el que te depositan). Si "
        "tenés varios trabajos o sos independiente, sumá todo lo que reportás a la CCSS.",
    )
    salario_promedio_ref = colon_input(
        c2, "Salario promedio de referencia IVM", "salario_promedio_ref_txt", 2_000_000.0,
        help="Este es un número técnico: la CCSS no usa tu salario de hoy para calcular la pensión, sino "
        "el **promedio de tus mejores 300 salarios mensuales** (25 años) de toda tu vida laboral, ya "
        "'traídos a colones de hoy' (ajustados por inflación). Si nunca lo has visto, la CCSS lo calcula "
        "por vos en el reporte 'Estudio de Cuotas y Proyección de Pensión' de la Oficina Virtual. "
        "Si no lo tenés a mano, usar tu salario actual es una aproximación razonable (asume que tu "
        "salario, en términos reales, se mantiene parecido en el tiempo).",
    )

    c1, c2 = st.columns(2)
    cuotas_ivm_hoy = c1.number_input(
        "Cuotas IVM acumuladas hoy", min_value=0, value=75, step=1,
        help="Una 'cuota' es simplemente **un mes en el que tu patrono pagó tu seguro a la CCSS** (o vos "
        "mismo, si sos independiente). 240 cuotas = 20 años trabajados formalmente. Lo ves en el reporte "
        "de 'Estudio de Cuotas' de la Oficina Virtual de la CCSS o en CCSSmóvil.",
    )
    cuotas_rop_hoy = c2.number_input(
        "Cuotas ROP acumuladas hoy", min_value=0, value=75, step=1,
        help="Igual que las cuotas del IVM, pero para tu cuenta ROP (el ahorro obligatorio). "
        "**Importante: NO asumas que este número es igual al de cuotas IVM.** El ROP empezó a existir en "
        "el año 2000 — si empezaste a trabajar antes de esa fecha, vas a tener MÁS cuotas IVM que ROP, "
        "porque esos años viejos no generaron ROP. También pueden diferir si alguna vez tu patrono no "
        "pagó bien tus cargas, o si cambiaste de OPC. Revisalo por separado en el Expediente Único de "
        "SUPEN o con tu OPC — no lo calcules copiando el número del IVM.",
    )

    saldo_actual_rop = colon_input(
        st, "Saldo actual del ROP", "saldo_actual_rop_txt", 8_000_000.0,
        help="El dinero que ya tenés ahorrado hoy en tu cuenta ROP (no es un salario, es un monto total "
        "acumulado, como el saldo de una cuenta de ahorros). Aparece en el estado de cuenta que te manda "
        "tu OPC (usualmente por correo o en su app/página) o en el Expediente Único de SUPEN. Si nunca "
        "has revisado esto y no tenés el dato, dejalo en ₡0 — la proyección solo contará lo que se "
        "acumule de aquí en adelante (será más conservadora que la realidad).",
    )

    with st.expander("⚙️ Supuestos financieros y parámetros regulatorios (ajustables)"):
        st.caption(
            "Estos números los fija el gobierno / la CCSS y cambian de vez en cuando. Vienen con un valor "
            "por defecto razonable, pero si tenés el dato más reciente, cambialo aquí."
        )
        c1, c2 = st.columns(2)
        salario_minimo_legal = colon_input(
            c1, "Salario mínimo legal de referencia", "salario_minimo_legal_txt", 350_000.0,
            help="La CCSS no le da el mismo % de pensión a todo el mundo: a quien gana salarios más bajos "
            "le reconoce un % más alto, y a quien gana más, un % más bajo (para ser más justos con quien "
            "gana menos). Para decidir en qué grupo caés, compara tu salario contra el 'salario mínimo de "
            "un trabajador no calificado' — un monto que fija el Ministerio de Trabajo cada año (buscalo "
            "como 'decreto de salarios mínimos Costa Rica' o pedile el dato a tu contador/asesor).",
        )
        meses_postergacion = c2.number_input(
            "Meses de postergación del retiro (IVM)", min_value=0, value=0, step=1,
            help="Si ya cumplís los requisitos para pensionarte (edad + cuotas) pero decidís seguir "
            "trabajando y cotizando en vez de pensionarte de una vez, la CCSS te premia con un % extra de "
            "pensión por cada mes que esperás. Si no pensás hacer esto, dejalo en 0.",
        )
        c1, c2 = st.columns(2)
        rentabilidad_nominal_rop_pct = c1.number_input(
            "Rentabilidad nominal esperada del ROP (%)", min_value=0.0, value=6.0, step=0.5, format="%0.1f",
            help="Cuánto esperás que crezca tu dinero del ROP cada año, ANTES de descontar inflación (por "
            "eso 'nominal'). Tu OPC invierte ese dinero en fondos de inversión; podés ver el rendimiento "
            "histórico de tu fondo específico en el estado de cuenta o la página de tu OPC.",
        )
        inflacion_esperada_pct = c2.number_input(
            "Inflación esperada (%)", min_value=0.0, value=3.0, step=0.5, format="%0.1f",
            help="Cuánto suben los precios en Costa Rica cada año, en promedio. Se usa para calcular el "
            "'rendimiento real' del ROP (lo que de verdad ganás en poder de compra, no solo en colones). "
            "3% anual es una referencia histórica razonable para Costa Rica; el Banco Central publica la "
            "meta de inflación vigente.",
        )
        plazo_pago_rop_anios = st.number_input(
            "Plazo de pago del ROP tras el retiro (años)", min_value=1, value=20, step=1,
            help="Solo aplica si te vas a pensionar después del 19-feb-2030 (ver explicación de "
            "modalidades más abajo): en cuántos años se reparte tu ahorro del ROP como pago mensual. Más "
            "años = pago mensual más bajo, pero dura más tiempo.",
        )
        c1, c2 = st.columns(2)
        monto_minimo_ivm = colon_input(
            c1, "Pensión mínima IVM", "monto_minimo_ivm_txt", MONTO_MINIMO_DEFAULT,
            help="Por ley, nadie que califique para el IVM puede recibir menos que este monto, sin "
            "importar qué tan bajo haya sido su salario. Es el 50% de la 'Base Mínima Contributiva' (BMC) "
            "que fija la CCSS cada año.",
        )
        monto_maximo_ivm = colon_input(
            c2, "Pensión máxima IVM (sin postergación)", "monto_maximo_ivm_txt", MONTO_MAXIMO_DEFAULT,
            help='Por ley, aunque hayas ganado salarios altísimos, la pensión IVM no puede pasar de este '
            'techo. Cita literal de la fuente: "documentos oficiales de la CCSS han citado un máximo sin '
            'postergación de ₡1.666.062" — la CCSS lo revalúa periódicamente, así que verificá el acuerdo '
            "más reciente antes de prometerle una cifra exacta a un cliente.",
        )

    ivm = calcular_pension_ivm(
        salario_promedio_referencia=salario_promedio_ref,
        cuotas_ivm_hoy=cuotas_ivm_hoy,
        anios_restantes=anios,
        salario_minimo_legal=salario_minimo_legal,
        meses_postergacion=meses_postergacion,
        monto_minimo=monto_minimo_ivm,
        monto_maximo=monto_maximo_ivm,
    )

    st.divider()
    st.subheader("🏦 Pensión IVM estimada")

    meses_futuros_ivm = max(0, anios) * 12
    st.caption(
        f"🧮 Cómo se cuentan tus cuotas totales: **{cuotas_ivm_hoy} cuotas que ya tenés hoy** + "
        f"**{meses_futuros_ivm} meses que faltan** (los {anios} años hasta tu retiro, en meses) = "
        f"**{ivm.cuotas_totales} cuotas totales** al momento de pensionarte. Eso es lo que se compara "
        "contra el mínimo de 300 cuotas para saber si te toca la pensión completa o una proporcional."
    )

    if not ivm.cumple_requisitos:
        st.error(
            f"{ivm.motivo} Con el horizonte actual ({anios} años) no alcanzarías ni la pensión mínima "
            "proporcional. Probá aumentando los 'Años de acumulación' en la pestaña 'Proyección de "
            "Inversión con Empowered Investor', o revisá si tus cuotas de hoy están completas."
        )
    elif ivm.es_proporcional:
        st.info(
            f"**{ivm.motivo}**\n\n"
            "En palabras simples: para la pensión **completa**, la CCSS pide un mínimo de 300 cuotas "
            f"(25 años cotizando). Con el horizonte que pusiste llegarías con {ivm.cuotas_totales} cuotas "
            "— menos de 300 — así que te toca una versión **proporcional**: te reconocen el mismo % que "
            f"le darían a alguien con la pensión completa, pero multiplicado por {ivm.factor_proporcional * 100:.0f}% "
            "(la fracción de las 300 cuotas que sí lograste completar). Si seguís cotizando más años de "
            "los que pusiste arriba, esa fracción va subiendo hasta llegar al 100% cuando cumplas las 300 cuotas."
        )
    else:
        st.info(
            "En palabras simples: la CCSS exige un mínimo de 300 cuotas (25 años cotizando) para la "
            f"pensión completa, sin ningún recorte por proporcionalidad. Con el horizonte que pusiste "
            f"llegarías con {ivm.cuotas_totales} cuotas — ya cumplís de sobra ese mínimo, así que la "
            "estimación de abajo es tu pensión completa (antes de aplicar el piso/techo legal)."
        )

    m1, m2, m3 = st.columns(3)
    m1.metric("Cuotas totales al retiro", f"{ivm.cuotas_totales:,}")
    m2.metric(
        "% del salario reconocido", f"{ivm.porcentaje_reconocido:.1f}%",
        help="Tu pensión NO es el 100% de tu salario. La CCSS te reconoce un porcentaje de tu 'salario "
        "promedio de referencia' (el campo de arriba). Este % combina: (1) la cuantía básica según tu "
        "nivel salarial, (2) un extra por cada cuota que tengas por encima de 300, y (3) el extra por "
        "postergación si aplica. Pensión IVM mensual = salario promedio de referencia × este %.",
    )
    m3.metric("Pensión IVM mensual estimada", f"₡{ivm.monto_mensual:,.0f}")

    st.divider()
    st.subheader("💼 Pensión ROP estimada")
    st.caption(
        "El ROP funciona muy distinto al IVM: no es un % de tu salario, es literalmente **la plata que se "
        "ha ido acumulando en tu cuenta personal** (como una alcancía obligatoria), invertida por tu OPC. "
        "Al pensionarte, ese dinero se te devuelve poco a poco, no de un solo pago."
    )
    rop = proyectar_rop(
        salario_bruto_actual=salario_actual_crc,
        saldo_actual_rop=saldo_actual_rop,
        cuotas_rop_hoy=cuotas_rop_hoy,
        anios_restantes=anios,
        rentabilidad_nominal_pct=rentabilidad_nominal_rop_pct,
        inflacion_pct=inflacion_esperada_pct,
        plazo_pago_anios=plazo_pago_rop_anios,
        monto_minimo_ivm=monto_minimo_ivm,
    )

    with st.expander("📖 ¿Cómo te dan la plata del ROP? (modalidades, explicadas simple)"):
        st.markdown(
            f"""
La forma en que te entregan el dinero del ROP depende de **cuándo te pensionés**, según la ley (art. 22
de la Ley 7983). Hay dos "puertas" distintas:

**🚪 Puerta 1 — Te pensionás antes del 19 de febrero de 2030 (régimen transitorio):**
Podés pedir que te den todo tu ahorro del ROP repartido en partes iguales, un pago mensual, durante
tantos meses como cuotas hayas aportado al ROP. Es como decir "tengo ₡18,000,000 ahorrados y aporté
300 meses, entonces me dan ₡18,000,000 ÷ 300 = ₡60,000 cada mes durante 300 meses (25 años)". Es la
forma más simple y más rápida de recibir el dinero.

**🚪 Puerta 2 — Te pensionás a partir del 19 de febrero de 2030 (modalidades normales):**
Aquí ya no aplica la división simple de arriba. La ley te da a elegir entre varias formas de recibir el
dinero poco a poco, para que te dure más tiempo (parecido a cómo funciona una pensión de verdad):

- **Retiro programado** (la más común): cada año te recalculan cuánto te toca ese mes, tomando en
  cuenta cuánto te queda ahorrado, cuánto rinde la inversión, y cuántos años más se espera que vivás
  (según tablas actuariales). Es como ir sacando de una cuenta de ahorros que sigue generando
  intereses mientras la usás — el pago puede subir o bajar un poco cada año.
- **Renta permanente**: en vez de gastarte el ahorro, la OPC solo te paga los **intereses/rendimientos**
  que genera esa plata invertida, y el capital original se queda intacto. Esto significa un pago mensual
  más bajo, pero la ventaja es que si te morís, ese capital completo queda para tus beneficiarios (hijos,
  cónyuge, etc.), porque nunca lo gastaste.
- **Renta temporal hasta expectativa de vida**: un pago fijo mensual calculado para que el dinero dure
  aproximadamente hasta la edad en la que, según las tablas oficiales, se espera que una persona de tu
  edad y sexo viva.
- **Renta vitalicia**: técnicamente la ley la permite (un seguro te paga un monto fijo de por vida, sin
  importar cuánto vivas), pero SUPEN indica que **hoy en día ninguna aseguradora la ofrece**, así que en
  la práctica no está disponible.

Esta calculadora aproxima la Puerta 2 como una anualidad simple (parecida al "retiro programado"), no la
fórmula exacta del reglamento (que usa tablas de mortalidad oficiales). Es una buena idea de referencia,
pero para el número exacto siempre hay que confirmar con tu OPC.

Tu fecha de retiro estimada con el horizonte actual es **{rop.fecha_retiro_estimada.year}**.
"""
        )

    if rop.modalidad_aplicable == "transitoria":
        st.warning(
            f"⚠️ Con tu horizonte actual te pensionarías en **{rop.fecha_retiro_estimada.year}** — antes "
            "del 19-feb-2030 — así que en tu caso aplicaría el régimen transitorio (Puerta 1) en vez de "
            "la anualidad de abajo. Mirá el signo de interrogación del ingreso mensual para ese número."
        )

    m1, m2 = st.columns(2)
    m1.metric(
        "Saldo ROP proyectado real", f"₡{rop.saldo_proyectado:,.0f}",
        help="Cuánto tendrás acumulado en tu cuenta ROP el día que te pensiones, expresado en poder de "
        "compra de hoy (ya se le descontó la inflación proyectada).",
    )
    m2.metric(
        "Ingreso mensual (anualidad)", f"₡{rop.ingreso_mensual_anualidad:,.0f}",
        help="'Anualidad' es la forma en que se reparte tu ahorro del ROP como un pago mensual "
        "aproximadamente constante durante el retiro — es la modalidad que casi seguro te va a aplicar, "
        "porque es para quienes se pensionan a partir del 19-feb-2030 (la 'Puerta 2' del expander de "
        "arriba). Por ley, ningún pago del ROP puede ser menor al 20% de la pensión mínima del IVM "
        f"(hoy ₡{rop.piso_pension_minima:,.0f}). Si en cambio te tocara la modalidad transitoria (Puerta "
        f"1, solo si te pensionás antes de esa fecha), el ingreso mensual equivalente sería "
        f"₡{rop.ingreso_mensual_transitorio:,.0f}.",
    )

with tab_teorica:
    st.divider()
    st.subheader("🧮 Proyección total de tu retiro")
    st.caption(
        "Suma la pensión IVM, el ingreso ROP (según la modalidad aplicable, calculados en la pestaña "
        "'Pensión del Estado') y el ingreso mensual **real** (ajustado por inflación, en poder de compra "
        "de hoy) ya calculado más arriba en esta misma pestaña, en 'Ingreso mensual con método "
        "porcentual', para ver tu ingreso total estimado en colones de hoy."
    )
    st.caption(
        "⚠️ El ingreso de 'Retiro Empoderado' está en USD y la pensión IVM/ROP en colones; para sumarlos, "
        "se convierte con el tipo de cambio que definas abajo. Usamos el valor **real** (ya descontada la "
        "inflación) para que sea comparable con el IVM/ROP, que también están en colones de hoy.",
    )
    tipo_cambio = st.number_input(
        "Tipo de cambio (₡ por USD)", min_value=1.0, value=515.0, step=1.0,
        help="Cuántos colones vale un dólar hoy. Puedes buscarlo como 'tipo de cambio dólar Costa Rica' "
        "o consultarlo en el sitio del Banco Central de Costa Rica (BCCR).",
    )

    ingreso_retiro_empoderado_crc = ingreso_mensual_real * tipo_cambio
    total_mensual = ivm.monto_mensual + rop.ingreso_mensual_aplicable + ingreso_retiro_empoderado_crc

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("IVM", f"₡{ivm.monto_mensual:,.0f}")
    c2.metric("ROP", f"₡{rop.ingreso_mensual_aplicable:,.0f}")
    c3.metric("Retiro Empoderado", f"₡{ingreso_retiro_empoderado_crc:,.0f}")
    c4.metric("Total mensual estimado", f"₡{total_mensual:,.0f}")

    fig_total = go.Figure(
        go.Bar(
            x=["IVM", "ROP", "Retiro Empoderado", "Total"],
            y=[ivm.monto_mensual, rop.ingreso_mensual_aplicable, ingreso_retiro_empoderado_crc, total_mensual],
            marker_color=[BRAND_MUTED, "#a78bfa", BRAND_GREEN, BRAND_BLUE],
            text=[
                f"₡{v:,.0f}"
                for v in [ivm.monto_mensual, rop.ingreso_mensual_aplicable, ingreso_retiro_empoderado_crc, total_mensual]
            ],
            textposition="outside",
        )
    )
    fig_total.update_layout(
        template=PLOTLY_TEMPLATE, showlegend=False, margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        yaxis=dict(title="₡ / mes", fixedrange=True, tickprefix="₡", separatethousands=True, gridcolor=PLOTLY_GRIDCOLOR),
        xaxis=dict(fixedrange=True),
    )
    st.plotly_chart(fig_total, config=PLOTLY_CONFIG, width="stretch")

# ----------------------------------------------------------
# TAB 4 — BACKTESTING AVANZADO (BUY THE DIP CON APALANCAMIENTO ESCALONADO)
# ----------------------------------------------------------
with tab_avanzado:
    st.subheader("⚡ Buy the Dip con Apalancamiento")
    st.caption(
        "Backtest: \"si hubiera comprado **todas** las caídas históricas de QQQ o SPY con mis aportes de "
        "esta simulación, ¿qué me habría dado si compro un activo apalancado como TQQQ o QLD?\" — un "
        "historial concreto, operación por operación, de cómo se hubiera visto seguir esta estrategia en "
        "la vida real."
    )
    c1, c2, c3 = st.columns(3)
    ticker_senal = c1.selectbox(
        "Activo que genera la señal", ["QQQ", "SPY"], key="ticker_senal_dip",
        help="El ticker cuya caída desde su máximo de las últimas 52 semanas dispara la señal de compra.",
    )
    _default_comprar = "TQQQ" if "TQQQ" in TICKERS_DISPONIBLES else TICKERS_DISPONIBLES[0]
    ticker_comprar = c2.selectbox(
        "Activo que se compra", TICKERS_DISPONIBLES,
        index=TICKERS_DISPONIBLES.index(_default_comprar), key="ticker_comprar_dip",
        help="El ticker que realmente se compra cuando aparece la señal — puede ser el mismo de la señal "
        "o uno apalancado (TQQQ, QLD).",
    )
    umbral_caida_pct = c3.number_input(
        f"Caída desde el máximo de 52 semanas de {ticker_senal} (%)", min_value=-90.0, max_value=-1.0,
        value=-5.0, step=1.0, key="umbral_caida_hist",
        help="Se mide contra el máximo móvil de las últimas 52 semanas, no el máximo histórico (ATH) — "
        "así la señal se recalibra en caídas muy largas (p.ej. la puntocom) en vez de quedarse sin "
        "dispararse por años.",
    )
    horizonte_backtest_lbl = st.selectbox(
        "Horizonte de venta (N) — cuánto se mantiene cada posición antes de vender y resetear",
        [h for h, _ in HORIZONTES_DIAS_HABILES], index=5, key="horizonte_backtest_dip",
    )

    with st.spinner(f"Analizando caídas históricas de {ticker_senal}..."):
        precio_senal_diario = obtener_serie_diaria(
            ticker_senal, fecha_inicio_hist, fecha_fin_hist,
            expense_ratio_anual_pct=expense_ratio_anual_pct,
            financing_rate_anual_pct=financing_rate_anual_pct,
        )
        fechas_disparo = fechas_disparo_caida(precio_senal_diario, float(umbral_caida_pct))

    if precio_senal_diario.empty or len(fechas_disparo) == 0:
        st.warning("No se detectaron caídas de ese tamaño en el período elegido.")
    else:
        precio_comprar_diario = obtener_serie_diaria(
            ticker_comprar, fecha_inicio_hist, fecha_fin_hist,
            expense_ratio_anual_pct=expense_ratio_anual_pct,
            financing_rate_anual_pct=financing_rate_anual_pct,
        )
        horizonte_backtest_dias = dict(HORIZONTES_DIAS_HABILES)[horizonte_backtest_lbl]

        st.markdown(f"##### 🔁 Backtest: comprar {ticker_comprar} en cada caída de {ticker_senal}")
        st.caption(
            f"Tus aportes (\\${aporte_inicial_hist:,.0f} inicial + \\${aporte_periodico_hist:,.0f} "
            f"{frecuencia_aporte_hist.lower()}, igual que en 'Simulación con Datos Históricos') se quedan "
            f"en efectivo mientras no hay ninguna posición abierta. Cuando aparece una señal de caída de "
            f"{ticker_senal} (≥{abs(umbral_caida_pct):.0f}%), se invierte **todo** el efectivo acumulado en "
            f"{ticker_comprar}; cualquier aporte nuevo que llegue mientras la posición sigue abierta se "
            "invierte de inmediato en esa misma posición (mejorando el precio promedio si está en "
            f"pérdida). La venta recién se evalúa {horizonte_backtest_lbl} después de la entrada, y **solo "
            "se vende si para entonces la posición está en ganancia** — si sigue en pérdida, se sigue "
            "esperando (y promediando con los aportes que vayan llegando) hasta que sea rentable."
        )

        resultado_bt = backtest_buy_the_dip(
            precio_comprar_diario, fechas_disparo, horizonte_backtest_dias,
            aporte_inicial=aporte_inicial_hist, aporte_periodico=aporte_periodico_hist,
            frecuencia=frecuencia_aporte_hist,
        )

        if resultado_bt.n_operaciones == 0:
            st.warning("No hubo suficientes datos para completar ninguna operación con estos parámetros.")
        else:
            bm1, bm2, bm3, bm4, bm5 = st.columns(5)
            bm1.metric("Total aportado", f"${resultado_bt.total_aportado:,.0f}")
            bm2.metric("Valor final", f"${resultado_bt.valor_final:,.0f}")
            bm3.metric("Retorno anualizado (XIRR)", f"{resultado_bt.retorno_anualizado_pct:.1f}%")
            bm4.metric(
                "Max drawdown", f"{max_drawdown(resultado_bt.serie_valor) * 100:.1f}%",
                help="La peor caída (de pico a valle) que sufrió el VALOR de la estrategia en todo el "
                "período — no el precio del activo, sino tu cuenta siguiendo esta estrategia.",
            )
            bm5.metric(
                "Operaciones ganadoras",
                f"{resultado_bt.pct_operaciones_ganadoras:.0f}% de {resultado_bt.n_operaciones}",
            )

            edades_bt = [
                _edad_en_anio(f.year, edad_actual_hist, anio_actual)
                for f in resultado_bt.serie_valor.index
            ]
            fig_bt = go.Figure()
            fig_bt.add_trace(
                go.Scatter(
                    x=resultado_bt.serie_aportado.index, y=resultado_bt.serie_aportado.values,
                    mode="lines", name="Aportado acumulado",
                    line=dict(color=BRAND_MUTED, dash="dash", width=2),
                    customdata=edades_bt,
                    hovertemplate="%{x|%b %Y} (edad %{customdata})<br>Aportado: %{y:$,.0f}<extra></extra>",
                )
            )
            fig_bt.add_trace(
                go.Scatter(
                    x=resultado_bt.serie_valor.index, y=resultado_bt.serie_valor.values,
                    mode="lines", name=f"Valor de la estrategia ({ticker_comprar})",
                    line=dict(color=BRAND_GREEN, width=3),
                    customdata=edades_bt,
                    hovertemplate="%{x|%b %Y} (edad %{customdata})<br>Valor: %{y:$,.0f}<extra></extra>",
                )
            )
            _agregar_marcadores_entrada_salida(
                fig_bt,
                [op.fecha_entrada for op in resultado_bt.operaciones],
                [op.fecha_salida for op in resultado_bt.operaciones if not op.posicion_abierta_al_final],
                resultado_bt.serie_valor,
            )
            fig_bt.update_layout(
                template=PLOTLY_TEMPLATE,
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                hovermode="x unified",
                dragmode="pan",
                xaxis=_eje_x_anio_edad_fechas(
                    int(fecha_inicio_hist.year), int(fecha_fin_hist.year), edad_actual_hist, anio_actual
                ),
                yaxis=dict(
                    title="$", fixedrange=False, showgrid=True, gridcolor=PLOTLY_GRIDCOLOR,
                    tickprefix="$", separatethousands=True, rangemode="tozero", minallowed=0,
                ),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                margin=dict(l=10, r=10, t=40, b=10),
            )
            fig_bt.update_xaxes(fixedrange=False)
            st.plotly_chart(fig_bt, config=PLOTLY_CONFIG_ZOOM, width="stretch")
            st.caption(
                f"🔺 verde = entrada de una posición, 🔻 rojo = salida (venta real, en ganancia) — "
                f"{resultado_bt.n_operaciones} operaciones, de {len(fechas_disparo)} señales detectadas (el "
                "resto cayó mientras ya había una posición abierta). Si la última posición seguía abierta "
                "al cierre del período, su valor está marcado a mercado con el último precio disponible, no "
                "vendido de verdad. 🔍 Podés hacer scroll para zoom y clic + arrastrar para moverte en el "
                "tiempo."
            )

            st.markdown("**Historial de operaciones**")
            df_ops = pd.DataFrame(
                [
                    {
                        "Entrada": op.fecha_entrada.date(),
                        "Salida": op.fecha_salida.date(),
                        "Monto invertido ($)": op.monto_invertido,
                        "Precio entrada": op.precio_entrada,
                        "Precio salida": op.precio_salida,
                        "Retorno (%)": op.retorno_pct,
                        "Estado": "Abierta (a mercado)" if op.posicion_abierta_al_final else "Cerrada",
                    }
                    for op in resultado_bt.operaciones
                ]
            )
            st.dataframe(
                df_ops.style.format(
                    {
                        "Monto invertido ($)": "{:,.0f}",
                        "Precio entrada": "{:,.2f}",
                        "Precio salida": "{:,.2f}",
                        "Retorno (%)": "{:.1f}%",
                    }
                ),
                width="stretch", hide_index=True,
            )

            st.markdown("**10 peores drawdowns de la estrategia**")
            st.caption(
                "Peor caída de pico a valle del VALOR de la estrategia (no del precio del activo), con "
                "fechas — para dimensionar cuánto habría dolido seguir esta estrategia en la práctica."
            )
            drawdowns_estrategia = peores_drawdowns(resultado_bt.serie_valor, top_n=10)
            if drawdowns_estrategia:
                df_dd_estrategia = pd.DataFrame(
                    [
                        {
                            "Pico": d["fecha_pico"].date(),
                            "Valle": d["fecha_valle"].date(),
                            "Caída (%)": d["drawdown_pct"],
                        }
                        for d in drawdowns_estrategia
                    ]
                )
                st.dataframe(
                    df_dd_estrategia.style.format({"Caída (%)": "{:.1f}%"}), width="stretch", hide_index=True,
                )
            else:
                st.caption("Sin suficientes datos.")

        st.divider()
        st.markdown(
            f"##### 📊 Cada caída de {ticker_senal}, fecha por fecha: retorno real de {ticker_comprar}"
        )
        st.caption(
            "Una fila por cada caída detectada — no promedios, el dato real de cada evento — para que se "
            "vea la granularidad completa. Al final se agrega el promedio de estas señales vs. el promedio "
            "histórico incondicional (cualquier día), para dimensionar qué tanto ayuda comprar después de "
            "una caída."
        )

        df_eventos = retornos_individuales_por_evento(precio_comprar_diario, fechas_disparo)
        if df_eventos.empty:
            st.warning("No hay suficientes datos para mostrar el detalle de eventos.")
        else:
            filas_promedio = retornos_post_caida_vs_promedio(precio_comprar_diario, fechas_disparo)
            promedio_incondicional_por_h = {f["horizonte"]: f["retorno_promedio_pct"] for f in filas_promedio}

            df_mostrar = df_eventos.reset_index()
            df_mostrar["Fecha de la señal"] = df_mostrar["Fecha de la señal"].dt.strftime("%Y-%m-%d")

            columnas_horizonte = [h for h, _ in HORIZONTES_DIAS_HABILES]
            fila_prom_senales = {"Fecha de la señal": f"Promedio ({len(df_eventos)} señales)"}
            fila_prom_incond = {"Fecha de la señal": "Promedio histórico (cualquier momento)"}
            for h in columnas_horizonte:
                fila_prom_senales[h] = df_eventos[h].mean() if h in df_eventos else None
                fila_prom_incond[h] = promedio_incondicional_por_h.get(h)

            df_final = pd.concat(
                [df_mostrar, pd.DataFrame([fila_prom_senales, fila_prom_incond])], ignore_index=True
            )
            st.dataframe(
                df_final.style.format({h: "{:.1f}%" for h in columnas_horizonte}, na_rep="—"),
                width="stretch", hide_index=True,
            )
            st.caption(
                "⚠️ En plazos cortos (días a ~1 año), comprar después de una caída rindió de forma "
                "consistente más que el promedio histórico. En plazos muy largos (varios años) esto puede "
                "no cumplirse con activos apalancados: una caída suele venir seguida de más volatilidad de "
                "lo normal, y esa volatilidad extra le pega más fuerte al retorno compuesto de un producto "
                "2x/3x que a un período tranquilo típico — por eso el \"promedio histórico\" a largo plazo "
                "puede verse mejor que comprar justo después de una caída, aunque en plazos cortos la caída "
                "sí haya sido el mejor momento para entrar."
            )

        st.divider()
        st.markdown("##### 🔬 Barrido: umbral de caída × horizonte de venta")
        st.caption(
            f"Corre el mismo backtest (señal {ticker_senal} → compra {ticker_comprar}) para muchas "
            "combinaciones de umbral de caída y horizonte de venta, y ordena los resultados — para "
            "explorar con evidencia qué tan profunda debería ser la caída que dispara la compra."
        )
        sc1, sc2 = st.columns(2)
        u_min = sc1.number_input(
            "Umbral desde (%)", min_value=-90.0, max_value=-1.0, value=-15.0, step=1.0, key="u_dip_min"
        )
        u_max = sc2.number_input(
            "Umbral hasta (%)", min_value=-90.0, max_value=-1.0, value=-3.0, step=1.0, key="u_dip_max"
        )
        horizontes_barrido_dip = st.multiselect(
            "Horizontes de salida a incluir en el barrido",
            [h for h, _ in HORIZONTES_DIAS_HABILES],
            default=["6 meses", "1 año", "2 años"], key="horizontes_barrido_dip",
        )

        umbrales_dip_lista = [float(u) for u in range(int(min(u_min, u_max)), int(max(u_min, u_max)) + 1)]
        horizontes_dict_dip = dict(HORIZONTES_DIAS_HABILES)
        horizontes_sel_dip = [(h, horizontes_dict_dip[h]) for h in horizontes_barrido_dip]
        n_combos_dip = len(umbrales_dip_lista) * max(1, len(horizontes_sel_dip))
        tiempo_estimado_dip = n_combos_dip * 0.02

        st.caption(
            f"Esto va a correr **{n_combos_dip:,}** combinaciones — un estimado de "
            f"**{tiempo_estimado_dip:.0f} segundos**."
        )

        if n_combos_dip == 0 or not horizontes_sel_dip:
            st.info("Elegí al menos un horizonte y un rango válido para poder correr el barrido.")
        elif st.button("▶️ Ejecutar barrido", key="ejecutar_barrido_dip"):
            barra_dip = st.progress(0.0, text="Corriendo combinaciones...")

            def _progreso_dip(hecho, total):
                barra_dip.progress(hecho / total, text=f"Corriendo combinaciones... {hecho}/{total}")

            df_barrido_dip_res = sweep_buy_the_dip(
                precio_senal_diario, precio_comprar_diario, umbrales_dip_lista, horizontes_sel_dip,
                aporte_inicial_hist, aporte_periodico_hist, frecuencia_aporte_hist,
                progreso_callback=_progreso_dip,
            )
            barra_dip.empty()

            if df_barrido_dip_res.empty:
                st.warning("Ninguna combinación produjo resultados.")
            else:
                st.session_state["df_barrido_dip"] = df_barrido_dip_res.sort_values(
                    "Valor Final ($)", ascending=False
                ).reset_index(drop=True)

        if "df_barrido_dip" in st.session_state:
            df_mostrar_barrido_dip = st.session_state["df_barrido_dip"]
            st.markdown(
                f"**Resultados del barrido — {len(df_mostrar_barrido_dip)} combinaciones, "
                "ordenadas por Valor Final**"
            )
            st.dataframe(
                df_mostrar_barrido_dip.style.format(
                    {
                        "Umbral de caída (%)": "{:.0f}%",
                        "Valor Final ($)": "{:,.0f}",
                        "Total Aportado ($)": "{:,.0f}",
                        "Retorno anualizado XIRR (%)": "{:.1f}%",
                        "Max drawdown (%)": "{:.1f}%",
                        "% operaciones ganadoras": "{:.0f}%",
                    }
                ),
                width="stretch", hide_index=True,
            )

    st.divider()
    st.subheader("🌪️ Filtro por Nivel de VIX")
    st.caption(
        "Otra forma de decidir cuándo invertir: en vez de mirar la caída del precio, se usa el nivel del "
        "VIX (el índice de volatilidad del S&P 500, ^VIX) como señal — comprar cuando el mercado está "
        "tranquilo (VIX por debajo de un nivel) y vender cuando se dispara el miedo (VIX por encima de "
        "otro nivel). A diferencia de la estrategia de caída, acá la venta **no exige estar en ganancia**: "
        "se vende siempre que el VIX cruce el umbral de venta."
    )
    vc1, vc2, vc3 = st.columns(3)
    ticker_comprar_vix = vc1.selectbox(
        "Activo que se compra", TICKERS_DISPONIBLES,
        index=TICKERS_DISPONIBLES.index(_default_comprar), key="ticker_comprar_vix",
    )
    umbral_compra_vix = vc2.number_input(
        "Comprar cuando el VIX esté por debajo de", min_value=8.0, max_value=50.0, value=15.0, step=1.0,
        key="umbral_compra_vix",
    )
    umbral_venta_vix = vc3.number_input(
        "Vender cuando el VIX esté por arriba de", min_value=10.0, max_value=90.0, value=25.0, step=1.0,
        key="umbral_venta_vix",
    )

    if umbral_venta_vix <= umbral_compra_vix:
        st.error("El umbral de venta debe ser mayor que el umbral de compra.")
    else:
        with st.spinner("Descargando datos del VIX..."):
            vix_diario = obtener_vix_diario(fecha_inicio_hist, fecha_fin_hist)
            precio_comprar_vix_diario = obtener_serie_diaria(
                ticker_comprar_vix, fecha_inicio_hist, fecha_fin_hist,
                expense_ratio_anual_pct=expense_ratio_anual_pct,
                financing_rate_anual_pct=financing_rate_anual_pct,
            )

        if vix_diario.empty or precio_comprar_vix_diario.empty:
            st.warning("No se pudieron descargar los datos necesarios (VIX o el activo elegido) para este período.")
        else:
            resultado_vix = backtest_filtro_vix(
                precio_comprar_vix_diario, vix_diario, umbral_compra_vix, umbral_venta_vix,
                aporte_inicial_hist, aporte_periodico_hist, frecuencia_aporte_hist,
            )

            if resultado_vix.n_operaciones == 0:
                st.warning("No hubo suficientes señales para completar ninguna operación con estos parámetros.")
            else:
                vm1, vm2, vm3, vm4, vm5 = st.columns(5)
                vm1.metric("Total aportado", f"${resultado_vix.total_aportado:,.0f}")
                vm2.metric("Valor final", f"${resultado_vix.valor_final:,.0f}")
                vm3.metric("Retorno anualizado (XIRR)", f"{resultado_vix.retorno_anualizado_pct:.1f}%")
                vm4.metric(
                    "Max drawdown", f"{max_drawdown(resultado_vix.serie_valor) * 100:.1f}%",
                    help="La peor caída (de pico a valle) que sufrió el VALOR de la estrategia en todo el "
                    "período — no el precio del activo, sino tu cuenta siguiendo esta estrategia.",
                )
                vm5.metric(
                    "Operaciones ganadoras",
                    f"{resultado_vix.pct_operaciones_ganadoras:.0f}% de {resultado_vix.n_operaciones}",
                )

                edades_vix = [
                    _edad_en_anio(f.year, edad_actual_hist, anio_actual)
                    for f in resultado_vix.serie_valor.index
                ]
                fig_vix = go.Figure()
                fig_vix.add_trace(
                    go.Scatter(
                        x=resultado_vix.serie_aportado.index, y=resultado_vix.serie_aportado.values,
                        mode="lines", name="Aportado acumulado",
                        line=dict(color=BRAND_MUTED, dash="dash", width=2),
                        customdata=edades_vix,
                        hovertemplate="%{x|%b %Y} (edad %{customdata})<br>Aportado: %{y:$,.0f}<extra></extra>",
                    )
                )
                fig_vix.add_trace(
                    go.Scatter(
                        x=resultado_vix.serie_valor.index, y=resultado_vix.serie_valor.values,
                        mode="lines", name=f"Valor de la estrategia ({ticker_comprar_vix})",
                        line=dict(color=BRAND_GREEN, width=3),
                        customdata=edades_vix,
                        hovertemplate="%{x|%b %Y} (edad %{customdata})<br>Valor: %{y:$,.0f}<extra></extra>",
                    )
                )
                fig_vix.add_trace(
                    go.Scatter(
                        x=precio_comprar_vix_diario.index, y=precio_comprar_vix_diario.values,
                        mode="lines", name=f"Precio {ticker_comprar_vix} (subyacente)",
                        line=dict(color=BRAND_BLUE, width=1.5, dash="dot"),
                        yaxis="y2",
                        hovertemplate=f"%{{x|%d %b %Y}}<br>{ticker_comprar_vix}: %{{y:$,.2f}}<extra></extra>",
                    )
                )
                _agregar_marcadores_entrada_salida(
                    fig_vix,
                    [op.fecha_entrada for op in resultado_vix.operaciones],
                    [op.fecha_salida for op in resultado_vix.operaciones if not op.posicion_abierta_al_final],
                    resultado_vix.serie_valor,
                )
                fig_vix.update_layout(
                    template=PLOTLY_TEMPLATE,
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    hovermode="x unified",
                    dragmode="pan",
                    xaxis=_eje_x_anio_edad_fechas(
                        int(fecha_inicio_hist.year), int(fecha_fin_hist.year), edad_actual_hist, anio_actual
                    ),
                    yaxis=dict(
                        title="Valor de la estrategia ($)", fixedrange=False, showgrid=True,
                        gridcolor=PLOTLY_GRIDCOLOR, tickprefix="$", separatethousands=True,
                        rangemode="tozero", minallowed=0,
                    ),
                    yaxis2=dict(
                        title=f"Precio {ticker_comprar_vix} ($)", overlaying="y", side="right",
                        showgrid=False, fixedrange=False, rangemode="tozero", minallowed=0,
                        tickprefix="$", separatethousands=True,
                    ),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    margin=dict(l=10, r=10, t=40, b=10),
                )
                fig_vix.update_xaxes(fixedrange=False)
                st.plotly_chart(fig_vix, config=PLOTLY_CONFIG_ZOOM, width="stretch")
                st.caption(
                    f"🔺 verde = entrada (VIX < {umbral_compra_vix:.0f}), 🔻 rojo = salida (VIX > "
                    f"{umbral_venta_vix:.0f}) — {resultado_vix.n_operaciones} operaciones en total. La línea "
                    f"punteada de {ticker_comprar_vix} (eje derecho) deja comparar la estrategia contra "
                    "simplemente sostener el activo subyacente. Si la última posición seguía abierta al "
                    "cierre del período, su valor está marcado a mercado con el último precio disponible, "
                    "no vendido de verdad. 🔍 Podés hacer scroll para zoom y clic + arrastrar para moverte "
                    "en el tiempo."
                )

                st.markdown("**Historial de operaciones**")
                df_ops_vix = pd.DataFrame(
                    [
                        {
                            "Entrada": op.fecha_entrada.date(),
                            "Salida": op.fecha_salida.date(),
                            "Monto invertido ($)": op.monto_invertido,
                            "Precio entrada": op.precio_entrada,
                            "Precio salida": op.precio_salida,
                            "Retorno (%)": op.retorno_pct,
                            "Estado": "Abierta (a mercado)" if op.posicion_abierta_al_final else "Cerrada",
                        }
                        for op in resultado_vix.operaciones
                    ]
                )
                st.dataframe(
                    df_ops_vix.style.format(
                        {
                            "Monto invertido ($)": "{:,.0f}",
                            "Precio entrada": "{:,.2f}",
                            "Precio salida": "{:,.2f}",
                            "Retorno (%)": "{:.1f}%",
                        }
                    ),
                    width="stretch", hide_index=True,
                )

            st.divider()
            st.markdown("##### 🔬 Barrido: umbral de compra × umbral de venta del VIX")
            st.caption(
                f"Corre el mismo backtest (comprando {ticker_comprar_vix}) para muchas combinaciones de "
                "umbrales de VIX, y ordena los resultados por Valor Final."
            )
            vsc1, vsc2 = st.columns(2)
            with vsc1:
                st.caption("Rango del umbral de compra (VIX bajo)")
                vr1a, vr1b = st.columns(2)
                vu_compra_min = vr1a.number_input(
                    "Desde", min_value=8.0, max_value=50.0, value=12.0, step=1.0, key="vu_compra_min"
                )
                vu_compra_max = vr1b.number_input(
                    "Hasta", min_value=8.0, max_value=50.0, value=18.0, step=1.0, key="vu_compra_max"
                )
            with vsc2:
                st.caption("Rango del umbral de venta (VIX alto)")
                vr2a, vr2b = st.columns(2)
                vu_venta_min = vr2a.number_input(
                    "Desde", min_value=10.0, max_value=90.0, value=22.0, step=1.0, key="vu_venta_min"
                )
                vu_venta_max = vr2b.number_input(
                    "Hasta", min_value=10.0, max_value=90.0, value=32.0, step=1.0, key="vu_venta_max"
                )

            umbrales_compra_vix_lista = [
                float(u) for u in range(int(min(vu_compra_min, vu_compra_max)), int(max(vu_compra_min, vu_compra_max)) + 1)
            ]
            umbrales_venta_vix_lista = [
                float(u) for u in range(int(min(vu_venta_min, vu_venta_max)), int(max(vu_venta_min, vu_venta_max)) + 1)
            ]
            n_combos_vix = sum(
                1 for uc in umbrales_compra_vix_lista for uv in umbrales_venta_vix_lista if uv > uc
            )
            tiempo_estimado_vix = n_combos_vix * 0.04

            st.caption(
                f"Esto va a correr **{n_combos_vix:,}** combinaciones — un estimado de "
                f"**{tiempo_estimado_vix:.0f} segundos**."
            )

            if n_combos_vix == 0:
                st.info("Elegí un rango válido (venta > compra) para poder correr el barrido.")
            elif st.button("▶️ Ejecutar barrido", key="ejecutar_barrido_vix"):
                barra_vix = st.progress(0.0, text="Corriendo combinaciones...")

                def _progreso_vix(hecho, total):
                    barra_vix.progress(hecho / total, text=f"Corriendo combinaciones... {hecho}/{total}")

                df_barrido_vix_res = sweep_filtro_vix(
                    precio_comprar_vix_diario, vix_diario, umbrales_compra_vix_lista, umbrales_venta_vix_lista,
                    aporte_inicial_hist, aporte_periodico_hist, frecuencia_aporte_hist,
                    progreso_callback=_progreso_vix,
                )
                barra_vix.empty()

                if df_barrido_vix_res.empty:
                    st.warning("Ninguna combinación produjo resultados.")
                else:
                    st.session_state["df_barrido_vix"] = df_barrido_vix_res.sort_values(
                        "Valor Final ($)", ascending=False
                    ).reset_index(drop=True)

            if "df_barrido_vix" in st.session_state:
                df_mostrar_barrido_vix = st.session_state["df_barrido_vix"]
                st.markdown(
                    f"**Resultados del barrido — {len(df_mostrar_barrido_vix)} combinaciones, "
                    "ordenadas por Valor Final**"
                )
                st.dataframe(
                    df_mostrar_barrido_vix.style.format(
                        {
                            "Umbral compra VIX": "{:.0f}",
                            "Umbral venta VIX": "{:.0f}",
                            "Valor Final ($)": "{:,.0f}",
                            "Total Aportado ($)": "{:,.0f}",
                            "Retorno anualizado XIRR (%)": "{:.1f}%",
                            "Max drawdown (%)": "{:.1f}%",
                            "% operaciones ganadoras": "{:.0f}%",
                        }
                    ),
                    width="stretch", hide_index=True,
                )

    st.divider()
    st.subheader("📶 Buy the Dip con Apalancamiento Escalonado")
    st.caption(
        "Estrategia escalonada: mientras el mercado está cerca de su máximo de 52 semanas, todo va al "
        "activo base (QQQ). Al cruzar el primer umbral de caída, la posición existente rota hacia el "
        "primer activo apalancado y los aportes nuevos también van ahí. Si elegís un segundo nivel y la "
        "caída se profundiza más allá del segundo umbral, rota hacia el segundo apalancado. La "
        "recuperación es asimétrica: solo cambia hacia dónde van los aportes **nuevos** — el capital que "
        "ya escaló de nivel se queda ahí según el modo de salida que elijas abajo."
    )
    st.caption(
        f"Usa el mismo plan de aportes de 'Simulación con Datos Históricos': \\${aporte_inicial_hist:,.0f} "
        f"inicial + \\${aporte_periodico_hist:,.0f} {frecuencia_aporte_hist.lower()}, desde "
        f"{fecha_inicio_hist:%b %Y} hasta {fecha_fin_hist:%b %Y}."
    )

    ticker_senal_tiered = st.selectbox(
        "Activo que genera la señal (drawdown desde su máximo de 52 semanas)", ["QQQ", "SPY"],
        key="ticker_senal_tiered",
    )

    st.markdown("**Configuración de niveles**")
    nc1, nc2 = st.columns(2)
    n_niveles_tiered = nc1.radio(
        "¿Cuántos niveles apalancados?", ["1 nivel", "2 niveles"], index=1, key="n_niveles_tiered",
        horizontal=True,
    )
    modo_salida_tiered = nc2.radio(
        "¿Qué hacer con las posiciones apalancadas?",
        ["Vender al llegar al horizonte", "Mantener siempre (nunca vender, solo escalar)"],
        index=0, key="modo_salida_tiered", horizontal=True,
    )
    mantener_apalancamiento_tiered = modo_salida_tiered.startswith("Mantener")
    dos_niveles_tiered = n_niveles_tiered == "2 niveles"

    _opciones_apalancadas = [t for t in TICKERS_DISPONIBLES if t != "QQQ"]
    tc1, tc2 = st.columns(2)
    _default_n1 = "QLD" if "QLD" in _opciones_apalancadas else _opciones_apalancadas[0]
    ticker_tier1_tiered = tc1.selectbox(
        "Activo Nivel 1 (apalancado)", _opciones_apalancadas,
        index=_opciones_apalancadas.index(_default_n1), key="ticker_tier1_tiered",
    )
    umbral_tier1_tiered = tc2.number_input(
        "Umbral Nivel 1 — base → Nivel 1 (%)", min_value=-50.0, max_value=-1.0, value=-5.0, step=1.0,
        key="umbral_tier1_tiered",
    )

    if dos_niveles_tiered:
        tc3, tc4 = st.columns(2)
        _default_n2 = "TQQQ" if "TQQQ" in _opciones_apalancadas else _opciones_apalancadas[-1]
        ticker_tier2_tiered = tc3.selectbox(
            "Activo Nivel 2 (más apalancado)", _opciones_apalancadas,
            index=_opciones_apalancadas.index(_default_n2), key="ticker_tier2_tiered",
        )
        umbral_tier2_tiered = tc4.number_input(
            "Umbral Nivel 2 — Nivel 1 → Nivel 2 (%)", min_value=-90.0, max_value=-2.0, value=-10.0, step=1.0,
            key="umbral_tier2_tiered",
        )
    else:
        ticker_tier2_tiered = None
        umbral_tier2_tiered = None

    if not mantener_apalancamiento_tiered:
        horizonte_tiered_lbl = st.selectbox(
            "Horizonte de salida (N) — desde la primera caída del episodio",
            [h for h, _ in HORIZONTES_DIAS_HABILES], index=5, key="horizonte_tiered",
        )
    else:
        horizonte_tiered_lbl = None
        st.caption(
            "Modo 'Mantener': las posiciones apalancadas nunca se venden automáticamente por tiempo — "
            "solo se sigue invirtiendo cada aporte nuevo según el nivel que corresponda al drawdown de "
            "ese momento. Útil para ver qué tan bien (o mal) va simplemente sostener el apalancamiento "
            "sin vender nunca."
        )

    with st.spinner("Descargando precios diarios..."):
        precio_tier0_tiered = obtener_serie_diaria(
            "QQQ", fecha_inicio_hist, fecha_fin_hist,
            expense_ratio_anual_pct=expense_ratio_anual_pct, financing_rate_anual_pct=financing_rate_anual_pct,
        )
        precio_tier1_diario_tiered = obtener_serie_diaria(
            ticker_tier1_tiered, fecha_inicio_hist, fecha_fin_hist,
            expense_ratio_anual_pct=expense_ratio_anual_pct, financing_rate_anual_pct=financing_rate_anual_pct,
        )
        precio_tier2_diario_tiered = (
            obtener_serie_diaria(
                ticker_tier2_tiered, fecha_inicio_hist, fecha_fin_hist,
                expense_ratio_anual_pct=expense_ratio_anual_pct,
                financing_rate_anual_pct=financing_rate_anual_pct,
            )
            if dos_niveles_tiered else None
        )
        precio_senal_tiered = (
            precio_tier0_tiered if ticker_senal_tiered == "QQQ"
            else obtener_serie_diaria(
                "SPY", fecha_inicio_hist, fecha_fin_hist,
                expense_ratio_anual_pct=expense_ratio_anual_pct, financing_rate_anual_pct=financing_rate_anual_pct,
            )
        )

    _precios_faltantes = (
        precio_tier0_tiered.empty or precio_tier1_diario_tiered.empty
        or (dos_niveles_tiered and precio_tier2_diario_tiered.empty)
    )
    if _precios_faltantes:
        st.warning("No se pudieron descargar los precios necesarios para este período.")
    else:
        precios_apalancados_tiered = [precio_tier1_diario_tiered] + (
            [precio_tier2_diario_tiered] if dos_niveles_tiered else []
        )
        tier_nombres_actual = {0: "QQQ", 1: ticker_tier1_tiered}
        if dos_niveles_tiered:
            tier_nombres_actual[2] = ticker_tier2_tiered

        if dos_niveles_tiered and umbral_tier2_tiered >= umbral_tier1_tiered:
            st.error("El umbral Nivel 2 debe ser una caída más profunda (más negativa) que el umbral Nivel 1.")
        else:
            st.divider()
            st.markdown("##### 🔁 Backtest de una combinación")

            umbrales_tiered = [umbral_tier1_tiered] + ([umbral_tier2_tiered] if dos_niveles_tiered else [])
            horizonte_tiered_dias = (
                dict(HORIZONTES_DIAS_HABILES)[horizonte_tiered_lbl] if horizonte_tiered_lbl else None
            )

            resultado_tiered = backtest_tiered_leverage(
                precio_senal_tiered, precio_tier0_tiered, precios_apalancados_tiered, umbrales_tiered,
                horizonte_tiered_dias, aporte_inicial_hist, aporte_periodico_hist, frecuencia_aporte_hist,
                mantener_apalancamiento=mantener_apalancamiento_tiered,
            )

            if resultado_tiered.n_episodios == 0:
                st.warning("No hubo caídas suficientes para activar ningún episodio con estos parámetros.")
            else:
                tm1, tm2, tm3, tm4, tm5 = st.columns(5)
                tm1.metric("Total aportado", f"${resultado_tiered.total_aportado:,.0f}")
                tm2.metric("Valor final", f"${resultado_tiered.valor_final:,.0f}")
                tm3.metric("Retorno anualizado (XIRR)", f"{resultado_tiered.retorno_anualizado_pct:.1f}%")
                tm4.metric("Max drawdown de la estrategia", f"{resultado_tiered.max_drawdown_pct:.1f}%")
                tm5.metric("N° episodios", f"{resultado_tiered.n_episodios}")

                edades_tiered = [
                    _edad_en_anio(f.year, edad_actual_hist, anio_actual)
                    for f in resultado_tiered.serie_valor.index
                ]
                fig_tiered = go.Figure()
                fig_tiered.add_trace(
                    go.Scatter(
                        x=resultado_tiered.serie_aportado.index, y=resultado_tiered.serie_aportado.values,
                        mode="lines", name="Aportado acumulado",
                        line=dict(color=BRAND_MUTED, dash="dash", width=2),
                        customdata=edades_tiered,
                        hovertemplate="%{x|%b %Y} (edad %{customdata})<br>Aportado: %{y:$,.0f}<extra></extra>",
                    )
                )
                fig_tiered.add_trace(
                    go.Scatter(
                        x=resultado_tiered.serie_valor.index, y=resultado_tiered.serie_valor.values,
                        mode="lines", name="Valor de la estrategia",
                        line=dict(color=BRAND_GREEN, width=3),
                        customdata=edades_tiered,
                        hovertemplate="%{x|%b %Y} (edad %{customdata})<br>Valor: %{y:$,.0f}<extra></extra>",
                    )
                )
                _agregar_marcadores_entrada_salida(
                    fig_tiered,
                    [ep.fecha_inicio for ep in resultado_tiered.episodios],
                    [ep.fecha_fin for ep in resultado_tiered.episodios if not ep.abierto_al_final],
                    resultado_tiered.serie_valor,
                )
                fig_tiered.update_layout(
                    template=PLOTLY_TEMPLATE, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    hovermode="x unified", dragmode="pan",
                    xaxis=_eje_x_anio_edad_fechas(
                        int(fecha_inicio_hist.year), int(fecha_fin_hist.year), edad_actual_hist, anio_actual
                    ),
                    yaxis=dict(
                        title="$", fixedrange=False, showgrid=True, gridcolor=PLOTLY_GRIDCOLOR,
                        tickprefix="$", separatethousands=True, rangemode="tozero", minallowed=0,
                    ),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    margin=dict(l=10, r=10, t=40, b=10),
                )
                fig_tiered.update_xaxes(fixedrange=False)
                st.plotly_chart(fig_tiered, config=PLOTLY_CONFIG_ZOOM, width="stretch")
                if mantener_apalancamiento_tiered:
                    st.caption(
                        "🔺 verde = inicio del (único) episodio, la primera caída que disparó la primera "
                        "escalada — en modo 'Mantener' nunca hay 🔻 rojo porque las posiciones jamás se "
                        "liquidan. 🔍 Podés hacer scroll para zoom y clic + arrastrar para moverte en el "
                        "tiempo."
                    )
                else:
                    st.caption(
                        "🔺 verde = inicio de un episodio (primera caída que lo disparó), 🔻 rojo = fin del "
                        "episodio (todo vendido y consolidado de vuelta en el activo base). 🔍 Podés hacer "
                        "scroll para zoom y clic + arrastrar para moverte en el tiempo."
                    )

                fig_tier_ocup = go.Figure()
                fig_tier_ocup.add_trace(
                    go.Scatter(
                        x=resultado_tiered.serie_tier.index, y=resultado_tiered.serie_tier.values,
                        mode="lines", line=dict(color=BRAND_BLUE, width=1.5, shape="hv"), fill="tozeroy",
                        showlegend=False,
                        hovertemplate="%{x|%b %Y}: nivel %{y}<extra></extra>",
                    )
                )
                _tickvals_ocup = sorted(tier_nombres_actual.keys())
                fig_tier_ocup.update_layout(
                    template=PLOTLY_TEMPLATE, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    height=140, margin=dict(l=10, r=10, t=10, b=10),
                    xaxis=dict(fixedrange=True, showticklabels=False),
                    yaxis=dict(
                        title="Nivel", fixedrange=True, tickmode="array", tickvals=_tickvals_ocup,
                        ticktext=[tier_nombres_actual[t] for t in _tickvals_ocup],
                        range=[-0.1, max(_tickvals_ocup) + 0.3],
                    ),
                )
                st.plotly_chart(fig_tier_ocup, config=PLOTLY_CONFIG, width="stretch")
                st.caption(
                    "Nivel ocupado por la posición escalada a lo largo del tiempo (0 = activo base en modo "
                    "normal, sin episodio activo)."
                )

                with st.expander("Ver historial de episodios"):
                    df_episodios = pd.DataFrame(
                        [
                            {
                                "Inicio": ep.fecha_inicio.date(),
                                "Fin": ep.fecha_fin.date(),
                                "Nivel máximo alcanzado": tier_nombres_actual[ep.tier_maximo],
                                "Retorno (%)": ep.retorno_pct,
                                "Estado": "Abierto (a mercado)" if ep.abierto_al_final else "Cerrado",
                            }
                            for ep in resultado_tiered.episodios
                        ]
                    )
                    st.dataframe(
                        df_episodios.style.format({"Retorno (%)": "{:.1f}%"}), width="stretch", hide_index=True,
                    )

        st.divider()
        st.markdown("##### 🔬 Barrido de combinaciones (para descubrir qué umbrales funcionan mejor)")
        st.caption(
            "Corre el mismo backtest para muchas combinaciones de umbrales"
            + (" y horizontes de salida" if not mantener_apalancamiento_tiered else "")
            + ", y ordena los resultados — para explorar con evidencia, no adivinar, qué tan profunda "
            "debería ser la caída que dispara cada rotación de apalancamiento."
        )

        if dos_niveles_tiered:
            sc1, sc2 = st.columns(2)
        else:
            sc1, sc2 = st, None

        sc1.caption(f"Rango del umbral Nivel 1 (QQQ → {ticker_tier1_tiered})")
        r1a, r1b = sc1.columns(2)
        u1_min = r1a.number_input(
            "Desde (%)", min_value=-50.0, max_value=-1.0, value=-8.0, step=1.0, key="u1_min"
        )
        u1_max = r1b.number_input(
            "Hasta (%)", min_value=-50.0, max_value=-1.0, value=-3.0, step=1.0, key="u1_max"
        )
        umbrales_por_nivel_tiered = [
            [float(u) for u in range(int(min(u1_min, u1_max)), int(max(u1_min, u1_max)) + 1)]
        ]

        if dos_niveles_tiered:
            sc2.caption(f"Rango del umbral Nivel 2 ({ticker_tier1_tiered} → {ticker_tier2_tiered})")
            r2a, r2b = sc2.columns(2)
            u2_min = r2a.number_input(
                "Desde (%)", min_value=-90.0, max_value=-2.0, value=-15.0, step=1.0, key="u2_min"
            )
            u2_max = r2b.number_input(
                "Hasta (%)", min_value=-90.0, max_value=-2.0, value=-8.0, step=1.0, key="u2_max"
            )
            umbrales_por_nivel_tiered.append(
                [float(u) for u in range(int(min(u2_min, u2_max)), int(max(u2_min, u2_max)) + 1)]
            )

        if not mantener_apalancamiento_tiered:
            horizontes_barrido = st.multiselect(
                "Horizontes de salida a incluir en el barrido",
                [h for h, _ in HORIZONTES_DIAS_HABILES],
                default=["6 meses", "1 año", "2 años"], key="horizontes_barrido",
            )
            horizontes_dict = dict(HORIZONTES_DIAS_HABILES)
            horizontes_seleccionados = [(h, horizontes_dict[h]) for h in horizontes_barrido]
        else:
            horizontes_seleccionados = []
            st.caption("Modo 'Mantener': el barrido no necesita horizontes (nunca se vende por tiempo).")

        if dos_niveles_tiered:
            combos_umbrales_validos = sum(
                1 for u1 in umbrales_por_nivel_tiered[0] for u2 in umbrales_por_nivel_tiered[1] if u2 < u1
            )
        else:
            combos_umbrales_validos = len(umbrales_por_nivel_tiered[0])
        n_combos_estimado = combos_umbrales_validos * (
            1 if mantener_apalancamiento_tiered else max(1, len(horizontes_seleccionados))
        )
        tiempo_estimado_seg = n_combos_estimado * 0.1

        st.caption(
            f"Esto va a correr **{n_combos_estimado:,}** combinaciones — un estimado de "
            f"**{tiempo_estimado_seg:.0f} segundos**. Si es demasiado, angosta los rangos o quita horizontes."
        )

        _faltan_horizontes = (not mantener_apalancamiento_tiered) and not horizontes_seleccionados
        if n_combos_estimado == 0 or _faltan_horizontes:
            st.info("Elegí al menos un horizonte (si aplica) y un rango válido para poder correr el barrido.")
        elif st.button("▶️ Ejecutar barrido", key="ejecutar_barrido_tiered"):
            barra = st.progress(0.0, text="Corriendo combinaciones...")

            def _progreso(hecho, total):
                barra.progress(hecho / total, text=f"Corriendo combinaciones... {hecho}/{total}")

            df_barrido = sweep_tiered_leverage(
                precio_senal_tiered, precio_tier0_tiered, precios_apalancados_tiered,
                umbrales_por_nivel_tiered, horizontes_seleccionados,
                aporte_inicial_hist, aporte_periodico_hist, frecuencia_aporte_hist,
                mantener_apalancamiento=mantener_apalancamiento_tiered,
                progreso_callback=_progreso,
            )
            barra.empty()

            if df_barrido.empty:
                st.warning(
                    "Ninguna combinación produjo resultados (revisá que el umbral Nivel 2 sea más "
                    "profundo que el Nivel 1, si aplica)."
                )
            else:
                st.session_state["df_barrido_tiered"] = df_barrido.sort_values(
                    "Valor Final ($)", ascending=False
                ).reset_index(drop=True)

        if "df_barrido_tiered" in st.session_state:
            df_mostrar_barrido = st.session_state["df_barrido_tiered"]
            st.markdown(
                f"**Resultados del barrido — {len(df_mostrar_barrido)} combinaciones, ordenadas por Valor Final**"
            )
            _formato_barrido = {
                "Valor Final ($)": "{:,.0f}",
                "Total Aportado ($)": "{:,.0f}",
                "Retorno anualizado XIRR (%)": "{:.1f}%",
                "Max drawdown (%)": "{:.1f}%",
            }
            for _col in df_mostrar_barrido.columns:
                if _col.startswith("Umbral Nivel"):
                    _formato_barrido[_col] = "{:.0f}%"
            st.dataframe(
                df_mostrar_barrido.style.format(_formato_barrido), width="stretch", hide_index=True,
            )

# ----------------------------------------------------------
# TAB 4 (continuación) — FRACCIONES DE CAPITAL POR UMBRAL
# ----------------------------------------------------------
with tab_avanzado:
    st.divider()
    st.subheader("🧩 Fracciones de Capital por Umbral")
    st.caption(
        "Otra forma de escalar: en vez de rotar TODO el capital entre niveles (como en la sección "
        "anterior), acá el capital se divide en partes iguales desde el principio. Cada parte tiene su "
        "propio umbral y su propio activo — cuando la caída llega a SU umbral, esa parte (y solo esa) "
        "rota hacia su activo asignado y se queda ahí (no hay marcha atrás por recuperación del mercado) "
        "hasta alcanzar SU propio porcentaje de ganancia objetivo; ahí se vende todo, vuelve al activo "
        "base, y queda lista para activarse de nuevo en una futura caída. Los aportes que lleguen "
        "mientras una fracción está activa se invierten ahí mismo, promediando el precio si está en "
        "pérdida. Podés dejar alguna parte siempre en el activo base si no querés apalancar el 100% del "
        "capital."
    )
    st.caption(
        f"Usa el mismo plan de aportes de 'Simulación con Datos Históricos': \\${aporte_inicial_hist:,.0f} "
        f"inicial + \\${aporte_periodico_hist:,.0f} {frecuencia_aporte_hist.lower()}, desde "
        f"{fecha_inicio_hist:%b %Y} hasta {fecha_fin_hist:%b %Y}. Cada aporte se reparte en partes "
        "iguales entre las fracciones."
    )

    fc0_1, fc0_2, fc0_3 = st.columns(3)
    ticker_senal_frac = fc0_1.selectbox(
        "Activo que genera la señal", ["QQQ", "SPY"], key="ticker_senal_frac",
    )
    ticker_base_frac = fc0_2.selectbox(
        "Activo base (donde vive el capital mientras no está escalado)", TICKERS_DISPONIBLES,
        index=TICKERS_DISPONIBLES.index("QQQ") if "QQQ" in TICKERS_DISPONIBLES else 0,
        key="ticker_base_frac",
    )
    n_fracciones_frac = int(
        fc0_3.number_input(
            "¿En cuántas fracciones dividir el capital?", min_value=2, max_value=6, value=3, step=1,
            key="n_fracciones_frac",
        )
    )

    st.markdown("**Configuración de cada fracción** (todas del mismo tamaño — 1/N cada una)")
    _opciones_apalancadas_frac = [t for t in TICKERS_DISPONIBLES if t != ticker_base_frac]
    configs_frac_ui: list[ConfigFraccion] = []
    for k in range(n_fracciones_frac):
        fk1, fk2, fk3, fk4 = st.columns([1.3, 1, 1, 1])
        escala_k = fk1.checkbox(
            f"Fracción {k + 1}: ¿escala a un activo apalancado?", value=(k > 0), key=f"frac_escala_{k}",
        )
        if escala_k:
            _default_activo_k = "TQQQ" if "TQQQ" in _opciones_apalancadas_frac else _opciones_apalancadas_frac[0]
            activo_k = fk2.selectbox(
                f"Activo Fracción {k + 1}", _opciones_apalancadas_frac,
                index=_opciones_apalancadas_frac.index(_default_activo_k), key=f"frac_activo_{k}",
            )
            umbral_k = fk3.number_input(
                f"Umbral Fracción {k + 1} (%)", min_value=-90.0, max_value=-1.0,
                value=max(-90.0, -5.0 * (k + 1)), step=1.0, key=f"frac_umbral_{k}",
            )
            ganancia_k = fk4.number_input(
                f"Ganancia objetivo {k + 1} (%)", min_value=5.0, max_value=1000.0,
                value=100.0 * (k + 1), step=5.0, key=f"frac_ganancia_{k}",
                help="Al llegar a esta ganancia se vende TODO y vuelve al activo base. Pedile más a un "
                "activo más apalancado (p.ej. 300% a TQQQ vs. 100% a QLD).",
            )
            configs_frac_ui.append(
                ConfigFraccion(activo=activo_k, umbral_pct=umbral_k, ganancia_objetivo_pct=ganancia_k)
            )
        else:
            fk2.caption(f"Se queda siempre en {ticker_base_frac} (activo base).")
            configs_frac_ui.append(ConfigFraccion(activo=None, umbral_pct=None, ganancia_objetivo_pct=None))

    with st.spinner("Descargando precios diarios..."):
        precio_base_frac = obtener_serie_diaria(
            ticker_base_frac, fecha_inicio_hist, fecha_fin_hist,
            expense_ratio_anual_pct=expense_ratio_anual_pct, financing_rate_anual_pct=financing_rate_anual_pct,
        )
        precio_senal_frac = (
            precio_base_frac if ticker_senal_frac == ticker_base_frac
            else obtener_serie_diaria(
                ticker_senal_frac, fecha_inicio_hist, fecha_fin_hist,
                expense_ratio_anual_pct=expense_ratio_anual_pct, financing_rate_anual_pct=financing_rate_anual_pct,
            )
        )
        _tickers_usados_frac = sorted({c.activo for c in configs_frac_ui if c.activo is not None})
        precios_por_activo_frac = {
            t: obtener_serie_diaria(
                t, fecha_inicio_hist, fecha_fin_hist,
                expense_ratio_anual_pct=expense_ratio_anual_pct, financing_rate_anual_pct=financing_rate_anual_pct,
            )
            for t in _tickers_usados_frac
        }

    _precios_faltantes_frac = (
        precio_base_frac.empty or precio_senal_frac.empty
        or any(p.empty for p in precios_por_activo_frac.values())
    )
    if _precios_faltantes_frac:
        st.warning("No se pudieron descargar los precios necesarios para este período.")
    else:
        st.divider()
        st.markdown("##### 🔁 Backtest de una combinación")

        resultado_frac = backtest_fracciones_capital(
            precio_senal_frac, precio_base_frac, configs_frac_ui, precios_por_activo_frac,
            aporte_inicial_hist, aporte_periodico_hist, frecuencia_aporte_hist,
        )

        if resultado_frac.valor_final == 0:
            st.warning("No se pudo calcular ningún resultado con estos parámetros.")
        else:
            fm1, fm2, fm3, fm4 = st.columns(4)
            fm1.metric("Total aportado", f"${resultado_frac.total_aportado:,.0f}")
            fm2.metric("Valor final", f"${resultado_frac.valor_final:,.0f}")
            fm3.metric("Retorno anualizado (XIRR)", f"{resultado_frac.retorno_anualizado_pct:.1f}%")
            fm4.metric(
                "Max drawdown", f"{resultado_frac.max_drawdown_pct:.1f}%",
                help="La peor caída (de pico a valle) que sufrió el VALOR de la estrategia en todo el "
                "período — no el precio de un activo, sino tu cuenta siguiendo esta estrategia.",
            )

            _todas_entradas_frac = [t.fecha_entrada for f in resultado_frac.fracciones for t in f.trades]
            _todas_salidas_frac = [
                t.fecha_salida for f in resultado_frac.fracciones for t in f.trades if not t.abierta_al_final
            ]

            edades_frac = [
                _edad_en_anio(f.year, edad_actual_hist, anio_actual)
                for f in resultado_frac.serie_valor.index
            ]
            fig_frac = go.Figure()
            fig_frac.add_trace(
                go.Scatter(
                    x=resultado_frac.serie_aportado.index, y=resultado_frac.serie_aportado.values,
                    mode="lines", name="Aportado acumulado",
                    line=dict(color=BRAND_MUTED, dash="dash", width=2),
                    customdata=edades_frac,
                    hovertemplate="%{x|%b %Y} (edad %{customdata})<br>Aportado: %{y:$,.0f}<extra></extra>",
                )
            )
            fig_frac.add_trace(
                go.Scatter(
                    x=resultado_frac.serie_valor.index, y=resultado_frac.serie_valor.values,
                    mode="lines", name="Valor de la estrategia",
                    line=dict(color=BRAND_GREEN, width=3),
                    customdata=edades_frac,
                    hovertemplate="%{x|%b %Y} (edad %{customdata})<br>Valor: %{y:$,.0f}<extra></extra>",
                )
            )
            _agregar_marcadores_entrada_salida(
                fig_frac, _todas_entradas_frac, _todas_salidas_frac, resultado_frac.serie_valor,
            )
            fig_frac.update_layout(
                template=PLOTLY_TEMPLATE, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                hovermode="x unified", dragmode="pan",
                xaxis=_eje_x_anio_edad_fechas(
                    int(fecha_inicio_hist.year), int(fecha_fin_hist.year), edad_actual_hist, anio_actual
                ),
                yaxis=dict(
                    title="$", fixedrange=False, showgrid=True, gridcolor=PLOTLY_GRIDCOLOR,
                    tickprefix="$", separatethousands=True, rangemode="tozero", minallowed=0,
                ),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                margin=dict(l=10, r=10, t=40, b=10),
            )
            fig_frac.update_xaxes(fixedrange=False)
            st.plotly_chart(fig_frac, config=PLOTLY_CONFIG_ZOOM, width="stretch")
            st.caption(
                "🔺 verde = entrada de una fracción (rota hacia su activo apalancado), 🔻 rojo = salida "
                "(alcanzó su ganancia objetivo y vuelve al activo base). 🔍 Podés hacer scroll para zoom y "
                "clic + arrastrar para moverte en el tiempo."
            )

            st.markdown("**Análisis por fracción**")
            df_fracciones = pd.DataFrame(
                [
                    {
                        "Fracción": f"Fracción {f.indice + 1}",
                        "Activo": f.activo,
                        "Umbral (%)": f.umbral_pct,
                        "Ganancia objetivo (%)": f.ganancia_objetivo_pct,
                        "N° operaciones": f.n_trades,
                        "Ganancia generada ($)": f.ganancia_generada,
                        "Días promedio por operación": f.dias_promedio_por_trade,
                        "Max drawdown (%)": f.max_drawdown_pct,
                        "Valor final ($)": f.valor_final,
                    }
                    for f in resultado_frac.fracciones
                ]
            )
            st.dataframe(
                df_fracciones.style.format(
                    {
                        "Umbral (%)": "{:.0f}%",
                        "Ganancia objetivo (%)": "{:.0f}%",
                        "Ganancia generada ($)": "{:,.0f}",
                        "Días promedio por operación": "{:.0f}",
                        "Max drawdown (%)": "{:.1f}%",
                        "Valor final ($)": "{:,.0f}",
                    },
                    na_rep="—",
                ),
                width="stretch", hide_index=True,
            )
            st.caption(
                "\"Días promedio por operación\" = cuánto tardó en promedio cada ciclo de esa fracción en "
                "llegar a su ganancia objetivo (o marcarse a mercado, si sigue abierta). \"Max drawdown\" "
                "acá es de la SERIE de esa fracción durante todo el período (mezcla los tramos parqueados "
                "en el activo base, de bajo riesgo, con los tramos apalancados)."
            )

            with st.expander("Ver historial de operaciones, fracción por fracción"):
                _filas_trades_frac = [
                    {
                        "Fracción": f"Fracción {f.indice + 1}",
                        "Activo": t.activo,
                        "Entrada": t.fecha_entrada.date(),
                        "Salida": t.fecha_salida.date(),
                        "Días mantenida": t.dias_mantenida,
                        "Monto invertido ($)": t.monto_invertido,
                        "Valor al salir ($)": t.valor_al_salir,
                        "Retorno (%)": t.retorno_pct,
                        "Ganancia objetivo (%)": t.ganancia_objetivo_pct,
                        "Peor drawdown del trade (%)": t.peor_drawdown_pct,
                        "Estado": "Abierta (a mercado)" if t.abierta_al_final else "Cerrada",
                    }
                    for f in resultado_frac.fracciones
                    for t in f.trades
                ]
                if not _filas_trades_frac:
                    st.caption("Ninguna fracción activó una posición todavía con estos parámetros.")
                else:
                    df_trades_frac = pd.DataFrame(_filas_trades_frac)
                    st.dataframe(
                        df_trades_frac.style.format(
                            {
                                "Monto invertido ($)": "{:,.0f}",
                                "Valor al salir ($)": "{:,.0f}",
                                "Retorno (%)": "{:.1f}%",
                                "Ganancia objetivo (%)": "{:.0f}%",
                                "Peor drawdown del trade (%)": "{:.1f}%",
                            }
                        ),
                        width="stretch", hide_index=True,
                    )

        st.divider()
        st.markdown("##### 🔬 Barrido de todas las combinaciones (umbral × ganancia objetivo)")
        st.caption(
            "Corre el mismo backtest para TODAS las combinaciones de umbral Y ganancia objetivo de las "
            "fracciones que escalan (los activos elegidos arriba se mantienen fijos), y ordena los "
            "resultados — sin que tengas que ir probando combinación por combinación."
        )

        _indices_escalables_frac = [k for k, c in enumerate(configs_frac_ui) if c.activo is not None]
        if not _indices_escalables_frac:
            st.info("Ninguna fracción está configurada para escalar — no hay nada que barrer.")
        else:
            umbrales_por_fraccion_frac: dict[int, list[float]] = {}
            ganancias_por_fraccion_frac: dict[int, list[float]] = {}
            for k in _indices_escalables_frac:
                st.caption(f"Fracción {k + 1} ({configs_frac_ui[k].activo})")
                ra, rb, rc, rd = st.columns(4)
                u_min = ra.number_input(
                    "Umbral desde (%)", min_value=-90.0, max_value=-1.0, value=-10.0, step=1.0,
                    key=f"frac_u_min_{k}",
                )
                u_max = rb.number_input(
                    "Umbral hasta (%)", min_value=-90.0, max_value=-1.0, value=-4.0, step=1.0,
                    key=f"frac_u_max_{k}",
                )
                g_min = rc.number_input(
                    "Ganancia desde (%)", min_value=5.0, max_value=1000.0, value=100.0, step=50.0,
                    key=f"frac_g_min_{k}",
                )
                g_max = rd.number_input(
                    "Ganancia hasta (%)", min_value=5.0, max_value=1000.0, value=300.0, step=50.0,
                    key=f"frac_g_max_{k}",
                )
                umbrales_por_fraccion_frac[k] = [
                    float(u) for u in range(int(min(u_min, u_max)), int(max(u_min, u_max)) + 1)
                ]
                ganancias_por_fraccion_frac[k] = [
                    float(g) for g in range(int(min(g_min, g_max)), int(max(g_min, g_max)) + 1, 100)
                ]

            n_combos_frac = 1
            for k in _indices_escalables_frac:
                n_combos_frac *= max(1, len(umbrales_por_fraccion_frac[k])) * max(
                    1, len(ganancias_por_fraccion_frac[k])
                )
            tiempo_estimado_frac = n_combos_frac * 0.1

            st.caption(
                f"Esto va a correr **{n_combos_frac:,}** combinaciones — un estimado de "
                f"**{tiempo_estimado_frac:.0f} segundos**. Si es demasiado, angosta los rangos."
            )

            _columnas_variables_frac = []
            for k in _indices_escalables_frac:
                _columnas_variables_frac.append(f"Umbral Fracción {k + 1} (%)")
                _columnas_variables_frac.append(f"Ganancia obj. Fracción {k + 1} (%)")

            if n_combos_frac == 0:
                st.info("Elegí un rango válido para poder correr el barrido.")
            elif n_combos_frac > 5000:
                st.warning(
                    f"{n_combos_frac:,} combinaciones es demasiado para correr de forma interactiva — "
                    "angosta los rangos (por debajo de 5,000 combinaciones)."
                )
            elif st.button("▶️ Ejecutar barrido", key="ejecutar_barrido_frac"):
                barra_frac = st.progress(0.0, text="Corriendo combinaciones...")

                def _progreso_frac(hecho, total):
                    barra_frac.progress(hecho / total, text=f"Corriendo combinaciones... {hecho}/{total}")

                df_barrido_frac = sweep_fracciones_capital(
                    precio_senal_frac, precio_base_frac, configs_frac_ui, precios_por_activo_frac,
                    umbrales_por_fraccion_frac, ganancias_por_fraccion_frac,
                    aporte_inicial_hist, aporte_periodico_hist,
                    frecuencia_aporte_hist, progreso_callback=_progreso_frac,
                )
                barra_frac.empty()

                if df_barrido_frac.empty:
                    st.warning("Ninguna combinación produjo resultados.")
                else:
                    st.session_state["df_barrido_frac"] = df_barrido_frac.sort_values(
                        "Valor Final ($)", ascending=False
                    ).reset_index(drop=True)
                    st.session_state["cols_barrido_frac"] = _columnas_variables_frac

            if "df_barrido_frac" in st.session_state:
                df_mostrar_barrido_frac = st.session_state["df_barrido_frac"]
                st.markdown(
                    f"**Resultados del barrido — {len(df_mostrar_barrido_frac)} combinaciones, "
                    "ordenadas por Valor Final**"
                )
                _formato_barrido_frac = {
                    "Valor Final ($)": "{:,.0f}",
                    "Total Aportado ($)": "{:,.0f}",
                    "Retorno anualizado XIRR (%)": "{:.1f}%",
                    "Max drawdown (%)": "{:.1f}%",
                }
                for _col in df_mostrar_barrido_frac.columns:
                    if _col.startswith("Umbral Fracción") or _col.startswith("Ganancia obj."):
                        _formato_barrido_frac[_col] = "{:.0f}%"
                st.dataframe(
                    df_mostrar_barrido_frac.style.format(_formato_barrido_frac),
                    width="stretch", hide_index=True,
                )

                st.markdown("**📝 Conclusión del barrido**")
                _cols_conclusion = st.session_state.get("cols_barrido_frac", [])
                _cols_conclusion_validas = [c for c in _cols_conclusion if c in df_mostrar_barrido_frac.columns]
                st.markdown(generar_conclusion_barrido(df_mostrar_barrido_frac, _cols_conclusion_validas))


# ----------------------------------------------------------
# TAB 5 — BACKTESTING AVANZADO V2 (marco reproducible, sin look-ahead)
# ----------------------------------------------------------
_FORMATO_METRICAS_V2 = {
    "CAGR (%)": "{:.2f}%", "Vol. anual (%)": "{:.1f}%", "Sharpe": "{:.2f}",
    "Sortino": "{:.2f}", "Max Drawdown (%)": "{:.1f}%", "Calmar": "{:.2f}",
    "Meses bajo agua": "{:.0f}", "% meses positivos": "{:.0f}%",
    "Rolling 12m p5 (%)": "{:.1f}%", "Rolling 12m p50 (%)": "{:.1f}%", "Rolling 12m p95 (%)": "{:.1f}%",
}


def _df_metricas_v2(series_por_nombre: dict, cash) -> pd.DataFrame:
    """Tabla de métricas (filas) × estrategias (columnas), ya formateadas como texto."""
    datos = {nombre: btv2.resumen_metricas(r, cash) for nombre, r in series_por_nombre.items()}
    filas = []
    for met, fmt in _FORMATO_METRICAS_V2.items():
        fila = {"Métrica": met}
        for nombre in series_por_nombre:
            val = datos[nombre][met]
            fila[nombre] = fmt.format(val) if pd.notna(val) else "—"
        filas.append(fila)
    return pd.DataFrame(filas)


def _dd_en_ventana(retornos: pd.Series, inicio: str, fin: str) -> float:
    """Peor drawdown (%) dentro de una ventana de fechas, midiendo desde el inicio de la ventana."""
    r = retornos[(retornos.index >= inicio) & (retornos.index <= fin)].dropna()
    if r.empty:
        return float("nan")
    eq = (1.0 + r).cumprod()
    return float((eq / eq.cummax() - 1.0).min()) * 100.0


def _pie_disclaimer_v2(fig: go.Figure) -> None:
    fig.add_annotation(
        text=DISCLAIMER_V2, xref="paper", yref="paper", x=0.0, y=-0.18, showarrow=False,
        font=dict(size=9, color=BRAND_MUTED), align="left", xanchor="left",
    )


with tab_avanzado_v2:
    st.subheader("🔬 Backtesting Avanzado V2 — marco reproducible")
    st.caption(
        "Una segunda generación de backtests construida sobre reglas metodológicas estrictas, pensada "
        "para mostrar a clientes con transparencia. Se van agregando módulos uno por uno; el primero es "
        "el clásico **trend following** de Faber."
    )
    with st.expander("📏 Reglas metodológicas (aplican a todos los módulos V2)"):
        st.markdown(
            """
- **Cero look-ahead:** la señal se calcula con el cierre de **fin de mes** y la posición se aplica el
  **mes siguiente** (nunca se opera con datos que no existían al decidir).
- **Retorno total:** precios ajustados por dividendos y splits (Yahoo Finance, caché local).
- **Efectivo (cash):** retorno mensual de **BIL** (T-bills) desde 2007; antes de 2007 se aproxima con
  una tasa anual fija (3%), porque sin FRED no hay serie de T-bills más larga disponible.
- **Costos:** 0.10% por operación (una vía), siempre incluidos.
- **Validación out-of-sample:** el período hasta **2019-12-31** es *in-sample* y **2020-01-01 a hoy** es
  *holdout* (nunca se ajustan parámetros mirando el holdout). Se reportan por separado.
- **Estabilidad de parámetros:** cada módulo incluye una tabla de sensibilidad. Si el resultado solo
  funciona con un valor exacto, es sospechoso de sobreajuste.

⚠️ *Limitaciones de datos en esta versión:* el historial arranca en ~1990 (no 1927/1950), no hay datos
de **desempleo (FRED)** para los módulos macro, ni de **VIX3M** para la estructura de plazos del VIX.
"""
        )

    st.divider()
    st.markdown("### 1️⃣ Trend Following con SMA de 10 meses (Faber)")
    st.caption(
        "Al cierre de cada mes: si el precio está **por encima** de su promedio móvil (SMA) de N meses → "
        "100% invertido en el activo; si está **por debajo** → 100% en efectivo. El benchmark es "
        "*comprar y mantener* (buy & hold) el mismo activo. La idea no es ganarle en retorno, sino "
        "**capturar casi el mismo retorno con mucho menos drawdown**."
    )

    cbt1, cbt2 = st.columns(2)
    ticker_trend = cbt1.selectbox("Activo", ["SPY", "QQQ"], key="ticker_trend_v2")
    ventana_trend = cbt2.selectbox(
        "Ventana de la SMA (meses)", [6, 8, 10, 12], index=2, key="ventana_trend_v2",
        help="10 meses es el valor clásico de Faber. Podés probar otros para ver qué tan sensible es.",
    )

    try:
        res_trend = btv2.backtest_sma_trend(ticker_trend, ventana_meses=ventana_trend)
    except Exception as e:  # pragma: no cover - defensivo ante datos faltantes
        res_trend = None
        st.error(f"No se pudo correr el backtest: {e}")

    if res_trend is not None and len(res_trend.ret_estrategia) > 12:
        strat = res_trend.ret_estrategia
        bh = res_trend.ret_buy_hold
        cash_v = res_trend.cash

        # --- Métricas: período completo ---
        st.markdown("**📊 Métricas — período completo**")
        st.caption(
            f"Estrategia SMA{ventana_trend} vs. comprar y mantener {ticker_trend}, "
            f"de {strat.index.min():%b %Y} a {strat.index.max():%b %Y}. Costos incluidos."
        )
        st.dataframe(
            _df_metricas_v2({f"Estrategia SMA{ventana_trend}": strat, f"Buy & Hold {ticker_trend}": bh}, cash_v),
            width="stretch", hide_index=True,
        )

        # --- Métricas: in-sample vs holdout ---
        strat_is, strat_ho = btv2.particion_muestra(strat)
        bh_is, bh_ho = btv2.particion_muestra(bh)
        colis, colho = st.columns(2)
        with colis:
            st.markdown("**In-sample (hasta 2019)**")
            if len(strat_is) > 12:
                st.dataframe(
                    _df_metricas_v2({"Estrategia": strat_is, "Buy & Hold": bh_is}, cash_v),
                    width="stretch", hide_index=True,
                )
            else:
                st.caption("Sin suficiente historia in-sample.")
        with colho:
            st.markdown("**Holdout (2020 → hoy)**")
            if len(strat_ho) > 6:
                st.dataframe(
                    _df_metricas_v2({"Estrategia": strat_ho, "Buy & Hold": bh_ho}, cash_v),
                    width="stretch", hide_index=True,
                )
            else:
                st.caption("Sin suficiente historia holdout.")
        st.caption(
            "🔑 El **holdout** es la prueba honesta: son datos que 'no se vieron' al diseñar la regla. Si "
            "la estrategia se sostiene ahí (sobre todo el menor drawdown), es más creíble que un buen "
            "resultado in-sample."
        )

        # --- Curva de capital (log) + drawdown ---
        st.markdown("**📈 Curva de capital (escala log) y drawdown**")
        eq_strat = (1.0 + strat).cumprod()
        eq_bh = (1.0 + bh).cumprod()
        dd_strat = (eq_strat / eq_strat.cummax() - 1.0) * 100.0
        dd_bh = (eq_bh / eq_bh.cummax() - 1.0) * 100.0

        fig_eq = go.Figure()
        fig_eq.add_trace(go.Scatter(
            x=eq_bh.index, y=eq_bh.values, mode="lines", name=f"Buy & Hold {ticker_trend}",
            line=dict(color=BRAND_MUTED, width=2),
            hovertemplate="%{x|%b %Y}<br>Buy & Hold: %{y:.2f}×<extra></extra>",
        ))
        fig_eq.add_trace(go.Scatter(
            x=eq_strat.index, y=eq_strat.values, mode="lines", name=f"Estrategia SMA{ventana_trend}",
            line=dict(color=BRAND_GREEN, width=2.5),
            hovertemplate="%{x|%b %Y}<br>Estrategia: %{y:.2f}×<extra></extra>",
        ))
        fig_eq.update_layout(
            template=PLOTLY_TEMPLATE, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            hovermode="x unified", dragmode="pan",
            yaxis=dict(title="Crecimiento de $1 (log)", type="log", fixedrange=False, gridcolor=PLOTLY_GRIDCOLOR),
            xaxis=dict(title="Año", fixedrange=False, gridcolor=PLOTLY_GRIDCOLOR),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(l=10, r=10, t=40, b=70),
        )
        _pie_disclaimer_v2(fig_eq)
        st.plotly_chart(fig_eq, config=PLOTLY_CONFIG_ZOOM, width="stretch")

        fig_dd = go.Figure()
        fig_dd.add_trace(go.Scatter(
            x=dd_bh.index, y=dd_bh.values, mode="lines", name=f"Buy & Hold {ticker_trend}",
            line=dict(color=BRAND_MUTED, width=1.5), fill="tozeroy", fillcolor="rgba(154,160,166,0.15)",
            hovertemplate="%{x|%b %Y}<br>Buy & Hold: %{y:.1f}%<extra></extra>",
        ))
        fig_dd.add_trace(go.Scatter(
            x=dd_strat.index, y=dd_strat.values, mode="lines", name=f"Estrategia SMA{ventana_trend}",
            line=dict(color="#e0525a", width=2), fill="tozeroy", fillcolor="rgba(224,82,90,0.15)",
            hovertemplate="%{x|%b %Y}<br>Estrategia: %{y:.1f}%<extra></extra>",
        ))
        fig_dd.update_layout(
            template=PLOTLY_TEMPLATE, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            hovermode="x unified", dragmode="pan",
            yaxis=dict(title="Drawdown (%)", fixedrange=False, gridcolor=PLOTLY_GRIDCOLOR),
            xaxis=dict(title="Año", fixedrange=False, gridcolor=PLOTLY_GRIDCOLOR),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(l=10, r=10, t=40, b=70),
        )
        _pie_disclaimer_v2(fig_dd)
        st.plotly_chart(fig_dd, config=PLOTLY_CONFIG_ZOOM, width="stretch")

        # --- Drawdown por crisis (gráfico clave para clientes) ---
        st.markdown("**🛡️ Drawdown por crisis: estrategia vs. comprar y mantener**")
        crisis = [
            ("Puntocom<br>2000–2002", "2000-01-01", "2002-12-31"),
            ("Financiera<br>2008–2009", "2007-10-01", "2009-06-30"),
            ("COVID<br>2020", "2020-01-01", "2020-12-31"),
            ("Inflación<br>2022", "2022-01-01", "2022-12-31"),
        ]
        nombres_c = [c[0] for c in crisis]
        dd_strat_c = [_dd_en_ventana(strat, c[1], c[2]) for c in crisis]
        dd_bh_c = [_dd_en_ventana(bh, c[1], c[2]) for c in crisis]
        fig_crisis = go.Figure()
        fig_crisis.add_trace(go.Bar(
            x=nombres_c, y=dd_bh_c, name=f"Buy & Hold {ticker_trend}", marker_color=BRAND_MUTED,
            text=[f"{v:.0f}%" if pd.notna(v) else "s/d" for v in dd_bh_c], textposition="outside",
        ))
        fig_crisis.add_trace(go.Bar(
            x=nombres_c, y=dd_strat_c, name=f"Estrategia SMA{ventana_trend}", marker_color=BRAND_GREEN,
            text=[f"{v:.0f}%" if pd.notna(v) else "s/d" for v in dd_strat_c], textposition="outside",
        ))
        fig_crisis.update_layout(
            template=PLOTLY_TEMPLATE, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            barmode="group", dragmode=False,
            yaxis=dict(title="Peor drawdown en la ventana (%)", fixedrange=True, gridcolor=PLOTLY_GRIDCOLOR),
            xaxis=dict(fixedrange=True),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(l=10, r=10, t=40, b=70),
        )
        _pie_disclaimer_v2(fig_crisis)
        st.plotly_chart(fig_crisis, config=PLOTLY_CONFIG, width="stretch")
        st.caption(
            "Barras más cortas (menos negativas) = menos dolor. El trend following suele brillar en las "
            "caídas largas y ordenadas (2000–2002, 2008) porque tiene tiempo de salir a efectivo; en "
            "caídas relámpago como marzo 2020 protege menos, porque para cuando la señal mensual dice "
            "'salí', el rebote ya empezó."
        )

        # --- Sensibilidad ---
        st.markdown("**🔬 Sensibilidad a la ventana de la SMA**")
        st.caption(
            "Si la estrategia solo funciona con un número mágico de meses, es sospechosa de sobreajuste. "
            "Queremos ver resultados parecidos across varias ventanas."
        )
        df_sens = btv2.tabla_sensibilidad_trend(ticker_trend, [6, 8, 10, 12])
        st.dataframe(
            df_sens.style.format({
                "CAGR (%)": "{:.2f}%", "Vol. anual (%)": "{:.1f}%", "Sharpe": "{:.2f}",
                "Max Drawdown (%)": "{:.1f}%", "Calmar": "{:.2f}", "N° operaciones": "{:.0f}",
            }),
            width="stretch", hide_index=True,
        )

        # --- Operaciones (whipsaws) ---
        ops = res_trend.operaciones
        n_whipsaw = sum(1 for o in ops if o.meses <= 2)
        n_perdedoras = sum(1 for o in ops if o.retorno_pct < 0)
        st.markdown("**🔁 Operaciones y señales falsas (whipsaws)**")
        m1, m2, m3 = st.columns(3)
        m1.metric("Total de operaciones", f"{len(ops)}")
        m2.metric("Whipsaws (≤ 2 meses)", f"{n_whipsaw}", help="Entradas cortas que suelen ser señales falsas y solo cuestan comisiones.")
        m3.metric("Operaciones perdedoras", f"{n_perdedoras}")
        df_ops = pd.DataFrame([
            {
                "Entrada": o.fecha_entrada.strftime("%Y-%m"),
                "Salida": o.fecha_salida.strftime("%Y-%m") if o.fecha_salida is not None else "Abierta",
                "Meses": o.meses,
                "Retorno (%)": o.retorno_pct,
            }
            for o in ops
        ])
        with st.expander("Ver todas las operaciones"):
            st.dataframe(
                df_ops.style.format({"Retorno (%)": "{:.1f}%", "Meses": "{:.0f}"}),
                width="stretch", hide_index=True,
            )

        # --- Operaciones por año ---
        ops_anio = btv2.operaciones_por_anio(ops)
        if not ops_anio.empty:
            st.markdown("**📅 Operaciones por año** (para dimensionar el esfuerzo de ejecución)")
            fig_opy = go.Figure(go.Bar(
                x=[str(a) for a in ops_anio.index], y=ops_anio.values, marker_color=BRAND_BLUE,
                text=ops_anio.values, textposition="outside",
            ))
            fig_opy.update_layout(
                template=PLOTLY_TEMPLATE, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                showlegend=False, dragmode=False,
                yaxis=dict(title="N° de entradas", fixedrange=True, gridcolor=PLOTLY_GRIDCOLOR),
                xaxis=dict(fixedrange=True),
                margin=dict(l=10, r=10, t=20, b=70),
            )
            _pie_disclaimer_v2(fig_opy)
            st.plotly_chart(fig_opy, config=PLOTLY_CONFIG, width="stretch")

        st.caption(f":gray[{DISCLAIMER_V2}]")
    elif res_trend is not None:
        st.warning("No hay suficiente historia mensual para este activo/ventana.")

    # ======================================================
    # MÓDULO 2 — DUAL MOMENTUM
    # ======================================================
    st.divider()
    st.markdown("### 2️⃣ Momentum con ETFs (Dual Momentum)")
    st.caption(
        "Cada fin de mes se compara el **momentum** (retorno de los últimos meses) de EE.UU. (SPY) e "
        "internacional (EFA) y se invierte en el ganador — pero solo si su momentum le gana al efectivo "
        "(T-bills); si no, 100% a efectivo. Es momentum **relativo + absoluto**, sin escoger acciones "
        "individuales. Benchmarks: comprar y mantener SPY, y una cartera 60/40 (SPY/bonos IEF)."
    )
    lookback_lbl = st.selectbox(
        "Ventana de momentum", ["12 meses", "6 meses", "12-1 (excluye el último mes)"],
        key="lookback_dm_v2",
        help="El '12-1' excluye el mes más reciente, un ajuste clásico para evitar la reversión de corto plazo.",
    )
    _lb_map = {"12 meses": (12, False), "6 meses": (6, False), "12-1 (excluye el último mes)": (12, True)}
    _lb, _excl = _lb_map[lookback_lbl]
    try:
        res_dm = btv2.backtest_dual_momentum(_lb, excluir_ultimo=_excl)
    except Exception as e:  # pragma: no cover
        res_dm = None
        st.error(f"No se pudo correr el dual momentum: {e}")

    if res_dm is not None and len(res_dm.ret_estrategia) > 12:
        st.markdown("**📊 Métricas — período completo**")
        st.caption(f"Desde {res_dm.ret_estrategia.index.min():%b %Y} (limitado por el inicio de EFA en 2001).")
        st.dataframe(
            _df_metricas_v2(
                {"Dual Momentum": res_dm.ret_estrategia, "Buy & Hold SPY": res_dm.ret_spy, "Cartera 60/40": res_dm.ret_6040},
                res_dm.cash,
            ),
            width="stretch", hide_index=True,
        )
        dm_is, dm_ho = btv2.particion_muestra(res_dm.ret_estrategia)
        spy_is, spy_ho = btv2.particion_muestra(res_dm.ret_spy)
        cdm1, cdm2 = st.columns(2)
        with cdm1:
            st.markdown("**In-sample (hasta 2019)**")
            if len(dm_is) > 12:
                st.dataframe(_df_metricas_v2({"Dual Mom.": dm_is, "SPY": spy_is}, res_dm.cash), width="stretch", hide_index=True)
        with cdm2:
            st.markdown("**Holdout (2020 → hoy)**")
            if len(dm_ho) > 6:
                st.dataframe(_df_metricas_v2({"Dual Mom.": dm_ho, "SPY": spy_ho}, res_dm.cash), width="stretch", hide_index=True)

        e_dm = (1.0 + res_dm.ret_estrategia).cumprod()
        e_spy = (1.0 + res_dm.ret_spy).cumprod()
        e_60 = (1.0 + res_dm.ret_6040).cumprod()
        fig_dm = go.Figure()
        fig_dm.add_trace(go.Scatter(x=e_spy.index, y=e_spy.values, name="Buy & Hold SPY", line=dict(color=BRAND_MUTED, width=2)))
        fig_dm.add_trace(go.Scatter(x=e_60.index, y=e_60.values, name="Cartera 60/40", line=dict(color=BRAND_BLUE, width=1.8, dash="dot")))
        fig_dm.add_trace(go.Scatter(x=e_dm.index, y=e_dm.values, name="Dual Momentum", line=dict(color=BRAND_GREEN, width=2.5)))
        fig_dm.update_layout(
            template=PLOTLY_TEMPLATE, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            hovermode="x unified", dragmode="pan",
            yaxis=dict(title="Crecimiento de $1 (log)", type="log", fixedrange=False, gridcolor=PLOTLY_GRIDCOLOR),
            xaxis=dict(title="Año", fixedrange=False, gridcolor=PLOTLY_GRIDCOLOR),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(l=10, r=10, t=40, b=70),
        )
        _pie_disclaimer_v2(fig_dm)
        st.plotly_chart(fig_dm, config=PLOTLY_CONFIG_ZOOM, width="stretch")

        cdm3, cdm4 = st.columns(2)
        cdm3.metric("Rotaciones (cambios de posición)", f"{res_dm.n_rotaciones}")
        cdm4.metric("Drag por costos (acumulado)", f"−{res_dm.drag_costos_pct:.2f}%", help="Suma de las comisiones de todas las rotaciones.")

        st.markdown("**🌪️ ¿Se debilita el momentum en alta volatilidad?**")
        st.caption("Retorno mensual promedio de la estrategia según el tercil del VIX del **mes anterior**.")
        reg = btv2.regimen_vix_momentum(res_dm.ret_estrategia)
        if not reg.empty:
            st.dataframe(
                reg.style.format({
                    "Retorno mensual prom. (%)": "{:.2f}%", "% meses positivos": "{:.0f}%", "N° meses": "{:.0f}",
                }),
                width="stretch", hide_index=True,
            )
        st.caption(f":gray[{DISCLAIMER_V2}]")

    # ======================================================
    # MÓDULO 4 — SEÑALES DE VIX (nivel, percentil, estructura de plazos)
    # ======================================================
    st.divider()
    st.markdown("### 4️⃣ Refinamiento de la señal de VIX")
    st.caption(
        "Tres formas de usar el VIX como **señal de compra** (miedo = oportunidad), comparadas por el "
        "retorno del SPY a 1/3/6/12 meses **desde el primer día** de cada episodio de señal (episodios "
        "separados por ≥ 21 días hábiles). Se comparan contra el retorno forward **incondicional** "
        "(comprar cualquier día) para ver si la señal realmente agrega. Datos de volatilidad: CSV "
        "oficiales de **CBOE** (VIX desde 1990; VIX3M desde ~2009)."
    )

    def _fmt_tabla_vix(df: pd.DataFrame):
        fmt = {c: "{:.1f}%" for c in df.columns if "(%)" in c or "% pos" in c}
        fmt["N° episodios"] = "{:.0f}"
        return df.style.format(fmt, na_rep="—")

    st.markdown("**A) Nivel absoluto del VIX**")
    st.dataframe(_fmt_tabla_vix(btv2.resumen_señal_a()), width="stretch", hide_index=True)
    st.caption(
        "👀 Ojo al patrón: un VIX 'moderado' (25–30) no ha sido buena señal — muchas veces es el "
        "**inicio** del problema, no el fondo. El **miedo extremo (VIX ≥ 35–40)** es el que "
        "históricamente pagó (y le ganó a comprar cualquier día)."
    )

    st.markdown("**B) Percentil rodante de 5 años**")
    st.dataframe(_fmt_tabla_vix(btv2.resumen_señal_b()), width="stretch", hide_index=True)
    with st.expander("Sensibilidad a la ventana del percentil (3 / 5 / 10 años, umbral ≥ 90)"):
        st.dataframe(_fmt_tabla_vix(btv2.resumen_señal_b_ventanas(90)), width="stretch", hide_index=True)
        st.caption(
            "El resultado depende de la ventana (más corta = más señales, mejores números aquí). Esa "
            "dependencia es una señal de fragilidad: no hay un número mágico."
        )

    st.markdown("**C) Estructura de plazos (backwardation: VIX / VIX3M)**")
    st.caption(
        "Cuando el VIX de corto plazo supera al de 3 meses (ratio > 1), el mercado teme **más el ahora "
        "que el futuro** — históricamente, capitulación. Solo disponible desde ~2009 (inicio del VIX3M)."
    )
    st.dataframe(_fmt_tabla_vix(btv2.resumen_señal_c()), width="stretch", hide_index=True)

    st.markdown("**🔗 ¿Son señales distintas o la misma contada tres veces?**")
    st.caption(
        "Coincidencia diaria entre las tres señales (solo desde ~2009, cuando existe el VIX3M). Cada "
        "celda = probabilidad de que la señal de la **columna** esté activa dado que la de la **fila** lo "
        "está."
    )
    mat_solap = btv2.matriz_solapamiento_vix()
    st.dataframe(mat_solap.style.format("{:.0f}%").background_gradient(cmap="Blues", vmin=0, vmax=100), width="stretch")
    st.caption(
        "La backwardation (C) suele ser la más **independiente**: se solapa poco con las otras, así que "
        "aporta información propia, no la misma señal repetida."
    )

    st.markdown("**🗓️ Episodios de cada señal sobre el SPY**")
    epis = btv2.episodios_para_grafico()
    spy_epis = epis["spy"]
    fig_vix = go.Figure()
    fig_vix.add_trace(go.Scatter(
        x=spy_epis.index, y=spy_epis.values, mode="lines", name="SPY",
        line=dict(color=BRAND_MUTED, width=1.3), hovertemplate="%{x|%b %Y}<extra></extra>",
    ))
    _marcas = [
        ("A: VIX ≥ 30", "A", BRAND_GREEN, "triangle-up"),
        ("B: percentil ≥ 90", "B", BRAND_BLUE, "circle"),
        ("C: backwardation ≥ 1.0", "C", "#e0a144", "diamond"),
    ]
    for nombre, clave, color, simbolo in _marcas:
        fechas = [f for f in epis[clave] if f in spy_epis.index]
        if fechas:
            fig_vix.add_trace(go.Scatter(
                x=fechas, y=[spy_epis.loc[f] for f in fechas], mode="markers", name=nombre,
                marker=dict(symbol=simbolo, color=color, size=9, line=dict(width=1, color="#14161c")),
                hovertemplate=f"{nombre}<br>%{{x|%d %b %Y}}<extra></extra>",
            ))
    fig_vix.update_layout(
        template=PLOTLY_TEMPLATE, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        hovermode="closest", dragmode="pan",
        yaxis=dict(title="SPY (log)", type="log", fixedrange=False, gridcolor=PLOTLY_GRIDCOLOR),
        xaxis=dict(title="Año", fixedrange=False, gridcolor=PLOTLY_GRIDCOLOR),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=10, r=10, t=40, b=70),
    )
    _pie_disclaimer_v2(fig_vix)
    st.plotly_chart(fig_vix, config=PLOTLY_CONFIG_ZOOM, width="stretch")
    st.caption(f":gray[{DISCLAIMER_V2}]")

    # ======================================================
    # MÓDULO 5 — FORWARD RETURNS POR PROFUNDIDAD DE CAÍDA
    # ======================================================
    st.divider()
    st.markdown("### 5️⃣ Retornos forward según profundidad de caída")
    st.caption(
        "¿Qué ha pasado históricamente después de comprar en una caída de cierta profundidad? Mide el "
        "retorno a futuro (1/3/5/10 años) desde el primer día que el índice cruza cada umbral de caída "
        "— pero también **el riesgo**: cuánto más cayó después, y cuántos años tardó en recuperar su "
        "máximo. Sin apalancamiento, puramente educativo."
    )
    st.caption(
        "⚠️ ^GSPC (S&P 500) y ^IXIC (Nasdaq) son índices de **precio** (sin dividendos) y la data acá "
        "arranca en 1990, así que no incluye 1973-74 ni 1987. SPY/QQQ sí son retorno total."
    )
    idx_caida = st.selectbox("Índice", ["^IXIC (Nasdaq)", "^GSPC (S&P 500)", "QQQ", "SPY"], key="idx_caida_v2")
    _tk_caida = idx_caida.split(" ")[0]
    ev_caida = btv2.analizar_caidas(btv2.get_daily_closes(_tk_caida))
    df_caida = btv2.resumen_caidas(ev_caida)
    if not df_caida.empty:
        _cols_fwd = [c for c in df_caida.columns if "prom." in c or "N°" in c or c == "Umbral" or "% positivo" in c]
        _fmt_caida = {c: "{:.0f}%" for c in df_caida.columns if "(%)" in c}
        _fmt_caida.update({c: "{:.1f}" for c in df_caida.columns if "Años" in c})
        st.dataframe(df_caida.style.format(_fmt_caida, na_rep="—"), width="stretch", hide_index=True)

        pa, pb, pc = st.columns(3)
        pa.metric("P(−20% → −40%)", f"{btv2.prob_escalada_caida(ev_caida, 20, 40):.0f}%", help="De los ciclos que cayeron a −20%, cuántos siguieron hasta −40%.")
        pb.metric("P(−10% → −20%)", f"{btv2.prob_escalada_caida(ev_caida, 10, 20):.0f}%")
        pc.metric("P(−30% → −50%)", f"{btv2.prob_escalada_caida(ev_caida, 30, 50):.0f}%")

        st.markdown("**📊 Retorno forward a 5 años por umbral de caída** (con el peor caso anotado)")
        _umbrales = [10, 15, 20, 30, 40, 50]
        _prom5 = [df_caida.loc[df_caida["Umbral"] == f"-{u}%", "Fwd 5a prom. (%)"].values[0] for u in _umbrales]
        _min5 = [df_caida.loc[df_caida["Umbral"] == f"-{u}%", "Fwd 5a mín. (%)"].values[0] for u in _umbrales]
        fig_c5 = go.Figure(go.Bar(
            x=[f"−{u}%" for u in _umbrales], y=_prom5, marker_color=BRAND_GREEN,
            text=[f"prom {p:.0f}%<br>peor {m:.0f}%" if pd.notna(p) else "s/d" for p, m in zip(_prom5, _min5)],
            textposition="outside",
        ))
        fig_c5.update_layout(
            template=PLOTLY_TEMPLATE, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            showlegend=False, dragmode=False,
            yaxis=dict(title="Retorno forward 5 años (%)", fixedrange=True, gridcolor=PLOTLY_GRIDCOLOR),
            xaxis=dict(title="Profundidad de la caída al comprar", fixedrange=True),
            margin=dict(l=10, r=10, t=20, b=70),
        )
        _pie_disclaimer_v2(fig_c5)
        st.plotly_chart(fig_c5, config=PLOTLY_CONFIG, width="stretch")

        st.markdown("**⚖️ S&P 500 vs. Nasdaq — el mismo umbral, distinto estómago**")
        st.caption("Retorno forward promedio a 5 años y años (peor caso) para recuperar, lado a lado.")
        comp_filas = []
        for tk, nombre in [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq")]:
            evc = btv2.analizar_caidas(btv2.get_daily_closes(tk))
            rc = btv2.resumen_caidas(evc)
            for u in [20, 30, 40]:
                row = rc[rc["Umbral"] == f"-{u}%"]
                if not row.empty:
                    comp_filas.append({
                        "Índice": nombre, "Umbral": f"−{u}%",
                        "Fwd 5a prom. (%)": row["Fwd 5a prom. (%)"].values[0],
                        "Años recuperar (peor)": row["Años recuperar (peor)"].values[0],
                    })
        if comp_filas:
            st.dataframe(
                pd.DataFrame(comp_filas).style.format({"Fwd 5a prom. (%)": "{:.0f}%", "Años recuperar (peor)": "{:.1f}"}, na_rep="—"),
                width="stretch", hide_index=True,
            )
        st.caption(f":gray[{DISCLAIMER_V2}]")

    # ======================================================
    # MÓDULO 3 — DESEMPLEO (requiere FRED)
    # ======================================================
    st.divider()
    st.markdown("### 3️⃣ Desempleo como indicador contrario")
    if not btv2.hay_datos_fred():
        st.info("Este módulo necesita datos de desempleo (UNRATE de FRED), que no están disponibles ahora.")
    else:
        st.caption(
            "Análisis **descriptivo/educativo** (no una estrategia de trading): ¿qué retornos ha dado el "
            "S&P 500 a 12/24/36 meses según el nivel de desempleo? Se usa UNRATE con un rezago de "
            "publicación de 1 mes (el dato de un mes se conoce el mes siguiente)."
        )
        modo_desempleo = st.radio(
            "Método de agrupación", ["Muestra completa (descriptivo)", "Percentil expansivo (operable)"],
            horizontal=True, key="modo_desempleo_v2",
        )
        _expansivo = modo_desempleo.startswith("Percentil")
        if not _expansivo:
            st.warning(
                "⚠️ Los quintiles de **muestra completa** usan toda la historia para definir 'alto/bajo', "
                "lo que incorpora información del futuro (look-ahead). Sirve para describir la relación, "
                "**no** como señal operable. Para eso, usá el percentil expansivo."
            )
        t_des = btv2.tabla_desempleo_forward(_expansivo)
        if not t_des.empty:
            _fmt_des = {c: "{:.1f}%" for c in t_des.columns if "(%)" in c}
            _fmt_des["N° meses"] = "{:.0f}"
            st.dataframe(t_des.style.format(_fmt_des, na_rep="—"), width="stretch", hide_index=True)

            _quintiles = t_des["Quintil desempleo"].tolist()
            _prom12 = t_des["Fwd 12m prom. (%)"].tolist()
            fig_des = go.Figure(go.Bar(
                x=_quintiles, y=_prom12, marker_color=BRAND_GREEN,
                text=[f"{v:.1f}%" if pd.notna(v) else "s/d" for v in _prom12], textposition="outside",
            ))
            fig_des.update_layout(
                template=PLOTLY_TEMPLATE, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                showlegend=False, dragmode=False,
                yaxis=dict(title="Retorno forward 12m promedio (%)", fixedrange=True, gridcolor=PLOTLY_GRIDCOLOR),
                xaxis=dict(title="Quintil de desempleo (Q5 = desempleo más alto)", fixedrange=True),
                margin=dict(l=10, r=10, t=20, b=70),
            )
            _pie_disclaimer_v2(fig_des)
            st.plotly_chart(fig_des, config=PLOTLY_CONFIG, width="stretch")
            st.caption(
                "📖 El mensaje histórico: los **mejores** retornos a futuro han venido justo cuando el "
                "desempleo estaba **más alto** (máximo miedo), no cuando todo se veía bien."
            )

        st.markdown("**🔀 Nivel + dirección del desempleo (matriz 2×2, forward 12m)**")
        m_des = btv2.matriz_nivel_direccion_desempleo()
        if not m_des.empty:
            st.dataframe(
                m_des.style.format({c: "{:.1f}%" for c in m_des.columns if "(%)" in c}, na_rep="—"),
                width="stretch", hide_index=True,
            )
            st.caption(
                "No solo importa el nivel, también la dirección: 'desempleo bajo pero **subiendo**' "
                "(fin de ciclo) suele ser la zona más floja; 'desempleo alto y **bajando**' "
                "(recuperación temprana), la más fuerte."
            )
        st.caption(f":gray[{DISCLAIMER_V2}]")

    # ======================================================
    # MÓDULO 6 — SEÑAL COMPUESTA (requiere FRED)
    # ======================================================
    st.divider()
    st.markdown("### 6️⃣ Señal compuesta (score 0–4 → exposición)")
    if not btv2.hay_datos_fred():
        st.info("Este módulo usa el componente macro de desempleo (FRED), que no está disponible ahora.")
    else:
        st.warning(
            "⚠️ **Sesgo de construcción:** este score se armó sabiendo qué señales funcionaron en el "
            "pasado, así que incluso el 'in-sample' es optimista. El holdout 2020+ y la estabilidad son "
            "la única defensa honesta."
        )
        st.caption(
            "Cuatro señales mensuales, 1 punto cada una: **tendencia** (SPY > SMA 10m), **momentum** "
            "(retorno 12m de SPY > efectivo), **volatilidad** (percentil rodante 5a del VIX < 80) y "
            "**macro** (desempleo alto pero mejorando). El score 0–4 mapea a % en acciones (resto efectivo)."
        )
        mapeo_lbl = st.radio(
            "Mapeo de exposición", ["Escalonado (0/40/70/100/100%)", "Lineal (score/3)"],
            horizontal=True, key="mapeo_comp_v2",
        )
        _mapeo = "lineal" if mapeo_lbl.startswith("Lineal") else "escalonado"
        try:
            res_c = btv2.backtest_senal_compuesta(_mapeo)
        except Exception as e:  # pragma: no cover
            res_c = None
            st.error(f"No se pudo correr la señal compuesta: {e}")

        if res_c is not None and len(res_c.ret_estrategia) > 12:
            st.markdown("**📊 Métricas — período completo**")
            st.dataframe(
                _df_metricas_v2(
                    {"Compuesta": res_c.ret_estrategia, "Buy & Hold SPY": res_c.ret_spy, "Cartera 60/40": res_c.ret_6040},
                    res_c.cash,
                ),
                width="stretch", hide_index=True,
            )
            c_is, c_ho = btv2.particion_muestra(res_c.ret_estrategia)
            s_is, s_ho = btv2.particion_muestra(res_c.ret_spy)
            cc1, cc2 = st.columns(2)
            with cc1:
                st.markdown("**In-sample (hasta 2019)**")
                if len(c_is) > 12:
                    st.dataframe(_df_metricas_v2({"Compuesta": c_is, "SPY": s_is}, res_c.cash), width="stretch", hide_index=True)
            with cc2:
                st.markdown("**Holdout (2020 → hoy)**")
                if len(c_ho) > 6:
                    st.dataframe(_df_metricas_v2({"Compuesta": c_ho, "SPY": s_ho}, res_c.cash), width="stretch", hide_index=True)

            # exposición en el tiempo
            fig_exp = go.Figure(go.Scatter(
                x=res_c.exposicion.index, y=res_c.exposicion.values * 100.0, mode="lines",
                line=dict(color=BRAND_GREEN, width=1.8), fill="tozeroy", fillcolor="rgba(52,211,153,0.15)",
                hovertemplate="%{x|%b %Y}<br>Exposición: %{y:.0f}%<extra></extra>",
            ))
            fig_exp.update_layout(
                template=PLOTLY_TEMPLATE, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                dragmode="pan", yaxis=dict(title="% en acciones", range=[0, 105], fixedrange=False, gridcolor=PLOTLY_GRIDCOLOR),
                xaxis=dict(title="Año", fixedrange=False, gridcolor=PLOTLY_GRIDCOLOR),
                margin=dict(l=10, r=10, t=20, b=70),
            )
            _pie_disclaimer_v2(fig_exp)
            st.plotly_chart(fig_exp, config=PLOTLY_CONFIG_ZOOM, width="stretch")

            # distribución del score
            dist = (res_c.score.value_counts(normalize=True).sort_index() * 100.0)
            st.markdown("**Distribución histórica del score**")
            st.dataframe(
                pd.DataFrame({"Score": [int(i) for i in dist.index], "% del tiempo": dist.values}).style.format({"% del tiempo": "{:.0f}%"}),
                width="stretch", hide_index=True,
            )

            # correlación de señales
            st.markdown("**🔗 Correlación entre las 4 señales**")
            corr = res_c.señales.corr()
            st.dataframe(corr.style.format("{:.2f}").background_gradient(cmap="RdYlGn_r", vmin=-1, vmax=1), width="stretch")
            _altas = [
                (a, b, corr.loc[a, b]) for i, a in enumerate(corr.columns) for b in corr.columns[i + 1:]
                if abs(corr.loc[a, b]) > 0.8
            ]
            if _altas:
                st.warning("⚠️ Señales muy correlacionadas (>0.8): " + ", ".join(f"{a}–{b} ({c:.2f})" for a, b, c in _altas) + ". El score las cuenta casi doble.")
            else:
                st.caption("✅ Ninguna pareja supera 0.8 de correlación: las señales aportan información razonablemente distinta.")

            # contribución drop-one
            st.markdown("**🧪 Contribución de cada señal (quitando una a la vez)**")
            st.dataframe(
                btv2.contribucion_señales(_mapeo).style.format({
                    "CAGR (%)": "{:.2f}%", "Sharpe": "{:.2f}", "Max Drawdown (%)": "{:.1f}%", "Calmar": "{:.2f}",
                }),
                width="stretch", hide_index=True,
            )
            st.caption(
                "Si al quitar una señal las métricas casi no cambian, esa señal aporta poco; si empeoran "
                "mucho, es clave. Probá también el otro mapeo de exposición: si las conclusiones cambian "
                "según el mapeo exacto, es señal de fragilidad."
            )
            st.caption(f":gray[{DISCLAIMER_V2}]")

    st.divider()
    st.caption(
        "📌 **Resumen de datos:** precios de Yahoo Finance (retorno total salvo ^GSPC/^IXIC), macro de "
        "FRED (UNRATE, TB3MS) y volatilidad de CBOE (VIX desde 1990, VIX3M desde ~2009). Limitaciones: "
        "historia de índices desde ~1990; la estructura de plazos del VIX (módulo 4-C) solo cubre desde "
        "~2009; y el módulo 6 tiene sesgo de construcción."
    )
