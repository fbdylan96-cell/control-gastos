import logging
import os

from dotenv import find_dotenv, load_dotenv
from flask import Flask, render_template, request

load_dotenv(find_dotenv())

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


@app.route('/privacidad-datos')
def privacidad_datos():
    return render_template('privacidad-datos.html')


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
