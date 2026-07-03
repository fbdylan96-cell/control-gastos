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
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.investment.decumulation import simular_decumulacion
from src.investment.drawdown_recovery import (
    HORIZONTES_DIAS_HABILES,
    backtest_buy_the_dip,
    fechas_disparo_caida,
    retornos_individuales_por_evento,
    retornos_post_caida_vs_promedio,
)
from src.investment.fixed_return_projection import proyectar_rendimiento_fijo
from src.investment.historical_dca import TICKERS_DISPONIBLES, obtener_serie_diaria, simular_dca_historico
from src.investment.leveraged_simulation import (
    DEFAULT_EXPENSE_RATIO_ANUAL_PCT,
    DEFAULT_FINANCING_RATE_ANUAL_PCT,
    FECHA_INCEPTION_GLOBAL,
)
from src.investment.risk_analysis import mejores_y_peores_anios, peores_drawdowns
from src.pension.ivm import MONTO_MAXIMO_DEFAULT, MONTO_MINIMO_DEFAULT, calcular_pension_ivm
from src.pension.rop import proyectar_rop

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

PLOTLY_CONFIG = {"displayModeBar": False, "scrollZoom": False}


def _edad_en_anio(anio: int, edad_actual: int, anio_actual: int) -> str:
    edad = edad_actual + (anio - anio_actual)
    return str(edad) if edad >= 0 else "–"


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
        xaxis=_eje_x_anio_edad(min(anios_x), max(anios_x), edad_actual, anio_actual),
        yaxis=dict(
            title=y_label, fixedrange=True, showgrid=True, gridcolor=PLOTLY_GRIDCOLOR,
            tickprefix="$", separatethousands=True,
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig


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


def money_input(contenedor, label: str, key: str, value: float, help: str | None = None, simbolo: str = "$") -> float:
    """Campo de dinero: muestra el símbolo antes del número, sin decimales por defecto (solo si el usuario los escribe)."""
    if key not in st.session_state:
        st.session_state[key] = _formatear_dinero(value, simbolo)
    contenedor.text_input(label, key=key, on_change=_on_change_dinero, args=(key, simbolo), help=help)
    return _parsear_dinero(st.session_state[key])


def colon_input(contenedor, label: str, key: str, value: float, help: str | None = None) -> float:
    return money_input(contenedor, label, key, value, help=help, simbolo="₡")


def _sumar_anios(fecha: date, anios: int) -> date:
    try:
        return fecha.replace(year=fecha.year + anios)
    except ValueError:
        # 29 de febrero cayendo en un año no bisiesto
        return fecha.replace(month=2, day=28, year=fecha.year + anios)


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
    st.sidebar, "Fee de apertura / setup (único)", "setup_fee_txt", 1500.0,
    help="Se cobra una sola vez, al inicio, y se descuenta del aporte inicial.",
)
costo_swift = money_input(
    st.sidebar, "Costo por transferencia — p.ej. SWIFT", "costo_swift_txt", 65.0,
    help="Se cobra cada vez que se envía dinero (el aporte inicial y cada aporte periódico) y se descuenta antes de invertir.",
)
management_fee_anual_pct = st.sidebar.number_input(
    "Management fee anual (%)", min_value=0.0, value=1.0, step=0.1,
    help="Se cobra cada mes sobre el saldo/AUM de la cartera (proporcional: %/12 por mes).",
)

