import logging
import os
import sys

from dotenv import find_dotenv, load_dotenv
from flask import (Flask, redirect, render_template, request,
                   send_from_directory, url_for)

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

app.register_blueprint(empresa_bp, url_prefix='/empresa')
app.register_blueprint(admin_bp, url_prefix='/administracion')
app.register_blueprint(persona_bp, url_prefix='/persona')
app.register_blueprint(whatsapp_webhook_bp, url_prefix='/whatsapp')

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
_DIAG_RATE = {}
_DIAG_RATE_MAX = 5
_DIAG_RATE_WINDOW = 3600  # segundos


def _diag_rate_ok(ip):
    import time
    now = time.time()
    hits = [ts for ts in _DIAG_RATE.get(ip, []) if now - ts < _DIAG_RATE_WINDOW]
    if len(hits) >= _DIAG_RATE_MAX:
        _DIAG_RATE[ip] = hits
        return False
    hits.append(now)
    _DIAG_RATE[ip] = hits
    return True


@app.route('/diagnostico/enviar', methods=['POST'])
def diagnostico_enviar():
    from diagnostico_report import (aplicar_tipo_cambio, sanitize_payload,
                                    send_report, usa_usd)

    # Detrás de nginx la IP real viene en X-Forwarded-For
    ip = (request.headers.get('X-Forwarded-For', request.remote_addr or '?')
          .split(',')[0].strip())
    if not _diag_rate_ok(ip):
        return {"ok": False, "error": "Demasiados envíos. Intente de nuevo en "
                "una hora."}, 429

    data = request.get_json(silent=True)
    payload, error = sanitize_payload(data)
    if error:
        return {"ok": False, "error": error}, 400

    # Montos en dólares: se convierten a colones con el tipo de cambio más
    # reciente antes de calcular agregados (todo el reporte va en colones).
    if usa_usd(payload):
        tc = _tipo_cambio_usd()
        if not tc["usd_crc"]:
            return {"ok": False, "error": "No se pudo obtener el tipo de cambio "
                    "para convertir los montos en dólares. Intente más tarde."}, 503
        aplicar_tipo_cambio(payload, tc["usd_crc"], tc["fecha"])

    try:
        send_report(payload)
    except Exception as e:
        app.logger.error(f"Diagnóstico: fallo enviando reporte a "
                         f"{payload['correo']}: {e}")
        return {"ok": False, "error": "No se pudo enviar el reporte. Intente de "
                "nuevo más tarde."}, 500

    app.logger.info(f"Diagnóstico enviado a {payload['correo']} (cc asesor)")
    return {"ok": True}


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
