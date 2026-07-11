# -*- coding: utf-8 -*-
"""
VERSIÓN MVP — llamada high ticket únicamente. No calificados → Instagram.

Lead magnet — "Tu brecha de retiro" (ruta retiro). Entregable: Reporte Completo (calificados).

Wizard lineal, móvil-first, sin sidebar ni tabs. Reutiliza los motores existentes:
  • src.pension.ivm / rop          → pensión del Estado (IVM + ROP), en ₡
  • src.investment.fixed_return_projection → acumulación de la inversión, en US$
Da el número gratis (la brecha + 3 actos) y captura el contacto para enviar el plan.

MONEDAS: la INVERSIÓN vive en dólares (el mercado global se opera en USD); la PENSIÓN ESTATAL y la
META van en colones. Para compararlas se convierte con `tc()`.

REGULACIÓN GUBERNAMENTAL (IVM / ROP): la lógica vive en `src.pension.*` (única fuente de verdad;
parámetros 2026 en `referencia_ivm_2026.md`). NO redefinir reglas de la CCSS acá.

⚠️ CONFIGURAR ANTES DE PUBLICAR (TODO): WHATSAPP_NUMERO y la tasa del instrumento de la alianza.
"""
from __future__ import annotations

import sys
import time
import urllib.parse
from datetime import date
from pathlib import Path

# Los motores (src/) y assets de marca viven en calculadora-core/, compartidos
# con la calculadora del asesor (calculadora-estrategia/calculadora_retiro.py).
_CORE_DIR = Path(__file__).resolve().parent.parent / "calculadora-core"
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from src.pension.ivm import MONTO_MAXIMO_DEFAULT, MONTO_MINIMO_DEFAULT, calcular_pension_ivm
from src.pension.rop import proyectar_rop
from src.investment.fixed_return_projection import proyectar_rendimiento_fijo

# ---------------------------------------------------------------------------
# CONFIGURACIÓN (editar antes de publicar)
# ---------------------------------------------------------------------------
MARCA = "Empowered Investor"
WHATSAPP_NUMERO = "50670558296"           # WhatsApp real de Jose (+506 7055-8296, sin '+').
# NOTA: usamos el formato wa.me/<número>?text=… porque es el único que precarga el mensaje de triage.
# El short link de WhatsApp Business (wa.me/message/LOLVS2JZI7G3A1) NO admite ?text=, así que perdería
# todos los datos del lead — por eso NO se usa acá.
CALENDLY_URL = "https://calendly.com/empoweredinvestor/reunion-de-30-minutos"
INSTAGRAM_URL = "https://www.instagram.com/joseinvestor.cr/"
# Prefill de Calendly (Rama A): nombre/apellido/correo son parámetros estándar y siempre precargan.
# El teléfono va en una "pregunta custom" del evento, numerada a1..aN según su orden. Verificado contra
# el evento real de Jose (2026-07): a1 = Número de teléfono, a2 = Perfil de Instagram, a3 = situación
# actual (esta última es MEDICIÓN — la contesta el lead, no se precarga). Por eso el teléfono es "a1".
CALENDLY_TEL_PARAM = "a1"
SJC_TASA_PCT = 8.0                        # TODO: verificar tasa vigente del instrumento de la alianza
CAPITAL_CALIFICADO = 10_000               # US$ — umbral para invitar a la sesión de diagnóstico
HORIZONTE_CALIFICADO = 10                 # años mínimos para invitar a la sesión
CALIFICA_TAMBIEN_POR_FLUJO = True         # capital + 12 aportes ≥ umbral también califica

INFLACION_PCT = 3.0
TASA_MERCADO_PCT = 9.5                    # S&P 500 nominal a largo plazo (~6.5% real)
TASA_ACTIVA_MIN_PCT = 12.0               # objetivo de nuestras estrategias (nominal)
TASA_ACTIVA_MAX_PCT = 15.0
FEE_ANUAL_PCT = 1.0                       # management fee anual (a partir del 2º año)
SETUP_FEE_USD = 1_500.0                   # fee único inicial (en dólares)
TASA_RETIRO_CLASICA_PCT = 4.0            # regla del 4%
TC_DEFAULT = 460.0                        # tipo de cambio ₡ por $1 (editable por el usuario)

# --- Defaults REGULATORIOS espejados del tab "Pensión del Estado" (única fuente de verdad) ---
SALARIO_MINIMO_LEGAL = 373_092.0          # Decreto 45303-MTSS, 2026 (antes 350_000)
ROP_NOMINAL_PCT = 6.0
ROP_INFLACION_PCT = INFLACION_PCT
ROP_PLAZO_PAGO_ANIOS = 20.0
ROP_ANIO_INICIO = 2000                    # el ROP arrancó ~2000 → cuotas ROP tope

# Métricas de la estrategia SMA-10m (regla de Faber) vs buy & hold.
# Fuente: backtest propio (calculadora_retiro.py, btv2.backtest_sma_trend), S&P 500 (SPY),
# período 1993–2026, costos de transacción 0.10%, precios ajustados, sin look-ahead.
# Extraídas con scripts/extraer_metricas_faber.py. NO editar a mano — volver a correr el script.
FABER_STATS = {
    "bh":  {"cagr": 10.8, "maxdd": -50.8, "calmar": 0.21},
    "sma": {"cagr": 9.8, "maxdd": -22.4, "calmar": 0.44},
}
FABER_PERIODO = "1993–2026"

VERDE = "#12b886"
ROJO = "#e0525a"
AMBAR = "#e0a020"
AZUL = "#4c7ef3"
TENUE = "#8a94a6"

# Logo: marca de ondas transparente compartida en calculadora-core. Capturas
# CCSS: propias del wizard (assets/ local; si no existen, la guía es solo texto).
_LOGO = _CORE_DIR / "assets" / "Logo_EmpoweredInvestor_transparente.png"
_CCSS_IMG = Path(__file__).resolve().parent / "assets" / "ccss_proyeccion_pension.png"
_CCSS_HOME = Path(__file__).resolve().parent / "assets" / "ccss_oficina_virtual_home.png"
CCSS_URL = "https://aissfa.ccss.sa.cr/afiliacion/index.xhtml?faces-redirect=true?faces-redirect=true"

st.set_page_config(page_title=f"Tu brecha de retiro · {MARCA}", page_icon="🎯",
                   layout="centered", initial_sidebar_state="collapsed")

st.markdown("""
<style>
  [data-testid="stSidebar"], [data-testid="collapsedControl"], #MainMenu, header, footer {display:none !important;}
  [data-testid="InputInstructions"] {display:none !important;}
  [data-testid="stImage"] {text-align:center;}
  [data-testid="stImage"] img {margin:0 auto; display:block;}
  [data-testid="stPlotlyChart"] {width:100% !important;}
  .js-plotly-plot {margin:0 auto;}
  .block-container {max-width: 700px; padding-top: 1.2rem; padding-bottom: 3.5rem;}
  .block-container p, .block-container li {font-size:1.15rem; line-height:1.5;}
  [data-testid="stWidgetLabel"] p, .stRadio label p {font-size:1.12rem !important; font-weight:600;}
  .stNumberInput input, .stTextInput input {font-size:1.25rem !important; padding:0.55rem 0.6rem;}
  .stButton>button, .stLinkButton>a {width:100%; border-radius:14px; padding:0.9rem 1rem; font-weight:700; font-size:1.2rem;}
  div[data-testid="stMetricValue"] {font-size:1.7rem; font-weight:800;}
  .stExpander summary p {font-size:1.05rem !important;}
  .wz-hero {font-size:2.05rem; font-weight:800; line-height:1.18; margin:0.3rem 0 0.2rem;}
  .wz-sub {color:#8a94a6; font-size:1.18rem; line-height:1.45; margin-bottom:1.2rem;}
  .wz-step {color:#8a94a6; font-size:0.92rem; letter-spacing:0.09em; text-transform:uppercase; font-weight:800; margin-bottom:0.2rem;}
  .wz-card {background:rgba(140,150,170,0.10); border:1px solid rgba(140,150,170,0.20); border-radius:18px; padding:1.25rem 1.35rem; margin:0.8rem 0; font-size:1.14rem; line-height:1.5;}
  .wz-mini {background:rgba(140,150,170,0.10); border:1px solid rgba(140,150,170,0.20); border-radius:16px; padding:1.0rem 1.1rem; margin:0.4rem 0; min-height:150px;}
  .wz-big {font-size:2.9rem; font-weight:800; line-height:1.05;}
  .wz-num {font-size:1.7rem; font-weight:800; line-height:1.1;}
  .wz-lbl {color:#8a94a6; font-size:1.02rem; font-weight:600;}
  .wz-bar {height:40px; border-radius:10px; display:flex; align-items:center; padding-left:14px; color:#fff; font-weight:800; font-size:1.05rem; white-space:nowrap; margin:8px 0;}
  .wz-eq {font-size:1.22rem; font-weight:800; margin:0.15rem 0 0.5rem;}
  .wz-eqs {font-size:1.0rem; font-weight:700; color:#8a94a6; margin:-0.2rem 0 0.7rem;}
  .wz-note {color:#8a94a6; font-size:1.0rem; line-height:1.45; margin:0.6rem 0;}
</style>
""", unsafe_allow_html=True)