tab_teorica, tab_historica, tab_pension = st.tabs(
    [
        "📐 Proyección de Inversión con Empowered Investor",
        "📊 Simulación con Datos Históricos",
        "🏛️ Pensión del Estado (IVM + ROP)",
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
        "Rendimiento real anual esperado (%)", min_value=0.0, value=8.0, step=0.5,
        format="%0.1f", key="rend_teorico",
        help="Este número es un rendimiento REAL (ya descontada la inflación), no nominal. 'Real' "
        "quiere decir cuánto crece de verdad tu poder de compra cada año. Todos los montos de esta "
        "calculadora ya están en dólares de hoy, comparables directamente con tu salario o gastos "
        "actuales.",
    )
    inflacion_pct = st.number_input(
        "Inflación anual esperada (%)", min_value=0.0, value=3.0, step=0.5, format="%0.1f",
        help="Cuánto suben los precios en promedio cada año en EE.UU. (si invertís en dólares). Se usa "
        "solo para mostrarte el equivalente nominal más abajo — no cambia ningún monto en dólares de "
        "esta calculadora, porque ya trabajamos directamente en términos reales.",
    )

    st.caption(
        "📊 Referencia histórica de mercado (retornos REALES, ya descontando inflación, a muy largo "
        "plazo): el S&P 500 ha rendido aproximadamente 6.5%–7% anual real; el Nasdaq-100 ha rendido "
        "aproximadamente 9%–11% anual real, con bastante más volatilidad en el camino. Son promedios "
        "de varias décadas — no garantizan lo que pase en tu horizonte específico, pero te dan una idea "
        "de qué tan realista es el número que pusiste arriba. Nuestras estrategias sistemáticas buscan "
        "superar estos promedios; resultados anteriores no son garantía de resultados futuros, pero el "
        "crecimiento de capital es nuestra especialidad."
    )

    resultado = proyectar_rendimiento_fijo(
        anios=anios,
        rendimiento_anual_pct=rendimiento_anual_pct,
        aporte_inicial=aporte_inicial,
        aporte_periodico=aporte_periodico,
        frecuencia=frecuencia_aporte,
        edad_actual=edad_actual,
        setup_fee=setup_fee,
        costo_swift=costo_swift,
        management_fee_anual_pct=management_fee_anual_pct,
        anio_actual=anio_actual,
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
        "de vida actual — por eso es el que usamos en toda esta calculadora."
    )
    st.caption(":green[Resultados incluyen costos del servicio y comisiones por transferencia internacional SWIFT]")

    anios_cal = [p.anio_calendario for p in resultado.puntos]
    aportado_serie = [p.aportado_bruto_cum for p in resultado.puntos]
    balance_serie = [p.balance for p in resultado.puntos]
    anios_con_aporte = [
        p.anio_calendario for p, p_prev in zip(resultado.puntos[1:], resultado.puntos[:-1])
        if p.aportado_bruto_cum > p_prev.aportado_bruto_cum
    ]

    fig = _grafico_lineas(
        anios_cal,
        {
            "Dinero aportado": (aportado_serie, BRAND_MUTED, "dash"),
            "Valor de la cartera": (balance_serie, BRAND_GREEN, "solid"),
        },
        edad_actual,
        anio_actual,
        anios_aporte=anios_con_aporte,
    )
    st.plotly_chart(fig, config=PLOTLY_CONFIG, width="stretch")
    st.caption(
        f"💰 = año en el que hubo al menos un aporte periódico (\\${aporte_periodico:,.0f} c/u, según tu "
        "plan de aportes). El primer punto puede incluir además el aporte inicial."
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
        "Tasa de retiro anual (%)", 1.0, 10.0, 4.0, 0.5,
        help="Cuánto retiras del portafolio cada año, como % del valor total. 4% es la regla clásica "
        "('regla del 4%') usada en planificación de retiro.",
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
        "Simulación simple: cada año el portafolio crece a una tasa fija y, al mismo tiempo, se retira un monto "
        f"fijo (el {tasa_retiro_pct:.1f}% del valor inicial del retiro, calculado arriba)."
    )
    c1, c2 = st.columns(2)
    crecimiento_retiro_pct = c1.slider(
        "Crecimiento anual asumido durante el retiro (%)", 0.0, 15.0, 7.0, 0.5,
        help="Igual que el rendimiento de arriba: este también es un número **real** (poder de compra), "
        "no nominal. El saldo y el retiro anual de esta simulación están en dólares de hoy.",
    )
    horizonte_anios = c2.number_input("Horizonte a simular (años)", min_value=10, max_value=80, value=50, step=5)

    anio_retiro = anios_cal[-1]
    edad_retiro = edad_actual + anios
    decum = simular_decumulacion(
        valor_inicial=resultado.valor_final,
        tasa_retiro_anual_pct=tasa_retiro_pct,
        crecimiento_anual_pct=crecimiento_retiro_pct,
        edad_inicio=edad_retiro,
        anio_calendario_inicio=anio_retiro,
        horizonte_anios=horizonte_anios,
    )

    anios_decum = [p.anio_calendario for p in decum.puntos]
    balance_decum = [p.balance for p in decum.puntos]

    fig_decum = _grafico_lineas(
        anios_decum,
        {"Saldo del portafolio en retiro": (balance_decum, "#d62728", "solid")},
        edad_actual,
        anio_actual,
    )
    st.plotly_chart(fig_decum, config=PLOTLY_CONFIG, width="stretch")

    if decum.se_agota:
        st.warning(
            f"⚠️ Con un retiro de \\${decum.retiro_anual:,.0f}/año y un crecimiento asumido del "
            f"{crecimiento_retiro_pct:.1f}% anual, el portafolio se agotaría en **{decum.anio_agotamiento} años** "
            f"(a los {edad_retiro + decum.anio_agotamiento} años de edad), porque la tasa de retiro "
            f"({tasa_retiro_pct:.1f}%) supera el crecimiento asumido ({crecimiento_retiro_pct:.1f}%)."
        )
    elif decum.tendencia == "crece":
        st.success(
            f"✅ Con un retiro de \\${decum.retiro_anual:,.0f}/año y un crecimiento asumido del "
            f"{crecimiento_retiro_pct:.1f}% anual, el portafolio **nunca se agota** — de hecho sigue creciendo, "
            f"porque el crecimiento ({crecimiento_retiro_pct:.1f}%) supera la tasa de retiro ({tasa_retiro_pct:.1f}%)."
        )
    elif decum.tendencia == "estable":
        st.info(
            f"➡️ Con un retiro de \\${decum.retiro_anual:,.0f}/año igual al crecimiento asumido "
            f"({crecimiento_retiro_pct:.1f}%), el portafolio **se mantiene estable**: ni crece ni se agota, "
            f"se queda en \\${resultado.valor_final:,.0f} indefinidamente."
        )
    else:  # decrece, pero no llegó a $0 dentro del horizonte simulado
        st.warning(
            f"⚠️ Con un retiro de \\${decum.retiro_anual:,.0f}/año, el portafolio **va en descenso** (la tasa "
            f"de retiro de {tasa_retiro_pct:.1f}% supera el crecimiento asumido de {crecimiento_retiro_pct:.1f}%) "
            f"y no alcanza a agotarse dentro de los {horizonte_anios} años simulados, pero la tendencia es a la "
            f"baja — se agotaría en algún año más allá de ese horizonte. Prueba un horizonte más largo para ver "
            f"el año exacto."
        )

# ----------------------------------------------------------
# TAB 2 — SIMULACIÓN CON DATOS HISTÓRICOS
# ----------------------------------------------------------
with tab_historica:
    st.subheader("¿Cómo hubiera sido con datos históricos reales?")
    st.caption("Precios reales de Yahoo Finance (splits y dividendos incluidos), cacheados localmente.")

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

    st.divider()
    st.markdown("##### 📥 Plan de aportes para esta simulación")
    st.caption(
        "Estos datos son independientes de la pestaña 'Proyección de Inversión con Empowered Investor' "
        "— podés usar un plan distinto para ver cómo le hubiera ido en el pasado. Los costos del "
        "servicio (fee de apertura, SWIFT, management fee) sí se toman del panel de la izquierda, "
        "compartidos con las demás pestañas. Todo se recalcula automáticamente al cambiar cualquier dato."
    )
    c1, c2 = st.columns(2)
    edad_actual_hist = c1.number_input("Edad hoy", min_value=1, max_value=100, value=35, key="edad_hist")
    frecuencia_aporte_hist = c2.selectbox(
        "Frecuencia del aporte", ["Mensual", "Trimestral", "Semestral", "Anual", "Cada 2 años", "Cada 3 años"], index=2, key="frecuencia_hist"
    )
    c1, c2 = st.columns(2)
    aporte_inicial_hist = money_input(c1, "Aporte inicial", "aporte_inicial_hist_txt", 10_000.0)
    aporte_periodico_hist = money_input(c2, "Aporte periódico", "aporte_periodico_hist_txt", 5_000.0)

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
    fecha_fin_hist = min(date.today(), _sumar_anios(fecha_inicio_hist, anios_aportes_hist))

    tickers_elegidos = st.multiselect(
        "Tickers a simular", TICKERS_DISPONIBLES, default=["QQQ", "SPY", "TQQQ", "QLD"]
    )

    if not tickers_elegidos:
        st.warning("Elige al menos un ticker.")
    else:
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

            hoy = date.today()
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
                    anios_desde_aporte = max(0.0, (hoy - fecha_aporte.date()).days / 365.25)
                    aportado_real += monto * (1.0 + inflacion_pct / 100.0) ** anios_desde_aporte

                anios_hasta_hoy = max(0.0, (hoy - r.fecha_fin_real.date()).days / 365.25)
                valor_final_real = r.valor_final * (1.0 + inflacion_pct / 100.0) ** anios_hasta_hoy

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
            st.caption(
                f"Valores en poder de compra de **hoy** (dólares reales), usando el mismo supuesto de "
                f"inflación anual ({inflacion_pct:.1f}%) de la pestaña 'Proyección de Inversión' — no una "
                "serie histórica de inflación real, es la misma tasa fija aplicada desde la fecha de cada "
                "aporte (o del cierre de la simulación, para el valor final) hasta hoy. Costos del servicio "
                f"incluidos: \\${setup_fee:,.0f} de apertura + \\${costo_swift:,.0f} por transferencia; el "
                f"management fee ({management_fee_anual_pct:.1f}% anual) también ya está reflejado.\n\n"
                "**XIRR** (\"Extended Internal Rate of Return\"): la tasa anual que, aplicada a cada uno de "
                "tus aportes según su fecha y monto exactos, hace que el valor presente de todos ellos "
                "cuadre exactamente con el valor final — o sea, tu retorno real como inversionista, tomando "
                "en cuenta que no todo tu dinero estuvo invertido desde el día uno. El **CAGR del activo** "
                "es distinto: es el crecimiento anual del precio del ticker solo, sin importar cuándo "
                "aportaste — sirve para comparar qué tan bien se portó el activo en sí, separado de tu plan "
                "de aportes. Las versiones \"real\" de ambas tasas restan el efecto de la inflación asumida "
                "(vía la ecuación de Fisher), igual que el resto de esta calculadora."
            )

            with st.expander("Ver en dólares nominales (sin ajustar por inflación)"):
                st.caption(
                    "Dólares **nominales** de cada fecha real — lo que literalmente valían ese día, sin "
                    "convertir a poder de compra de hoy. ⚠️ Un dólar aportado hace años representaba más "
                    "poder de compra que uno de hoy (los precios han subido desde entonces), así que estos "
                    "montos suelen verse más chicos que los de la tabla principal — no es un error, es la "
                    "diferencia entre lo que pusiste/tenés literalmente vs. lo que eso representa hoy."
                )
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
                xaxis=_eje_x_anio_edad_fechas(int(anio_min_hist), int(anio_max_hist), edad_actual_hist, anio_actual),
                yaxis=dict(
                    title="$", fixedrange=True, showgrid=True, gridcolor=PLOTLY_GRIDCOLOR,
                    tickprefix="$", separatethousands=True,
                ),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                margin=dict(l=10, r=10, t=40, b=10),
            )
            st.plotly_chart(fig_hist, config=PLOTLY_CONFIG, width="stretch")
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
            ticker_riesgo = st.selectbox("Ticker a analizar", list(resultados.keys()), key="ticker_riesgo_hist")
            r_riesgo = resultados[ticker_riesgo]
            drawdowns_riesgo = peores_drawdowns(r_riesgo.serie_precio, top_n=10)
            mejores_anios_riesgo, peores_anios_riesgo = mejores_y_peores_anios(r_riesgo.serie_precio, top_n=10)

            rc1, rc2, rc3 = st.columns(3)
            with rc1:
                st.caption("10 peores drawdowns")
                if drawdowns_riesgo:
                    df_dd = pd.DataFrame(
                        [
                            {
                                "Pico": d["fecha_pico"].date(),
                                "Valle": d["fecha_valle"].date(),
                                "Caída (%)": d["drawdown_pct"],
                            }
                            for d in drawdowns_riesgo
                        ]
                    )
                    st.dataframe(df_dd.style.format({"Caída (%)": "{:.1f}%"}), width="stretch", hide_index=True)
                else:
                    st.caption("Sin suficientes datos.")
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
                xaxis=_eje_x_anio_edad_fechas(int(anio_min_hist), int(anio_max_hist), edad_actual_hist, anio_actual),
                yaxis=dict(
                    title="Índice (100 = inicio)", fixedrange=True, showgrid=True, gridcolor=PLOTLY_GRIDCOLOR,
                ),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                margin=dict(l=10, r=10, t=40, b=10),
            )
            st.plotly_chart(fig_activos, config=PLOTLY_CONFIG, width="stretch")

            st.divider()
            st.markdown("**⚡ Buy the Dip con Apalancamiento**")
            st.caption(
                "Backtest: \"si hubiera comprado **todas** las caídas históricas de QQQ o SPY con mis "
                "aportes de esta simulación, ¿qué me habría dado si compro un activo apalancado como TQQQ "
                "o QLD?\" — un historial concreto, operación por operación, de cómo se hubiera visto seguir "
                "esta estrategia en la vida real."
            )
            c1, c2, c3 = st.columns(3)
            ticker_senal = c1.selectbox(
                "Activo que genera la señal", ["QQQ", "SPY"], key="ticker_senal_dip",
                help="El ticker cuya caída desde el máximo histórico dispara la señal de compra.",
            )
            _default_comprar = "TQQQ" if "TQQQ" in TICKERS_DISPONIBLES else TICKERS_DISPONIBLES[0]
            ticker_comprar = c2.selectbox(
                "Activo que se compra", TICKERS_DISPONIBLES,
                index=TICKERS_DISPONIBLES.index(_default_comprar), key="ticker_comprar_dip",
                help="El ticker que realmente se compra cuando aparece la señal — puede ser el mismo de "
                "la señal o uno apalancado (TQQQ, QLD).",
            )
            umbral_caida_pct = c3.number_input(
                f"Caída desde el ATH de {ticker_senal} (%)", min_value=-90.0, max_value=-1.0, value=-5.0,
                step=1.0, key="umbral_caida_hist",
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
                    f"{frecuencia_aporte_hist.lower()}, igual que arriba) entran a la cuenta y quedan "
                    "**parqueados en efectivo** — no se invierten de inmediato. Solo cuando aparece una "
                    f"señal de caída de {ticker_senal} (≥{abs(umbral_caida_pct):.0f}%) Y no hay ya una "
                    f"posición abierta, se invierte **todo** el efectivo acumulado hasta ese momento en "
                    f"{ticker_comprar}. La posición se vende {horizonte_backtest_lbl} después, el dinero "
                    "vuelve a quedar parqueado, y se espera la próxima señal — así es como de verdad "
                    "operaría un cliente que sigue esta estrategia hacia adelante."
                )

                resultado_bt = backtest_buy_the_dip(
                    precio_comprar_diario, fechas_disparo, horizonte_backtest_dias,
                    aporte_inicial=aporte_inicial_hist, aporte_periodico=aporte_periodico_hist,
                    frecuencia=frecuencia_aporte_hist,
                )

                if resultado_bt.n_operaciones == 0:
                    st.warning("No hubo suficientes datos para completar ninguna operación con estos parámetros.")
                else:
                    bm1, bm2, bm3, bm4 = st.columns(4)
                    bm1.metric("Total aportado", f"${resultado_bt.total_aportado:,.0f}")
                    bm2.metric("Valor final", f"${resultado_bt.valor_final:,.0f}")
                    bm3.metric("Retorno anualizado (XIRR)", f"{resultado_bt.retorno_anualizado_pct:.1f}%")
                    bm4.metric(
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
                    fechas_entradas_bt = [op.fecha_entrada for op in resultado_bt.operaciones]
                    _agregar_marcas_aportes(
                        fig_bt, fechas_entradas_bt,
                        list(resultado_bt.serie_valor.values) + list(resultado_bt.serie_aportado.values),
                    )
                    fig_bt.update_layout(
                        template=PLOTLY_TEMPLATE,
                        paper_bgcolor="rgba(0,0,0,0)",
                        plot_bgcolor="rgba(0,0,0,0)",
                        hovermode="x unified",
                        xaxis=_eje_x_anio_edad_fechas(
                            int(fecha_inicio_hist.year), int(fecha_fin_hist.year), edad_actual_hist, anio_actual
                        ),
                        yaxis=dict(
                            title="$", fixedrange=True, showgrid=True, gridcolor=PLOTLY_GRIDCOLOR,
                            tickprefix="$", separatethousands=True,
                        ),
                        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                        margin=dict(l=10, r=10, t=40, b=10),
                    )
                    st.plotly_chart(fig_bt, config=PLOTLY_CONFIG, width="stretch")
                    st.caption(
                        f"💰 = fecha de compra ({resultado_bt.n_operaciones} operaciones en total, de "
                        f"{len(fechas_disparo)} señales detectadas — el resto cayó mientras ya había una "
                        "posición abierta o no había efectivo parqueado todavía). Si la última posición "
                        "seguía abierta al cierre del período, su valor está marcado a mercado con el "
                        "último precio disponible, no vendido de verdad."
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

                st.divider()
                st.markdown(
                    f"##### 📊 Cada caída de {ticker_senal}, fecha por fecha: retorno real de {ticker_comprar}"
                )
                st.caption(
                    "Una fila por cada caída detectada — no promedios, el dato real de cada evento — para "
                    "que se vea la granularidad completa. Al final se agrega el promedio de estas señales "
                    "vs. el promedio histórico incondicional (cualquier día), para dimensionar qué tanto "
                    "ayuda comprar después de una caída."
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
                        "⚠️ En plazos cortos (días a ~1-2 años), comprar después de una caída rindió de forma "
                        "consistente más que el promedio histórico. En plazos muy largos (7-10 años) esto "
                        "puede no cumplirse: varias caídas detectadas ocurrieron durante la burbuja puntocom, "
                        "cuya recuperación completa tardó cerca de 14 años (QQQ no volvió a su máximo del "
                        "2000 sino hasta 2014) — el promedio histórico a esos plazos incluye muchos períodos "
                        "que arrancan en mercados alcistas más recientes, así que no siempre es una "
                        "comparación pareja."
                    )

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

    st.divider()
    st.subheader("🧮 Proyección total de tu retiro")
    st.caption(
        "Suma la pensión IVM, el ingreso ROP (según la modalidad aplicable) y el ingreso mensual **real** "
        "(ajustado por inflación, en poder de compra de hoy) ya calculado en la pestaña 'Proyección de Inversión con Empowered Investor' "
        "→ 'Ingreso mensual con método porcentual', para ver tu ingreso total estimado en colones de hoy."
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
