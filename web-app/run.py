import copy
import logging
import os
import sys
import uuid

from dotenv import find_dotenv, load_dotenv
from flask import (Flask, redirect, render_template, request,
                   send_from_directory, session, url_for)

load_dotenv(find_dotenv())

# Make the repo-root packages (e.g. tools/) importable from the web app.
# Appended (not prepended) so web-app's own db.py / utils.py keep priority.
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT_DIR not in sys.path:
    sys.path.append(_ROOT_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

from empresa import empresa_bp
from administracion import admin_bp
from persona import persona_bp
from whatsapp_webhook import whatsapp_webhook_bp
from paypal_webhook import paypal_webhook_bp

app.register_blueprint(empresa_bp, url_prefix='/empresa')
app.register_blueprint(admin_bp, url_prefix='/administracion')
app.register_blueprint(persona_bp, url_prefix='/persona')
app.register_blueprint(whatsapp_webhook_bp, url_prefix='/whatsapp')
app.register_blueprint(paypal_webhook_bp, url_prefix='/paypal')

# Start background scheduler (guard against Werkzeug reloader spawning it twice)
if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    from scheduler import create_scheduler
    _scheduler = create_scheduler()
    _scheduler.start()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/terminos')
def terminos():
    return render_template('terminos.html')


# Diagnóstico Financiero: página estática autocontenida (web-app/diagnostico/),
# enlazada desde el submenú "Finanzas Personales" del landing. Público, sin login
# (igual que /calculadora/): no muestra datos de clientes.
@app.route('/diagnostico')
@app.route('/diagnostico/')
def diagnostico():
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'diagnostico'),
        'diagnostico-financiero.html',
    )


# Mismo formulario, en modo edición para el asesor: carga un diagnóstico ya
# enviado, lo corrige junto al cliente y lo reenvía. Protegido con la MISMA
# sesión que /ruta y el Panel de Administración. La página detecta el modo por
# su propia URL, así que el archivo servido es el mismo.
@app.route('/diagnostico/editar')
def diagnostico_editar():
    if not session.get("admin_authenticated"):
        return redirect('/calculadora-acceso?next=/diagnostico/editar')
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'diagnostico'),
        'diagnostico-financiero.html',
    )


@app.route('/diagnostico/api/cargar')
def diagnostico_api_cargar():
    """Último diagnóstico de un correo, listo para reeditar. Solo asesor."""
    if not session.get("admin_authenticated"):
        return {"ok": False, "error": "No autorizado."}, 401
    correo = (request.args.get('correo') or '').strip().lower()
    if not correo or '@' not in correo:
        return {"ok": False, "error": "Ingrese un correo válido."}, 400

    from db import get_diagnostico_para_editar
    try:
        diag = get_diagnostico_para_editar(correo)
    except Exception as e:
        app.logger.error(f"Diagnóstico editar: fallo consultando {correo}: {e}")
        return {"ok": False, "error": "No se pudo consultar la base."}, 500
    if not diag:
        return {"ok": False, "error": "No hay diagnósticos para ese correo."}, 404

    payload = diag["payload"]
    if diag["convertido"]:
        # Fila anterior a payload_raw: los montos quedaron en colones y el
        # nombre trae la anotación del monto original en dólares.
        from diagnostico_report import limpiar_anotacion_usd
        payload = limpiar_anotacion_usd(payload)

    return {"ok": True, "payload": payload, "id": diag["id"],
            "created_at": diag["created_at"].isoformat(),
            "total": diag["total"], "convertido": diag["convertido"],
            "es_correccion": diag["corregido_de"] is not None}


# Estrategias de Inversión (calculadora Streamlit): acceso exclusivo del asesor.
# La app va proxied por nginx directo a :8501 sin pasar por Flask, así que nginx
# valida cada request con auth_request → GET /calculadora-auth (204/401) y ante
# 401 redirige a /calculadora-acceso. Se usa la MISMA contraseña y flag de
# sesión que el Panel de Administración (admin_authenticated): un solo login
# del asesor habilita ambos.

@app.route('/calculadora-auth')
def calculadora_auth():
    if session.get("admin_authenticated"):
        return "", 204
    return "", 401


def _safe_next(default='/calculadora/'):
    """Destino post-login: solo rutas internas (evita open redirect)."""
    nxt = request.args.get('next') or request.form.get('next') or ''
    if nxt.startswith('/') and not nxt.startswith('//'):
        return nxt
    return default