ss = st.session_state
ss.setdefault("paso", 0)
ss.setdefault("datos", {})
ss.setdefault("_t_inicio", time.time())   # marca de tiempo de inicio de la sesión (para medir cuánto tarda)
d = ss.datos


def _tiempo_transcurrido() -> str:
    seg = int(time.time() - ss.get("_t_inicio", time.time()))
    return f"{seg // 60} min {seg % 60} s"

if ss.get("_last_paso") != ss.paso:
    ss._last_paso = ss.paso
    components.html(
        "<script>const doc=window.parent.document;"
        "const el=doc.querySelector('section.main')||doc.querySelector('[data-testid=\"stMain\"]')||doc.querySelector('.main');"
        "if(el){el.scrollTo({top:0,behavior:'instant'});} try{window.parent.scrollTo(0,0);}catch(e){}</script>",
        height=0)

# Separadores de miles EN VIVO en los campos de dinero (Streamlit solo formatea al perder foco / Enter).
# Delegación en el documento padre, con guarda para no duplicar el listener entre reruns.
components.html("""
<script>
(function(){
  const W = window.parent, D = W.document;
  if (W.__moneyfmt) return; W.__moneyfmt = true;
  const isMoney = el => el && el.tagName === 'INPUT' &&
      /(₡|US\\$|recibir por mes)/.test(el.getAttribute('aria-label') || '');
  const nativeSetter = Object.getOwnPropertyDescriptor(W.HTMLInputElement.prototype, 'value').set;
  D.addEventListener('input', function(e){
    const el = e.target; if (!isMoney(el)) return;
    const raw = el.value; const digits = raw.replace(/[^0-9]/g, '');
    if (!digits) return;
    const formatted = Number(digits).toLocaleString('en-US');
    if (formatted === raw) return;
    const pos = el.selectionStart || raw.length;
    const digitsBefore = raw.slice(0, pos).replace(/[^0-9]/g, '').length;
    nativeSetter.call(el, formatted);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    let dc = 0, np = formatted.length;
    for (let i = 0; i < formatted.length; i++){ if (/[0-9]/.test(formatted[i])) dc++; if (dc >= digitsBefore){ np = i + 1; break; } }
    try { el.setSelectionRange(np, np); } catch(err){}
  }, true);
})();
</script>
""", height=0)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def tc() -> float:
    return float(d.get("tc", TC_DEFAULT) or TC_DEFAULT)


def col(m: float) -> str:
    return f"₡{m:,.0f}"


def usd(m: float) -> str:
    return f"${m:,.0f}"


def ambos_col(m: float) -> str:  # monto en ₡ → "₡X · $Y"
    return f"₡{m:,.0f} · ${m / tc():,.0f}"


def _tel_e164_cr(whats: str) -> str:
    """Normaliza un teléfono a formato internacional E.164 para el campo de teléfono de Calendly.
    Sin código de país explícito, Calendly interpreta los primeros dígitos como el código (ej.
    '8888-8888' → +886 Taiwán). Costa Rica = +506; los números locales son de 8 dígitos."""
    digits = "".join(c for c in str(whats) if c.isdigit())
    if not digits:
        return ""
    if digits.startswith("506"):
        return "+" + digits                 # ya trae el 506
    if len(digits) == 8:
        return "+506" + digits              # número local tico → le anteponemos +506
    return "+" + digits                     # otro largo: asumimos que ya trae código de país


# --- Prefill de las preguntas de opción múltiple del evento de Calendly ---
# OJO: Calendly solo marca la opción si el texto del prefill coincide EXACTO con el de la opción
# (acentos, comas, guiones "–" vs "-"). Estas cadenas están transcritas de las capturas del evento
# (2026-07). Si alguna no se marca al abrir el link, casi seguro es un carácter que no calza: corregí
# la cadena acá. Índices de las preguntas custom, en orden: a1=teléfono, a2=Instagram, a3=situación,
# a4=objetivo, a5=ingreso, a6=pareja, a7=capacidad, a8=presupuesto del servicio.
# Solo precargamos lo que la calculadora SÍ pregunta: a1 (tel), a4 (objetivo←intención), a5 (ingreso←
# salario) y a7 (capacidad←capital+aporte). a2/a3/a6/a8 no se precargan (no tenemos ese dato).
_A4_OBJETIVO = {   # "¿Qué te gustaría lograr idealmente en esta etapa?" ← intención del hand-raise
    "Acompañamiento": "Delegar el proceso porque estoy ocupado y quiero que alguien me ayude a abrir, estructurar y manejar la cuenta.",
    "Aprender":       "Aprender sobre inversiones desde cero.",
    "Solo números":   "No estoy seguro todavía, quiero entender qué opción tiene más sentido para mí.",
}


def _a5_ingreso(salario_col: float) -> str:
    """"¿En cuál de estos rangos… tu ingreso mensual?" ← salario (₡) de la calculadora, pasado a US$."""
    if salario_col <= 0:
        return ""
    usd = salario_col / tc()
    if usd < 2000:
        return "Menos de $2,000"
    if usd < 3000:
        return "$2,000 a $3,000"
    if usd < 6000:
        return "$3,000 a $6,000"
    return "Más de $6,000"


def _a7_capacidad(capital: float, aporte: float) -> str:
    """"Para invertir, ¿cuál describe mejor tu situación?" ← capital + aporte (US$) de la calculadora."""
    if capital >= 25000:
        return "Más de $25,000 disponibles"
    if capital >= 10000 or aporte >= 500:
        return "$10,000–$25,000 y $500+/mes"
    if capital >= 5000 or aporte >= 250:
        return "$5,000–$10,000, o puedo aportar $250–$500/mes"
    return "Menos de $5,000 y hasta $250/mes"


def calendly_prefill(nombre: str, apellido: str, correo: str, whats: str, r: dict, intencion_corto: str) -> str:
    """Link de Calendly con los datos del formulario y de la calculadora ya cargados (un formulario, dos destinos).
    Solo agrega parámetros no vacíos; las preguntas de opción múltiple solo se marcan si el texto calza exacto."""
    params = {"utm_source": "wizard", "utm_content": "calificado"}
    if nombre:
        params["first_name"] = nombre
    if apellido:
        params["last_name"] = apellido
    if correo:
        params["email"] = correo
    tel = _tel_e164_cr(whats)
    if tel and CALENDLY_TEL_PARAM:
        params[CALENDLY_TEL_PARAM] = tel
    # Preguntas de opción múltiple derivadas de la calculadora (ver nota sobre coincidencia exacta):
    _obj = _A4_OBJETIVO.get(intencion_corto)
    if _obj:
        params["a4"] = _obj
    _ing = _a5_ingreso(float(r.get("salario", 0)))
    if _ing:
        params["a5"] = _ing
    _cap = _a7_capacidad(float(r.get("capital", 0)), float(r.get("aporte", 0)))
    if _cap:
        params["a7"] = _cap
    return CALENDLY_URL + "?" + urllib.parse.urlencode(params)


def fmt_meta(monto_col: float) -> str:  # en la MONEDA que el usuario eligió en el Paso 1
    return f"${monto_col / tc():,.0f}" if d.get("moneda") == "$" else f"₡{monto_col:,.0f}"


def fmt_meta_sec(monto_col: float) -> str:  # la OTRA moneda
    return f"₡{monto_col:,.0f}" if d.get("moneda") == "$" else f"${monto_col / tc():,.0f}"


def fmt_meta_usd(monto_usd: float) -> str:  # un monto en US$ → en la moneda de la meta
    return f"${monto_usd:,.0f}" if d.get("moneda") == "$" else f"₡{monto_usd * tc():,.0f}"


def barra(valor: float, maximo: float, color: str, etiqueta: str) -> str:
    pct = 0 if maximo <= 0 else max(6.0, min(100.0, valor / maximo * 100.0))
    return f'<div class="wz-bar" style="width:{pct:.0f}%;background:{color};">{etiqueta}</div>'


def _fmt_dinero(valor: float, simbolo: str) -> str:
    return f"{simbolo}{valor:,.0f}"


def _parse_dinero(texto) -> float:
    limpio = "".join(c for c in str(texto) if c.isdigit() or c == ".").strip()
    try:
        return max(0.0, float(limpio)) if limpio else 0.0
    except ValueError:
        return 0.0


def _on_dinero(key: str, simbolo: str):
    v = _parse_dinero(ss[key])
    ss[key] = _fmt_dinero(v, simbolo) if v > 0 else ""   # vacío → deja el placeholder (no cuenta como valor)