@app.route('/calculadora-acceso', methods=['GET', 'POST'])
def calculadora_acceso():
    from utils import rate_limit_ok

    if session.get("admin_authenticated"):
        return redirect(_safe_next())

    error = None
    if request.method == 'POST':
        ip = (request.headers.get('X-Forwarded-For', request.remote_addr or '?')
              .split(',')[0].strip())
        if not rate_limit_ok("calculadora-acceso", ip, max_hits=10):
            error = "Demasiados intentos. Espere una hora e intente de nuevo."
        else:
            expected = os.environ.get("ADMIN_PASSWORD", "")
            if not expected:
                error = "ADMIN_PASSWORD no está configurado en el servidor."
            elif request.form.get("password", "") == expected:
                session["admin_authenticated"] = True
                return redirect(_safe_next())
            else:
                error = "Contraseña incorrecta."

    return render_template('auth/calculadora_acceso.html', error=error)


# Tipo de cambio USD→CRC para el formulario del diagnóstico (inputs en dólares).
# Usa el corte más reciente de core.exchange_rates vía compute_amount_local
# (mismo criterio que el pipeline). Cache en memoria: las tasas cambian 1 vez/día.
_TC_CACHE = {"t": 0.0, "data": None}
_TC_CACHE_TTL = 3600  # segundos


def _tipo_cambio_usd():
    """{'usd_crc': float|None, 'fecha': str|None} con cache de 1 hora."""
    import time
    if _TC_CACHE["data"] and time.time() - _TC_CACHE["t"] < _TC_CACHE_TTL:
        return _TC_CACHE["data"]
    from db import compute_amount_local, get_connection
    try:
        conn = get_connection()
        try:
            monto, _, fecha = compute_amount_local(conn, 1.0, "USD")
        finally:
            conn.close()
    except Exception as e:
        app.logger.error(f"Diagnóstico: no se pudo leer el tipo de cambio: {e}")
        return {"usd_crc": None, "fecha": None}  # sin cachear el fallo
    data = {"usd_crc": float(monto) if monto else None,
            "fecha": str(fecha) if fecha else None}
    _TC_CACHE.update(t=time.time(), data=data)
    return data


@app.route('/diagnostico/tipo-cambio')
def diagnostico_tipo_cambio():
    return _tipo_cambio_usd()


# Rate limit del envío de diagnósticos (endpoint público que manda correos):
# máx. 5 envíos por IP por hora, en memoria (aproximado con varios workers).
# El asesor autenticado usa un contador aparte y mucho más holgado: la ruta ya
# exige sesión de administrador, así que no hay vector de abuso, pero el tope
# sigue protegiendo de un bucle accidental.
_DIAG_RATE = {}
_DIAG_RATE_MAX = 5
_DIAG_RATE_ASESOR_MAX = 30
_DIAG_RATE_WINDOW = 3600  # segundos


def _diag_rate_ok(ip, asesor=False):
    import time
    now = time.time()
    clave = ("asesor:" if asesor else "") + ip
    tope = _DIAG_RATE_ASESOR_MAX if asesor else _DIAG_RATE_MAX
    hits = [ts for ts in _DIAG_RATE.get(clave, []) if now - ts < _DIAG_RATE_WINDOW]
    if len(hits) >= tope:
        _DIAG_RATE[clave] = hits
        return False
    hits.append(now)
    _DIAG_RATE[clave] = hits
    return True


@app.route('/diagnostico/enviar', methods=['POST'])
def diagnostico_enviar():
    from diagnostico_report import (aplicar_tipo_cambio, sanitize_payload,
                                    send_report, usa_usd)

    # Detrás de nginx la IP real viene en X-Forwarded-For
    ip = (request.headers.get('X-Forwarded-For', request.remote_addr or '?')
          .split(',')[0].strip())
    asesor = bool(session.get("admin_authenticated"))
    if not _diag_rate_ok(ip, asesor=asesor):
        return {"ok": False, "error": "Demasiados envíos. Intente de nuevo en "
                "una hora."}, 429

    data = request.get_json(silent=True)
    payload, error = sanitize_payload(data)
    if error:
        return {"ok": False, "error": error}, 400

    # Corrección del asesor: encadena esta fila con el diagnóstico que corrige.
    # Solo se acepta con sesión de asesor y con forma de UUID — un id inválido
    # reventaría el INSERT y la persistencia es best-effort (se perdería en
    # silencio).
    corregido_de = None
    if asesor and isinstance(data, dict) and data.get("corregido_de"):
        try:
            corregido_de = str(uuid.UUID(str(data["corregido_de"])))
        except (ValueError, AttributeError, TypeError):
            return {"ok": False, "error": "Referencia de corrección inválida."}, 400

    # Copia fiel de lo que se escribió, antes de que la conversión reescriba
    # montos y nombres: es lo que recarga el editor del asesor.
    payload_raw = copy.deepcopy(payload)

    # Montos en dólares: se convierten a colones con el tipo de cambio más
    # reciente antes de calcular agregados (todo el reporte va en colones).
    if usa_usd(payload):
        tc = _tipo_cambio_usd()
        if not tc["usd_crc"]:
            return {"ok": False, "error": "No se pudo obtener el tipo de cambio "
                    "para convertir los montos en dólares. Intente más tarde."}, 503
        aplicar_tipo_cambio(payload, tc["usd_crc"], tc["fecha"])

    # Persistencia en asesoria_db (base separada de Neto app): el payload
    # completo alimenta los reportes de la asesoría. Best-effort: un fallo de
    # BD se loguea fuerte pero NUNCA bloquea el reporte del prospecto.
    from db import mark_diagnostico_sent, save_diagnostico
    diag_id = None
    try:
        diag_id = save_diagnostico(payload, ip, payload_raw=payload_raw,
                                   corregido_de=corregido_de)
    except Exception as e:
        app.logger.error(f"Diagnóstico: fallo guardando en asesoria_db "
                         f"({payload['correo']}): {e}")

    try:
        send_report(payload)
    except Exception as e:
        app.logger.error(f"Diagnóstico: fallo enviando reporte a "
                         f"{payload['correo']}: {e}")
        return {"ok": False, "error": "No se pudo enviar el reporte. Intente de "
                "nuevo más tarde."}, 500

    if diag_id:
        try:
            mark_diagnostico_sent(diag_id)
        except Exception as e:
            app.logger.error(f"Diagnóstico: fallo marcando report_sent "
                             f"({diag_id}): {e}")

    app.logger.info(f"Diagnóstico enviado a {payload['correo']} (cc asesor)"
                    + (f" | corrige {corregido_de}" if corregido_de else "")
                    + (f" | guardado id={diag_id}" if diag_id else " | NO guardado"))
    return {"ok": True}


# ── Hoja de Ruta Financiera (uso exclusivo del asesor) ───────────────────────
# Dashboard de la primera reunión de asesoría: carga por correo el último
# diagnóstico guardado en asesoria_db y construye la ruta (baby steps
# adaptados). Protegido con la MISMA sesión del asesor que /calculadora/
# (admin_authenticated) porque muestra datos de prospectos.

@app.route('/ruta')
@app.route('/ruta/')
def ruta_financiera():
    if not session.get("admin_authenticated"):
        return redirect('/calculadora-acceso?next=/ruta')
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'diagnostico'),
        'ruta-financiera.html',
    )


@app.route('/ruta/api/diagnostico')
def ruta_api_diagnostico():
    if not session.get("admin_authenticated"):
        return {"ok": False, "error": "No autorizado."}, 401
    correo = (request.args.get('correo') or '').strip().lower()
    if not correo or '@' not in correo:
        return {"ok": False, "error": "Ingrese un correo válido."}, 400
    from db import get_diagnostico_by_correo
    try:
        diag = get_diagnostico_by_correo(correo)
    except Exception as e:
        app.logger.error(f"Ruta: fallo leyendo diagnóstico de {correo}: {e}")
        return {"ok": False, "error": "Error consultando la base de datos."}, 500
    if not diag:
        return {"ok": False, "error": "No hay diagnósticos guardados para ese "
                "correo."}, 404
    return {"ok": True, "id": diag["id"],
            "created_at": diag["created_at"].isoformat(),
            "total": diag["total"], "payload": diag["payload"]}


@app.route('/privacidad-datos')
def privacidad_datos():
    return redirect(url_for('terminos') + '#privacidad', code=301)


@app.route('/reclassify')
def reclassify():
    from db import get_connection, update_reclassification
    from utils import verify_reclassification

    nid = request.args.get('nid', '').strip()
    cat = request.args.get('cat', '').strip()
    sub = request.args.get('sub', '').strip()
    sig = request.args.get('sig', '').strip()

    if not nid or not cat or not sig:
        return render_template('reclassify.html', success=False, reason='invalid'), 400

    if not verify_reclassification(nid, cat, sub, sig):
        return render_template('reclassify.html', success=False, reason='invalid'), 403

    try:
        conn = get_connection()
        update_reclassification(conn, nid, cat, sub or None)
        conn.close()
    except Exception as e:
        app.logger.error(f"Reclassify DB error nid={nid}: {e}")
        return render_template('reclassify.html', success=False, reason='error'), 500

    return render_template('reclassify.html', success=True, category=cat, subcategory=sub or None)


if __name__ == '__main__':
    app.run(debug=True)