def money_input(contenedor, label: str, key: str, sugerido: float, simbolo: str, help=None) -> float:
    """Campo vacío por defecto: el valor sugerido se muestra como placeholder (sombreado), NO como valor.
    Así nadie avanza a puros 'Siguiente' sin escribir su número. Devuelve 0 si está vacío."""
    if key not in ss:
        ss[key] = ""
    contenedor.text_input(label, key=key, on_change=_on_dinero, args=(key, simbolo), help=help,
                          placeholder=f"ej. {_fmt_dinero(float(sugerido), simbolo)}")
    return _parse_dinero(ss[key])


def ir_a(paso: int):
    ss.paso = paso
    st.rerun()


# ---------------------------------------------------------------------------
# MOTOR DE CÁLCULO
# ---------------------------------------------------------------------------
def pension_estado(d: dict):
    """(pensión total ₡, ivm ₡, rop ₡, cumple_ivm, ivm_obj, rop_obj) — usa los mismos motores que el tab de Pensión."""
    salario = float(d.get("salario", 0)); anios_cot = int(d.get("anios_cot", 0))
    saldo_rop = float(d.get("saldo_rop", 0)); sin_estado = bool(d.get("sin_estado", False))
    anios = max(1, int(d.get("edad_ret", 65)) - int(d.get("edad_hoy", 35)))
    if sin_estado or salario <= 0:
        return 0.0, 0.0, 0.0, True, None, None
    ivm = calcular_pension_ivm(
        salario_promedio_referencia=salario, cuotas_ivm_hoy=anios_cot * 12, anios_restantes=anios,
        salario_minimo_legal=SALARIO_MINIMO_LEGAL, meses_postergacion=0,
        monto_minimo=MONTO_MINIMO_DEFAULT, monto_maximo=MONTO_MAXIMO_DEFAULT)
    ivm_monto = ivm.monto_mensual if ivm.cumple_requisitos else 0.0
    anios_rop_max = max(0, date.today().year - ROP_ANIO_INICIO)
    cuotas_rop_hoy = min(anios_cot, anios_rop_max) * 12
    rop = proyectar_rop(
        salario_bruto_actual=salario, saldo_actual_rop=saldo_rop, cuotas_rop_hoy=cuotas_rop_hoy,
        anios_restantes=anios, rentabilidad_nominal_pct=ROP_NOMINAL_PCT, inflacion_pct=ROP_INFLACION_PCT,
        plazo_pago_anios=ROP_PLAZO_PAGO_ANIOS, monto_minimo_ivm=MONTO_MINIMO_DEFAULT)
    return ivm_monto + rop.ingreso_mensual_aplicable, ivm_monto, rop.ingreso_mensual_aplicable, ivm.cumple_requisitos, ivm, rop


def calcular(d: dict) -> dict:
    """Pensión estatal en ₡; inversión (capital/aporte/valores/ingresos) en US$."""
    edad_hoy = int(d["edad_hoy"]); edad_ret = int(d["edad_ret"])
    anios = max(1, edad_ret - edad_hoy)
    desired = float(d["ingreso_deseado"])                 # ₡/mes
    capital = float(d.get("capital", 0)); aporte = float(d.get("aporte", 0))  # US$
    sin_estado = bool(d.get("sin_estado", False))

    pension_estatal, ivm_monto, rop_monto, cumple_ivm, ivm_obj, rop_obj = pension_estado(d)

    def proj(rate, fee):  # todo en US$; el setup fee entra directo en dólares
        return proyectar_rendimiento_fijo(
            anios=anios, rendimiento_anual_pct=rate, aporte_inicial=capital, aporte_periodico=aporte,
            frecuencia="Mensual", edad_actual=edad_hoy, management_fee_anual_pct=fee, setup_fee=SETUP_FEE_USD if fee else 0.0)

    def defl(v):
        return v / (1.0 + INFLACION_PCT / 100.0) ** anios

    def ingreso_hoy(vf, tasa):
        return defl(vf) * tasa / 100.0 / 12.0            # US$/mes en valor de hoy

    def serie_bal(res):
        return [(p.edad_cliente, defl(p.balance)) for p in res.puntos]

    r_mkt = proj(TASA_MERCADO_PCT, 0.0)
    r_min = proj(TASA_ACTIVA_MIN_PCT, FEE_ANUAL_PCT)
    r_max = proj(TASA_ACTIVA_MAX_PCT, FEE_ANUAL_PCT)

    vf_mkt, vf_min, vf_max = defl(r_mkt.valor_final), defl(r_min.valor_final), defl(r_max.valor_final)
    aportado = defl(r_mkt.puntos[-1].aportado_bruto_cum)
    serie_aportado = [(p.edad_cliente, defl(p.aportado_bruto_cum)) for p in r_mkt.puntos]

    ing_act_min = ingreso_hoy(r_min.valor_final, TASA_RETIRO_CLASICA_PCT)
    ing_act_max = ingreso_hoy(r_max.valor_final, TASA_RETIRO_CLASICA_PCT)
    ing_sjc_min = ingreso_hoy(r_min.valor_final, SJC_TASA_PCT)
    ing_sjc_max = ingreso_hoy(r_max.valor_final, SJC_TASA_PCT)

    # brecha en ₡ (meta − pensión estatal)
    brecha = max(0.0, desired - pension_estatal)
    brecha_pct = 0.0 if desired <= 0 else brecha / desired * 100.0
    brecha_usd = brecha / tc()

    ratio = brecha / max(desired, 1)
    if anios >= 15 and ratio >= 0.5:
        perfil = "Crecimiento"
    elif anios >= 10 and ratio >= 0.25:
        perfil = "Balanceado"
    else:
        perfil = "Conservador"

    # Ingreso total mensual en ₡ (inversión convertida + pensión estatal)
    total_min = pension_estatal + ing_act_min * tc()      # piso (4%, tope bajo del rango)
    total_max = pension_estatal + ing_sjc_max * tc()      # tope (8%, tope alto del rango)

    return dict(
        anios=anios, edad_ret=edad_ret, desired=desired, pension_estatal=pension_estatal,
        ivm_monto=ivm_monto, rop_monto=rop_monto, cumple_ivm=cumple_ivm,
        brecha=brecha, brecha_pct=brecha_pct, brecha_usd=brecha_usd, perfil=perfil,
        vf_mkt=vf_mkt, vf_min=vf_min, vf_max=vf_max, aportado=aportado,
        ing_act_min=ing_act_min, ing_act_max=ing_act_max, ing_sjc_min=ing_sjc_min, ing_sjc_max=ing_sjc_max,
        total_min=total_min, total_max=total_max,
        meta_4_usd=brecha_usd * 12 * 25, meta_8_usd=brecha_usd * 12 * 12.5,
        serie_market=serie_bal(r_mkt), serie_a15=serie_bal(r_max), serie_a12=serie_bal(r_min), serie_aportado=serie_aportado,
        capital=capital, aporte=aporte, sin_estado=sin_estado,
        ivm_obj=ivm_obj, rop_obj=rop_obj, salario=float(d.get("salario", 0)), anios_cot=int(d.get("anios_cot", 0)),
    )


def grafico_crecimiento(r: dict):
    xs = [e for e, _ in r["serie_market"]]
    ap = [v for _, v in r["serie_aportado"]]
    ym = [v for _, v in r["serie_market"]]
    y12 = [v for _, v in r["serie_a12"]]
    y15 = [v for _, v in r["serie_a15"]]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=xs, y=ap, name="Dinero aportado", mode="lines",
                             line=dict(color=TENUE, width=2, dash="dash"), hovertemplate="Edad %{x}: $%{y:,.0f}<extra>Aportado</extra>"))
    fig.add_trace(go.Scatter(x=xs, y=y15, name="Portafolio de Crecimiento", mode="lines",
                             line=dict(color=VERDE, width=3.5), fill="tozeroy", fillcolor="rgba(18,184,134,0.14)",
                             hovertemplate="Edad %{x}: $%{y:,.0f}<extra>Crecimiento</extra>"))
    fig.add_trace(go.Scatter(x=xs, y=ym, name="Portafolio Básico", mode="lines",
                             line=dict(color=AZUL, width=2.5), hovertemplate="Edad %{x}: $%{y:,.0f}<extra>Básico</extra>"))
    fig.update_layout(height=300, autosize=True, margin=dict(l=0, r=0, t=10, b=0), paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", hovermode="x unified", dragmode=False,
                      legend=dict(orientation="h", y=1.12, x=0, font=dict(size=12)),
                      yaxis=dict(title="", tickprefix="$", tickformat="~s", gridcolor="rgba(140,150,170,0.15)", fixedrange=True),
                      xaxis=dict(title="Edad", gridcolor="rgba(140,150,170,0.10)", fixedrange=True))
    return fig


# ===========================================================================
# PASO 0 — Portada
# ===========================================================================
if ss.paso == 0:
    if _LOGO.exists():
        st.image(str(_LOGO), width=240)   # centrado por CSS; marca ancha (~2.6:1), 240px ≈ 91px de alto
    st.markdown('<div class="wz-hero">¿Vas a poder retirarte con lo que querés? 🎯</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-sub">La mayoría no tiene ni idea de cuánto le va a dar la pensión. En 2 minutos te decimos '
                'tu número — y cuánto te falta (o te sobra) para vivir el retiro que querés. Gratis y sin registrarte.<br>'
                '<span style="font-size:1.02rem">Pensado para profesionales que ganan bien pero nunca han visto este número.</span></div>',
                unsafe_allow_html=True)
    st.markdown('<div class="wz-card">📊 1) Cuánto te va a dar el Estado por mes<br>📈 2) Cuánto podés llegar a tener si invertís'
                '<br>🎯 3) Cuánto te falta — y cómo cerrarlo</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-note">Somos Empowered Investor. Llevamos 11+ años manejando inversiones de una forma '
                'particular: <b>desde la cuenta de cada cliente, no desde la nuestra</b>. Tu plata nunca pasa por nuestras '
                'manos — por eso el nombre: acá el empoderado sos vos. Y hoy es más simple todavía: solo vamos a ver tus '
                'números.</div>', unsafe_allow_html=True)
    if st.button("Empezar →", type="primary", key="b0_next"):
        ir_a(1)
    st.caption(f"Herramienta educativa de {MARCA}. No es asesoría financiera individualizada ni una certificación de la CCSS.")


# ===========================================================================
# PASO 1 — Tu meta
# ===========================================================================
elif ss.paso == 1:
    st.markdown('<div class="wz-step">Paso 1 de 3 · Tu meta</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-hero">Empecemos por el sueño 🌅</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-sub">Primero lo que querés; después vemos si llegás.</div>', unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    edad_hoy = c1.number_input("Tu edad hoy", min_value=18, max_value=75, value=int(d.get("edad_hoy", 35)), step=1)
    edad_ret = c2.number_input("¿A qué edad querés retirarte?", min_value=edad_hoy + 1, max_value=80, value=max(int(d.get("edad_ret", 65)), edad_hoy + 1), step=1)

    moneda = st.radio("Moneda de tu meta", ["₡ Colones", "$ Dólares"], index=(0 if d.get("moneda") == "₡" else 1), horizontal=True)
    es_usd = moneda.startswith("$")
    with st.expander(f"Usamos ₡{TC_DEFAULT:,.0f} por $1 — podés cambiarlo aquí. Este tipo de cambio se usa en toda la calculadora."):
        tc_val = st.number_input("Tipo de cambio (₡ por $1)", min_value=100.0, value=float(d.get("tc", TC_DEFAULT)), step=5.0, format="%0.0f")
    _sim = "$" if es_usd else "₡"
    if ss.get("_goal_moneda") is not None and ss.get("_goal_moneda") != _sim and _parse_dinero(ss.get("wz_goal", "")) > 0:
        _prev = _parse_dinero(ss["wz_goal"])
        ss["wz_goal"] = _fmt_dinero(round(_prev / max(tc_val, 1)) if es_usd else round(_prev * tc_val), _sim)
    ss["_goal_moneda"] = _sim
    _def_col = float(d.get("ingreso_deseado", 7000 * TC_DEFAULT))
    _def_goal = (_def_col / max(tc_val, 1)) if es_usd else _def_col
    monto = money_input(st, f"¿Cuánto querés recibir por mes en tu retiro? ({_sim} de hoy)", "wz_goal", _def_goal, simbolo=_sim)
    ingreso_col = monto * tc_val if es_usd else monto
    st.markdown(f'<div class="wz-eq">= ₡{ingreso_col:,.0f} <span style="color:{TENUE}">·</span> ${ingreso_col / max(tc_val,1):,.0f} <span class="wz-lbl">por mes</span></div>', unsafe_allow_html=True)

    with st.expander("💡 ¿No sabés cuánto poner? Un dato que ayuda"):
        st.write("Para tus 65 es razonable asumir que **tu casa ya estará pagada** (o tu vivienda resuelta) — y la "
                 "vivienda suele comerse un **30–40% del ingreso** de una persona activa. Por eso muchos planifican su "
                 "retiro con el **60–70% de su ingreso actual**: mantenés tu estilo de vida, sin el gasto más grande. "
                 "Ejemplo: si hoy vivís bien con \\$4,000/mes, una meta de \\$2,500–\\$2,800 puede darte la misma vida a los 65.")
    with st.expander("¿Por qué te pregunto esto?"):
        st.write("Tu meta define todo. En vez de empezar por la burocracia, empezamos por lo que querés vivir. El monto "
                 "va en **valor de hoy** (poder de compra actual); nosotros ajustamos la inflación por dentro.")
    b1, b2 = st.columns([1, 2])
    if b1.button("← Volver", key="b1_volver"):
        ir_a(0)
    if b2.button("Siguiente →", type="primary", key="b1_next"):
        if monto <= 0:
            st.warning("✍️ Escribí tu meta mensual para continuar.")
        else:
            d.update(edad_hoy=edad_hoy, edad_ret=edad_ret, ingreso_deseado=ingreso_col,
                     moneda=("$" if es_usd else "₡"), tc=tc_val)
            ir_a(2)


# ===========================================================================
# PASO 2 — El Estado
# ===========================================================================
elif ss.paso == 2:
    st.markdown('<div class="wz-step">Paso 2 de 3 · La pensión del Estado</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-hero">Lo que el Estado te va a dar 🏛️</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-sub">Tu pensión del IVM + tu ahorro del ROP. Con un aproximado basta — después lo afinamos juntos.</div>', unsafe_allow_html=True)
    sin_estado = st.checkbox("Prefiero no incluir la pensión del Estado (calcular solo con mi inversión)", value=bool(d.get("sin_estado", False)))
    if sin_estado:
        st.markdown('<div class="wz-card">Perfecto — calculamos tu plan solo con tu inversión. (Podés volver a incluir la '
                    'pensión cuando querás.)</div>', unsafe_allow_html=True)
        salario = 0.0; anios_cot = 0; saldo_rop = 0.0; no_se = False
    else:
        salario = money_input(st, "Tu salario bruto mensual (₡)", "wz_salario", d.get("salario", 2_000_000), simbolo="₡")
        anios_cot = st.number_input("Años cotizados aproximados", min_value=0, max_value=50, value=int(d.get("anios_cot", 10)), step=1,
                                    help="No necesitás las cuotas exactas — los años bastan (los multiplicamos ×12 por dentro).")
        with st.expander("🔎 ¿Dónde veo mis años cotizados? (Oficina Virtual de la CCSS)"):
            st.markdown(
                "Es gratis y toma 2 minutos:\n\n"
                f"1. Entrá a la **Oficina Virtual de la CCSS** → [aissfa.ccss.sa.cr]({CCSS_URL}) e ingresá con tu "
                "**usuario** (si no tenés, lo creás ahí con tu cédula). Así se ve la pantalla de entrada:")
            if _CCSS_HOME.exists():
                st.image(str(_CCSS_HOME), width=300)
            st.markdown("2. Ya adentro, andá al menú **Pensiones → Reportes → «Proyección de Pensión»**:")
            if _CCSS_IMG.exists():
                st.image(str(_CCSS_IMG), width=380)
            st.markdown(
                "3. Ahí ves **cuántos meses (cuotas) has cotizado**. Dividí entre 12 = tus años.\n\n"
                "*Tip: 300 cuotas = 25 años es el mínimo para la pensión completa del IVM.*")
        no_se = st.checkbox("No sé mi saldo del ROP", value=bool(d.get("no_se_rop", False)))
        if no_se:
            saldo_rop = 0.0
            st.caption("Sin problema: usamos ₡0. La estimación te sale **conservadora** (hacia abajo).")
        else:
            saldo_rop = money_input(st, "Saldo actual de tu ROP (₡)", "wz_saldo_rop", d.get("saldo_rop", 0), simbolo="₡",
                                    help="Lo ves en tu estado de cuenta de la OPC (BN Vital, Popular, BAC, etc.).")
    b1, b2 = st.columns([1, 2])
    if b1.button("← Volver", key="b2_volver"):
        ir_a(1)
    if b2.button("Ver mi brecha →", type="primary", key="b2_next"):
        if (not sin_estado) and salario <= 0:
            st.warning("✍️ Escribí tu salario bruto mensual (o marcá «calcular solo con mi inversión»).")
        else:
            d.update(salario=salario, anios_cot=anios_cot, saldo_rop=saldo_rop, no_se_rop=no_se, sin_estado=sin_estado)
            ir_a(4)


# ===========================================================================
# PASO 3 — Tu capacidad (en US$)
# ===========================================================================
elif ss.paso == 3:
    st.markdown('<div class="wz-step">Paso 3 de 3 · Tu capacidad</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-hero">Lo que podés poner de tu lado 💪</div>', unsafe_allow_html=True)
    _pe3, *_ = pension_estado(d)
    _brecha3 = max(0.0, float(d.get("ingreso_deseado", 0)) - _pe3)
    if _brecha3 > 0:
        st.markdown(f'<div class="wz-sub">Tu brecha a cubrir es <b>{fmt_meta(_brecha3)}/mes</b>. Con lo que pongás vos, la '
                    'empezamos a cerrar 👇</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-note">💵 Tu inversión vive en <b>dólares</b> — el mercado global (acciones, ETFs) se opera en USD. '
                'Por eso te lo preguntamos en dólares; al lado ves el equivalente en colones.</div>', unsafe_allow_html=True)
    capital = money_input(st, "¿Con cuánto podés empezar? (US$)", "wz_cap_usd", d.get("capital", 10_000), simbolo="$")
    st.markdown(f'<div class="wz-eqs">≈ ₡{capital * tc():,.0f}</div>', unsafe_allow_html=True)
    aporte = money_input(st, "¿Cuánto podrías aportar por mes? (US$)", "wz_ap_usd", d.get("aporte", 500), simbolo="$")
    st.markdown(f'<div class="wz-eqs">≈ ₡{aporte * tc():,.0f}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="wz-card" style="border-color:{AZUL};padding:0.9rem 1.1rem;font-size:1.06rem;margin:0.5rem 0">'
                '🌱 Lo ideal es que esto ya sea plata que tenías <b>apartada para invertir</b>. Una guía sana: que ronde el '
                '<b>20% de lo que te entra al mes</b> — es la parte que te construye el futuro.</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="wz-eqs" style="font-size:0.88rem;color:{TENUE};font-weight:600">Los montos en ₡ usan '
                f'₡{tc():,.0f}/$1, el tipo de cambio que definiste en «Empecemos por el sueño».</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-note">📦 <b>Buena práctica que usamos con nuestros clientes:</b> el ahorro es mensual, pero '
                'el envío a tu cuenta de inversión se hace al <b>juntar ~$5,000</b> — así el costo de la transferencia '
                'internacional (SWIFT) se diluye lo más posible. La primera vez te ayudamos a dejar la transferencia lista, '
                'paso a paso; después es rutina.</div>', unsafe_allow_html=True)
    b1, b2 = st.columns([1, 2])
    if b1.button("← Volver a mi brecha", key="b3_volver"):
        ir_a(4)
    if b2.button("Ver cómo crece →", type="primary", key="b3_next"):
        if aporte <= 0:
            st.warning("✍️ Escribí cuánto podrías aportar por mes. (Podés empezar con $0 — lo importante es el aporte mensual.)")
        else:
            d.update(capital=capital, aporte=aporte)   # capital = 0 es válido: se puede empezar sin capital inicial
            ir_a(41)


# ===========================================================================
# PASO 4 — Acto 1: la brecha (₡)
# ===========================================================================
elif ss.paso == 4:
    r = calcular(d)
    st.markdown('<div class="wz-step">Tu resultado · Acto 1 de 3</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-hero">Tu brecha 🎯</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-sub">Tu <b>brecha</b> es el hueco entre lo que querés recibir por mes y lo que te va a dar '
                'la pensión del Estado. Es lo que <b>tu inversión</b> tiene que cubrir.</div>', unsafe_allow_html=True)
    _mx = max(r["desired"], r["pension_estatal"], 1)
    st.markdown(barra(r["desired"], _mx, AZUL, f'Querés: {fmt_meta(r["desired"])}/mes'), unsafe_allow_html=True)
    if not r["sin_estado"]:
        st.markdown(barra(r["pension_estatal"], _mx, VERDE, f'Estado te da: {fmt_meta(r["pension_estatal"])}/mes'), unsafe_allow_html=True)

    if r["ivm_obj"] is not None:
        _ivm = r["ivm_obj"]; _rop = r["rop_obj"]
        _cuotas_hoy = r["anios_cot"] * 12; _meses_fut = r["anios"] * 12
        with st.expander("🔍 ¿Cómo se calculó tu pensión del Estado? (IVM + ROP)"):
            st.markdown(
                "Tu pensión del Estado son **dos cheques** que se suman: el **IVM** (el fondo solidario de la CCSS) y el "
                f"**ROP** (tu cuenta individual obligatoria). Acá se calculan con las mismas reglas que la calculadora "
                f"completa de {MARCA}:")
            st.markdown("**1️⃣ IVM — el cheque de la CCSS**  ·  *Reglamento del Seguro de IVM, arts. 5, 23, 24 y 27*")
            st.markdown(
                f"- **Cuotas:** {_cuotas_hoy:,} que ya llevás ({r['anios_cot']} años × 12) + {_meses_fut:,} que faltan "
                f"({r['anios']} años hasta tu retiro) = **{_ivm.cuotas_totales:,} cuotas**. El mínimo para la pensión "
                "completa son **300 cuotas (25 años)**.")
            if not _ivm.cumple_requisitos:
                st.markdown(f"- **Resultado:** con {_ivm.cuotas_totales:,} cuotas no se llega ni al mínimo de **180** para "
                            "una pensión proporcional → el IVM sale en **₡0**.")
            elif _ivm.es_proporcional:
                st.markdown(f"- **Resultado:** llegás con {_ivm.cuotas_totales:,} cuotas (menos de 300) → **pensión "
                            f"proporcional**: el mismo % de una completa pero × **{_ivm.factor_proporcional * 100:.0f}%** "
                            "(la fracción de las 300 que sí completaste).")
            else:
                st.markdown(f"- **Resultado:** con {_ivm.cuotas_totales:,} cuotas superás las 300 → **pensión completa**.")
            _extra = ""
            if _ivm.cuantia_adicional_pct > 0:
                _extra += f" + **{_ivm.cuantia_adicional_pct:.1f}%** por las cuotas sobre 300 (0.0833% c/u)"
            if _ivm.incremento_postergacion_pct > 0:
                _extra += f" + **{_ivm.incremento_postergacion_pct:.1f}%** por postergación"
            st.markdown(
                f"- **% que te reconocen (art. 24):** cuantía básica **{_ivm.cuantia_basica_pct:.1f}%** (según tu nivel "
                f"salarial frente al salario mínimo legal){_extra} = **{_ivm.porcentaje_reconocido:.1f}%** de tu salario.")
            st.markdown(
                f"- **IVM = salario promedio {col(r['salario'])} × {_ivm.porcentaje_reconocido:.1f}% = "
                f"{col(_ivm.monto_bruto)}**, ajustado al piso ({col(MONTO_MINIMO_DEFAULT)}) y techo "
                f"({col(MONTO_MAXIMO_DEFAULT)}) vigentes 2026 → **{col(_ivm.monto_mensual)}/mes**.")
            st.markdown(
                f"💡 **Un dato que casi nadie sabe:** por más que trabajés o suba tu salario, la pensión del IVM tiene un "
                f"**tope máximo**. Hoy (2026) ese techo es **{col(MONTO_MAXIMO_DEFAULT)}/mes** — no importa cuánto cotizés, "
                "el IVM no puede pagarte más que eso. Es una razón de más para construir tu propio ingreso por fuera.")
            st.markdown("**2️⃣ ROP — tu cuenta individual**  ·  *Ley de Protección al Trabajador N.º 7983, art. 22*")
            _mod = ("régimen transitorio, en cuotas iguales" if _rop.modalidad_aplicable == "transitoria"
                    else "renta mensual (retiro programado)")
            st.markdown(
                "- No es un % del salario: es la **plata acumulada en tu cuenta personal** (una alcancía obligatoria que "
                "invierte tu operadora). Al pensionarte te la devuelven poco a poco.")
            st.markdown(
                f"- **Saldo proyectado al retiro** (en poder de compra de hoy): **{col(_rop.saldo_proyectado)}**, "
                f"entregado como {_mod} → **{col(_rop.ingreso_mensual_aplicable)}/mes**.")
            st.markdown(
                f"**➕ Total del Estado = {col(_ivm.monto_mensual)} (IVM) + {col(_rop.ingreso_mensual_aplicable)} (ROP) "
                f"= {col(r['pension_estatal'])}/mes.**")
            st.caption("Guía educativa con la normativa vigente (valores 2026), no una certificación oficial de la CCSS. "
                       "El monto real lo define la CCSS al momento de tu pensión.")

    if r["brecha"] <= 0:
        st.markdown(f'<div class="wz-card" style="border-color:{VERDE}">🎉 Buenas noticias: con tu pensión proyectada, tu '
                    'meta estaría cubierta. Estás mejor que la gran mayoría. El siguiente nivel ya no es llegar — es '
                    '<b>proteger ese nivel de vida contra la inflación y hacer crecer un patrimonio que quede</b> para los '
                    'tuyos. De eso se tratan los siguientes dos actos.</div>', unsafe_allow_html=True)
    else:
        if not r["sin_estado"]:
            st.markdown(f'<div class="wz-note">El Estado cubre el <b>{100 - r["brecha_pct"]:.0f}%</b> de tu meta. '
                        f'El otro <b>{r["brecha_pct"]:.0f}%</b> te toca a vos.</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="wz-card" style="border-color:{ROJO}">Tu brecha a cubrir con inversión<br>'
                    f'<span class="wz-big" style="color:{ROJO}">{fmt_meta(r["brecha"])}</span><br>'
                    f'<span class="wz-lbl">{fmt_meta_sec(r["brecha"])} por mes</span></div>', unsafe_allow_html=True)
        st.markdown('<div class="wz-note">Si este número te cayó como balde de agua fría: respirá. No es que hayás hecho algo '
                    'mal — a la mayoría le pasa, porque nadie nos enseñó a hacer este cálculo. La buena noticia: lo estás '
                    'viendo con tiempo, y con tiempo esto se resuelve.</div>', unsafe_allow_html=True)

    if (not r["sin_estado"]) and (not r["cumple_ivm"]) and float(d.get("salario", 0)) > 0:
        st.markdown('<div class="wz-note">Ojo: con tus años cotizados y tu horizonte, no llegarías al mínimo de cuotas que '
                    'pide la CCSS para el IVM — por eso aparece en ₡0. No es el fin del mundo: es la razón de más para que '
                    '<b>tu inversión sea el pilar principal</b> de tu plan.</div>', unsafe_allow_html=True)

    _p = r["perfil"]
    if _p == "Crecimiento":
        _pt = ("🚀 <b>Tu perfil sugerido: Crecimiento.</b> Tu brecha es grande pero tenés tiempo — la combinación perfecta "
               "para estrategias de crecimiento con riesgo administrado: más agresivas hoy, más conservadoras conforme te acerqués al retiro.")
    elif _p == "Balanceado":
        _pt = ("⚖️ <b>Tu perfil sugerido: Balanceado.</b> Tu brecha es manejable y tu horizonte lo permite: crecimiento con "
               "moderación, sin apuestas innecesarias.")
    else:
        _pt = ("🛡️ <b>Tu perfil sugerido: Conservador.</b> Estás cerca de tu meta o de tu retiro: la prioridad es proteger "
               "lo ganado y crecer sin sustos.")
    st.markdown(f'<div class="wz-card" style="border-color:{AZUL}">{_pt}<br><br><span class="wz-note">Esto es una sugerencia '
                'inicial — el perfil real lo definimos juntos en tu sesión de diagnóstico.</span></div>', unsafe_allow_html=True)

    st.write("")
    b1, b2 = st.columns([1, 2])
    if b1.button("← Ajustar datos", key="a1_volver"):
        ir_a(2)
    if b2.button("Ahora, lo que podés poner vos →", type="primary", key="a1_next"):
        ir_a(3)


# ===========================================================================
# PASO 41 — Acto 2: el crecimiento (US$)
# ===========================================================================
elif ss.paso == 41:
    r = calcular(d)
    st.markdown('<div class="wz-step">Tu resultado · Acto 2 de 3</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-hero">El crecimiento 📈</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-sub">Así crecería tu inversión (en US$ de hoy) durante los años que te faltan:</div>', unsafe_allow_html=True)
    st.plotly_chart(grafico_crecimiento(r), config={"displayModeBar": False}, width="stretch")
    st.write("")
    st.markdown(barra(r["aportado"], r["vf_max"], TENUE, f'Lo que aportás: ${r["aportado"]:,.0f}'), unsafe_allow_html=True)
    st.markdown(barra(r["vf_mkt"], r["vf_max"], AZUL, f'Portafolio Básico (el mercado solo): ${r["vf_mkt"]:,.0f}'), unsafe_allow_html=True)
    st.markdown(barra(r["vf_max"], r["vf_max"], VERDE, f'Con nuestro sistema: ${r["vf_max"]:,.0f}'), unsafe_allow_html=True)
    st.markdown(f'<div class="wz-eq" style="color:{VERDE}">👉 En esta proyección, la diferencia entre ambos portafolios es de '
                f'~${r["vf_max"] - r["vf_mkt"]:,.0f}. Un número así despierta una duda sana: <i>«¿y esto no será humo?»</i> '
                'Nos encanta el sano escepticismo — abrí el cuadro de abajo y te mostramos un ejemplo sencillo de una '
                'estrategia sistemática basada en reglas. Nuestro sistema general es el siguiente: acumular excelentes '
                'fundamentos sistemáticos de inversión, para luego sumarlos a nuestros 11 años de experiencia, y aplicarlos '
                'al contexto de tus metas, aumentando o disminuyendo riesgo/rendimiento según sea necesario.</div>', unsafe_allow_html=True)

    _bh, _sm = FABER_STATS["bh"], FABER_STATS["sma"]
    with st.expander("🔍 ¿De dónde sale esa diferencia? (spoiler: no es magia)"):
        st.markdown(
            "Las estrategias sistemáticas no son ciencia secreta ni una caja negra. Son **reglas sencillas, aplicadas "
            "sistemáticamente**.\n\n"
            "Te regalamos un ejemplo real que podés investigar — y hasta aplicar — por tu cuenta: la **estrategia del "
            "promedio móvil de 10 meses**, documentada en una investigación académica de Faber (2007) (cita completa está al pie):\n\n"
            "1. Al final de cada mes, mirá si el mercado está **por encima** de su promedio de los últimos 10 meses → te "
            "quedás invertido.\n"
            "2. Si está **por debajo** → te pasás a efectivo y esperás.\n"
            "3. Eso es todo. Una regla, una vez al mes.\n\n"
            "¿El resultado histórico? Los números hablan solos:")
        st.markdown(
            "| | Comprar y mantener | Con la regla de 10 meses |\n"
            "|---|---|---|\n"
            f"| Retorno anual promedio (CAGR) | {_bh['cagr']:.1f}% | {_sm['cagr']:.1f}% |\n"
            f"| Peor caída histórica (drawdown) | {_bh['maxdd']:.1f}% | {_sm['maxdd']:.1f}% |\n"
            f"| Calmar (retorno ÷ peor caída) | {_bh['calmar']:.2f} | {_sm['calmar']:.2f} |")
        st.markdown(
            '<div class="wz-note">'
            'Casi el mismo retorno 🙂<br>'
            'Mucho menos caída máxima 😃<br>'
            'Mejor relación riesgo–rendimiento 🤩</div>', unsafe_allow_html=True)
        st.caption(f"Regla original: Mebane T. Faber, *«A Quantitative Approach to Tactical Asset Allocation»*, "
                   f"The Journal of Wealth Management (2007). Métricas: backtest propio de {MARCA} sobre el **S&P 500, "
                   f"usando el ETF SPY** como referencia, período {FABER_PERIODO}, con costos de transacción incluidos. "
                   "Ejemplo educativo — los resultados pasados no garantizan resultados futuros.")
        st.markdown(
            "Ese es el «secreto» que no es secreto — y lo podés aplicar por tu cuenta. Nosotros usamos estrategias de este "
            "tipo — **la menor cantidad de variables posible, el mayor valor agregado posible** — que hemos perfeccionado "
            "durante 11 años, y **combinamos varias** para sustentar nuestro criterio de inversión. La proyección de arriba "
            "nace de ese trabajo. Es una proyección, no una promesa — pero tampoco es humo: es método. Dicho eso, siempre "
            "tenemos que aclararlo: los resultados pasados no garantizan resultados futuros.")

    st.markdown('<div class="wz-note">El S&P 500 ha rendido en promedio ~9.5% nominal anual a muy largo plazo (~6.5% real, '
                'descontando inflación). Nuestras estrategias apuntan a un <b>15% nominal anual</b> — el crecimiento de capital '
                'es nuestra especialidad. Cuánto riesgo tomamos no es una fórmula única: depende del contexto de cada cliente — '
                '<b>el tamaño de tu brecha frente a tu meta, tu horizonte y tu perfil</b>. Combinamos sistemas probados con el '
                'criterio de 11 años leyendo mercados. No se garantizan retornos. Los resultados pasados no garantizan '
                'resultados futuros.</div>', unsafe_allow_html=True)

    st.caption("✅ Estos números ya descuentan **todos los costos** del servicio — lo que ves es lo que te queda. Nuestro "
               "trabajo es simple: que la diferencia entre 'el mercado solo' y 'con estrategias' pague el servicio muchas veces.")
    with st.expander("¿Cuánto cuesta el servicio? (transparencia total)"):
        st.markdown(
            f"Nuestro modelo es simple: un **{FEE_ANUAL_PCT:.0f}% anual sobre lo que tengas en el portafolio** — sin costos "
            "de salida, sin permanencias, sin comisiones ocultas.\n\n"
            "Después de 11 años en el mercado, sabemos que dejar listo un sistema de inversión completo (cuenta "
            "internacional a tu nombre, fondeo, estrategia y primer año de manejo) requiere un **presupuesto mínimo de "
            f"~\\${SETUP_FEE_USD:,.0f} para empezar**.\n\n"
            "📍 Si tu presupuesto actual está por debajo de eso, no pasa nada — empezá por nuestro **contenido gratuito "
            "en Instagram** y construí tu base primero — publicamos educación práctica todas las semanas.\n\n"
            "📞 Y si ya te queda claro el retorno que puede generar que te acompañemos en el proceso completo de construir "
            "tu fondo de retiro, **agendá una llamada** — ahí revisamos tus números y definimos tu plan.")
        _e1, _e2 = st.columns(2)
        _e1.link_button("📞 Agendar mi llamada", CALENDLY_URL)
        _e2.link_button("📱 Seguirnos en Instagram", INSTAGRAM_URL)
    st.markdown('<div class="wz-card">🔒 <b>¿Y dónde estaría tu plata?</b> En una cuenta <b>a TU nombre</b> en un bróker '
                'regulado en EE.UU. Nosotros solo tenemos permiso de comprar y vender dentro de tu cuenta — '
                '<b>nadie puede retirar tu dinero excepto vos</b>. Cero letra pequeña. Y no estás solo en el trámite: '
                '<b>te guiamos paso a paso</b> en la apertura y el fondeo de tu cuenta.</div>', unsafe_allow_html=True)

    st.write("")
    b1, b2 = st.columns([1, 2])
    if b1.button("← Volver", key="a2_volver"):
        ir_a(3)
    if b2.button("Acto 3 — mi ingreso de retiro →", type="primary", key="a2_next"):
        ir_a(42)


# ===========================================================================
# PASO 42 — Acto 3: el ingreso de retiro
# ===========================================================================
elif ss.paso == 42:
    r = calcular(d)
    _ing4 = r["ing_act_max"]; _ing8 = r["ing_sjc_max"]; _total = r["total_max"]
    st.markdown('<div class="wz-step">Tu resultado · Acto 3 de 3</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-hero">Tu ingreso de retiro 🏖️</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-sub">Ya tenés tu portafolio. Ahora la pregunta importante: ¿cuánto te puede dar cada mes, '
                'para siempre, sin que se acabe?</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-note">Hay <b>dos formas</b> de convertir ese portafolio en un ingreso mensual — mirá la diferencia:</div>', unsafe_allow_html=True)

    cA, cB = st.columns(2)
    cA.markdown(f'<div class="wz-mini" style="border-color:{AZUL}"><div class="wz-lbl">🟦 Sacando el 4% al año</div>'
                f'<div class="wz-num" style="color:{AZUL}">{fmt_meta_usd(_ing4)}</div><div class="wz-lbl">por mes</div>'
                '<div class="wz-note">la vía prudente: tu portafolio te paga y aun así suele seguir creciendo.</div></div>', unsafe_allow_html=True)
    cB.markdown(f'<div class="wz-mini" style="border-color:{VERDE}"><div class="wz-lbl">🟩 Como ingreso pasivo (~{SJC_TASA_PCT:.0f}%)</div>'
                f'<div class="wz-num" style="color:{VERDE}">{fmt_meta_usd(_ing8)}</div><div class="wz-lbl">por mes</div>'
                '<div class="wz-note">vivís del rendimiento; el dinero que juntaste no se toca.</div></div>', unsafe_allow_html=True)

    with st.expander("🤔 ¿Cómo funciona eso de retirar plata sin quedarme sin plata?"):
        st.markdown(
            "La lógica es más simple de lo que suena: al llegar al retiro, tu portafolio se traslada a instrumentos "
            "**muy seguros** — como bonos del Tesoro de EE.UU., que usualmente rinden alrededor de un **4% al año**. "
            "Y entonces **vivís del rendimiento, no del capital**: sacás lo que el portafolio genera cada año, y el "
            "principal queda intacto, ahí, trabajando.\n\n"
            "Por eso el 4% es la guía clásica del retiro: es el ingreso que puede darte lo más seguro del mundo "
            "**sin tocarte la plata**.\n\n"
            "Parte de nuestro servicio es ir un paso más allá con herramientas conservadoras de ingreso pasivo que "
            "pueden generar cerca del doble — el detalle está en la nota de abajo.")
        st.caption("Tasas aproximadas e históricas, sujetas a condiciones de mercado y verificación; no constituyen una "
                   "promesa de rendimiento.")

    _desg = f'{fmt_meta_usd(_ing8)} de tu inversión'
    if r["pension_estatal"] > 0:
        _desg += f' + {fmt_meta(r["pension_estatal"])} de la pensión del Estado'
    st.markdown(f'<div class="wz-card" style="border-color:{VERDE}"><b>Tu ingreso total, por mes</b><br>'
                f'<span class="wz-big" style="color:{VERDE}">{fmt_meta(_total)}</span><br>'
                f'<span class="wz-lbl">{_desg}</span><br>'
                f'<span class="wz-lbl">tu meta era {fmt_meta(r["desired"])}/mes</span></div>', unsafe_allow_html=True)

    if _total >= r["desired"]:
        st.markdown(f'<div class="wz-card" style="border-color:{VERDE}">✅ <b>Tu meta es alcanzable.</b> Con lo que podés '
                    f'aportar y un poco de constancia, llegás a <b>{fmt_meta(_total)}/mes</b> — por encima de lo que soñabas. '
                    'No hace falta suerte: hace falta un plan y sostenerlo. En eso te acompañamos.</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="wz-card" style="border-color:{AMBAR}">📍 <b>Vas por buen camino</b> — hoy tu proyección llega '
                    f'a {fmt_meta(_total)}/mes y tu meta era {fmt_meta(r["desired"])}. Esa diferencia se cierra con detalles: aportar '
                    'un poquito más, mover un año el retiro, o ajustar la meta. <b>Eso lo resolvemos juntos en tu sesión de diagnóstico</b> '
                    '— no tenés que hacerlo solo.</div>', unsafe_allow_html=True)

    st.markdown(f'<div class="wz-card">🌳 <b>Y acá está la mejor parte: el legado.</b> Como vivís del rendimiento, el dinero que '
                f'construiste — <b>≈ {fmt_meta_usd(r["vf_min"])}</b> — <b>queda intacto</b>, y un día pasa a tus hijos, o a quien '
                'vos querás. No es magia: es '
                'exactamente lo que hace una familia en <b>Estados Unidos</b> de forma natural — dejar que las inversiones '
                'crezcan solas, año tras año, hasta volverse el patrimonio que se hereda de generación en generación. '
                'Nosotros te ayudamos a hacer lo mismo desde acá.</div>', unsafe_allow_html=True)

    st.markdown(f'<div class="wz-note">Ese ~{SJC_TASA_PCT:.0f}% de ingreso pasivo viene de una <b>alianza con un puesto de bolsa '
                'costarricense, San José Capital</b>, con instrumentos de <b>garantía real</b> (respaldados por bienes raíces) '
                f'que pueden generar alrededor de un <b>{SJC_TASA_PCT:.0f}%</b>. Así podés tener <b>el doble de ingreso con el '
                'mismo portafolio</b> — o, visto de otra manera, necesitar <b>la mitad del portafolio</b> para el mismo ingreso. '
                'Si tu perfil califica, te contamos el detalle (inversión mínima US\\$10,000). Tasa y condiciones sujetas a '
                'disponibilidad y verificación; no constituye una promesa de rendimiento.</div>', unsafe_allow_html=True)

    st.divider()
    st.markdown('<div class="wz-hero">📕 Un último paso</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-sub">Ya tenés tus números. Contanos qué querés hacer con ellos — y según tu caso, '
                'te decimos exactamente cómo seguir.</div>', unsafe_allow_html=True)
    b1, b2 = st.columns([1, 2])
    if b1.button("← Acto 2", key="a3_volver"):
        ir_a(41)
    if b2.button("Continuar →", type="primary", key="a3_next"):
        ir_a(5)


# ===========================================================================
# PASO 5 — Captura
# ===========================================================================
elif ss.paso == 5:
    r = calcular(d)
    _cap_ok = (r["capital"] >= CAPITAL_CALIFICADO) or (CALIFICA_TAMBIEN_POR_FLUJO and r["capital"] + r["aporte"] * 12 >= CAPITAL_CALIFICADO)
    califica = _cap_ok and (r["anios"] >= HORIZONTE_CALIFICADO)

    st.markdown('<div class="wz-step">Último paso</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-hero">Ya viste tus números 🎯</div>', unsafe_allow_html=True)
    st.markdown('<div class="wz-sub">Última pregunta — y es la más importante.</div>', unsafe_allow_html=True)

    _OPCIONES = [
        "🤝 Quiero que me acompañen porque estoy ocupado — ustedes me guían para abrir mi cuenta y se encargan de la estrategia y el manejo.",
        "📚 Quiero aprender primero — todavía no estoy listo para mover plata",
        "👀 Solo quería ver mis números — por ahora nada más",
    ]
    intencion = st.radio("**¿Qué te gustaría hacer ahora?**", _OPCIONES, index=None, key="intencion")
    _int_corto = ("Acompañamiento" if intencion and intencion.startswith("🤝") else
                  "Aprender" if intencion and intencion.startswith("📚") else
                  "Solo números" if intencion and intencion.startswith("👀") else "—")
    if intencion:
        d["intencion"] = _int_corto

    if califica:
        # ===================== RAMA A — CALIFICADO =====================
        _vs_d = "color:#8a94a6;font-size:0.98rem"
        st.markdown(
            '<div class="wz-card">📕 <b>Tu Reporte Completo de Retiro</b> — con tus números, incluye:<br><br>'
            f'✅ <b>Tus números explicados de manera sencilla</b> <span style="{_vs_d}">— tu brecha, tus escenarios y tu perfil de estrategia</span><br>'
            f'✅ <b>El ABC de invertir afuera</b> <span style="{_vs_d}">— qué es un bróker, qué es un custodio, y qué protege el SIPC (y qué no)</span><br>'
            f'✅ <b>La transferencia internacional sin misterio</b> <span style="{_vs_d}">— cómo se hace, cuánto cuesta, y las buenas prácticas para pagar lo menos posible</span><br>'
            f'✅ <b>Los montos que funcionan</b> <span style="{_vs_d}">— con cuánto conviene empezar y cuánto aportar por mes según tu capacidad</span><br>'
            f'✅ <b>Cómo funciona nuestro servicio</b> <span style="{_vs_d}">— y la alianza con San José Capital (~8% de ingreso pasivo con garantía real)</span><br>'
            f'✅ <b>Tus primeros pasos</b> <span style="{_vs_d}">— según tu situación específica</span><br><br>'
            'Lo preparamos con tus números y <b>te lo enviamos por WhatsApp</b>.</div>', unsafe_allow_html=True)

        c_nom, c_ape = st.columns(2)
        nombre = c_nom.text_input("Tu nombre")
        apellido = c_ape.text_input("Tu apellido")
        whats = st.text_input("Tu WhatsApp", placeholder="8888-8888")
        correo = st.text_input("Tu correo", placeholder="vos@correo.com")

        # Teclado móvil correcto por campo (Streamlit no expone inputmode): lo fijamos por aria-label en el documento padre.
        components.html("""
        <script>
        (function(){
          const D = window.parent.document;
          const MAP = [
            ['Tu WhatsApp', {inputmode:'tel', autocomplete:'tel'}],
            ['Tu correo',   {inputmode:'email', autocomplete:'email'}],
            ['Tu nombre',   {autocomplete:'given-name'}],
            ['Tu apellido', {autocomplete:'family-name'}],
          ];
          function apply(){ MAP.forEach(function(p){
            const el = D.querySelector('input[aria-label="'+p[0]+'"]');
            if(el){ for(const k in p[1]){ el.setAttribute(k, p[1][k]); } }
          }); }
          apply(); let n=0; const iv=setInterval(function(){ apply(); if(++n>20) clearInterval(iv); }, 150);
        })();
        </script>
        """, height=0)

        # Dato INTERNO: cuánto tardó el lead en la calculadora (señal de interés). Se guarda en el registro
        # del lead para el futuro webhook, pero NO se muestra en el mensaje de WhatsApp (no lo ve el lead).
        d["tiempo_calculadora"] = _tiempo_transcurrido()
        # TODO: webhook (Google Sheets/CRM) con nombre, apellido, whats, correo, intención, perfil, brecha,
        #       tiempo_calculadora y demás métricas — el pipeline automatizado del Reporte Completo se alimenta
        #       de aquí. Hoy no hay backend: la ÚNICA captura real es el mensaje de wa.me que envía el lead.
        _datos_ok = bool(nombre and apellido and whats and correo and "@" in correo)
        if intencion and _datos_ok:
            # Nota: NO se marca "cliente calificado" en el mensaje — es info interna y, en el MVP, TODO lead que
            # llega a este botón ya es calificado (los no calificados van a Instagram, sin formulario ni WhatsApp).
            _msg = (f"Hola, soy {nombre} {apellido}. Hice mi cálculo de brecha en la web de {MARCA} y quiero mi Reporte Completo de Retiro.\n"
                    f"• Intención: {_int_corto}\n"
                    f"• Meta: {ambos_col(r['desired'])}/mes  • Pensión del Estado: {ambos_col(r['pension_estatal'])}/mes  • Brecha: {ambos_col(r['brecha'])}/mes\n"
                    f"• Capacidad: ${r['capital']:,.0f} inicial + ${r['aporte']:,.0f}/mes\n"
                    f"• Perfil sugerido: {r['perfil']}\n"
                    f"• Mi correo: {correo}  • Mi WhatsApp: {whats}")
            _url = f"https://wa.me/{WHATSAPP_NUMERO}?text=" + urllib.parse.quote(_msg)
            st.link_button("📕 Enviame mi Reporte Completo →", _url, type="primary")
        else:
            if st.button("📕 Enviame mi Reporte Completo →", type="primary", key="b5_diag"):
                if not intencion:
                    st.warning("Contanos primero qué te gustaría hacer 👆")
                else:
                    st.warning("Completá tus datos para enviarte tu Reporte Completo — te toma 10 segundos.")

        # ---- CTA condicional según intención (todos calificados en esta rama) ----
        # UN solo formulario, DOS destinos: el mismo nombre/apellido/correo alimenta el wa.me y este Calendly precargado.
        _cal_url = calendly_prefill(nombre, apellido, correo, whats, r, _int_corto)
        if intencion:
            st.write("")
            if _int_corto == "Acompañamiento":
                st.markdown(f'<div class="wz-card" style="border-color:{VERDE}">Perfecto — <b>eso es exactamente lo que hacemos</b>: '
                            'te guiamos paso a paso en la apertura y el fondeo de tu cuenta, y la estrategia y el manejo corren por '
                            'nuestra cuenta. Agendá tu <b>sesión de diagnóstico</b> (30 min, sin costo ni compromiso): revisamos tus '
                            'números juntos y te decimos con honestidad <b>si tu caso es para nosotros o no</b>.</div>', unsafe_allow_html=True)
                st.link_button("📞 Agendar mi sesión de diagnóstico", _cal_url, type="primary")
                st.markdown(f'<div class="wz-note">💡 Llevá tu número de brecha a la llamada ({fmt_meta(r["brecha"])}) — '
                            'empezamos directo desde tu caso, sin vueltas.</div>', unsafe_allow_html=True)
            else:  # Aprender / Solo números, pero calificado
                st.markdown('<div class="wz-card">Tu <b>Reporte Completo</b> te llega igual, <b>sin compromiso</b>. Y si en algún '
                            'momento querés que veamos tus números juntos, la puerta está abierta:</div>', unsafe_allow_html=True)
                st.link_button("📞 Agendar una sesión de diagnóstico", _cal_url)

    else:
        # ===================== RAMA B — NO CALIFICADO =====================
        # Sin formulario ni promesa de reporte: hoy no hay backend y el tiempo de Jose se reserva para calificados.
        if intencion:
            st.write("")
            if _int_corto == "Acompañamiento":
                st.markdown('<div class="wz-card">Nos encanta esa decisión — y queremos que llegués bien preparado. Para que el '
                            'servicio tenga sentido para vos (por los costos de una cuenta internacional), lo ideal es llegar con una '
                            'base de capital. <b>El camino más rápido para construirla:</b> ordenar tus finanzas y ahorrar con '
                            'sistema. En nuestro Instagram publicamos educación práctica y gratuita para lograr exactamente eso. '
                            'Cuando tengás la base, volvé — la llamada te va a estar esperando 🤝</div>', unsafe_allow_html=True)
            elif _int_corto == "Aprender":
                st.markdown('<div class="wz-card">Esa es una gran respuesta — aprender primero es invertir en vos. En nuestro '
                            '<b>Instagram</b> publicamos educación práctica todas las semanas: finanzas personales, inversión y '
                            'retiro, explicado de manera sencilla. Empezá por ahí — gratis.</div>', unsafe_allow_html=True)
            elif _int_corto == "Solo números":
                st.markdown('<div class="wz-card">Perfecto — tus números ya son tuyos. Si querés seguir aprendiendo a tu ritmo, en '
                            'nuestro Instagram publicamos contenido gratuito todas las semanas. Y si algún día el número te empieza '
                            'a dar vueltas en la cabeza, ya sabés dónde encontrarnos 🤝</div>', unsafe_allow_html=True)
            st.link_button("📱 Seguirnos en Instagram → @joseinvestor.cr", INSTAGRAM_URL, type="primary")

    st.write("")
    if st.button("← Volver al resultado", key="b5_volver"):
        ir_a(42)
    if califica:
        st.caption(f"Tus datos se usan solo para enviarte tu Reporte Completo. {MARCA} · herramienta educativa.")

# ---- footer permanente: sesión directa (oculto en el paso final para no competir con los CTAs) ----
if ss.paso != 5:
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown(f'<div style="text-align:center;color:{TENUE};font-size:1.02rem">¿Preferís saltarte la calculadora y ver tus '
                f'números con nosotros directamente? <a href="{CALENDLY_URL}" target="_blank" style="color:{AZUL};font-weight:700">'
                'Agendá una sesión de diagnóstico →</a></div>', unsafe_allow_html=True)
