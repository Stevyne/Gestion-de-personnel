from flask import Flask, render_template, request, redirect, url_for, flash, session, make_response, jsonify, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
import os
import secrets
import logging
from datetime import date, datetime, timedelta
from functools import wraps
import io
from urllib.parse import urlencode

from flask_mail import Mail, Message
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

from services.email_outbox import ajouter_email, traiter_outbox
from services.roles import GLOBAL_DATA_ROLES, ROLE_LABELS
from services.phase1_schema import appliquer_contraintes_phase1
from blueprints.absence_justifications import (
    ABSENCE_ACCEPTEE, ABSENCE_STATUT_LABELS, creer_blueprint_justifications,
)
from blueprints.messagerie import creer_blueprint_messagerie
from blueprints.documents import creer_blueprint_documents
from blueprints.departements import creer_blueprint_departements
from blueprints.presences import creer_blueprint_presences
from blueprints.utilisateurs import creer_blueprint_utilisateurs
from blueprints.auth import creer_blueprint_auth
from blueprints.parc import (
    MAINTENANCE_OUVERTS,  # réexport de compatibilité pour les tests/plug-ins  # noqa: F401
    MAINTENANCE_VALIDATION_JOURS,
    creer_blueprint_parc,
)

# ==================== LOGGING ====================
logging.basicConfig(
    level=getattr(logging, os.environ.get('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger('gestion_personnel')


# ==================== EXPORTS (PDF + EXCEL) ====================
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
import openpyxl
from openpyxl.styles import Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

from dotenv import load_dotenv
load_dotenv()   # charge .env dans os.environ

app = Flask(__name__)

# === CONFIGURATION SÉCURITÉ ===
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    if os.environ.get('FLASK_ENV') == 'production':
        raise RuntimeError(
            "SECRET_KEY doit être défini dans l'environnement en production. "
            "Générez-en une avec: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    # Fallback uniquement pour le développement local
    SECRET_KEY = 'dev-only-insecure-key-do-not-use-in-production'
    logger.warning("SECRET_KEY absente de l'environnement, utilisation d'une clé de dev non sécurisée.")

app.secret_key = SECRET_KEY
app.config['SECRET_KEY'] = app.secret_key
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() == 'true'
app.config['PERMANENT_SESSION_LIFETIME'] = int(os.environ.get('PERMANENT_SESSION_LIFETIME', 3600))

# Limiter les uploads
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    if os.environ.get('FLASK_ENV') == 'production':
        raise RuntimeError(
            "DATABASE_URL doit être défini dans l'environnement en production. "
            "Exemple: postgresql://utilisateur:motdepasse@hote:5432/gestion_personnel"
        )
    # Fallback de développement local — aucun mot de passe sensible codé en dur
    DATABASE_URL = 'postgresql://postgres:postgres@localhost:5432/gestion_personnel'
    logger.warning("DATABASE_URL absente de l'environnement, utilisation du fallback de dev local (postgres/postgres).")

# ==================== UPLOADS (Documents) ====================
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'png', 'jpg', 'jpeg', 'txt'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Les photos de profil vivent dans un sous-dossier séparé des documents RH :
# elles sont servies publiquement par <img>, alors que les documents sont
# protégés par une route de téléchargement contrôlée.
AVATAR_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'avatars')
AVATAR_EXTENSIONS = {'png', 'jpg', 'jpeg'}
# 8 Mo à l'envoi : une photo prise au smartphone dépasse presque toujours 2 Mo.
# Elle est ensuite redimensionnée côté serveur, donc le fichier stocké reste petit.
AVATAR_MAX_BYTES = 8 * 1024 * 1024   # 8 Mo
AVATAR_MAX_SIDE = 512                # côté maximal de l'image stockée (px)
os.makedirs(AVATAR_FOLDER, exist_ok=True)

try:
    from PIL import Image, ImageOps
    PIL_DISPONIBLE = True
except ImportError:      # l'envoi reste possible, sans redimensionnement
    PIL_DISPONIBLE = False
    logger.warning("Pillow indisponible : les photos de profil ne seront pas redimensionnées.")

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Correspondance magic-bytes -> extensions cohérentes autorisées
_MAGIC_EXT = {
    b'%PDF': {'pdf'},
    b'\x89PNG\r\n\x1a\n': {'png'},
    b'\xff\xd8\xff': {'jpg', 'jpeg'},
    b'PK\x03\x04': {'docx', 'xlsx'},                 # Office Open XML (ZIP)
    b'PK\x05\x06': {'docx', 'xlsx'},                 # ZIP vide
    b'PK\x07\x08': {'docx', 'xlsx'},
    b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1': {'doc', 'xls'},  # OLE2 (anciens .doc/.xls)
}

def detect_file_type(fichier):
    """Détecte le vrai type du fichier via ses magic-bytes et renvoie
    l'extension (sans '.') cohérente avec le contenu parmi les types autorisés,
    ou None si le contenu ne correspond à rien de sûr."""
    stream = fichier.stream
    head = stream.read(8)
    stream.seek(0)
    candidates = None
    if head.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        candidates = {'doc', 'xls'}
    else:
        for magic, exts in _MAGIC_EXT.items():
            if head.startswith(magic):
                candidates = exts
                break
    if candidates is None:
        # Pas de magic connue : on accepte uniquement du texte UTF-8/ASCII
        try:
            chunk = stream.read(1024)
            stream.seek(0)
            chunk.decode('utf-8')
            candidates = {'txt'}
        except (UnicodeDecodeError, Exception):
            stream.seek(0)
            return None
    declared = fichier.filename.rsplit('.', 1)[-1].lower() if '.' in fichier.filename else ''
    return declared if declared in candidates else None


app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'gestion.personnel@entreprise.fr')
app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL', 'admin@entreprise.fr')
# Aucun SMTP n'est contacté sans activation explicite. En développement et
# pendant les tests, les événements restent couverts par les notifications
# internes, sans erreur ni tentative réseau.
app.config['EMAIL_ENABLED'] = os.environ.get('EMAIL_ENABLED', 'false').lower() == 'true'
app.config['EMAIL_BATCH_SIZE'] = int(os.environ.get('EMAIL_BATCH_SIZE', 20))
app.config['EMAIL_MAX_ATTEMPTS'] = int(os.environ.get('EMAIL_MAX_ATTEMPTS', 5))
app.config['EMAIL_POLL_SECONDS'] = int(os.environ.get('EMAIL_POLL_SECONDS', 60))

def get_admin_email():
    try:
        conn = get_db()
        cur = get_cursor(conn)
        cur.execute("SELECT email FROM employes WHERE LOWER(poste) LIKE '%admin%' OR LOWER(nom) LIKE '%admin%' OR email ILIKE '%admin%' LIMIT 1")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row.get('email'): return row['email']
    except: pass
    return app.config.get('ADMIN_EMAIL') or 'admin@entreprise.fr'

mail = Mail(app)


def queue_email(destinataire, sujet, corps_texte, corps_html=None,
                cur=None, event_key=None):
    """Place un e-mail dans l'outbox persistante.

    L'appel est un no-op assumé tant que ``EMAIL_ENABLED`` n'est pas activé.
    Si un curseur est fourni, le message appartient à la même transaction que
    l'événement métier ; sinon une courte transaction dédiée est ouverte.
    """
    if not app.config.get('EMAIL_ENABLED'):
        logger.info("[EMAIL DÉSACTIVÉ] → %s | %s", destinataire, sujet)
        return None
    if not destinataire:
        return None
    try:
        if cur is not None:
            # Une panne de l'outbox ne doit jamais annuler l'action métier.
            # Le SAVEPOINT restaure la transaction si l'INSERT e-mail échoue.
            cur.execute("SAVEPOINT ajout_email_outbox")
            try:
                resultat = ajouter_email(cur, destinataire, sujet, corps_texte,
                                          corps_html, event_key)
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT ajout_email_outbox")
                cur.execute("RELEASE SAVEPOINT ajout_email_outbox")
                raise
            cur.execute("RELEASE SAVEPOINT ajout_email_outbox")
            return resultat
        with db_cursor(commit=True) as (conn, outbox_cur):
            return ajouter_email(outbox_cur, destinataire, sujet, corps_texte,
                                 corps_html, event_key)
    except Exception as exc:
        logger.error("Impossible d'ajouter l'e-mail à l'outbox: %s", exc,
                     exc_info=True)
        return None


def _send_outbox_message(message):
    """Adaptateur Flask-Mail appelé uniquement par le worker d'outbox."""
    msg = Message(
        subject=message['sujet'],
        recipients=[message['destinataire']],
        sender=app.config.get('MAIL_DEFAULT_SENDER'),
    )
    msg.body = message.get('corps_texte') or ''
    if message.get('corps_html'):
        msg.html = message['corps_html']
    mail.send(msg)


# === INITIALISATION SÉCURITÉ ===
csrf = CSRFProtect(app)

# Rate limiter — désactivable avant l'import (tests/CI). La configuration
# modifiée après l'initialisation de l'extension arrivait trop tard.
app.config['RATELIMIT_ENABLED'] = os.environ.get(
    'RATELIMIT_ENABLED', 'true').lower() == 'true'
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# Headers de sécurité (Talisman)
csp = {
    'default-src': "'self'",
    'script-src': ["'self'", "'unsafe-inline'"],  # pour les scripts inline actuels
    'style-src': ["'self'", "'unsafe-inline'"],
    'img-src': ["'self'", "data:"],
}
talisman = Talisman(
    app,
    force_https=False,                    # passez à True en production HTTPS
    frame_options='DENY',
    content_security_policy=csp,
    referrer_policy='strict-origin-when-cross-origin',
    session_cookie_secure=app.config['SESSION_COOKIE_SECURE']
)
logger.info("Sécurité activée : CSRF + RateLimit + Talisman")



# ==================== HTML EMAIL ====================
def send_html_email(recipients, subject, html_template, event_key=None, **context):
    """Rend un template puis met l'e-mail en file, sans SMTP dans la requête."""
    try:
        html_body = render_template(html_template, **context)
        liste = [recipients] if isinstance(recipients, str) else list(recipients or [])
        admin = get_admin_email()
        if admin and admin not in liste:
            liste.append(admin)  # équivalent de l'ancien CC administrateur
        for index, destinataire in enumerate(liste):
            queue_email(
                destinataire, subject,
                "Une notification de Gestion du Personnel vous attend. "
                "Consultez l'application pour les détails.",
                corps_html=html_body,
                event_key=f"{event_key}:{index}" if event_key else None,
            )
        return True
    except Exception as e:
        logger.error("Erreur préparation e-mail HTML: %s", e, exc_info=True)
        return False

HEURE_ARRIVEE_ATTENDUE = "09:00"

def get_role_label(role):
    return ROLE_LABELS.get(role, role or 'Employé')

# ==================== SESSIONS ACTIVES (présence + déconnexion à distance) ====================
# Durée d'inactivité au-delà de laquelle une session n'est plus considérée
# comme « en ligne » dans l'interface (elle reste valide tant qu'elle n'est
# ni expirée ni révoquée).
SESSION_ONLINE_WINDOW_MIN = int(os.environ.get('SESSION_ONLINE_WINDOW_MIN', 5))

def _client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()[:45]
    return (request.remote_addr or '')[:45]


def enregistrer_session(user_id, username):
    """Inscrit la session courante au registre et renvoie son identifiant."""
    sid = secrets.token_urlsafe(32)
    try:
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("""
                INSERT INTO sessions_actives (sid, user_id, username, ip_address, user_agent)
                VALUES (%s, %s, %s, %s, %s)
            """, (sid, user_id, username, _client_ip(),
                  (request.headers.get('User-Agent') or '')[:300]))
    except Exception as e:
        logger.error("Erreur enregistrer_session: %s", e, exc_info=True)
        return None
    return sid


def cloturer_session(sid, par=None):
    """Marque une session comme terminée (déconnexion volontaire ou révocation)."""
    if not sid:
        return
    try:
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("""
                UPDATE sessions_actives
                   SET revoked_at = CURRENT_TIMESTAMP, revoked_by = %s
                 WHERE sid = %s AND revoked_at IS NULL
            """, (par, sid))
    except Exception as e:
        logger.error("Erreur cloturer_session: %s", e, exc_info=True)


def session_active():
    """Vrai si la session présentée par le navigateur est toujours valide.

    Rafraîchit `last_seen` au passage, ce qui alimente l'indicateur de présence.
    En cas d'incident base, on renvoie True : mieux vaut laisser passer que
    déconnecter tout le monde parce que la base est momentanément indisponible.
    """
    sid = session.get('sid')
    if not sid:
        # Session ouverte avant la mise en place du registre : on la tolère.
        return True
    try:
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT revoked_at FROM sessions_actives WHERE sid = %s", (sid,))
            row = cur.fetchone()
            if row is None:
                return True          # inconnue (base réinitialisée) : on tolère
            if row['revoked_at'] is not None:
                return False         # révoquée par un administrateur
            cur.execute("UPDATE sessions_actives SET last_seen = CURRENT_TIMESTAMP WHERE sid = %s", (sid,))
    except Exception as e:
        logger.error("Erreur session_active: %s", e, exc_info=True)
    return True


def _refuser_session_revoquee():
    """Termine proprement une session dont l'accès vient d'être retiré."""
    session.clear()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        resp = make_response('', 204)
        resp.headers['X-Redirect-To'] = url_for('auth.login')
        return resp
    flash("Votre session a été fermée par un administrateur.", "warning")
    return redirect(url_for('auth.login'))


def role_required(*allowed_roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                flash('Veuillez vous connecter.', 'warning')
                return redirect(url_for('auth.login'))
            if not session_active():
                return _refuser_session_revoquee()
            role = session.get('role', 'employe')
            if role == 'admin' or role in allowed_roles:
                return f(*args, **kwargs)
            flash('Accès refusé.', 'danger')
            return redirect(url_for('dashboard'))
        return decorated
    return decorator

# ==================== CLOISONNEMENT DES DONNÉES PAR DÉPARTEMENT =============
# Admin et RH ont une portée globale. Tous les autres rôles sont limités au
# département de leur fiche employé. Une absence de rattachement produit une
# portée vide — jamais un accès global implicite.


def get_department_scope(cur=None):
    """Portée courante, calculée depuis la base et mise en cache par requête.

    Le rôle n'est pas seulement lu dans le cookie de session : une promotion ou
    rétrogradation administrative prend ainsi effet dès la requête suivante.
    """
    if hasattr(g, 'department_scope'):
        return g.department_scope

    def charger(cursor):
        cursor.execute("""
            SELECT u.role, e.id AS employe_id, e.departement
              FROM users u LEFT JOIN employes e ON e.id = u.employe_id
             WHERE u.id = %s
        """, (session.get('user_id'),))
        return cursor.fetchone() or {}

    if cur is not None:
        row = charger(cur)
    else:
        with db_cursor() as (conn, cursor):
            row = charger(cursor)
    role = row.get('role') or 'employe'
    session['role'] = role
    session['role_label'] = get_role_label(role)
    if role in GLOBAL_DATA_ROLES:
        scope = {'is_global': True, 'department': None,
                 'employee_id': row.get('employe_id'), 'is_empty': False}
    else:
        departement = (row.get('departement') or '').strip() or None
        scope = {
            'is_global': False,
            'department': departement,
            'employee_id': row.get('employe_id'),
            'is_empty': not bool(departement),
        }
    g.department_scope = scope
    return scope


def department_scope_sql(alias='e', field='departement', cur=None):
    """Renvoie une condition SQL sûre et ses paramètres pour la portée courante."""
    scope = get_department_scope(cur)
    if scope['is_global']:
        return 'TRUE', []
    if scope['is_empty']:
        return 'FALSE', []
    return f"{alias}.{field} = %s", [scope['department']]


def employee_in_scope(cur, employe_id):
    """Vrai si la fiche employé appartient à la portée autorisée."""
    scope = get_department_scope(cur)
    if scope['is_global']:
        return True
    if not employe_id or scope['is_empty']:
        return False
    try:
        employe_id = int(employe_id)
    except (TypeError, ValueError):
        return False
    cur.execute("SELECT departement FROM employes WHERE id = %s", (employe_id,))
    row = cur.fetchone()
    return bool(row and row.get('departement') == scope['department'])


def department_id_in_scope(cur, departement_id):
    scope = get_department_scope(cur)
    if scope['is_global']:
        return True
    if not departement_id or scope['is_empty']:
        return False
    try:
        departement_id = int(departement_id)
    except (TypeError, ValueError):
        return False
    cur.execute("SELECT nom FROM departements WHERE id = %s", (departement_id,))
    row = cur.fetchone()
    return bool(row and row.get('nom') == scope['department'])


def _department_access_denied():
    logger.warning("Accès inter-département refusé: user=%s endpoint=%s",
                   session.get('username'), request.endpoint)
    if request.endpoint == 'api_recherche':
        return jsonify({'erreur': True, 'message': 'Accès refusé'}), 403
    flash("Accès refusé : cette donnée n'appartient pas à votre département.", "danger")
    return redirect(url_for('dashboard'))


# Ressources identifiées par l'URL. Le SELECT renvoie toujours une colonne
# `departement`; le garde s'applique aux GET comme aux écritures.
DEPARTMENT_RESOURCE_GUARDS = {
    'view_employee': ("SELECT departement FROM employes WHERE id = %s", 'id'),
    'edit_employee': ("SELECT departement FROM employes WHERE id = %s", 'id'),
    'delete_employee': ("SELECT departement FROM employes WHERE id = %s", 'id'),
    'documents.add_employee_document': ("SELECT departement FROM employes WHERE id = %s", 'id'),
    'presences.clock_in': ("SELECT departement FROM employes WHERE id = %s", 'employe_id'),
    'presences.clock_out': ("SELECT departement FROM employes WHERE id = %s", 'employe_id'),
    'update_solde_conges': ("SELECT departement FROM employes WHERE id = %s", 'employe_id'),
    'presences.delete_presence': ("SELECT e.departement FROM presences p JOIN employes e ON e.id=p.employe_id WHERE p.id=%s", 'id'),
    'avis_conge': ("SELECT e.departement FROM conges x JOIN employes e ON e.id=x.employe_id WHERE x.id=%s", 'id'),
    'update_conge': ("SELECT e.departement FROM conges x JOIN employes e ON e.id=x.employe_id WHERE x.id=%s", 'id'),
    'annuler_conge': ("SELECT e.departement FROM conges x JOIN employes e ON e.id=x.employe_id WHERE x.id=%s", 'id'),
    'delete_conge': ("SELECT e.departement FROM conges x JOIN employes e ON e.id=x.employe_id WHERE x.id=%s", 'id'),
    'avis_permission': ("SELECT e.departement FROM permissions x JOIN employes e ON e.id=x.employe_id WHERE x.id=%s", 'id'),
    'update_permission': ("SELECT e.departement FROM permissions x JOIN employes e ON e.id=x.employe_id WHERE x.id=%s", 'id'),
    'annuler_permission': ("SELECT e.departement FROM permissions x JOIN employes e ON e.id=x.employe_id WHERE x.id=%s", 'id'),
    'delete_permission': ("SELECT e.departement FROM permissions x JOIN employes e ON e.id=x.employe_id WHERE x.id=%s", 'id'),
    'delete_absence': ("SELECT e.departement FROM absences x JOIN employes e ON e.id=x.employe_id WHERE x.id=%s", 'id'),
    'documents.download_document': ("SELECT e.departement FROM documents x LEFT JOIN employes e ON e.id=x.employe_id WHERE x.id=%s", 'doc_id'),
    'documents.delete_document': ("SELECT e.departement FROM documents x LEFT JOIN employes e ON e.id=x.employe_id WHERE x.id=%s", 'doc_id'),
    'parc.edit_materiel': ("SELECT d.nom AS departement FROM materiels x LEFT JOIN departements d ON d.id=x.departement_id WHERE x.id=%s", 'id'),
    'parc.view_materiel': ("SELECT d.nom AS departement FROM materiels x LEFT JOIN departements d ON d.id=x.departement_id WHERE x.id=%s", 'id'),
    'parc.add_mouvement_materiel': ("SELECT d.nom AS departement FROM materiels x LEFT JOIN departements d ON d.id=x.departement_id WHERE x.id=%s", 'id'),
    'parc.attribuer_materiel': ("SELECT d.nom AS departement FROM materiels x LEFT JOIN departements d ON d.id=x.departement_id WHERE x.id=%s", 'id'),
    'parc.delete_materiel': ("SELECT d.nom AS departement FROM materiels x LEFT JOIN departements d ON d.id=x.departement_id WHERE x.id=%s", 'id'),
    'parc.add_exemplaire': ("SELECT d.nom AS departement FROM materiels x LEFT JOIN departements d ON d.id=x.departement_id WHERE x.id=%s", 'id'),
    'parc.etiquettes_materiel': ("SELECT d.nom AS departement FROM materiels x LEFT JOIN departements d ON d.id=x.departement_id WHERE x.id=%s", 'id'),
    'parc.retour_materiel': ("SELECT d.nom AS departement FROM materiels_attributions a JOIN materiels m ON m.id=a.materiel_id LEFT JOIN departements d ON d.id=m.departement_id WHERE a.id=%s", 'attribution_id'),
    'parc.accuser_attribution': ("SELECT d.nom AS departement FROM materiels_attributions a JOIN materiels m ON m.id=a.materiel_id LEFT JOIN departements d ON d.id=m.departement_id WHERE a.id=%s", 'attribution_id'),
    'parc.relancer_accuse': ("SELECT d.nom AS departement FROM materiels_attributions a JOIN materiels m ON m.id=a.materiel_id LEFT JOIN departements d ON d.id=m.departement_id WHERE a.id=%s", 'attribution_id'),
    'parc.view_inventaire': ("SELECT d.nom AS departement FROM inventaires x LEFT JOIN departements d ON d.id=x.departement_id WHERE x.id=%s", 'id'),
    'parc.compter_inventaire': ("SELECT d.nom AS departement FROM inventaires x LEFT JOIN departements d ON d.id=x.departement_id WHERE x.id=%s", 'id'),
    'parc.cloturer_inventaire': ("SELECT d.nom AS departement FROM inventaires x LEFT JOIN departements d ON d.id=x.departement_id WHERE x.id=%s", 'id'),
    'parc.annuler_inventaire': ("SELECT d.nom AS departement FROM inventaires x LEFT JOIN departements d ON d.id=x.departement_id WHERE x.id=%s", 'id'),
    'parc.view_exemplaire': ("SELECT d.nom AS departement FROM materiel_exemplaires ex JOIN materiels m ON m.id=ex.materiel_id LEFT JOIN departements d ON d.id=m.departement_id WHERE ex.id=%s", 'id'),
    'parc.edit_exemplaire': ("SELECT d.nom AS departement FROM materiel_exemplaires ex JOIN materiels m ON m.id=ex.materiel_id LEFT JOIN departements d ON d.id=m.departement_id WHERE ex.id=%s", 'id'),
    'parc.delete_exemplaire': ("SELECT d.nom AS departement FROM materiel_exemplaires ex JOIN materiels m ON m.id=ex.materiel_id LEFT JOIN departements d ON d.id=m.departement_id WHERE ex.id=%s", 'id'),
    'parc.signaler_panne': ("SELECT d.nom AS departement FROM materiel_exemplaires ex JOIN materiels m ON m.id=ex.materiel_id LEFT JOIN departements d ON d.id=m.departement_id WHERE ex.id=%s", 'id'),
    'parc.assigner_maintenance': ("SELECT d.nom AS departement FROM materiel_maintenances mt JOIN materiel_exemplaires ex ON ex.id=mt.exemplaire_id JOIN materiels m ON m.id=ex.materiel_id LEFT JOIN departements d ON d.id=m.departement_id WHERE mt.id=%s", 'id'),
    'parc.envoyer_maintenance': ("SELECT d.nom AS departement FROM materiel_maintenances mt JOIN materiel_exemplaires ex ON ex.id=mt.exemplaire_id JOIN materiels m ON m.id=ex.materiel_id LEFT JOIN departements d ON d.id=m.departement_id WHERE mt.id=%s", 'id'),
    'parc.cloturer_maintenance': ("SELECT d.nom AS departement FROM materiel_maintenances mt JOIN materiel_exemplaires ex ON ex.id=mt.exemplaire_id JOIN materiels m ON m.id=ex.materiel_id LEFT JOIN departements d ON d.id=m.departement_id WHERE mt.id=%s", 'id'),
    'parc.valider_maintenance': ("SELECT d.nom AS departement FROM materiel_maintenances mt JOIN materiel_exemplaires ex ON ex.id=mt.exemplaire_id JOIN materiels m ON m.id=ex.materiel_id LEFT JOIN departements d ON d.id=m.departement_id WHERE mt.id=%s", 'id'),
    'parc.annuler_maintenance': ("SELECT d.nom AS departement FROM materiel_maintenances mt JOIN materiel_exemplaires ex ON ex.id=mt.exemplaire_id JOIN materiels m ON m.id=ex.materiel_id LEFT JOIN departements d ON d.id=m.departement_id WHERE mt.id=%s", 'id'),
    'parc.etiquettes_departement': ("SELECT nom AS departement FROM departements WHERE id=%s", 'id'),
    'parc.materiels_departement': ("SELECT nom AS departement FROM departements WHERE id=%s", 'id'),
    'auth.avatar_image': ("SELECT e.departement FROM users u LEFT JOIN employes e ON e.id=u.employe_id WHERE u.photo=%s", 'filename'),
    # Le Blueprint des justificatifs applique une règle plus stricte que le
    # département (propriétaire ou RH) et conserve ses réponses 404/403.
}

FORM_EMPLOYEE_SCOPE_FIELDS = {
    'presences.presences': 'quick_employe_id', 'presences.add_presence': 'employe_id',
    'add_conge': 'employe_id', 'add_permission': 'employe_id',
    'add_absence': 'employe_id', 'documents.documents': 'employe_id',
    'parc.attribuer_materiel': 'employe_id', 'parc.add_mouvement_materiel': 'employe_id',
    'parc.edit_exemplaire': 'employe_id',
}
FORM_DEPARTMENT_SCOPE_FIELDS = {
    'parc.add_materiel': 'departement_id', 'parc.edit_materiel': 'departement_id',
    'parc.add_inventaire': 'departement_id',
}
FORM_USER_SCOPE_FIELDS = {'parc.assigner_maintenance': 'assigne_user_id'}


@app.before_request
def enforce_department_resource_scope():
    """Garde central anti-contournement pour URL et formulaires forgés."""
    if not session.get('user_id'):
        return None
    scope = get_department_scope()
    if scope['is_global']:
        return None

    # Les prestataires sont un référentiel global sans département : seuls
    # admin/RH peuvent le consulter ou le modifier.
    if request.endpoint in ('parc.prestataires_page', 'parc.basculer_prestataire'):
        return _department_access_denied()

    guard = DEPARTMENT_RESOURCE_GUARDS.get(request.endpoint)
    if guard and request.view_args:
        query, key = guard
        value = request.view_args.get(key)
        if value is not None:
            with db_cursor() as (conn, cur):
                cur.execute(query, (value,))
                row = cur.fetchone()
            if row and (scope['is_empty'] or row.get('departement') != scope['department']):
                return _department_access_denied()

    if request.method == 'POST':
        employee_field = FORM_EMPLOYEE_SCOPE_FIELDS.get(request.endpoint)
        raw_employee = request.form.get(employee_field) if employee_field else None
        if raw_employee:
            with db_cursor() as (conn, cur):
                if not employee_in_scope(cur, raw_employee):
                    return _department_access_denied()
        department_field = FORM_DEPARTMENT_SCOPE_FIELDS.get(request.endpoint)
        raw_department = request.form.get(department_field) if department_field else None
        if raw_department:
            with db_cursor() as (conn, cur):
                if not department_id_in_scope(cur, raw_department):
                    return _department_access_denied()
        user_field = FORM_USER_SCOPE_FIELDS.get(request.endpoint)
        raw_user = request.form.get(user_field) if user_field else None
        if raw_user:
            try:
                raw_user = int(raw_user)
            except (TypeError, ValueError):
                return _department_access_denied()
            with db_cursor() as (conn, cur):
                user_scope, user_params = department_scope_sql('e', 'departement', cur)
                cur.execute(f"""SELECT 1 FROM users u JOIN employes e ON e.id=u.employe_id
                                WHERE u.id=%s AND {user_scope}""",
                            [raw_user] + user_params)
                if not cur.fetchone():
                    return _department_access_denied()
    return None


# ==================== NOTIFICATIONS (Base de données - support multi-utilisateur réel) ====================
def create_notification(user_id, title, message, type_="info", cur=None):
    """Crée une notification persistante.

    ``cur`` permet de rattacher la notification à la transaction métier : pas
    de notification fantôme si l'enregistrement principal est annulé.
    """
    try:
        if cur is not None:
            # Même garantie que pour l'outbox : une notification invalide ou
            # une indisponibilité ponctuelle ne fait pas échouer le workflow.
            cur.execute("SAVEPOINT ajout_notification")
            try:
                cur.execute("""
                    INSERT INTO notifications (user_id, title, message, type, is_read)
                    VALUES (%s, %s, %s, %s, FALSE)
                """, (user_id, title, message, type_))
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT ajout_notification")
                cur.execute("RELEASE SAVEPOINT ajout_notification")
                raise
            cur.execute("RELEASE SAVEPOINT ajout_notification")
            return True
        with db_cursor(commit=True) as (conn, notification_cur):
            notification_cur.execute("""
                INSERT INTO notifications (user_id, title, message, type, is_read)
                VALUES (%s, %s, %s, %s, FALSE)
            """, (user_id, title, message, type_))
        return True
    except Exception as e:
        logger.error("Erreur create_notification DB: %s", e, exc_info=True)
        return False

def get_unread_notifications(user_id=None):
    """Retourne les notifications non lues depuis PostgreSQL"""
    try:
        conn = get_db()
        cur = get_cursor(conn)
        if user_id is not None:
            cur.execute("""
                SELECT * FROM notifications 
                WHERE user_id = %s AND is_read = FALSE 
                ORDER BY timestamp DESC LIMIT 50
            """, (user_id,))
        else:
            cur.execute("""
                SELECT * FROM notifications 
                WHERE is_read = FALSE 
                ORDER BY timestamp DESC LIMIT 50
            """)
        notifs = cur.fetchall()
        cur.close()
        conn.close()
        return notifs
    except Exception as e:
        logger.error("Erreur get_unread_notifications: %s", e, exc_info=True)
        return []

def mark_all_read(user_id=None):
    """Marque les notifications comme lues"""
    try:
        with db_cursor(commit=True) as (conn, cur):
            if user_id is not None:
                cur.execute("UPDATE notifications SET is_read = TRUE WHERE user_id = %s", (user_id,))
            else:
                cur.execute("UPDATE notifications SET is_read = TRUE")
        return True
    except Exception as e:
        logger.error("Erreur mark_all_read: %s", e, exc_info=True)
        return False

def get_all_notifications(user_id=None, limit=30):
    try:
        conn = get_db()
        cur = get_cursor(conn)
        if user_id is not None:
            cur.execute("""
                SELECT * FROM notifications 
                WHERE user_id = %s 
                ORDER BY timestamp DESC LIMIT %s
            """, (user_id, limit))
        else:
            cur.execute("""
                SELECT * FROM notifications 
                ORDER BY timestamp DESC LIMIT %s
            """, (limit,))
        notifs = cur.fetchall()
        cur.close()
        conn.close()
        return notifs
    except Exception as e:
        logger.error("Erreur get_all_notifications: %s", e, exc_info=True)
        return []

@app.after_request
def modal_redirect_passthrough(response):
    """Ne pas suivre les redirections pour les soumissions de la popup.

    Le JS de la fenêtre modale poste en AJAX (`fetch`) avec l'en-tête
    X-Requested-With. Si le navigateur suit lui-même la redirection 302,
    la page de destination est rendue dans le fetch : les messages flash
    sont alors consommés et « perdus » avant que le navigateur ne recharge
    réellement la liste.

    On transforme donc la redirection en réponse 204 portant l'URL cible
    dans l'en-tête X-Redirect-To ; le JS lit cet en-tête et navigue
    lui-même, ce qui laisse le flash intact pour le vrai chargement.
    """
    try:
        if (response.status_code in (301, 302, 303, 307, 308)
                and request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                and request.method == 'POST'):
            target = response.headers.get('Location')
            if target:
                resp = make_response('', 204)
                resp.headers['X-Redirect-To'] = target
                return resp
    except Exception:
        pass
    return response


@app.context_processor
def inject_context():
    try:
        user_id = session.get('user_id')
        unread_count = len(get_unread_notifications(user_id)) if user_id else 0
    except:
        unread_count = 0
    # Mode « popup » : quand une page de formulaire est demandée avec ?modal=1
    # (par le JS d'ouverture de la fenêtre modale), on l'affiche via un layout
    # réduit au seul contenu, sans <html>/navigation/pied de page.
    # Les templates de formulaire font `{% extends layout %}`, ce qui évite de
    # dupliquer les formulaires entre la page classique et la popup.
    is_modal = request.args.get('modal') == '1'
    # Photo de profil du compte connecté, pour l'avatar de la barre de navigation.
    photo_profil = None
    try:
        if session.get('user_id'):
            with db_cursor() as (conn, cur):
                cur.execute("SELECT photo FROM users WHERE id = %s", (session['user_id'],))
                row = cur.fetchone()
                photo_profil = row['photo'] if row else None
    except Exception:
        photo_profil = None
    return {
        'unread_notifications': unread_count,
        'current_role': session.get('role', 'employe'),
        'role_label': session.get('role_label') or get_role_label(session.get('role', 'employe')),
        'is_modal': is_modal,
        'layout': '_modal_layout.html' if is_modal else 'base.html',
        'session_online_window': SESSION_ONLINE_WINDOW_MIN,
        'photo_profil': photo_profil,
    }

# ==================== RETARD EMAIL (HTML) ====================
def send_retard_email(employee_name, employee_email, retard_minutes, date_str, heure_arrivee):
    admin_email = get_admin_email()
    if not app.config.get('EMAIL_ENABLED'):
        logger.info(f"[EMAIL DÉSACTIVÉ] → {employee_name} +{retard_minutes} min")
        return True
    try:
        subject = f"⚠️ Retard détecté - {employee_name}"
        return send_html_email(
            recipients=[employee_email] if employee_email else [admin_email],
            subject=subject,
            html_template="emails/retard.html",
            event_key=f"retard:{employee_email or employee_name}:{date_str}:{heure_arrivee}",
            prenom=employee_name.split()[0] if employee_name else "Employé",
            nom_complet=employee_name,
            date_str=date_str,
            heure_arrivee=heure_arrivee,
            retard_minutes=retard_minutes,
            heure_attendue=HEURE_ARRIVEE_ATTENDUE,
            admin_name="Administrateur Système"
        )
    except Exception as e:
        logger.error("Erreur mise en file e-mail retard: %s", e, exc_info=True)
        return False

# ==================== DB ====================
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SET timezone TO 'Indian/Antananarivo'")
    cur.close()
    return conn

from contextlib import contextmanager

@contextmanager
def db_cursor(commit=False):
    """
    Fournit (conn, cur) et garantit leur fermeture, même en cas d'exception
    ou de `return` anticipé dans le bloc `with`. Utiliser commit=True pour
    les opérations d'écriture (INSERT/UPDATE/DELETE).
    """
    conn = get_db()
    cur = get_cursor(conn)
    try:
        yield conn, cur
        if commit:
            conn.commit()
    finally:
        cur.close()
        conn.close()

def get_cursor(conn):
    return conn.cursor(cursor_factory=RealDictCursor)


# ==================== PAGINATION ====================
def pagination_info(total, page, per_page=10):
    """Calcule les infos de pagination (page bornée dans [1, pages])."""
    pages = max(1, (total + per_page - 1) // per_page) if per_page else 1
    page = max(1, min(page, pages))
    return {'page': page, 'per_page': per_page, 'total': total, 'pages': pages,
            'has_prev': page > 1, 'has_next': page < pages}


def page_list(page, pages):
    """Liste de numéros de pages à afficher (avec '...' pour les trous)."""
    if pages <= 9:
        return list(range(1, pages + 1))
    items = [1]
    lo, hi = max(2, page - 1), min(pages - 1, page + 1)
    if lo > 2:
        items.append('...')
    items += list(range(lo, hi + 1))
    if hi < pages - 1:
        items.append('...')
    items.append(pages)
    return items

def log_action(user_id=None, username=None, action="", entity_type=None, entity_id=None, details=None):
    try:
        conn = get_db()
        cur = get_cursor(conn)
        ip = getattr(request, 'remote_addr', None)
        cur.execute('INSERT INTO audit_logs (user_id, username, action, entity_type, entity_id, details, ip_address) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                    (user_id, username, action, entity_type, entity_id, details, ip))
        conn.commit()
        cur.close(); conn.close()
    except: pass

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Veuillez vous connecter.', 'warning')
            return redirect(url_for('auth.login'))
        # Une session révoquée à distance est rejetée dès la requête suivante.
        if not session_active():
            return _refuser_session_revoquee()
        return f(*args, **kwargs)
    return decorated

def calculer_retard(h):
    if not h: return 0
    try:
        if isinstance(h, str):
            hh, mm = map(int, h.split(':')[:2])
        else:
            hh, mm = h.hour, h.minute
        ha, ma = map(int, HEURE_ARRIVEE_ATTENDUE.split(':'))
        return max(0, (hh*60 + mm) - (ha*60 + ma))
    except:
        return 0

def get_current_user_row():
    """Renvoie la ligne `users` du compte connecté (identifiant, rôle, photo…)."""
    if 'user_id' not in session:
        return None
    try:
        with db_cursor() as (conn, cur):
            cur.execute("SELECT id, username, role, employe_id, photo FROM users WHERE id = %s",
                        (session['user_id'],))
            return cur.fetchone()
    except Exception as e:
        logger.error("Erreur get_current_user_row: %s", e, exc_info=True)
        return None


def enregistrer_photo_profil(fichier, user_id):
    """Valide puis enregistre une photo de profil.

    Renvoie (nom_du_fichier, None, contenu_bytes) en cas de succès, sinon
    (None, message, None). La validation porte sur le CONTENU réel
    (magic-bytes) et pas seulement sur l'extension : un script renommé en
    .png est rejeté.

    Le fichier est toujours écrit sur disque (cache pour la durée de vie de
    l'instance) ET ses octets sont renvoyés pour être stockés en base
    (persistant) par l'appelant — voir la remarque sur le disque éphémère
    dans `job_alertes_expiration_documents` / `_traiter_upload_document`.
    """
    if not fichier or not fichier.filename:
        return None, "Aucune image sélectionnée.", None

    ext = fichier.filename.rsplit('.', 1)[-1].lower() if '.' in fichier.filename else ''
    if ext not in AVATAR_EXTENSIONS:
        return None, "Format non accepté. Utilisez une image PNG ou JPEG.", None

    # Taille : on mesure sans charger tout le fichier en mémoire.
    fichier.stream.seek(0, os.SEEK_END)
    taille = fichier.stream.tell()
    fichier.stream.seek(0)
    if taille > AVATAR_MAX_BYTES:
        return None, (f"Image trop volumineuse ({taille // (1024 * 1024)} Mo). "
                      f"Maximum : {AVATAR_MAX_BYTES // (1024 * 1024)} Mo."), None
    if taille == 0:
        return None, "Le fichier est vide.", None

    if detect_file_type(fichier) not in AVATAR_EXTENSIONS:
        return None, "Ce fichier n'est pas une image valide.", None

    # Nom imprévisible : empêche de deviner l'URL de la photo d'autrui.
    nom = f"u{user_id}_{secrets.token_hex(8)}.{ 'jpg' if ext == 'jpeg' else ext }"
    chemin = os.path.join(AVATAR_FOLDER, secure_filename(nom))

    if not PIL_DISPONIBLE:
        fichier.stream.seek(0)
        contenu = fichier.stream.read()
        with open(chemin, 'wb') as f:
            f.write(contenu)
        return nom, None, contenu

    # Redimensionnement : l'image est recadrée en carré puis réduite. Le
    # ré-encodage par Pillow supprime au passage toute charge utile cachée
    # dans le fichier d'origine (métadonnées EXIF, données après l'image).
    try:
        fichier.stream.seek(0)
        with Image.open(fichier.stream) as img:
            img = ImageOps.exif_transpose(img)          # respecte l'orientation photo
            img = ImageOps.fit(img, (AVATAR_MAX_SIDE, AVATAR_MAX_SIDE),
                               method=Image.LANCZOS, centering=(0.5, 0.4))
            buffer = io.BytesIO()
            if nom.endswith('.png'):
                img.convert('RGBA').save(buffer, 'PNG', optimize=True)
            else:
                img.convert('RGB').save(buffer, 'JPEG', quality=88, optimize=True)
            contenu = buffer.getvalue()
            with open(chemin, 'wb') as f:
                f.write(contenu)
    except Exception as e:
        logger.error("Erreur de traitement de la photo de profil : %s", e, exc_info=True)
        return None, "Cette image n'a pas pu être traitée. Essayez un autre fichier.", None

    return nom, None, contenu


def supprimer_photo_profil(nom_fichier):
    """Efface le fichier d'une ancienne photo, sans jamais interrompre l'appelant."""
    if not nom_fichier:
        return
    try:
        chemin = os.path.join(AVATAR_FOLDER, secure_filename(nom_fichier))
        # Garde-fou : on ne supprime rien en dehors du dossier des avatars.
        if os.path.dirname(os.path.abspath(chemin)) == os.path.abspath(AVATAR_FOLDER) \
                and os.path.isfile(chemin):
            os.remove(chemin)
    except Exception as e:
        logger.error("Erreur supprimer_photo_profil: %s", e, exc_info=True)


def get_current_employee():
    if 'user_id' not in session: return None
    try:
        with db_cursor() as (conn, cur):
            cur.execute("SELECT e.* FROM employes e JOIN users u ON u.employe_id = e.id WHERE u.id = %s LIMIT 1", (session['user_id'],))
            return cur.fetchone()
    except:
        return None

Q_SOLDE = ("SELECT jours_acquis, jours_utilises FROM soldes_conges "
           "WHERE employe_id = %s AND annee = %s")

# ==================== WORKFLOWS RH (congés / permissions) ====================
# Circuit : l'employé dépose → le manager de son département donne un avis →
# le RH tranche. Les statuts historiques ('en attente', 'approuvé', 'refusé')
# sont conservés tels quels pour ne pas invalider les demandes déjà en base.
DEMANDE_EN_ATTENTE = 'en attente'      # déposée, avis manager non rendu
DEMANDE_AVIS_RENDU = 'avis rendu'      # le manager s'est prononcé, RH à décider
DEMANDE_APPROUVEE  = 'approuvé'
DEMANDE_REFUSEE    = 'refusé'
DEMANDE_ANNULEE    = 'annulé'
DEMANDE_OUVERTES   = (DEMANDE_EN_ATTENTE, DEMANDE_AVIS_RENDU)

DEMANDE_LIBELLES = {
    DEMANDE_EN_ATTENTE: "En attente d'avis du manager",
    DEMANDE_AVIS_RENDU: "Avis rendu — décision RH attendue",
    DEMANDE_APPROUVEE:  "Approuvé",
    DEMANDE_REFUSEE:    "Refusé",
    DEMANDE_ANNULEE:    "Annulé",
}


def _peut_decider_rh():
    """Décision finale sur une demande de congé / permission."""
    return session.get('role') in ('admin', 'rh')


def _peut_donner_avis():
    """Étape intermédiaire : réservée aux managers (l'admin dépanne)."""
    return session.get('role') in ('admin', 'manager')


def _managers_du_departement(cur, departement):
    """Comptes managers rattachés à un département (lien par nom).

    Le champ `departements.responsable` est un texte libre, inexploitable pour
    router une demande : on passe donc par les comptes de rôle `manager` dont
    l'employé lié appartient au même département.
    """
    if not departement:
        return []
    cur.execute("""
        SELECT u.id, u.username, e.email FROM users u
          JOIN employes e ON e.id = u.employe_id
         WHERE u.role = 'manager' AND e.departement = %s
    """, (departement,))
    return cur.fetchall()


def _notifier_roles(cur, roles, titre, message, type_='info', sauf=None):
    """Notifie tous les comptes portant l'un des rôles donnés."""
    cur.execute("SELECT id FROM users WHERE role IN %s", (tuple(roles),))
    for row in cur.fetchall():
        if sauf and row['id'] == sauf:
            continue
        create_notification(row['id'], titre, message, type_, cur=cur)


def _envoyer_roles(cur, roles, sujet, message, cle_prefixe):
    """Met en file un e-mail pour chaque compte de rôle disposant d'une adresse."""
    cur.execute("""
        SELECT u.id, e.email FROM users u
        LEFT JOIN employes e ON e.id = u.employe_id
        WHERE u.role IN %s AND e.email IS NOT NULL
    """, (tuple(roles),))
    for row in cur.fetchall():
        queue_email(row['email'], sujet, message, cur=cur,
                    event_key=f"{cle_prefixe}:{row['id']}")


def _notifier_employe_evenement(cur, employe_id, titre, message,
                                 type_='info', cle_evenement=None):
    """Notification interne + e-mail éventuel à l'employé concerné."""
    cur.execute("""
        SELECT e.email, u.id AS user_id
          FROM employes e LEFT JOIN users u ON u.employe_id = e.id
         WHERE e.id = %s ORDER BY u.id NULLS LAST LIMIT 1
    """, (employe_id,))
    cible = cur.fetchone()
    if not cible:
        return
    if cible.get('user_id'):
        create_notification(cible['user_id'], titre, message, type_, cur=cur)
    queue_email(cible.get('email'), titre, message, cur=cur,
                event_key=cle_evenement)


def _user_id_de_employe(cur, employe_id):
    """Compte utilisateur lié à un employé, s'il en a un."""
    if not employe_id:
        return None
    cur.execute("SELECT id FROM users WHERE employe_id = %s LIMIT 1", (employe_id,))
    row = cur.fetchone()
    return row['id'] if row else None


def _libelle_employe(cur, employe_id):
    cur.execute("SELECT nom, prenom, departement FROM employes WHERE id = %s", (employe_id,))
    e = cur.fetchone()
    if not e:
        return "?", None
    return f"{e['prenom']} {e['nom']}".strip(), e['departement']


# ==================== SOLDES DE CONGÉS (Phase 2) ====================

TAUX_ACQUISITION_CONGES_PAR_MOIS = 25 / 12  # ≈ 2.0833 j/mois (25 j/an, convention jours ouvrés)


def calculer_jours_acquis_prorata(date_embauche, annee):
    """Accumulation mensuelle des congés : ~2,08 jour(s) acquis par mois
    complet travaillé, proratisé sur l'année d'embauche. Pour une année déjà
    terminée, retourne le total complet (jusqu'à 25). Pour l'année en cours,
    retourne seulement ce qui est acquis à ce jour (pas les mois futurs).
    """
    debut_annee = date(annee, 1, 1)
    fin_annee = date(annee, 12, 31)
    aujourd_hui = date.today()

    if date_embauche and date_embauche > debut_annee:
        debut_calcul = date_embauche.replace(day=1)
        if debut_calcul < debut_annee:
            debut_calcul = debut_annee
    else:
        debut_calcul = debut_annee

    fin_calcul = min(aujourd_hui, fin_annee)
    if fin_calcul < debut_calcul:
        return 0.0

    mois_acquis = (fin_calcul.year - debut_calcul.year) * 12 + (fin_calcul.month - debut_calcul.month) + 1
    mois_acquis = max(0, min(12, mois_acquis))
    return round(mois_acquis * TAUX_ACQUISITION_CONGES_PAR_MOIS, 1)


def get_solde_conges(employe_id, annee=None):
    """Retourne le solde de congés d'un employé (jours acquis, utilisés, restants)"""
    if annee is None:
        annee = datetime.now().year
    try:
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("""
                SELECT * FROM soldes_conges 
                WHERE employe_id = %s AND annee = %s
            """, (employe_id, annee))
            solde = cur.fetchone()

            if not solde:
                cur.execute("SELECT date_embauche FROM employes WHERE id = %s", (employe_id,))
                emp_row = cur.fetchone()
                acquis_initial = calculer_jours_acquis_prorata(
                    emp_row.get('date_embauche') if emp_row else None, annee
                )
                cur.execute("""
                    INSERT INTO soldes_conges (employe_id, annee, jours_acquis, jours_utilises)
                    VALUES (%s, %s, %s, 0)
                    RETURNING *
                """, (employe_id, annee, acquis_initial))
                solde = cur.fetchone()

            acquis = float(solde.get('jours_acquis') or 25)
            utilises = float(solde.get('jours_utilises') or 0)
            return {
                'jours_acquis': acquis,
                'jours_utilises': utilises,
                'jours_restants': round(acquis - utilises, 1),
                'jours_acquis_manuel': bool(solde.get('jours_acquis_manuel')),
                'annee': annee
            }
    except Exception as e:
        logger.error("Erreur get_solde_conges: %s", e, exc_info=True)
        return {'jours_acquis': 25, 'jours_utilises': 0, 'jours_restants': 25,
                'jours_acquis_manuel': False, 'annee': annee}


def mettre_a_jour_solde(employe_id, jours_delta, annee=None):
    """Ajoute ou soustrait des jours du solde (appelé lors de l'approbation/refus)"""
    if annee is None:
        annee = datetime.now().year
    try:
        conn = get_db()
        cur = get_cursor(conn)
        cur.execute("SELECT date_embauche FROM employes WHERE id = %s", (employe_id,))
        emp_row = cur.fetchone()
        acquis_initial = calculer_jours_acquis_prorata(
            emp_row.get('date_embauche') if emp_row else None, annee
        )
        cur.execute("""
            INSERT INTO soldes_conges (employe_id, annee, jours_acquis, jours_utilises)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (employe_id, annee) 
            DO UPDATE SET jours_utilises = GREATEST(0, soldes_conges.jours_utilises + %s)
        """, (employe_id, annee, acquis_initial, jours_delta, jours_delta))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error("Erreur mise à jour solde: %s", e, exc_info=True)
        return False


def recalculer_solde(employe_id, annee=None, cur=None):
    """
    Recalcule automatiquement les jours utilisés depuis les congés approuvés.

    Si `cur` est fourni, réutilise ce curseur/cette transaction (nécessaire quand
    on est appelé depuis update_conge : la connexion séparée qu'on ouvrait ici
    avant ne voyait pas l'UPDATE du statut pas encore commité par l'appelant,
    en isolation READ COMMITTED — le solde ne se mettait donc jamais à jour).
    """
    if annee is None:
        annee = datetime.now().year

    def _do(cur):
        cur.execute("""
            SELECT COALESCE(SUM(nombre_jours), 0) as total
            FROM conges 
            WHERE employe_id = %s 
              AND statut = 'approuvé'
              AND type_conge = 'congé payé'
              AND EXTRACT(YEAR FROM date_debut) = %s
        """, (employe_id, annee))
        total = float(cur.fetchone()['total'] or 0)

        cur.execute("SELECT date_embauche FROM employes WHERE id = %s", (employe_id,))
        emp_row = cur.fetchone()
        acquis_initial = calculer_jours_acquis_prorata(
            emp_row.get('date_embauche') if emp_row else None, annee
        )
        cur.execute("""
            INSERT INTO soldes_conges (employe_id, annee, jours_acquis, jours_utilises)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (employe_id, annee) 
            DO UPDATE SET jours_utilises = %s
        """, (employe_id, annee, acquis_initial, total, total))
        return total

    try:
        if cur is not None:
            return _do(cur)
        with db_cursor(commit=True) as (conn, cur):
            return _do(cur)
    except Exception as e:
        logger.error("Erreur recalcul solde: %s", e, exc_info=True)
        return 0


def init_db():
    conn = get_db()
    cur = get_cursor(conn)

    cur.execute('''CREATE TABLE IF NOT EXISTS departements (
        id SERIAL PRIMARY KEY,
        nom VARCHAR(100) UNIQUE,
        description TEXT,
        responsable VARCHAR(150)
    )''')
    # Migration : ajoute les colonnes si la table existait déjà sans elles
    cur.execute("ALTER TABLE departements ADD COLUMN IF NOT EXISTS description TEXT")
    cur.execute("ALTER TABLE departements ADD COLUMN IF NOT EXISTS responsable VARCHAR(150)")
    cur.execute('''CREATE TABLE IF NOT EXISTS employes (id SERIAL PRIMARY KEY, nom VARCHAR(100) NOT NULL, prenom VARCHAR(100) NOT NULL, poste VARCHAR(150), departement VARCHAR(100), email VARCHAR(150), telephone VARCHAR(20), date_embauche DATE, salaire NUMERIC(10,2))''')
    cur.execute('''CREATE TABLE IF NOT EXISTS presences (id SERIAL PRIMARY KEY, employe_id INTEGER REFERENCES employes(id), date DATE, heure_arrivee TIME, heure_depart TIME, statut VARCHAR(30) DEFAULT 'présent', commentaire TEXT, UNIQUE(employe_id, date))''')
    cur.execute('''CREATE TABLE IF NOT EXISTS conges (id SERIAL PRIMARY KEY, employe_id INTEGER REFERENCES employes(id), type_conge VARCHAR(50), date_debut DATE, date_fin DATE, nombre_jours INTEGER, motif TEXT, statut VARCHAR(20) DEFAULT 'en attente', date_demande DATE DEFAULT CURRENT_DATE)''')

    # ==================== TABLE SOLDES_CONGES (CRITIQUE) ====================
    cur.execute('''CREATE TABLE IF NOT EXISTS soldes_conges (
        id SERIAL PRIMARY KEY,
        employe_id INTEGER REFERENCES employes(id) ON DELETE CASCADE,
        annee INTEGER NOT NULL,
        jours_acquis NUMERIC(5,1) DEFAULT 25,
        jours_utilises NUMERIC(5,1) DEFAULT 0,
        UNIQUE(employe_id, annee)
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_soldes_employe_annee ON soldes_conges(employe_id, annee)")
    # Si RH fixe manuellement jours_acquis (ex: jours de congé exceptionnels
    # accordés), le job de recalcul mensuel automatique ne doit PAS l'écraser.
    cur.execute("ALTER TABLE soldes_conges ADD COLUMN IF NOT EXISTS jours_acquis_manuel BOOLEAN DEFAULT FALSE")

    cur.execute('''CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username VARCHAR(80) UNIQUE, password_hash VARCHAR(255), role VARCHAR(20) DEFAULT 'employe', employe_id INTEGER REFERENCES employes(id))''')
    # Absences non justifiées : jours d'absence qui ne relèvent ni d'un congé
    # ni d'une permission approuvés (ex. absence non signalée, no-show).
    cur.execute('''CREATE TABLE IF NOT EXISTS absences (
        id SERIAL PRIMARY KEY,
        employe_id INTEGER REFERENCES employes(id) ON DELETE CASCADE,
        date DATE NOT NULL,
        motif TEXT,
        enregistre_par INTEGER REFERENCES users(id),
        date_enregistrement TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(employe_id, date)
    )''')
    # Circuit de justification : les anciennes lignes deviennent explicitement
    # « non justifiées ». Le binaire reste en BYTEA, comme les documents RH et
    # avatars, afin de survivre aux redémarrages du disque éphémère Render.
    for col, typ in (
        ('statut', 'VARCHAR(30) DEFAULT \'non_justifiee\''),
        ('justificatif_nom', 'VARCHAR(255)'),
        ('justificatif_type', 'VARCHAR(20)'),
        ('justificatif_taille', 'INTEGER'),
        ('justificatif_contenu', 'BYTEA'),
        ('date_depot_justificatif', 'TIMESTAMP'),
        ('justification_commentaire', 'TEXT'),
        ('decide_par', 'INTEGER REFERENCES users(id) ON DELETE SET NULL'),
        ('decide_le', 'TIMESTAMP'),
        ('motif_refus', 'TEXT'),
        ('conge_id', 'INTEGER REFERENCES conges(id) ON DELETE SET NULL'),
    ):
        cur.execute(f"ALTER TABLE absences ADD COLUMN IF NOT EXISTS {col} {typ}")
    cur.execute("UPDATE absences SET statut = 'non_justifiee' WHERE statut IS NULL")
    cur.execute("ALTER TABLE absences ALTER COLUMN statut SET DEFAULT 'non_justifiee'")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_absences_statut ON absences(statut)")
    # Absences supprimées manuellement : on mémorise les couples (employe_id, date)
    # à NE PAS régénérer automatiquement. Sans cela, la génération auto recréerait
    # immédiatement toute absence supprimée (le jour reste sans présence) et la
    # suppression semblait ne pas fonctionner.
    cur.execute('''CREATE TABLE IF NOT EXISTS absences_exclues (
        employe_id INTEGER REFERENCES employes(id) ON DELETE CASCADE,
        date DATE NOT NULL,
        PRIMARY KEY (employe_id, date)
    )''')
    # Garde-fou d'idempotence pour les jobs planifiés (scheduler) : empêche un
    # job de tourner deux fois le même jour (redémarrage du dyno, plusieurs
    # workers gunicorn...).
    cur.execute('''CREATE TABLE IF NOT EXISTS scheduler_runs (
        job_name VARCHAR(100) NOT NULL,
        run_date DATE NOT NULL,
        executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (job_name, run_date)
    )''')
    # ==================== TABLE PERMISSIONS (MODULE SÉPARÉ) ====================
    # Une permission fonctionne COMME un congé (demande → approbation/refus),
    # mais c'est une entité à part entière : elle NE fait PAS partie des congés
    # et ne déduit JAMAIS de jours du solde de congés (soldes_conges).
    cur.execute('''CREATE TABLE IF NOT EXISTS permissions (
        id SERIAL PRIMARY KEY,
        employe_id INTEGER REFERENCES employes(id) ON DELETE CASCADE,
        motif TEXT,
        date_debut DATE NOT NULL,
        date_fin DATE NOT NULL,
        nombre_jours INTEGER NOT NULL DEFAULT 1,
        statut VARCHAR(20) DEFAULT 'en attente',
        date_demande DATE DEFAULT CURRENT_DATE
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_permissions_employe ON permissions(employe_id)")

    # ==================== WORKFLOWS RH (validation à deux niveaux) ==========
    # Congés et permissions suivent le même circuit : l'employé dépose sa
    # demande, le manager de son département donne un avis, le RH tranche.
    # Colonnes nullables : les demandes déjà en base restent valides.
    for table in ('conges', 'permissions'):
        for col, typ in [
            ('demande_par_id',   'INTEGER'),      # qui a déposé (self-service)
            ('avis_manager',     'VARCHAR(15)'),  # favorable / defavorable
            ('avis_manager_par', 'VARCHAR(80)'),
            ('avis_manager_le',  'DATE'),
            ('avis_commentaire', 'TEXT'),
            ('decide_par',       'VARCHAR(80)'),  # décision finale RH
            ('decide_le',        'DATE'),
            ('motif_refus',      'TEXT'),         # refus expliqué
            ('annule_par',       'VARCHAR(80)'),
        ]:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_conges_statut ON conges(statut)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_conges_demandeur ON conges(demande_par_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_perms_statut ON permissions(statut)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_perms_demandeur ON permissions(demande_par_id)")
    cur.execute('''CREATE TABLE IF NOT EXISTS audit_logs (id SERIAL PRIMARY KEY, user_id INTEGER, username VARCHAR(80), action VARCHAR(100), entity_type VARCHAR(50), entity_id INTEGER, details TEXT, ip_address VARCHAR(45), timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(timestamp DESC)")

    # ---- Registre des sessions actives -------------------------------------
    # Flask stocke la session dans un cookie signé côté navigateur : le serveur
    # ne sait donc pas qui est connecté et ne peut pas « reprendre » un cookie
    # déjà émis. Ce registre comble ce manque : chaque connexion y inscrit un
    # identifiant de session, que l'on peut révoquer à distance. Chaque requête
    # vérifie que la session présentée est toujours valide (voir session_active).
    cur.execute('''CREATE TABLE IF NOT EXISTS sessions_actives (
        id SERIAL PRIMARY KEY,
        sid VARCHAR(64) UNIQUE NOT NULL,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        username VARCHAR(80),
        ip_address VARCHAR(45),
        user_agent VARCHAR(300),
        login_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        revoked_at TIMESTAMP,
        revoked_by VARCHAR(80)
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions_actives(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_seen ON sessions_actives(last_seen DESC)")

    # Table documents
    cur.execute('''CREATE TABLE IF NOT EXISTS documents (
        id SERIAL PRIMARY KEY,
        employe_id INTEGER REFERENCES employes(id) ON DELETE CASCADE,
        titre VARCHAR(255) NOT NULL,
        nom_fichier VARCHAR(255) NOT NULL,
        chemin_fichier VARCHAR(500) NOT NULL,
        type_fichier VARCHAR(50),
        taille INTEGER,
        date_upload TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        description TEXT
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_employe ON documents(employe_id)")
    # Date d'expiration optionnelle (CDD, visa, certification, contrat...) pour
    # les alertes automatiques avant échéance.
    cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS date_expiration DATE")
    # Le contenu du fichier est stocké EN BASE (persistant), pas sur le disque
    # local du service (éphémère sur Render : perdu après une inactivité
    # prolongée ou un redéploiement). Les documents uploadés avant ce
    # correctif n'ont pas de `contenu` (colonne NULL) : leur fichier disque
    # est probablement déjà perdu, voir download_document().
    cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS contenu BYTEA")
    # Photo de profil : portée par le COMPTE et non par la fiche employé, afin
    # que les comptes sans employé lié puissent aussi en avoir une.
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS photo VARCHAR(255)")
    # Contenu de la photo stocké EN BASE (persistant), pas sur le disque local
    # du service (éphémère sur Render : perdu après une inactivité prolongée
    # ou un redéploiement — c'était la cause des photos cassées).
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS photo_contenu BYTEA")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_expiration ON documents(date_expiration)")
    # Empêche de renvoyer la même alerte d'expiration chaque jour : une seule
    # notif/email par document et par type d'alerte ('bientot' / 'expire').
    cur.execute('''CREATE TABLE IF NOT EXISTS documents_alertes (
        document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
        type_alerte VARCHAR(20) NOT NULL,
        envoye_le DATE DEFAULT CURRENT_DATE,
        PRIMARY KEY (document_id, type_alerte)
    )''')

    # Table notifications (multi-utilisateur)
    cur.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        title VARCHAR(200) NOT NULL,
        message TEXT,
        type VARCHAR(30) DEFAULT 'info',
        is_read BOOLEAN DEFAULT FALSE,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_notif_unread ON notifications(user_id, is_read)")

    # Outbox SMTP persistante : la requête web ne dépend jamais de la latence
    # du fournisseur e-mail. Une clé d'événement optionnelle évite les doublons.
    cur.execute('''CREATE TABLE IF NOT EXISTS email_outbox (
        id SERIAL PRIMARY KEY,
        destinataire VARCHAR(320) NOT NULL,
        sujet VARCHAR(255) NOT NULL,
        corps_texte TEXT,
        corps_html TEXT,
        cle_evenement VARCHAR(200) UNIQUE,
        statut VARCHAR(20) NOT NULL DEFAULT 'en_attente',
        tentatives INTEGER NOT NULL DEFAULT 0,
        disponible_le TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        verrouille_le TIMESTAMP,
        date_creation TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        envoye_le TIMESTAMP,
        derniere_erreur TEXT
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_outbox_a_envoyer ON email_outbox(statut, disponible_le)")

    # ==================== MATÉRIELS (stock par département) ====================
    # Un matériel appartient à un département (papiers, stylos, classeurs...).
    # `quantite` = stock actuel, recalculé à partir des mouvements.
    cur.execute('''CREATE TABLE IF NOT EXISTS materiels (
        id SERIAL PRIMARY KEY,
        nom VARCHAR(150) NOT NULL,
        categorie VARCHAR(50) DEFAULT 'fourniture',
        departement_id INTEGER REFERENCES departements(id) ON DELETE CASCADE,
        quantite INTEGER NOT NULL DEFAULT 0,
        seuil_alerte INTEGER NOT NULL DEFAULT 0,
        unite VARCHAR(30) DEFAULT 'unité',
        description TEXT,
        date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_materiels_dept ON materiels(departement_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_materiels_nom ON materiels(nom)")

    # Historique des mouvements : toute entrée/sortie est tracée (auditable).
    # type_mouvement : 'entree' (approvisionnement) | 'sortie' (consommation)
    cur.execute('''CREATE TABLE IF NOT EXISTS materiels_mouvements (
        id SERIAL PRIMARY KEY,
        materiel_id INTEGER REFERENCES materiels(id) ON DELETE CASCADE,
        type_mouvement VARCHAR(10) NOT NULL,
        quantite INTEGER NOT NULL,
        employe_id INTEGER REFERENCES employes(id) ON DELETE SET NULL,
        motif TEXT,
        user_id INTEGER,
        username VARCHAR(80),
        date_mouvement TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mvt_materiel ON materiels_mouvements(materiel_id, date_mouvement DESC)")

    # Attributions durables (PC, téléphone, clés...) : remise à un employé
    # puis retour éventuel. Une attribution active a date_retour IS NULL.
    cur.execute('''CREATE TABLE IF NOT EXISTS materiels_attributions (
        id SERIAL PRIMARY KEY,
        materiel_id INTEGER REFERENCES materiels(id) ON DELETE CASCADE,
        employe_id INTEGER REFERENCES employes(id) ON DELETE CASCADE,
        quantite INTEGER NOT NULL DEFAULT 1,
        date_attribution DATE DEFAULT CURRENT_DATE,
        date_retour DATE,
        commentaire TEXT
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attrib_materiel ON materiels_attributions(materiel_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attrib_employe ON materiels_attributions(employe_id)")
    # Accusé de réception : l'employé confirme avoir reçu le matériel, ce qui
    # évite les « je n'ai jamais eu ce PC » lors des inventaires ou des soldes
    # de tout compte.
    for col, typ in [
        ('accuse_reception', 'BOOLEAN DEFAULT FALSE'),
        ('accuse_le',        'DATE'),
        ('accuse_par',       'VARCHAR(80)'),
        ('conteste_motif',   'TEXT'),
        ('attribue_par',     'VARCHAR(80)'),
    ]:
        cur.execute(f"ALTER TABLE materiels_attributions ADD COLUMN IF NOT EXISTS {col} {typ}")
    # Évite de renvoyer l'alerte de stock bas en boucle : une seule notif tant
    # que le stock n'est pas repassé au-dessus du seuil (remis à FALSE alors).
    cur.execute("ALTER TABLE materiels ADD COLUMN IF NOT EXISTS alerte_envoyee BOOLEAN DEFAULT FALSE")

    # --- Inventaire physique -------------------------------------------------
    # Une campagne fige, à un instant T, la liste des articles d'un département
    # et leur stock théorique ; on y saisit ensuite le comptage réel. Le stock
    # n'est corrigé qu'à la clôture, via un mouvement d'ajustement tracé.
    # statut : 'en_cours' | 'cloture' | 'annule'
    cur.execute('''CREATE TABLE IF NOT EXISTS inventaires (
        id SERIAL PRIMARY KEY,
        reference VARCHAR(40),
        departement_id INTEGER REFERENCES departements(id) ON DELETE CASCADE,
        statut VARCHAR(15) NOT NULL DEFAULT 'en_cours',
        commentaire TEXT,
        date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        date_cloture TIMESTAMP,
        cree_par INTEGER,
        cree_par_nom VARCHAR(80),
        cloture_par INTEGER,
        cloture_par_nom VARCHAR(80)
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_inventaires_dept ON inventaires(departement_id, date_creation DESC)")

    # Une ligne par article inventorié. quantite_theorique est figée à
    # l'ouverture (photo du stock) ; quantite_comptee est NULL tant que
    # l'article n'a pas été compté — un écart de 0 n'est PAS la même chose
    # qu'un article non compté.
    cur.execute('''CREATE TABLE IF NOT EXISTS inventaire_lignes (
        id SERIAL PRIMARY KEY,
        inventaire_id INTEGER REFERENCES inventaires(id) ON DELETE CASCADE,
        materiel_id INTEGER REFERENCES materiels(id) ON DELETE CASCADE,
        quantite_theorique INTEGER NOT NULL DEFAULT 0,
        quantite_comptee INTEGER,
        commentaire TEXT,
        date_comptage TIMESTAMP,
        compte_par_nom VARCHAR(80),
        UNIQUE (inventaire_id, materiel_id)
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_inv_lignes_inv ON inventaire_lignes(inventaire_id)")
    # Permet de distinguer, dans l'historique d'un matériel, une entrée/sortie
    # saisie à la main d'un ajustement issu d'un inventaire physique.
    cur.execute("ALTER TABLE materiels_mouvements ADD COLUMN IF NOT EXISTS origine VARCHAR(20) DEFAULT 'manuel'")

    # --- Gestion de parc : patrimoine, exemplaires, maintenance --------------
    # Informations patrimoniales portées par l'ARTICLE (valables pour tout le
    # lot : marque, modèle, fournisseur...). Ce qui est propre à une unité
    # précise (n° de série, garantie, état) vit dans `materiel_exemplaires`.
    for col, typ in (
        ('marque',            'VARCHAR(80)'),
        ('modele',            'VARCHAR(120)'),
        ('fournisseur',       'VARCHAR(150)'),
        ('prix_acquisition',  'NUMERIC(14,2)'),
        ('date_acquisition',  'DATE'),
        ('duree_garantie_mois', 'INTEGER'),
        # Un article « suivi à l'unité » génère des exemplaires numérotés
        # (PC, mobilier) ; les consommables restent gérés en quantité.
        ('suivi_unitaire',    'BOOLEAN DEFAULT FALSE'),
        ('prefixe_inventaire', 'VARCHAR(12)'),
    ):
        cur.execute(f"ALTER TABLE materiels ADD COLUMN IF NOT EXISTS {col} {typ}")

    # Un exemplaire = une unité physique identifiable, étiquetable, réparable.
    # etat : 'bon' | 'usage' | 'panne' | 'reparation' | 'rebut'
    cur.execute('''CREATE TABLE IF NOT EXISTS materiel_exemplaires (
        id SERIAL PRIMARY KEY,
        materiel_id INTEGER REFERENCES materiels(id) ON DELETE CASCADE,
        numero_inventaire VARCHAR(40) UNIQUE NOT NULL,
        numero_serie VARCHAR(120),
        etat VARCHAR(15) NOT NULL DEFAULT 'bon',
        employe_id INTEGER REFERENCES employes(id) ON DELETE SET NULL,
        date_acquisition DATE,
        prix_acquisition NUMERIC(14,2),
        fournisseur VARCHAR(150),
        garantie_fin DATE,
        emplacement VARCHAR(150),
        commentaire TEXT,
        date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exemplaires_materiel ON materiel_exemplaires(materiel_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exemplaires_num ON materiel_exemplaires(numero_inventaire)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exemplaires_etat ON materiel_exemplaires(etat)")

    # Circuit de réparation : panne → envoi → retour (réparé ou irréparable).
    # statut : 'signale' | 'envoye' | 'repare' | 'irreparable' | 'annule'
    cur.execute('''CREATE TABLE IF NOT EXISTS materiel_maintenances (
        id SERIAL PRIMARY KEY,
        exemplaire_id INTEGER REFERENCES materiel_exemplaires(id) ON DELETE CASCADE,
        statut VARCHAR(15) NOT NULL DEFAULT 'signale',
        panne TEXT NOT NULL,
        technicien VARCHAR(150),
        date_signalement DATE DEFAULT CURRENT_DATE,
        date_envoi DATE,
        date_retour DATE,
        cout NUMERIC(14,2),
        diagnostic TEXT,
        signale_par VARCHAR(80),
        cloture_par VARCHAR(80),
        date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_maint_exemplaire ON materiel_maintenances(exemplaire_id, date_creation DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_maint_statut ON materiel_maintenances(statut)")

    # --- Workflow d'assignation à 4 acteurs -------------------------------
    # Ajouté après coup : les colonnes sont nullables pour que les
    # interventions déjà enregistrées restent valides sans reprise de données.
    for col, typ in [
        # Qui a signalé : on garde l'id en plus du username, pour pouvoir
        # notifier le demandeur et lui demander de valider le retour.
        ('signale_par_id',    'INTEGER'),
        # Assignation : soit un utilisateur interne, soit un prestataire.
        ('assigne_user_id',   'INTEGER'),
        ('prestataire_id',    'INTEGER'),
        ('date_assignation',  'DATE'),
        ('assigne_par',       'VARCHAR(80)'),
        # Retour d'atelier saisi par l'exécutant, avant validation.
        ('date_execution',    'DATE'),
        ('execute_par',       'VARCHAR(80)'),
        # Validation par le demandeur.
        ('valide_par',        'VARCHAR(80)'),
        ('date_validation',   'DATE'),
        ('motif_refus',       'TEXT'),
        ('validation_forcee', 'BOOLEAN DEFAULT FALSE'),
    ]:
        cur.execute(f"ALTER TABLE materiel_maintenances ADD COLUMN IF NOT EXISTS {col} {typ}")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_maint_assigne ON materiel_maintenances(assigne_user_id, statut)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_maint_demandeur ON materiel_maintenances(signale_par_id, statut)")

    # Annuaire des prestataires externes : remplace le champ texte libre, pour
    # que « Atelier Info+ » soit la même entité d'une intervention à l'autre.
    cur.execute('''CREATE TABLE IF NOT EXISTS prestataires (
        id SERIAL PRIMARY KEY,
        nom VARCHAR(150) NOT NULL,
        contact VARCHAR(150),
        telephone VARCHAR(40),
        email VARCHAR(150),
        specialite VARCHAR(100),
        actif BOOLEAN DEFAULT TRUE,
        date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Compteurs des numéros d'inventaire : une séquence par préfixe et par
    # année (PC-2026-001, PC-2026-002...). Table dédiée plutôt que MAX()+1,
    # qui réattribuerait un numéro après suppression d'un exemplaire.
    cur.execute('''CREATE TABLE IF NOT EXISTS materiel_compteurs (
        prefixe VARCHAR(12) NOT NULL,
        annee INTEGER NOT NULL,
        dernier INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (prefixe, annee)
    )''')

    # ==================== MESSAGERIE INTERNE ====================
    # Messages privés, discussions de groupe, annonces RH. Voir
    # blueprints/messagerie.py pour la logique.
    cur.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id SERIAL PRIMARY KEY,
        type VARCHAR(20) NOT NULL DEFAULT 'prive',
        titre VARCHAR(200),
        cible_role VARCHAR(20),
        cree_par INTEGER REFERENCES users(id),
        date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS conversation_membres (
        conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        dernier_message_lu_id INTEGER,
        date_ajout TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (conversation_id, user_id)
    )''')
    # Le contenu des pièces jointes est stocké EN BASE (BYTEA), pas sur le
    # disque local éphémère du service — même raison que pour les documents
    # et les photos de profil.
    cur.execute('''CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
        sender_id INTEGER REFERENCES users(id),
        contenu TEXT,
        piece_jointe_nom VARCHAR(255),
        piece_jointe_type VARCHAR(50),
        piece_jointe_taille INTEGER,
        piece_jointe_contenu BYTEA,
        date_envoi TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id)")
    # La suppression d'un compte ne doit pas casser toute la gestion des
    # utilisateurs dès qu'il a écrit un message. On conserve l'historique en
    # anonymisant l'auteur (SET NULL) ; membres et lectures, eux, sont supprimés
    # par leurs clés étrangères CASCADE.
    cur.execute("""
        DO $$ BEGIN
          IF EXISTS (
              SELECT 1 FROM pg_constraint
               WHERE conname='conversations_cree_par_fkey' AND confdeltype <> 'n'
          ) THEN
            ALTER TABLE conversations DROP CONSTRAINT conversations_cree_par_fkey;
            ALTER TABLE conversations ADD CONSTRAINT conversations_cree_par_fkey
              FOREIGN KEY (cree_par) REFERENCES users(id) ON DELETE SET NULL;
          END IF;
          IF EXISTS (
              SELECT 1 FROM pg_constraint
               WHERE conname='messages_sender_id_fkey' AND confdeltype <> 'n'
          ) THEN
            ALTER TABLE messages DROP CONSTRAINT messages_sender_id_fkey;
            ALTER TABLE messages ADD CONSTRAINT messages_sender_id_fkey
              FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE SET NULL;
          END IF;
        END $$;
    """)
    # Suivi de lecture des annonces : pas de ligne de membre par destinataire
    # potentiel (pourrait être tous les employés), juste une marque de lecture.
    cur.execute('''CREATE TABLE IF NOT EXISTS annonce_lues (
        conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        lu_le TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (conversation_id, user_id)
    )''')

    appliquer_contraintes_phase1(cur, logger)

    # Seed employés
    cur.execute("SELECT COUNT(*) FROM employes")
    if cur.fetchone()['count'] == 0:
        cur.executemany('INSERT INTO employes (nom, prenom, poste, departement, email, telephone, date_embauche, salaire) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)', [
            ('Dupont','Jean','Développeur','Informatique','jean.dupont@entreprise.fr','0612345678','2023-01-15',52000),
            ('Martin','Sophie','Responsable RH','Ressources Humaines','sophie.martin@entreprise.fr','0698765432','2022-06-01',58000),
            ('Bernard','Pierre','Chef de projet','Informatique','pierre.bernard@entreprise.fr','0678912345','2021-09-10',61000),
            ('Administrateur','Système','Administrateur Système','Administration','admin@entreprise.fr','0600000001','2022-01-01',72000),
        ])

    # Seed utilisateurs
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()['count'] == 0:
        cur.execute("INSERT INTO users (username, password_hash, role, employe_id) VALUES (%s,%s,%s,%s)", ('admin', generate_password_hash('admin123'), 'admin', 4))
        cur.execute("INSERT INTO users (username, password_hash, role, employe_id) VALUES (%s,%s,%s,%s)", ('rh', generate_password_hash('rh123'), 'rh', 2))
        cur.execute("INSERT INTO users (username, password_hash, role, employe_id) VALUES (%s,%s,%s,%s)", ('manager', generate_password_hash('manager123'), 'manager', 3))
        cur.execute("INSERT INTO users (username, password_hash, role, employe_id) VALUES (%s,%s,%s,%s)", ('employe', generate_password_hash('user123'), 'employe', 1))

    # Seed soldes congés (maintenant possible car la table existe)
    annee_courante = datetime.now().year
    cur.execute("SELECT COUNT(*) FROM soldes_conges WHERE annee = %s", (annee_courante,))
    if cur.fetchone()['count'] == 0:
        cur.execute("SELECT id, date_embauche FROM employes")
        for emp in cur.fetchall():
            acquis_initial = calculer_jours_acquis_prorata(emp.get('date_embauche'), annee_courante)
            cur.execute("""
                INSERT INTO soldes_conges (employe_id, annee, jours_acquis, jours_utilises)
                VALUES (%s, %s, %s, 0)
                ON CONFLICT (employe_id, annee) DO NOTHING
            """, (emp['id'], annee_courante, acquis_initial))

    conn.commit()
    cur.close()
    conn.close()
    logger.info("Base PostgreSQL initialisée (Self-Service + Exports + Emails HTML + Soldes Congés)")
# ==================== AUTH ====================
# ==================== AUTHENTIFICATION ======================================
# Routes extraites dans blueprints/auth.py.

# ==================== RECHERCHE GLOBALE ====================
# Une seule fonction sert l'aperçu instantané (JSON) et la page de résultats :
# les deux voient donc exactement la même chose, y compris les règles d'accès.

# Qui a le droit de voir quel type de résultat. La règle reproduit celle des
# pages correspondantes : un employé n'a rien à faire dans la liste des comptes.
# `None` = accessible à tous les rôles connectés.
RECHERCHE_ACCES = {
    'employe':     None,
    'departement': None,
    'materiel':    None,
    'conge':       ('admin', 'rh', 'manager'),
    'absence':     ('admin', 'rh', 'manager'),
    'document':    None,
    'utilisateur': ('admin', 'rh'),
    'page':        None,
}

# Pages de l'application atteignables depuis la recherche : taper « congé »
# doit proposer d'aller sur la page des congés, pas seulement lister des demandes.
RECHERCHE_PAGES = [
    ('Tableau de bord',        'dashboard',            None,                        'dashboard'),
    ('Employés',               'index',                None,                        'users'),
    ('Départements',           'departements.departements', None,                    'building'),
    ('Matériels',              'parc.materiels',       None,                        'box'),
    ('Inventaire physique',    'parc.inventaires',     None,                        'box'),
    ('Maintenance',            'parc.maintenances',    None,                        'box'),
    ('Présences',              'presences.presences',  None,                        'clock'),
    ('Historique',             'presences.historique', None,                        'history'),
    ('Absences',               'absences',             ('admin', 'rh', 'manager'),  'user-x'),
    ('Congés',                 'conges',               None,                        'palm'),
    ('Calendrier des congés',  'calendrier_conges',    None,                        'calendar'),
    ('Soldes de congés',       'soldes_conges_page',   None,                        'wallet'),
    ('Permissions',            'permissions',          None,                        'file'),
    ('Documents',              'documents.documents',  None,                        'file'),
    ('Utilisateurs',           'utilisateurs.utilisateurs_page', ('admin', 'rh'),            'shield'),
    ('Notifications',          'notifications',        None,                        'bell'),
    ('Mon espace',             'auth.mon_profil',      None,                        'user'),
]


def _date_courte(valeur):
    """Formate une date pour l'affichage des résultats (tolère None)."""
    try:
        return valeur.strftime('%d/%m/%Y')
    except Exception:
        return str(valeur or '')


def _total_exact(lignes, defaut=0):
    """Total réel renvoyé par COUNT(*) OVER() (calculé avant le LIMIT)."""
    return lignes[0]['_total'] if lignes else defaut


def _recherche_autorise(categorie, role):
    """Le rôle a-t-il le droit de voir cette catégorie ? (admin voit tout)"""
    roles = RECHERCHE_ACCES.get(categorie)
    return roles is None or role == 'admin' or role in roles


def recherche_globale(terme, role, limite_par_categorie=5):
    """Cherche `terme` dans tout le contenu métier visible par `role`.

    Renvoie une liste de groupes [{categorie, libelle, icone, resultats[], total}].
    Chaque résultat porte un titre, un sous-titre et une URL de destination.
    """
    terme = (terme or '').strip()
    if len(terme) < 2:          # en dessous, le bruit dépasse l'utilité
        return []

    # LIKE insensible à la casse. On échappe les jokers SQL pour qu'un terme
    # contenant % ou _ soit cherché littéralement au lieu de tout retourner.
    motif = '%' + terme.lower().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
    groupes = []

    def ajouter(categorie, libelle, icone, lignes, total, url_liste=None):
        if lignes:
            groupes.append({
                'categorie': categorie, 'libelle': libelle, 'icone': icone,
                'resultats': lignes, 'total': total, 'url_liste': url_liste,
            })

    with db_cursor() as (conn, cur):
        employee_scope, employee_scope_params = department_scope_sql('e', cur=cur)
        department_scope, department_scope_params = department_scope_sql('d', 'nom', cur)
        # ---- Employés ----
        if _recherche_autorise('employe', role):
            cur.execute(f"""
                SELECT COUNT(*) OVER() AS _total,
                       e.id, e.nom, e.prenom, e.poste, e.departement, e.email, (
                    SELECT u.photo FROM users u
                     WHERE u.employe_id = e.id AND u.photo IS NOT NULL
                     ORDER BY u.id LIMIT 1
                ) AS photo
                  FROM employes e
                 WHERE (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s
                    OR LOWER(e.nom || ' ' || e.prenom) LIKE %s
                    OR LOWER(e.prenom || ' ' || e.nom) LIKE %s
                    OR LOWER(COALESCE(e.poste, '')) LIKE %s
                    OR LOWER(COALESCE(e.email, '')) LIKE %s
                    OR LOWER(COALESCE(e.telephone, '')) LIKE %s)
                   AND {employee_scope}
                 ORDER BY e.nom, e.prenom LIMIT %s
            """, [motif] * 7 + employee_scope_params + [limite_par_categorie + 1])
            lignes = cur.fetchall()
            total = _total_exact(lignes)
            ajouter('employe', 'Employés', 'users', [{
                'titre': f"{r['prenom']} {r['nom']}",
                'sous_titre': ' · '.join(x for x in [r.get('poste'), r.get('departement')] if x) or r.get('email') or '',
                'url': url_for('view_employee', id=r['id']),
                'photo': r.get('photo'),
            } for r in lignes[:limite_par_categorie]], total, url_for('index', search=terme))

        # ---- Départements ----
        if _recherche_autorise('departement', role):
            cur.execute(f"""
                SELECT COUNT(*) OVER() AS _total, d.id, d.nom, d.description, d.responsable
                  FROM departements d
                 WHERE (LOWER(COALESCE(d.nom, '')) LIKE %s
                    OR LOWER(COALESCE(d.description, '')) LIKE %s
                    OR LOWER(COALESCE(d.responsable, '')) LIKE %s)
                   AND {department_scope}
                 ORDER BY d.nom LIMIT %s
            """, [motif, motif, motif] + department_scope_params +
                  [limite_par_categorie + 1])
            lignes = cur.fetchall()
            ajouter('departement', 'Départements', 'building', [{
                'titre': r['nom'],
                'sous_titre': (f"Responsable : {r['responsable']}" if r.get('responsable') else (r.get('description') or '')),
                'url': url_for('parc.materiels_departement', id=r['id']),
            } for r in lignes[:limite_par_categorie]], _total_exact(lignes), url_for('departements.departements'))

        # ---- Matériels ----
        if _recherche_autorise('materiel', role):
            cur.execute(f"""
                SELECT COUNT(*) OVER() AS _total,
                       m.id, m.nom, m.categorie, m.quantite, m.unite, m.departement_id, d.nom AS dept
                  FROM materiels m LEFT JOIN departements d ON d.id = m.departement_id
                 WHERE (LOWER(m.nom) LIKE %s
                    OR LOWER(COALESCE(m.description, '')) LIKE %s
                    OR LOWER(COALESCE(m.categorie, '')) LIKE %s)
                   AND {department_scope}
                 ORDER BY m.nom LIMIT %s
            """, [motif, motif, motif] + department_scope_params +
                  [limite_par_categorie + 1])
            lignes = cur.fetchall()
            ajouter('materiel', 'Matériels', 'box', [{
                'titre': r['nom'],
                'sous_titre': ' · '.join(x for x in [
                    r.get('dept'), f"{r['quantite']} {r.get('unite') or ''}".strip()] if x),
                'url': (url_for('parc.materiels_departement', id=r['departement_id'])
                        if r.get('departement_id') else url_for('parc.materiels')),
            } for r in lignes[:limite_par_categorie]], _total_exact(lignes), url_for('parc.materiels'))

        # ---- Congés ----
        if _recherche_autorise('conge', role):
            cur.execute(f"""
                SELECT COUNT(*) OVER() AS _total,
                       c.id, c.type_conge, c.statut, c.date_debut, c.date_fin, c.motif,
                       e.nom, e.prenom
                  FROM conges c JOIN employes e ON c.employe_id = e.id
                 WHERE (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s
                    OR LOWER(COALESCE(c.motif, '')) LIKE %s
                    OR LOWER(COALESCE(c.type_conge, '')) LIKE %s
                    OR LOWER(COALESCE(c.statut, '')) LIKE %s)
                   AND {employee_scope}
                 ORDER BY c.date_debut DESC LIMIT %s
            """, [motif] * 5 + employee_scope_params + [limite_par_categorie + 1])
            lignes = cur.fetchall()
            ajouter('conge', 'Congés', 'palm', [{
                'titre': f"{r['prenom']} {r['nom']} — {r.get('type_conge') or 'congé'}",
                'sous_titre': f"{_date_courte(r['date_debut'])} → {_date_courte(r['date_fin'])} · {r.get('statut') or ''}",
                'url': url_for('conges', search=f"{r['prenom']} {r['nom']}"),
            } for r in lignes[:limite_par_categorie]], _total_exact(lignes), url_for('conges', search=terme))

        # ---- Absences ----
        if _recherche_autorise('absence', role):
            cur.execute(f"""
                SELECT COUNT(*) OVER() AS _total, a.id, a.date, a.motif, e.nom, e.prenom
                  FROM absences a JOIN employes e ON a.employe_id = e.id
                 WHERE (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s
                    OR LOWER(COALESCE(a.motif, '')) LIKE %s)
                   AND {employee_scope}
                 ORDER BY a.date DESC LIMIT %s
            """, [motif, motif, motif] + employee_scope_params +
                  [limite_par_categorie + 1])
            lignes = cur.fetchall()
            ajouter('absence', 'Absences', 'user-x', [{
                'titre': f"{r['prenom']} {r['nom']}",
                'sous_titre': f"{_date_courte(r['date'])}" + (f" · {r['motif']}" if r.get('motif') else ''),
                'url': url_for('absences', search=f"{r['prenom']} {r['nom']}"),
            } for r in lignes[:limite_par_categorie]], _total_exact(lignes), url_for('absences', search=terme))

        # ---- Documents ----
        if _recherche_autorise('document', role):
            cur.execute(f"""
                SELECT COUNT(*) OVER() AS _total, d.id, d.titre, d.nom_fichier, d.description, e.nom, e.prenom
                  FROM documents d LEFT JOIN employes e ON d.employe_id = e.id
                 WHERE (LOWER(d.titre) LIKE %s
                    OR LOWER(COALESCE(d.description, '')) LIKE %s
                    OR LOWER(COALESCE(d.nom_fichier, '')) LIKE %s)
                   AND {employee_scope}
                 ORDER BY d.date_upload DESC LIMIT %s
            """, [motif, motif, motif] + employee_scope_params +
                  [limite_par_categorie + 1])
            lignes = cur.fetchall()
            ajouter('document', 'Documents', 'file', [{
                'titre': r['titre'],
                'sous_titre': (f"{r['prenom']} {r['nom']}" if r.get('nom') else (r.get('nom_fichier') or '')),
                'url': url_for('documents.documents'),
            } for r in lignes[:limite_par_categorie]], _total_exact(lignes), url_for('documents.documents'))

        # ---- Comptes utilisateurs ----
        if _recherche_autorise('utilisateur', role):
            cur.execute("""
                SELECT COUNT(*) OVER() AS _total, u.id, u.username, u.role, u.photo, e.nom, e.prenom
                  FROM users u LEFT JOIN employes e ON u.employe_id = e.id
                 WHERE LOWER(u.username) LIKE %s
                    OR LOWER(COALESCE(e.nom, '')) LIKE %s
                    OR LOWER(COALESCE(e.prenom, '')) LIKE %s
                 ORDER BY u.username LIMIT %s
            """, [motif, motif, motif, limite_par_categorie + 1])
            lignes = cur.fetchall()
            ajouter('utilisateur', 'Comptes', 'shield', [{
                'titre': r['username'],
                'sous_titre': ' · '.join(x for x in [
                    get_role_label(r.get('role')),
                    (f"{r['prenom']} {r['nom']}" if r.get('nom') else None)] if x),
                'url': url_for('utilisateurs.utilisateurs_page'),
                'photo': r.get('photo'),
            } for r in lignes[:limite_par_categorie]], _total_exact(lignes), url_for('utilisateurs.utilisateurs_page'))

    # ---- Pages de l'application ----
    terme_bas = terme.lower()
    pages = []
    for libelle, endpoint, roles, icone in RECHERCHE_PAGES:
        if roles and role != 'admin' and role not in roles:
            continue
        if terme_bas in libelle.lower():
            try:
                pages.append({'titre': libelle, 'sous_titre': 'Aller à la page',
                              'url': url_for(endpoint)})
            except Exception:
                continue        # endpoint absent : on ignore la page
    if pages:
        groupes.append({'categorie': 'page', 'libelle': 'Navigation', 'icone': 'dashboard',
                        'resultats': pages[:limite_par_categorie], 'total': len(pages),
                        'url_liste': None})

    return groupes


@app.route('/api/recherche')
@login_required
def api_recherche():
    """Aperçu instantané : renvoie les résultats en JSON pour le panneau déroulant."""
    terme = request.args.get('q', '').strip()
    role = session.get('role', 'employe')
    if len(terme) < 2:
        return jsonify({'terme': terme, 'groupes': [], 'total': 0})
    try:
        groupes = recherche_globale(terme, role, limite_par_categorie=4)
    except Exception as e:
        logger.error("Erreur de recherche globale : %s", e, exc_info=True)
        return jsonify({'terme': terme, 'groupes': [], 'total': 0, 'erreur': True}), 200
    total = sum(g['total'] for g in groupes)
    return jsonify({'terme': terme, 'groupes': groupes, 'total': total})


@app.route('/recherche')
@login_required
def recherche_page():
    """Page listant tous les résultats, groupés par catégorie."""
    terme = request.args.get('q', '').strip()
    role = session.get('role', 'employe')
    groupes = []
    if len(terme) >= 2:
        try:
            groupes = recherche_globale(terme, role, limite_par_categorie=20)
        except Exception as e:
            logger.error("Erreur de recherche globale : %s", e, exc_info=True)
            flash("La recherche a échoué. Réessayez.", "danger")
    total = sum(g['total'] for g in groupes)
    return render_template('recherche.html', terme=terme, groupes=groupes, total=total)

# ==================== ESPACE PERSONNEL =====================================
# Routes extraites dans blueprints/auth.py.

# ==================== SELF-SERVICE ====================
@app.route('/self-service')
@app.route('/mon-espace')
@login_required
def self_service():
    emp = get_current_employee()
    my_presences = []
    my_conges = []
    my_absences = []
    mon_solde = None
    materiels_a_confirmer = 0
    if emp:
        conn = get_db()
        cur = get_cursor(conn)
        cur.execute("SELECT * FROM presences WHERE employe_id = %s ORDER BY date DESC LIMIT 30", (emp['id'],))
        my_presences = cur.fetchall()
        for p in my_presences: p['retard_minutes'] = calculer_retard(p['heure_arrivee'])
        cur.execute("SELECT * FROM conges WHERE employe_id = %s ORDER BY date_demande DESC LIMIT 15", (emp['id'],))
        my_conges = cur.fetchall()
        cur.execute("""SELECT id, date, statut, motif_refus FROM absences
                       WHERE employe_id = %s ORDER BY date DESC LIMIT 8""",
                    (emp['id'],))
        my_absences = cur.fetchall()
        mon_solde = get_solde_conges(emp['id'])
        # Nombre d'équipements dont l'employé n'a pas encore accusé réception :
        # sert à afficher un rappel visible dès l'accueil de l'espace.
        cur.execute("""SELECT COUNT(*) AS nb FROM materiels_attributions
                        WHERE employe_id = %s AND accuse_reception = FALSE""", (emp['id'],))
        materiels_a_confirmer = cur.fetchone()['nb']
        cur.close(); conn.close()
    return render_template('self_service.html', employee=emp, my_presences=my_presences,
                           my_conges=my_conges, my_absences=my_absences,
                           absence_statut_labels=ABSENCE_STATUT_LABELS,
                           mon_solde=mon_solde, libelles=DEMANDE_LIBELLES,
                           materiels_a_confirmer=materiels_a_confirmer)

@app.route('/self-service/presences')
@login_required
def self_service_presences():
    emp = get_current_employee()
    if not emp:
        flash("Aucun employé lié à votre compte.", "warning")
        return redirect(url_for('self_service'))
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM presences WHERE employe_id = %s ORDER BY date DESC", (emp['id'],))
    presences = cur.fetchall()
    for p in presences: p['retard_minutes'] = calculer_retard(p['heure_arrivee'])
    cur.close(); conn.close()
    return render_template('self_service_presences.html', presences=presences, employee=emp)

@app.route('/self-service/conges')
@login_required
def self_service_conges():
    """Mes demandes de congés et de permissions, avec dépôt et annulation."""
    emp = get_current_employee()
    if not emp:
        flash("Aucun employé lié à votre compte.", "warning")
        return redirect(url_for('self_service'))
    with db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM conges WHERE employe_id = %s "
                    "ORDER BY date_demande DESC", (emp['id'],))
        conges = cur.fetchall()
        cur.execute("SELECT * FROM permissions WHERE employe_id = %s "
                    "ORDER BY date_debut DESC", (emp['id'],))
        permissions_list = cur.fetchall()
    return render_template('self_service_conges.html', conges=conges,
                           permissions=permissions_list, employee=emp,
                           mon_solde=get_solde_conges(emp['id']),
                           libelles=DEMANDE_LIBELLES,
                           ouvertes=DEMANDE_OUVERTES)


@app.route('/self-service/materiels')
@login_required
def self_service_materiels():
    """Matériel qui m'est attribué, et accusés de réception en attente."""
    emp = get_current_employee()
    if not emp:
        flash("Aucun employé lié à votre compte.", "warning")
        return redirect(url_for('self_service'))
    with db_cursor() as (conn, cur):
        cur.execute("""
            SELECT a.*, m.nom AS materiel_nom, m.categorie, m.unite,
                   ex.numero_inventaire AS exemplaire_numero
              FROM materiels_attributions a
              JOIN materiels m ON m.id = a.materiel_id
              LEFT JOIN materiel_exemplaires ex ON ex.id = a.exemplaire_id
             WHERE a.employe_id = %s
             ORDER BY a.accuse_reception ASC, a.date_attribution DESC
        """, (emp['id'],))
        attributions = cur.fetchall()
    return render_template('self_service_materiels.html',
                           attributions=attributions, employee=emp)


# ==================== EXPORTS ====================
def create_presences_pdf(data, title="Rapport des Présences"):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=1.5*cm, leftMargin=1.5*cm, topMargin=1.5*cm, bottomMargin=1.5*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=16, spaceAfter=18, textColor=colors.HexColor('#1e40af'))
    elements = [Paragraph(title, title_style), Paragraph(f"Généré le {datetime.now().strftime('%d/%m/%Y %H:%M')}", styles['Normal']), Spacer(1, 12)]
    if data:
        tdata = [["Date", "Employé", "Arrivée", "Retard", "Départ", "Statut"]]
        for row in data:
            ret = calculer_retard(row.get('heure_arrivee'))
            nom = f"{row.get('prenom','')} {row.get('nom','')}".strip()
            tdata.append([str(row.get('date','')), nom, str(row.get('heure_arrivee') or '—')[:5], f"+{ret} min" if ret > 0 else "—", str(row.get('heure_depart') or '—')[:5], row.get('statut','')])
        t = Table(tdata, colWidths=[2.3*cm,5*cm,2*cm,2.1*cm,2*cm,2.5*cm])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1e40af')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 8),
            ('ALIGN', (0,0), (-1,-1), 'CENTER'),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#e2e8f0')),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f8fafc')]),
        ]))
        elements.append(t)
    doc.build(elements)
    buffer.seek(0)
    return buffer

def create_conges_pdf(data, title="Rapport des Congés"):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=1.5*cm, leftMargin=1.5*cm, topMargin=1.5*cm, bottomMargin=1.5*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=16, spaceAfter=18, textColor=colors.HexColor('#166534'))
    elements = [Paragraph(title, title_style), Paragraph(f"Généré le {datetime.now().strftime('%d/%m/%Y %H:%M')}", styles['Normal']), Spacer(1, 12)]
    if data:
        tdata = [["Employé", "Type", "Début", "Fin", "Jours", "Statut"]]
        for row in data:
            nom = f"{row.get('prenom','')} {row.get('nom','')}".strip()
            tdata.append([nom, row.get('type_conge',''), str(row.get('date_debut','')), str(row.get('date_fin','')), str(row.get('nombre_jours','')), row.get('statut','')])
        t = Table(tdata, colWidths=[5*cm,3.3*cm,2.7*cm,2.7*cm,1.4*cm,2.4*cm])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#166534')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 8),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#e2e8f0')),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f8fafc')]),
        ]))
        elements.append(t)
    doc.build(elements)
    buffer.seek(0)
    return buffer

def create_presences_excel(data):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Présences"
    header_fill = PatternFill(start_color="1E40AF", end_color="1E40AF", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=10)
    thin = Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))
    headers = ["Date", "Employé", "Arrivée", "Retard (min)", "Départ", "Statut"]
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.border = thin
    for i, row in enumerate(data, 2):
        ret = calculer_retard(row.get('heure_arrivee'))
        nom = f"{row.get('prenom','')} {row.get('nom','')}".strip()
        vals = [str(row.get('date','')), nom, str(row.get('heure_arrivee') or '')[:5], ret if ret > 0 else 0, str(row.get('heure_depart') or '')[:5], row.get('statut','')]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=i, column=c, value=v)
            cell.border = thin
            if c == 4 and v > 0: cell.font = Font(color="DC2626", bold=True)
    for c in range(1, len(headers)+1):
        ws.column_dimensions[get_column_letter(c)].width = 16
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf

def create_conges_excel(data):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Congés"
    header_fill = PatternFill(start_color="166534", end_color="166534", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=10)
    thin = Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))
    headers = ["Employé", "Type", "Début", "Fin", "Jours", "Statut"]
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.border = thin
    for i, row in enumerate(data, 2):
        nom = f"{row.get('prenom','')} {row.get('nom','')}".strip()
        vals = [nom, row.get('type_conge',''), str(row.get('date_debut','')), str(row.get('date_fin','')), row.get('nombre_jours',''), row.get('statut','')]
        for c, v in enumerate(vals, 1):
            ws.cell(row=i, column=c, value=v).border = thin
    for c in range(1, len(headers)+1):
        ws.column_dimensions[get_column_letter(c)].width = 15
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf

# EXPORT ROUTES
@app.route('/export/presences/pdf')
@login_required
def export_presences_pdf():
    my_only = request.args.get('my') == '1'
    emp = get_current_employee() if my_only else None
    conn = get_db()
    cur = get_cursor(conn)
    q = "SELECT p.*, e.nom, e.prenom FROM presences p JOIN employes e ON p.employe_id = e.id "
    params = []
    if my_only:
        if emp:
            q += "WHERE p.employe_id = %s "
            params.append(emp['id'])
        else:
            q += "WHERE FALSE "
    else:
        scope_where, scope_params = department_scope_sql('e')
        q += f"WHERE {scope_where} "
        params.extend(scope_params)
    q += "ORDER BY p.date DESC LIMIT 500"
    cur.execute(q, params)
    data = cur.fetchall()
    cur.close(); conn.close()
    pdf = create_presences_pdf(data)
    resp = make_response(pdf.read())
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = 'attachment; filename=presences.pdf'
    return resp

@app.route('/export/presences/excel')
@login_required
def export_presences_excel():
    my_only = request.args.get('my') == '1'
    emp = get_current_employee() if my_only else None
    conn = get_db()
    cur = get_cursor(conn)
    q = "SELECT p.*, e.nom, e.prenom FROM presences p JOIN employes e ON p.employe_id = e.id "
    params = []
    if my_only:
        if emp:
            q += "WHERE p.employe_id = %s "
            params.append(emp['id'])
        else:
            q += "WHERE FALSE "
    else:
        scope_where, scope_params = department_scope_sql('e')
        q += f"WHERE {scope_where} "
        params.extend(scope_params)
    q += "ORDER BY p.date DESC LIMIT 800"
    cur.execute(q, params)
    data = cur.fetchall()
    cur.close(); conn.close()
    xlsx = create_presences_excel(data)
    resp = make_response(xlsx.read())
    resp.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    resp.headers['Content-Disposition'] = 'attachment; filename=presences.xlsx'
    return resp

@app.route('/export/conges/pdf')
@login_required
def export_conges_pdf():
    my_only = request.args.get('my') == '1'
    emp = get_current_employee() if my_only else None
    conn = get_db()
    cur = get_cursor(conn)
    q = "SELECT c.*, e.nom, e.prenom FROM conges c JOIN employes e ON c.employe_id = e.id "
    params = []
    if my_only:
        if emp:
            q += "WHERE c.employe_id = %s "
            params.append(emp['id'])
        else:
            q += "WHERE FALSE "
    else:
        scope_where, scope_params = department_scope_sql('e')
        q += f"WHERE {scope_where} "
        params.extend(scope_params)
    q += "ORDER BY c.date_demande DESC LIMIT 500"
    cur.execute(q, params)
    data = cur.fetchall()
    cur.close(); conn.close()
    pdf = create_conges_pdf(data)
    resp = make_response(pdf.read())
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = 'attachment; filename=conges.pdf'
    return resp

@app.route('/export/conges/excel')
@login_required
def export_conges_excel():
    my_only = request.args.get('my') == '1'
    emp = get_current_employee() if my_only else None
    conn = get_db()
    cur = get_cursor(conn)
    q = "SELECT c.*, e.nom, e.prenom FROM conges c JOIN employes e ON c.employe_id = e.id "
    params = []
    if my_only:
        if emp:
            q += "WHERE c.employe_id = %s "
            params.append(emp['id'])
        else:
            q += "WHERE FALSE "
    else:
        scope_where, scope_params = department_scope_sql('e')
        q += f"WHERE {scope_where} "
        params.extend(scope_params)
    q += "ORDER BY c.date_demande DESC"
    cur.execute(q, params)
    data = cur.fetchall()
    cur.close(); conn.close()
    xlsx = create_conges_excel(data)
    resp = make_response(xlsx.read())
    resp.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    resp.headers['Content-Disposition'] = 'attachment; filename=conges.xlsx'
    return resp

# ==================== BASIC ROUTES ====================
@app.route('/')
@login_required
def dashboard():
    """Tableau de bord global pour admin/RH, départemental pour les autres.

    Le cloisonnement est appliqué dans CHAQUE requête SQL. Un compte non
    privilégié sans fiche employé ou sans département voit volontairement des
    compteurs vides : il ne doit jamais basculer implicitement sur la vue
    globale.
    """
    with db_cursor() as (conn, cur):
        data_scope = get_department_scope(cur)
        vue_globale = data_scope['is_global']
        departement = data_scope.get('department')
        scope_vide = data_scope['is_empty']
        dashboard_scope = {
            **data_scope,
            'label': "Toute l'entreprise" if vue_globale else
                     (departement or 'Aucun département rattaché'),
        }

        def scope_employe(alias='e'):
            return department_scope_sql(alias, 'departement', cur)

        def scope_departement(alias='d'):
            return department_scope_sql(alias, 'nom', cur)

        today = date.today()
        annee = today.year
        emp_where, emp_params = scope_employe('e')

        # === Personnel ======================================================
        cur.execute(f"""
            SELECT COUNT(*) AS total,
                   COALESCE(AVG(e.salaire), 0) AS salaire_moyen,
                   COALESCE(AVG(CURRENT_DATE - e.date_embauche), 0) AS anciennete_jours
              FROM employes e WHERE {emp_where}
        """, emp_params)
        personnel = cur.fetchone()
        total_employes = personnel['total'] or 0
        salaire_moyen = personnel['salaire_moyen'] or 0
        anciennete_moyenne = round(float(personnel['anciennete_jours'] or 0) / 365.25, 1)

        if vue_globale:
            cur.execute("""
                SELECT COUNT(*) AS total FROM (
                    SELECT nom FROM departements WHERE nom IS NOT NULL
                    UNION
                    SELECT departement FROM employes
                     WHERE departement IS NOT NULL AND departement <> ''
                ) x
            """)
            total_departements = cur.fetchone()['total'] or 0
        else:
            total_departements = 0 if scope_vide else 1

        # === Présences et temps ============================================
        cur.execute(f"""
            SELECT p.*, e.nom, e.prenom
              FROM presences p JOIN employes e ON e.id = p.employe_id
             WHERE p.date = %s AND {emp_where}
        """, [today] + emp_params)
        presences_today = cur.fetchall()
        teletravail = 0
        presents = 0
        retards_aujourdhui = []
        total_retards_minutes = 0
        for presence in presences_today:
            statut = (presence.get('statut') or 'présent').lower()
            if statut == 'télétravail':
                teletravail += 1
            elif statut != 'absent':
                presents += 1
            retard = calculer_retard(presence.get('heure_arrivee'))
            if retard > 0 and statut != 'télétravail':
                presence['retard_minutes'] = retard
                retards_aujourdhui.append(presence)
                total_retards_minutes += retard

        total_presences_aujourdhui = presents + teletravail
        presences_stat = {
            'present': presents,
            'absent': max(0, total_employes - presents - teletravail),
            'teletravail': teletravail,
        }
        taux_presence = round(
            total_presences_aujourdhui / total_employes * 100, 1
        ) if total_employes else 0

        cur.execute(f"""
            SELECT COALESCE(SUM(EXTRACT(EPOCH FROM
                       (p.heure_depart - p.heure_arrivee)) / 3600), 0) AS heures
              FROM presences p JOIN employes e ON e.id = p.employe_id
             WHERE p.heure_arrivee IS NOT NULL AND p.heure_depart IS NOT NULL
               AND DATE_TRUNC('month', p.date) = DATE_TRUNC('month', CURRENT_DATE)
               AND {emp_where}
        """, emp_params)
        heures_totales = round(float(cur.fetchone()['heures'] or 0), 1)

        # === Congés, permissions et soldes ================================
        cur.execute(f"""
            SELECT COUNT(*) FILTER (WHERE c.statut IN ('en attente','en_attente','avis rendu')) AS en_attente,
                   COUNT(*) FILTER (WHERE c.statut = 'approuvé'
                         AND EXTRACT(YEAR FROM c.date_debut) = %s) AS approuve,
                   COUNT(*) FILTER (WHERE c.statut = 'refusé'
                         AND EXTRACT(YEAR FROM c.date_debut) = %s) AS refuse,
                   COALESCE(SUM(c.nombre_jours) FILTER (WHERE c.statut = 'approuvé'
                         AND EXTRACT(YEAR FROM c.date_debut) = %s), 0) AS jours_approuves
              FROM conges c JOIN employes e ON e.id = c.employe_id
             WHERE {emp_where}
        """, [annee, annee, annee] + emp_params)
        conges_row = cur.fetchone()
        conges_stat = {
            'en_attente': conges_row['en_attente'] or 0,
            'approuve': conges_row['approuve'] or 0,
            'refuse': conges_row['refuse'] or 0,
            'jours_approuves': float(conges_row['jours_approuves'] or 0),
        }

        cur.execute(f"""
            SELECT COUNT(*) FILTER (WHERE p.statut IN ('en attente','en_attente','avis rendu')) AS en_attente,
                   COUNT(*) FILTER (WHERE p.statut = 'approuvé'
                         AND EXTRACT(YEAR FROM p.date_debut) = %s) AS approuve,
                   COUNT(*) FILTER (WHERE p.statut = 'refusé'
                         AND EXTRACT(YEAR FROM p.date_debut) = %s) AS refuse
              FROM permissions p JOIN employes e ON e.id = p.employe_id
             WHERE {emp_where}
        """, [annee, annee] + emp_params)
        permission_row = cur.fetchone()
        permissions_stat = {
            'en_attente': permission_row['en_attente'] or 0,
            'approuve': permission_row['approuve'] or 0,
            'refuse': permission_row['refuse'] or 0,
        }

        cur.execute(f"""
            SELECT COALESCE(SUM(s.jours_acquis - s.jours_utilises), 0) AS restant
              FROM soldes_conges s JOIN employes e ON e.id = s.employe_id
             WHERE s.annee = %s AND {emp_where}
        """, [annee] + emp_params)
        solde_conges_restant = round(float(cur.fetchone()['restant'] or 0), 1)

        # === Absences et justificatifs =====================================
        cur.execute(f"""
            SELECT COUNT(*) FILTER (WHERE EXTRACT(YEAR FROM a.date) = %s) AS total,
                   COUNT(*) FILTER (WHERE EXTRACT(YEAR FROM a.date) = %s
                       AND COALESCE(a.statut,'non_justifiee') IN ('non_justifiee','refusee')) AS a_regulariser,
                   COUNT(*) FILTER (WHERE a.statut = 'justificatif_depose') AS justificatifs_attente,
                   COUNT(*) FILTER (WHERE EXTRACT(YEAR FROM a.date) = %s
                       AND a.statut = 'acceptee') AS acceptees
              FROM absences a JOIN employes e ON e.id = a.employe_id
             WHERE {emp_where}
        """, [annee, annee, annee] + emp_params)
        absence_row = cur.fetchone()
        absences_stat = {
            'total': absence_row['total'] or 0,
            'a_regulariser': absence_row['a_regulariser'] or 0,
            'justificatifs_attente': absence_row['justificatifs_attente'] or 0,
            'acceptees': absence_row['acceptees'] or 0,
        }

        # === Documents =====================================================
        cur.execute(f"""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE d.date_expiration < CURRENT_DATE) AS expires,
                   COUNT(*) FILTER (WHERE d.date_expiration BETWEEN CURRENT_DATE
                                      AND CURRENT_DATE + INTERVAL '30 days') AS expirent_bientot
              FROM documents d LEFT JOIN employes e ON e.id = d.employe_id
             WHERE {emp_where}
        """, emp_params)
        document_row = cur.fetchone()
        documents_stat = {
            'total': document_row['total'] or 0,
            'expires': document_row['expires'] or 0,
            'expirent_bientot': document_row['expirent_bientot'] or 0,
        }

        # === Matériels, parc et attributions ===============================
        dept_where, dept_params = scope_departement('d')
        cur.execute(f"""
            SELECT COUNT(*) AS articles,
                   COALESCE(SUM(m.quantite), 0) AS stock_total,
                   COUNT(*) FILTER (WHERE m.seuil_alerte > 0
                                      AND m.quantite <= m.seuil_alerte) AS alertes_stock,
                   COALESCE(SUM(
                     CASE WHEN COALESCE(m.suivi_unitaire, FALSE) THEN
                       COALESCE((SELECT SUM(COALESCE(ex.prix_acquisition,
                                                    m.prix_acquisition, 0))
                                   FROM materiel_exemplaires ex
                                  WHERE ex.materiel_id = m.id AND ex.etat <> 'rebut'), 0)
                     ELSE COALESCE(m.prix_acquisition, 0) * m.quantite END
                   ), 0) AS valeur_parc
              FROM materiels m LEFT JOIN departements d ON d.id = m.departement_id
             WHERE {dept_where}
        """, dept_params)
        materiel_row = cur.fetchone()
        cur.execute(f"""
            SELECT COUNT(*) AS total
              FROM materiels_attributions a
              JOIN materiels m ON m.id = a.materiel_id
              LEFT JOIN departements d ON d.id = m.departement_id
             WHERE a.date_retour IS NULL AND {dept_where}
        """, dept_params)
        attributions_actives = cur.fetchone()['total'] or 0
        materiels_stat = {
            'articles': materiel_row['articles'] or 0,
            'stock_total': materiel_row['stock_total'] or 0,
            'alertes_stock': materiel_row['alertes_stock'] or 0,
            'valeur_parc': float(materiel_row['valeur_parc'] or 0),
            'attributions_actives': attributions_actives,
        }

        # === Maintenance ===================================================
        cur.execute(f"""
            SELECT COUNT(*) FILTER (WHERE mt.statut IN ('signale','assigne','envoye','a_valider')) AS ouvertes,
                   COUNT(*) FILTER (WHERE mt.statut = 'a_valider') AS a_valider,
                   COALESCE(SUM(mt.cout) FILTER (
                       WHERE EXTRACT(YEAR FROM mt.date_signalement) = %s), 0) AS cout_annee
              FROM materiel_maintenances mt
              JOIN materiel_exemplaires ex ON ex.id = mt.exemplaire_id
              JOIN materiels m ON m.id = ex.materiel_id
              LEFT JOIN departements d ON d.id = m.departement_id
             WHERE {dept_where}
        """, [annee] + dept_params)
        maintenance_row = cur.fetchone()
        maintenances_stat = {
            'ouvertes': maintenance_row['ouvertes'] or 0,
            'a_valider': maintenance_row['a_valider'] or 0,
            'cout_annee': float(maintenance_row['cout_annee'] or 0),
        }

        # === Inventaires ===================================================
        cur.execute(f"""
            SELECT COUNT(DISTINCT i.id) FILTER (WHERE i.statut = 'en_cours') AS en_cours,
                   COUNT(il.id) FILTER (WHERE il.quantite_comptee IS NOT NULL
                       AND il.quantite_comptee <> il.quantite_theorique) AS ecarts
              FROM inventaires i
              LEFT JOIN inventaire_lignes il ON il.inventaire_id = i.id
              LEFT JOIN departements d ON d.id = i.departement_id
             WHERE {dept_where}
        """, dept_params)
        inventaire_row = cur.fetchone()
        inventaires_stat = {
            'en_cours': inventaire_row['en_cours'] or 0,
            'ecarts': inventaire_row['ecarts'] or 0,
        }

        # === Accès, audit et messagerie (vue globale uniquement) ===========
        systeme_stat = None
        if vue_globale:
            cur.execute("""
                SELECT (SELECT COUNT(*) FROM users) AS utilisateurs,
                       (SELECT COUNT(*) FROM sessions_actives
                         WHERE revoked_at IS NULL
                           AND last_seen > CURRENT_TIMESTAMP - INTERVAL '1 hour') AS sessions_actives,
                       (SELECT COUNT(*) FROM audit_logs
                         WHERE timestamp::date = CURRENT_DATE) AS actions_audit,
                       (SELECT COUNT(*) FROM notifications
                         WHERE is_read = FALSE) AS notifications_non_lues,
                       (SELECT COUNT(*) FROM email_outbox
                         WHERE statut = 'en_attente') AS emails_attente,
                       (SELECT COUNT(*) FROM email_outbox
                         WHERE statut = 'echec') AS emails_echec
            """)
            systeme_stat = cur.fetchone()

        # === Répartition et activité récente ===============================
        cur.execute(f"""
            SELECT COALESCE(NULLIF(e.departement,''), 'Sans département') AS nom,
                   COUNT(*) AS nb_employes
              FROM employes e WHERE {emp_where}
             GROUP BY COALESCE(NULLIF(e.departement,''), 'Sans département')
             ORDER BY nb_employes DESC LIMIT 8
        """, emp_params)
        dept_stats = []
        for row in cur.fetchall():
            pct = round(row['nb_employes'] / total_employes * 100, 1) if total_employes else 0
            dept_stats.append({'nom': row['nom'], 'nb_employes': row['nb_employes'],
                               'pourcentage': pct})

        cur.execute(f"""
            SELECT p.*, e.nom, e.prenom FROM presences p
            JOIN employes e ON e.id = p.employe_id
            WHERE {emp_where} ORDER BY p.date DESC, p.id DESC LIMIT 5
        """, emp_params)
        recent_presences = cur.fetchall()
        cur.execute(f"""
            SELECT c.*, e.nom, e.prenom FROM conges c
            JOIN employes e ON e.id = c.employe_id
            WHERE {emp_where} ORDER BY c.date_demande DESC, c.id DESC LIMIT 5
        """, emp_params)
        recent_conges = cur.fetchall()

        # === Graphiques ====================================================
        if vue_globale:
            tendance_expr, tendance_params = 'COUNT(p.id)', []
        elif scope_vide:
            tendance_expr, tendance_params = '0', []
        else:
            tendance_expr = 'COUNT(p.id) FILTER (WHERE e.departement = %s)'
            tendance_params = [departement]
        cur.execute(f"""
            SELECT serie::date AS jour, {tendance_expr} AS nb
              FROM generate_series(CURRENT_DATE - INTERVAL '29 days', CURRENT_DATE,
                                   INTERVAL '1 day') AS serie
              LEFT JOIN presences p ON p.date = serie::date
              LEFT JOIN employes e ON e.id = p.employe_id
             GROUP BY serie ORDER BY serie
        """, tendance_params)
        tendance_rows = cur.fetchall()

    chart_tendance = {
        'labels': [r['jour'].strftime('%d/%m') for r in tendance_rows],
        'valeurs': [r['nb'] for r in tendance_rows],
    }
    chart_presences_jour = {
        'labels': ['Présent', 'Absent', 'Télétravail'],
        'valeurs': [presences_stat['present'], presences_stat['absent'],
                    presences_stat['teletravail']],
    }
    chart_conges = {
        'labels': ['En attente', 'Approuvés', 'Refusés'],
        'valeurs': [conges_stat['en_attente'], conges_stat['approuve'],
                    conges_stat['refuse']],
    }
    chart_departements = {
        'labels': [d['nom'] for d in dept_stats],
        'valeurs': [d['nb_employes'] for d in dept_stats],
    }

    return render_template(
        'dashboard.html', dashboard_scope=dashboard_scope,
        peut_voir_salaires=vue_globale, total_employes=total_employes,
        total_departements=total_departements, salaire_moyen=salaire_moyen,
        anciennete_moyenne=anciennete_moyenne,
        total_presences_aujourdhui=total_presences_aujourdhui,
        today=today, presences_stat=presences_stat, taux_presence=taux_presence,
        retards_aujourdhui=retards_aujourdhui, nb_retards=len(retards_aujourdhui),
        total_retards_minutes=total_retards_minutes, heures_totales=heures_totales,
        conges_stat=conges_stat, permissions_stat=permissions_stat,
        solde_conges_restant=solde_conges_restant, absences_stat=absences_stat,
        documents_stat=documents_stat, materiels_stat=materiels_stat,
        maintenances_stat=maintenances_stat, inventaires_stat=inventaires_stat,
        systeme_stat=systeme_stat,
        dept_stats=dept_stats, recent_presences=recent_presences,
        recent_conges=recent_conges, chart_tendance=chart_tendance,
        chart_presences_jour=chart_presences_jour, chart_conges=chart_conges,
        chart_departements=chart_departements,
        modules_couverts=12 if vue_globale else 9,
    )

# ==================== PRÉSENCES ==============================================
# Routes extraites dans blueprints/presences.py.

@app.route('/conges')
@login_required
def conges():
    conn = get_db()
    cur = get_cursor(conn)
    scope_where, scope_params = department_scope_sql('e', cur=cur)

    search = request.args.get('search', '').strip()
    statut = request.args.get('statut', '').strip()
    type_conge = request.args.get('type_conge', '').strip()
    date_debut = request.args.get('date_debut', '').strip()
    date_fin = request.args.get('date_fin', '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 10

    where = f" AND {scope_where}"
    params = list(scope_params)
    if search:
        where += " AND (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s)"
        params += [f"%{search.lower()}%", f"%{search.lower()}%"]
    if statut:
        where += " AND c.statut = %s"; params.append(statut)
    if type_conge:
        where += " AND c.type_conge = %s"; params.append(type_conge)
    if date_debut:
        where += " AND c.date_debut >= %s"; params.append(date_debut)
    if date_fin:
        where += " AND c.date_fin <= %s"; params.append(date_fin)

    from_ = "conges c JOIN employes e ON c.employe_id = e.id"
    cur.execute(f"SELECT COUNT(*) AS nb FROM {from_} WHERE 1=1{where}", params)
    total = cur.fetchone()['nb']
    pg = pagination_info(total, page, per_page)
    offset = (pg['page'] - 1) * per_page
    cur.execute(f"SELECT c.*, e.nom, e.prenom FROM {from_} WHERE 1=1{where} ORDER BY c.date_demande DESC LIMIT %s OFFSET %s", params + [per_page, offset])
    conges_list = cur.fetchall()

    cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom",
                scope_params)
    employees = cur.fetchall()

    soldes = {}
    annee_courante = datetime.now().year
    if session.get('role') in ['admin', 'rh', 'manager']:
        for emp in employees:
            s = get_solde_conges(emp['id'], annee_courante)
            s['nom'] = f"{emp['prenom']} {emp['nom']}"
            soldes[emp['id']] = s

    cur.execute(f"""SELECT DISTINCT c.type_conge FROM conges c
                    JOIN employes e ON e.id = c.employe_id
                    WHERE c.type_conge IS NOT NULL AND {scope_where}
                    ORDER BY c.type_conge""", scope_params)
    types = [r['type_conge'] for r in cur.fetchall()]

    cur.close()
    conn.close()
    filters = {'search': search, 'statut': statut, 'type_conge': type_conge, 'date_debut': date_debut, 'date_fin': date_fin}
    return render_template('conges.html', conges=conges_list, employees=employees, soldes=soldes,
                           annee_courante=annee_courante, types=types, filters=filters,
                           libelles=DEMANDE_LIBELLES, ouvertes=DEMANDE_OUVERTES,
                           peut_decider=_peut_decider_rh(), peut_avis=_peut_donner_avis(),
                           pg=pg, page_items=page_list(pg['page'], pg['pages']),
                           base_qs=urlencode({k: v for k, v in filters.items() if v}))

@app.route('/conges/add', methods=['GET', 'POST'])
@login_required
def add_conge():
    """Dépôt d'une demande de congé.

    Un employé ne peut déposer que pour lui-même ; un gestionnaire peut saisir
    pour n'importe qui (cas des demandes transmises sur papier).
    """
    moi = get_current_employee()
    gestionnaire = session.get('role') in ('admin', 'rh', 'manager')

    with db_cursor(commit=True) as (conn, cur):
        scope_where, scope_params = department_scope_sql('e', cur=cur)
        if request.method == 'POST':
            employe_id = request.form.get('employe_id', type=int)
            type_conge = request.form.get('type_conge')
            date_debut = request.form.get('date_debut')
            date_fin = request.form.get('date_fin')
            motif = request.form.get('motif', '')

            # Un non-gestionnaire ne dépose que pour lui-même : on ignore
            # l'employe_id transmis, qui pourrait être falsifié.
            if not gestionnaire:
                if not moi:
                    flash("Aucun employé n'est lié à votre compte : "
                          "contactez les RH.", "warning")
                    return redirect(url_for('self_service'))
                employe_id = moi['id']

            if employe_id and type_conge and date_debut and date_fin:
                d1 = datetime.strptime(date_debut, '%Y-%m-%d')
                d2 = datetime.strptime(date_fin, '%Y-%m-%d')
                if d2 < d1:
                    flash("La date de fin ne peut pas être avant la date de début", "danger")
                    cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom", scope_params)
                    return render_template('conge_form.html', employees=cur.fetchall(),
                                           moi=moi, gestionnaire=gestionnaire)
                nombre_jours = (d2 - d1).days + 1

                # Contrôle du solde : on prévient avant d'engager le circuit,
                # plutôt que de faire refuser la demande trois jours plus tard.
                annee = d1.year
                cur.execute(Q_SOLDE, (employe_id, annee))
                solde = cur.fetchone()
                # Seuls les congés payés sont décomptés du solde (cf. recalculer_solde).
                if solde and type_conge == 'congé payé':
                    restant = float(solde['jours_acquis'] or 0) - float(solde['jours_utilises'] or 0)
                    if nombre_jours > restant:
                        flash("Solde insuffisant : %s jour(s) demandé(s) pour %.1f restant(s) en %s."
                              % (nombre_jours, restant, annee), "danger")
                        cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom", scope_params)
                        return render_template('conge_form.html', employees=cur.fetchall(),
                                               moi=moi, gestionnaire=gestionnaire)

                cur.execute("""
                    INSERT INTO conges (employe_id, type_conge, date_debut, date_fin,
                                        nombre_jours, motif, statut, demande_par_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """, (employe_id, type_conge, date_debut, date_fin, nombre_jours,
                      motif, DEMANDE_EN_ATTENTE, session.get('user_id')))
                cid = cur.fetchone()['id']

                # Étape 1 : router vers le manager du département.
                nom, dept = _libelle_employe(cur, employe_id)
                titre = "Demande de congé : %s" % nom
                corps = ("%s jour(s) du %s au %s. Votre avis est attendu."
                         % (nombre_jours, d1.strftime('%d/%m/%Y'), d2.strftime('%d/%m/%Y')))
                managers = _managers_du_departement(cur, dept)
                for m in managers:
                    create_notification(m['id'], titre, corps, 'info', cur=cur)
                    queue_email(m.get('email'), titre, corps, cur=cur,
                                event_key=f"conge-a-traiter:{cid}:{m['id']}")
                if not managers:
                    # Aucun manager sur ce département : le RH traite directement.
                    message_rh = corps + " (aucun manager sur ce département)"
                    _notifier_roles(cur, ('admin', 'rh'), titre, message_rh,
                                    'info', sauf=session.get('user_id'))
                    _envoyer_roles(cur, ('admin', 'rh'), titre, message_rh,
                                   f"conge-a-traiter:{cid}")

                log_action(session.get('user_id'), session.get('username'),
                           "Demande de congé", "conge", cid, f"{nombre_jours} j")
                flash("Demande de congé soumise. Vous serez notifié à chaque étape.", "success")
                return redirect(url_for('self_service_conges') if not gestionnaire
                                else url_for('conges'))
            else:
                flash("Veuillez remplir tous les champs obligatoires", "danger")

        cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom", scope_params)
        employees = cur.fetchall()
    return render_template('conge_form.html', employees=employees,
                           moi=moi, gestionnaire=gestionnaire)


@app.route('/conges/avis/<int:id>', methods=['POST'])
@login_required
@role_required('admin', 'rh', 'manager')
def avis_conge(id):
    """Étape 2 : le manager du département rend son avis (non décisionnel)."""
    avis = (request.form.get('avis') or '').strip()
    commentaire = (request.form.get('commentaire') or '').strip()
    if avis not in ('favorable', 'defavorable'):
        flash("Avis invalide.", "danger")
        return redirect(url_for('conges'))
    if avis == 'defavorable' and not commentaire:
        flash("Merci de motiver un avis défavorable.", "danger")
        return redirect(url_for('conges'))

    with db_cursor(commit=True) as (conn, cur):
        cur.execute("SELECT * FROM conges WHERE id = %s", (id,))
        c = cur.fetchone()
        if not c:
            flash("Demande introuvable.", "danger")
            return redirect(url_for('conges'))
        if c['statut'] not in DEMANDE_OUVERTES:
            flash("Cette demande est déjà tranchée.", "warning")
            return redirect(url_for('conges'))

        cur.execute("""UPDATE conges SET statut = %s, avis_manager = %s,
                          avis_manager_par = %s, avis_manager_le = CURRENT_DATE,
                          avis_commentaire = %s
                       WHERE id = %s""",
                    (DEMANDE_AVIS_RENDU, avis, session.get('username'),
                     commentaire or None, id))

        nom, _ = _libelle_employe(cur, c['employe_id'])
        sujet_rh = "Congé à décider : %s" % nom
        message_rh = ("Avis %s du manager %s. %s"
                      % (avis, session.get('username'), commentaire[:120]))
        _notifier_roles(cur, ('admin', 'rh'), sujet_rh, message_rh,
                        'info', sauf=session.get('user_id'))
        _envoyer_roles(cur, ('admin', 'rh'), sujet_rh, message_rh,
                       f"conge-a-decider:{id}")
        uid = _user_id_de_employe(cur, c['employe_id'])
        if uid:
            create_notification(uid, "Votre demande de congé avance",
                                "Votre manager a rendu un avis %s. "
                                "La décision RH suit." % avis, 'info')

    log_action(session.get('user_id'), session.get('username'),
               "Avis manager congé", "conge", id, avis)
    flash("Avis enregistré : les RH vont trancher.", "success")
    return redirect(url_for('conges'))


@app.route('/conges/update/<int:id>', methods=['POST'])
@login_required
@role_required('admin', 'rh')
def update_conge(id):
    """Étape 3 : décision finale des RH (approbation ou refus motivé)."""
    action = request.form.get('action')
    motif_refus = (request.form.get('motif_refus') or '').strip()
    if action == 'refuser' and not motif_refus:
        flash("Merci d'indiquer le motif du refus : l'employé en sera informé.", "danger")
        return redirect(url_for('conges'))

    with db_cursor(commit=True) as (conn, cur):
        cur.execute("SELECT * FROM conges WHERE id = %s", (id,))
        c = cur.fetchone()
        if not c:
            flash("Demande introuvable.", "danger")
            return redirect(url_for('conges'))
        if c['statut'] not in DEMANDE_OUVERTES:
            flash("Cette demande est déjà tranchée.", "warning")
            return redirect(url_for('conges'))

        annee = datetime.strptime(str(c['date_debut']), '%Y-%m-%d').year

        if action == 'approuver':
            cur.execute("""UPDATE conges SET statut = %s, decide_par = %s,
                              decide_le = CURRENT_DATE, motif_refus = NULL
                           WHERE id = %s""",
                        (DEMANDE_APPROUVEE, session.get('username'), id))
            recalculer_solde(c['employe_id'], annee, cur=cur)
            _notifier_employe_evenement(
                cur, c['employe_id'], "Congé approuvé",
                "Votre congé du %s au %s est approuvé."
                % (c['date_debut'], c['date_fin']), 'success',
                cle_evenement=f"conge-decision:{id}:approuve",
            )
            flash("Congé approuvé et solde mis à jour", "success")
        elif action == 'refuser':
            cur.execute("""UPDATE conges SET statut = %s, decide_par = %s,
                              decide_le = CURRENT_DATE, motif_refus = %s
                           WHERE id = %s""",
                        (DEMANDE_REFUSEE, session.get('username'), motif_refus, id))
            recalculer_solde(c['employe_id'], annee, cur=cur)
            _notifier_employe_evenement(
                cur, c['employe_id'], "Congé refusé",
                "Votre demande du %s au %s a été refusée : %s"
                % (c['date_debut'], c['date_fin'], motif_refus), 'danger',
                cle_evenement=f"conge-decision:{id}:refuse",
            )
            flash("Congé refusé : l'employé est informé du motif.", "info")
        else:
            flash("Action inconnue.", "danger")
            return redirect(url_for('conges'))

    log_action(session.get('user_id'), session.get('username'),
               "Décision congé", "conge", id, action)
    return redirect(url_for('conges'))


@app.route('/conges/<int:id>/annuler', methods=['POST'])
@login_required
def annuler_conge(id):
    """L'employé retire sa demande tant qu'elle n'est pas tranchée."""
    with db_cursor(commit=True) as (conn, cur):
        cur.execute("SELECT * FROM conges WHERE id = %s", (id,))
        c = cur.fetchone()
        if not c:
            flash("Demande introuvable.", "danger")
            return redirect(url_for('self_service_conges'))
        moi = get_current_employee()
        est_le_mien = moi and moi['id'] == c['employe_id']
        if not (est_le_mien or _peut_decider_rh()):
            flash("Vous ne pouvez annuler que vos propres demandes.", "danger")
            return redirect(url_for('self_service_conges'))
        if c['statut'] not in DEMANDE_OUVERTES:
            flash("Cette demande est déjà tranchée : elle ne peut plus être annulée.",
                  "warning")
            return redirect(url_for('self_service_conges') if est_le_mien
                            else url_for('conges'))

        cur.execute("""UPDATE conges SET statut = %s, annule_par = %s
                       WHERE id = %s""",
                    (DEMANDE_ANNULEE, session.get('username'), id))
        nom, dept = _libelle_employe(cur, c['employe_id'])
        for m in _managers_du_departement(cur, dept):
            create_notification(m['id'], "Demande de congé annulée",
                                "%s a retiré sa demande du %s au %s."
                                % (nom, c['date_debut'], c['date_fin']), 'info')

    log_action(session.get('user_id'), session.get('username'),
               "Annulation congé", "conge", id, None)
    flash("Demande annulée.", "success")
    return redirect(url_for('self_service_conges') if est_le_mien else url_for('conges'))


@app.route('/conges/delete/<int:id>', methods=['POST'])
@login_required
@role_required('admin', 'rh', 'manager')
def delete_conge(id):
    with db_cursor(commit=True) as (conn, cur):
        cur.execute("SELECT id FROM absences WHERE conge_id = %s", (id,))
        if cur.fetchone():
            flash("Ce congé maladie provient d'un justificatif accepté et ne peut pas être supprimé.",
                  "warning")
            return redirect(url_for('conges'))
        cur.execute("DELETE FROM conges WHERE id = %s", (id,))
    flash("Demande de congé supprimée", "success")
    return redirect(url_for('conges'))


# ==================== ABSENCES NON JUSTIFIÉES ====================
# Jours ouvrés pris en compte pour la génération automatique des absences
# (0 = lundi ... 6 = dimanche). Par défaut : lundi → vendredi.
JOURS_OUVRES = {0, 1, 2, 3, 4}

# Fenêtre max de génération (en jours). Borné pour rester rapide et léger en
# mémoire sur les hébergements contraints (Render : timeout worker ~30 s,
# RAM 512 Mo). Ajustable.
LOOKBACK_JOURS = 365

def _dates_couvertes(cur, employe_id):
    """Ensemble des dates qui ne doivent PAS être comptées comme une absence
    pour un employé : jours avec présence + jours couverts par un congé
    approuvé + jours couverts par une permission approuvée."""
    couverts = set()

    # Présences enregistrées
    cur.execute("SELECT date FROM presences WHERE employe_id = %s", (employe_id,))
    for row in cur.fetchall():
        couverts.add(row['date'])

    # Congés approuvés (tous types de congé)
    cur.execute("""
        SELECT date_debut, date_fin FROM conges
        WHERE employe_id = %s AND statut = 'approuvé'
    """, (employe_id,))
    for row in cur.fetchall():
        d, fin = row.get('date_debut'), row.get('date_fin')
        if not d or not fin:
            continue  # plage incomplète (dates NULL) : on l'ignore
        while d <= fin:
            couverts.add(d)
            d += timedelta(days=1)

    # Permissions approuvées (module séparé, mais un jour de permission
    # approuvée n'est pas non plus une absence non justifiée)
    cur.execute("""
        SELECT date_debut, date_fin FROM permissions
        WHERE employe_id = %s AND statut = 'approuvé'
    """, (employe_id,))
    for row in cur.fetchall():
        d, fin = row.get('date_debut'), row.get('date_fin')
        if not d or not fin:
            continue
        while d <= fin:
            couverts.add(d)
            d += timedelta(days=1)

    return couverts


def generer_absences_automatiques(cur, date_jusqua=None, date_depuis=None,
                                  departement=None):
    """Enregistre automatiquement dans `absences` chaque jour ouvré passé SANS
    présence (et non couvert par un congé/permission approuvé) = « tout jour où
    l'employé n'a pas de présence ».

    Règles : jour ouvré (lun→ven) ; >= date d'embauche ; <= la veille ; aucune
    présence ; non couvert par un congé ou une permission approuvés.

    IMPORTANT (perf/mémoire) : la génération est BORNÉE à une fenêtre récente
    (LOOKBACK_JOURS) et les insertions sont envoyées par LOTS via execute_values
    (1 requête par lot au lieu d'1 par ligne). Sans cela, sur un hébergement
    contraint (Render : timeout worker 30 s, RAM 512 Mo), la génération depuis
    la date d'embauche + executemany => timeout worker + kill OOM => HTTP 500.
    Idempotent (UNIQUE(employe_id, date)). Retourne le nb d'absences créées.
    """
    if date_jusqua is None:
        date_jusqua = date.today() - timedelta(days=1)  # on exclut aujourd'hui
    if date_depuis is None:
        date_depuis = date_jusqua - timedelta(days=LOOKBACK_JOURS - 1)

    def _to_date(v):
        return v if isinstance(v, date) else datetime.strptime(str(v), '%Y-%m-%d').date()

    fin_globale = _to_date(date_jusqua)
    debut_global = _to_date(date_depuis)

    if departement is None:
        cur.execute("SELECT id, date_embauche FROM employes ORDER BY id")
    else:
        cur.execute("""SELECT id, date_embauche FROM employes
                       WHERE departement = %s ORDER BY id""", (departement,))
    employes = cur.fetchall()

    nb_creees = 0
    motif_auto = "Aucune présence enregistrée (généré automatiquement)"

    for emp in employes:
        embauche = emp['date_embauche']
        if not embauche:
            continue  # sans date d'embauche, période indéterminée
        debut = max(debut_global, _to_date(embauche))
        if debut > fin_globale:
            continue

        couverts = _dates_couvertes(cur, emp['id'])

        cur.execute(
            "SELECT date FROM absences WHERE employe_id = %s AND date BETWEEN %s AND %s",
            (emp['id'], debut, fin_globale),
        )
        deja = {row['date'] for row in cur.fetchall()}
        # Absences supprimées manuellement : ne JAMAIS les régénérer
        cur.execute("SELECT date FROM absences_exclues WHERE employe_id = %s", (emp['id'],))
        deja |= {row['date'] for row in cur.fetchall()}

        valeurs = []
        jour = debut
        while jour <= fin_globale:
            if jour.weekday() in JOURS_OUVRES and jour not in couverts and jour not in deja:
                valeurs.append((emp['id'], jour, motif_auto))
            jour += timedelta(days=1)

        if valeurs:
            execute_values(
                cur,
                "INSERT INTO absences (employe_id, date, motif) "
                "VALUES %s ON CONFLICT (employe_id, date) DO NOTHING",
                valeurs,
                page_size=500,
            )
            nb_creees += len(valeurs)

    return nb_creees


SEUIL_ALERTE_EXPIRATION_DOCUMENTS_JOURS = 30


def job_alertes_expiration_documents():
    """Job planifié (1x/jour) : alerte RH/admin (+ l'employé concerné) pour
    les documents dont la date d'expiration approche ou est dépassée
    (CDD, visa, certification, contrat...).

    Deux alertes possibles par document, chacune envoyée UNE SEULE FOIS
    (table `documents_alertes`) :
      - 'bientot' : expire dans <= 30 jours (mais pas encore expiré)
      - 'expire'  : date d'expiration dépassée
    """
    with app.app_context():
        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("""
                    INSERT INTO scheduler_runs (job_name, run_date)
                    VALUES ('alertes_expiration_documents', CURRENT_DATE)
                    ON CONFLICT DO NOTHING
                    RETURNING job_name
                """)
                if cur.fetchone() is None:
                    logger.info("Job alertes expiration documents : déjà exécuté aujourd'hui, on saute.")
                    return

                cur.execute("""
                    SELECT d.id, d.titre, d.date_expiration, d.employe_id,
                           e.nom, e.prenom, e.email
                    FROM documents d
                    LEFT JOIN employes e ON d.employe_id = e.id
                    WHERE d.date_expiration IS NOT NULL
                      AND d.date_expiration <= CURRENT_DATE + INTERVAL %s
                """, (f"{SEUIL_ALERTE_EXPIRATION_DOCUMENTS_JOURS} days",))
                candidats = cur.fetchall()
                if not candidats:
                    return

                cur.execute("SELECT id, employe_id FROM users WHERE employe_id IS NOT NULL")
                user_id_par_employe = {u['employe_id']: u['id'] for u in cur.fetchall()}
                cur.execute("SELECT id FROM users WHERE role IN ('admin', 'rh')")
                ids_rh = [u['id'] for u in cur.fetchall()]

                a_notifier = []
                for d in candidats:
                    type_alerte = 'expire' if d['date_expiration'] <= date.today() else 'bientot'
                    cur.execute("""
                        INSERT INTO documents_alertes (document_id, type_alerte)
                        VALUES (%s, %s) ON CONFLICT DO NOTHING
                        RETURNING document_id
                    """, (d['id'], type_alerte))
                    if cur.fetchone() is None:
                        continue  # déjà alerté pour ce document + ce type
                    a_notifier.append((d, type_alerte))

                if not a_notifier:
                    return

                for d, type_alerte in a_notifier:
                    proprietaire = f"{d['prenom']} {d['nom']}" if d['nom'] else "document général"
                    if type_alerte == 'expire':
                        message = f"« {d['titre']} » ({proprietaire}) a expiré le {d['date_expiration']}."
                    else:
                        message = f"« {d['titre']} » ({proprietaire}) expire le {d['date_expiration']}."

                    for uid in ids_rh:
                        create_notification(uid, "Document arrivant à expiration" if type_alerte == 'bientot'
                                             else "Document expiré", message, "warning")

                    if d['employe_id']:
                        uid = user_id_par_employe.get(d['employe_id'])
                        if uid:
                            create_notification(uid, "Votre document arrive à expiration" if type_alerte == 'bientot'
                                                 else "Votre document a expiré", message, "warning",
                                                 cur=cur)
                        queue_email(
                            d.get('email'),
                            "Votre document arrive à expiration" if type_alerte == 'bientot'
                            else "Votre document a expiré",
                            message + " Contactez les RH pour son renouvellement.",
                            cur=cur,
                            event_key=f"document-expiration:{d['id']}:{type_alerte}",
                        )

                details = "\n".join(
                    f"- [{'EXPIRÉ' if t == 'expire' else 'bientôt'}] {d['titre']} "
                    f"({d['prenom']} {d['nom']} — {d['date_expiration']})" if d['nom']
                    else f"- [{'EXPIRÉ' if t == 'expire' else 'bientôt'}] {d['titre']} ({d['date_expiration']})"
                    for d, t in a_notifier
                )
                _envoyer_email_texte(
                    [get_admin_email()],
                    f"📄 {len(a_notifier)} document(s) à vérifier (expiration)",
                    "Bonjour,\n\nLes documents suivants nécessitent votre attention :\n\n"
                    f"{details}\n\nConsultez la page Documents pour les renouveler si besoin.",
                    event_key=f"documents-expiration:{date.today().isoformat()}"
                )
        except Exception:
            logger.exception("Erreur lors du job planifié d'alertes d'expiration de documents")



def _envoyer_email_texte(destinataires, sujet, corps, event_key=None):
    """Met en file un e-mail texte ; aucun appel SMTP n'a lieu ici."""
    try:
        for index, destinataire in enumerate(destinataires or []):
            queue_email(destinataire, sujet, corps,
                        event_key=f"{event_key}:{index}" if event_key else None)
        return True
    except Exception as e:
        logger.error("Erreur mise en file e-mail texte: %s", e, exc_info=True)
        return False


def job_generation_quotidienne_absences():
    """Job planifié (1x/jour, voir `demarrer_scheduler`) : génère les absences
    automatiques (remplace l'ancienne génération au chargement de la page, qui
    empêchait les suppressions de "tenir"), puis notifie RH/admin ainsi que
    chaque employé concerné.

    Idempotent via `scheduler_runs` : si le job a déjà tourné aujourd'hui
    (redémarrage du service, plusieurs workers gunicorn...), il ne s'exécute
    pas deux fois et n'envoie donc pas deux fois les mêmes notifications/emails.
    """
    with app.app_context():
        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("""
                    INSERT INTO scheduler_runs (job_name, run_date)
                    VALUES ('generation_absences', CURRENT_DATE)
                    ON CONFLICT DO NOTHING
                    RETURNING job_name
                """)
                if cur.fetchone() is None:
                    logger.info("Job génération absences : déjà exécuté aujourd'hui, on saute.")
                    return

                nb = generer_absences_automatiques(cur)
                logger.info("Job génération absences : %d absence(s) créée(s).", nb)
                if nb == 0:
                    return

                motif_auto = "Aucune présence enregistrée (généré automatiquement)"
                cur.execute("""
                    SELECT a.employe_id, a.date, e.nom, e.prenom, e.email
                    FROM absences a JOIN employes e ON a.employe_id = e.id
                    WHERE a.date_enregistrement::date = CURRENT_DATE AND a.motif = %s
                    ORDER BY a.date
                """, (motif_auto,))
                nouvelles = cur.fetchall()

                # Notifier chaque employé concerné
                cur.execute("SELECT id, employe_id FROM users WHERE employe_id IS NOT NULL")
                user_id_par_employe = {u['employe_id']: u['id'] for u in cur.fetchall()}
                for a in nouvelles:
                    uid = user_id_par_employe.get(a['employe_id'])
                    message_absence = (
                        f"Aucune présence relevée le {a['date']} — une absence a été "
                        "enregistrée automatiquement. Vous pouvez déposer un justificatif "
                        "depuis votre espace employé."
                    )
                    if uid:
                        create_notification(uid, "Absence enregistrée", message_absence,
                                            "warning", cur=cur)
                    queue_email(
                        a.get('email'), "Absence enregistrée", message_absence,
                        cur=cur,
                        event_key=f"absence-auto:{a['employe_id']}:{a['date']}",
                    )

                # Notifier RH/admin (résumé global)
                cur.execute("SELECT id FROM users WHERE role IN ('admin', 'rh')")
                for u in cur.fetchall():
                    create_notification(
                        u['id'], "Absences générées automatiquement",
                        f"{nb} nouvelle(s) absence(s) détectée(s) (aucune présence enregistrée). "
                        f"Vérifiez la page Absences.",
                        "info"
                    )

                # Email récapitulatif à l'admin
                details = "\n".join(f"- {a['prenom']} {a['nom']} : {a['date']}" for a in nouvelles)
                _envoyer_email_texte(
                    [get_admin_email()],
                    f"🚫 {nb} absence(s) générée(s) automatiquement",
                    "Bonjour,\n\n"
                    f"{nb} absence(s) ont été enregistrée(s) automatiquement cette nuit "
                    "(aucune présence relevée le jour ouvré concerné) :\n\n"
                    f"{details}\n\n"
                    "Vous pouvez les consulter et les corriger si besoin sur la page Absences."
                )
        except Exception:
            logger.exception("Erreur lors du job planifié de génération des absences")


def job_purge_sessions():
    """Efface les sessions expirées ou révoquées de plus de 30 jours.

    Sans cela, le registre grossit indéfiniment et le compteur de sessions
    ouvertes finit par inclure des navigateurs fermés depuis longtemps.
    """
    try:
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("""
                DELETE FROM sessions_actives
                 WHERE (revoked_at IS NOT NULL AND revoked_at < CURRENT_TIMESTAMP - INTERVAL '30 days')
                    OR (revoked_at IS NULL     AND last_seen  < CURRENT_TIMESTAMP - INTERVAL '30 days')
            """)
            n = cur.rowcount
        if n:
            logger.info("Purge des sessions : %s entrée(s) supprimée(s).", n)
    except Exception as e:
        logger.error("Erreur job_purge_sessions: %s", e, exc_info=True)


def job_validation_auto_maintenances():
    """Valide d'office les retours d'atelier restés sans réponse.

    Sans ce filet, une intervention resterait « en attente de validation »
    indéfiniment si le demandeur est absent, a quitté l'entreprise ou oublie
    simplement de répondre — et fausserait les indicateurs.
    """
    try:
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("""
                SELECT mt.id, mt.exemplaire_id, ex.numero_inventaire
                  FROM materiel_maintenances mt
                  JOIN materiel_exemplaires ex ON ex.id = mt.exemplaire_id
                 WHERE mt.statut = 'a_valider'
                   AND mt.date_retour IS NOT NULL
                   AND mt.date_retour < CURRENT_DATE - make_interval(days => %s)
            """, (MAINTENANCE_VALIDATION_JOURS,))
            echues = cur.fetchall()
            for mt in echues:
                cur.execute("""UPDATE materiel_maintenances
                               SET statut = 'repare', valide_par = 'système',
                                   date_validation = CURRENT_DATE,
                                   validation_forcee = TRUE,
                                   cloture_par = 'système'
                               WHERE id = %s""", (mt['id'],))
        if echues:
            logger.info("Validation automatique de %s intervention(s) : %s",
                        len(echues), ", ".join(m['numero_inventaire'] for m in echues))
    except Exception as e:
        logger.error("Erreur job_validation_auto_maintenances: %s", e, exc_info=True)


def job_recalcul_soldes_conges():
    """Job planifié (1x/jour, voir `demarrer_scheduler`) : recalcule le nombre
    de jours de congé acquis (`jours_acquis`) de chaque employé pour l'année en
    cours, selon l'accumulation mensuelle (~2,08 j/mois, proratisée pour les
    nouveaux embauchés). Ne touche jamais les soldes fixés manuellement par
    RH/admin (`jours_acquis_manuel = TRUE`).

    Tourner ce job une fois par jour suffit très largement (le résultat ne
    change qu'au changement de mois), mais c'est sans risque de le faire
    tourner plus souvent : le calcul est idempotent.
    """
    with app.app_context():
        try:
            with db_cursor(commit=True) as (conn, cur):
                annee_courante = datetime.now().year
                cur.execute("""
                    SELECT e.id, e.date_embauche
                    FROM employes e
                    LEFT JOIN soldes_conges s ON s.employe_id = e.id AND s.annee = %s
                    WHERE COALESCE(s.jours_acquis_manuel, FALSE) = FALSE
                """, (annee_courante,))
                employes = cur.fetchall()

                nb_mis_a_jour = 0
                for emp in employes:
                    nouveau_solde = calculer_jours_acquis_prorata(emp.get('date_embauche'), annee_courante)
                    cur.execute("""
                        INSERT INTO soldes_conges (employe_id, annee, jours_acquis, jours_utilises)
                        VALUES (%s, %s, %s, 0)
                        ON CONFLICT (employe_id, annee) DO UPDATE
                        SET jours_acquis = %s
                        WHERE soldes_conges.jours_acquis_manuel = FALSE
                    """, (emp['id'], annee_courante, nouveau_solde, nouveau_solde))
                    nb_mis_a_jour += 1
                logger.info("Job recalcul soldes congés : %d employé(s) mis à jour (accumulation mensuelle).",
                            nb_mis_a_jour)
        except Exception:
            logger.exception("Erreur lors du job planifié de recalcul des soldes de congés")


def job_traiter_file_emails():
    """Vide périodiquement l'outbox SMTP avec reprise et tentatives bornées."""
    if not app.config.get('EMAIL_ENABLED'):
        return {'traites': 0, 'envoyes': 0, 'replanifies': 0, 'echecs': 0}
    with app.app_context():
        resultat = traiter_outbox(
            db_cursor, _send_outbox_message, logger,
            taille_lot=app.config.get('EMAIL_BATCH_SIZE', 20),
            tentatives_max=app.config.get('EMAIL_MAX_ATTEMPTS', 5),
        )
        if resultat['traites']:
            logger.info("Outbox e-mail : %s", resultat)
        return resultat


def demarrer_scheduler():
    """Démarre le scheduler en tâche de fond (1 seul process gunicorn en
    production, cf. render.yaml). Avec le rechargeur Flask (FLASK_DEBUG=true en
    local), le module est importé deux fois : on ne démarre le job que dans le
    process qui sert réellement les requêtes (WERKZEUG_RUN_MAIN), pas dans le
    process de surveillance du rechargeur, pour éviter un double job.

    Si l'appli est un jour déployée avec plusieurs workers (gunicorn -w N), la
    table `scheduler_runs` empêche quand même toute double exécution/notif.
    """
    if os.environ.get('FLASK_ENV') == 'testing':
        return  # jamais de job planifié pendant les tests automatisés
    debug_actif = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    if debug_actif and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return
    scheduler = BackgroundScheduler(timezone='Indian/Antananarivo')
    scheduler.add_job(
        job_generation_quotidienne_absences, 'cron',
        hour=1, minute=0, id='generation_absences_quotidienne', replace_existing=True
    )
    scheduler.add_job(
        job_alertes_expiration_documents, 'cron',
        hour=1, minute=30, id='alertes_expiration_documents', replace_existing=True
    )
    scheduler.add_job(
        job_recalcul_soldes_conges, 'cron',
        hour=2, minute=0, id='recalcul_soldes_conges', replace_existing=True
    )
    scheduler.add_job(
        job_purge_sessions, 'cron',
        hour=3, minute=0, id='purge_sessions_actives', replace_existing=True
    )
    scheduler.add_job(
        job_validation_auto_maintenances, 'cron',
        hour=3, minute=30, id='validation_auto_maintenances', replace_existing=True
    )
    if app.config.get('EMAIL_ENABLED'):
        scheduler.add_job(
            job_traiter_file_emails, 'interval',
            seconds=max(15, app.config.get('EMAIL_POLL_SECONDS', 60)),
            id='traiter_file_emails', replace_existing=True,
            max_instances=1, coalesce=True,
        )
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    logger.info("Scheduler démarré : génération auto des absences (01h00) + alertes "
                "d'expiration de documents (01h30) + recalcul des soldes de congés "
                "(02h00), tous les jours.")


@app.route('/absences')
@login_required
@role_required('admin', 'rh', 'manager')
def absences():
    # NOTE : la génération automatique se faisait ici, à CHAQUE affichage de la
    # page. Conséquence : supprimer une absence "aucune présence enregistrée"
    # ne servait à rien, puisque la condition (toujours aucune présence ce
    # jour-là) redevenait vraie au rechargement suivant et la ligne était
    # aussitôt recréée. La génération se fait maintenant uniquement via le
    # bouton "Synchroniser" (route /absences/synchroniser), de façon explicite.

    search = request.args.get('search', '').strip()
    employe_id = request.args.get('employe_id', '').strip()
    date_debut = request.args.get('date_debut', '').strip()
    date_fin = request.args.get('date_fin', '').strip()
    statut = request.args.get('statut', '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 10

    where = ""
    params = []
    if search:
        where += " AND (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s)"
        params += [f"%{search.lower()}%", f"%{search.lower()}%"]
    if employe_id and employe_id.isdigit():
        where += " AND a.employe_id = %s"; params.append(int(employe_id))
    if date_debut:
        where += " AND a.date >= %s"; params.append(date_debut)
    if date_fin:
        where += " AND a.date <= %s"; params.append(date_fin)
    if statut in ABSENCE_STATUT_LABELS:
        where += " AND a.statut = %s"; params.append(statut)

    with db_cursor() as (conn, cur):
        scope_where, scope_params = department_scope_sql('e', cur=cur)
        where += f" AND {scope_where}"
        params += scope_params
        from_ = "absences a JOIN employes e ON a.employe_id = e.id"
        cur.execute(f"SELECT COUNT(*) AS nb FROM {from_} WHERE 1=1{where}", params)
        total = cur.fetchone()['nb']
        pg = pagination_info(total, page, per_page)
        offset = (pg['page'] - 1) * per_page
        cur.execute(f"SELECT a.*, e.nom, e.prenom FROM {from_} WHERE 1=1{where} ORDER BY a.date DESC LIMIT %s OFFSET %s", params + [per_page, offset])
        absences_list = cur.fetchall()
        cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom",
                    scope_params)
        employees = cur.fetchall()
    filters = {'search': search, 'employe_id': employe_id, 'date_debut': date_debut,
               'date_fin': date_fin, 'statut': statut}
    return render_template('absences.html', absences=absences_list, employees=employees,
                           nb_total=total, filters=filters,
                           statut_labels=ABSENCE_STATUT_LABELS,
                           pg=pg, page_items=page_list(pg['page'], pg['pages']),
                           base_qs=urlencode({k: v for k, v in filters.items() if v}))


@app.route('/absences/add', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'rh', 'manager')
def add_absence():
    if request.method == 'POST':
        employe_id = request.form.get('employe_id')
        date_val = request.form.get('date')
        motif = request.form.get('motif', '')
        if employe_id and date_val:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("""
                    INSERT INTO absences (employe_id, date, motif, enregistre_par)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (employe_id, date) DO UPDATE SET
                        motif = EXCLUDED.motif,
                        enregistre_par = EXCLUDED.enregistre_par
                """, (employe_id, date_val, motif, session.get('user_id')))
                message = (f"Une absence non justifiée a été enregistrée pour le {date_val}. "
                           "Vous pouvez déposer un justificatif depuis votre espace employé.")
                _notifier_employe_evenement(
                    cur, int(employe_id), "Absence enregistrée", message, 'warning',
                    cle_evenement=f"absence-manuelle:{employe_id}:{date_val}",
                )
            flash("Absence non justifiée enregistrée et employé informé", "success")
            return redirect(url_for('absences'))
        flash("Employé et date requis", "danger")
    with db_cursor() as (conn, cur):
        scope_where, scope_params = department_scope_sql('e', cur=cur)
        cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom",
                    scope_params)
        employees = cur.fetchall()
    return render_template('absence_form.html', employees=employees)


@app.route('/absences/delete/<int:id>', methods=['POST'])
@login_required
@role_required('admin', 'rh')
def delete_absence(id):
    with db_cursor(commit=True) as (conn, cur):
        # On mémorise la date supprimée pour empêcher la génération automatique
        # de la recréer immédiatement (sinon elle réapparaît au prochain affichage).
        cur.execute("SELECT employe_id, date, statut, conge_id FROM absences WHERE id = %s", (id,))
        row = cur.fetchone()
        if row and row.get('statut') == ABSENCE_ACCEPTEE:
            flash("Une absence déjà requalifiée en congé maladie ne peut pas être supprimée.",
                  "warning")
            return redirect(url_for('absences'))
        if row:
            cur.execute("""
                INSERT INTO absences_exclues (employe_id, date) VALUES (%s, %s)
                ON CONFLICT (employe_id, date) DO NOTHING
            """, (row['employe_id'], row['date']))
        cur.execute("DELETE FROM absences WHERE id = %s", (id,))
    flash("Absence supprimée", "success")
    return redirect(url_for('absences'))


@app.route('/absences/synchroniser', methods=['POST'])
@login_required
@role_required('admin', 'rh', 'manager')
def synchroniser_absences():
    """Recalcule explicitement toutes les absences (depuis la date d'embauche
    jusqu'à la veille) et renvoie le nombre d'absences nouvellement créées."""
    with db_cursor(commit=True) as (conn, cur):
        scope = get_department_scope(cur)
        if scope['is_global']:
            nb = generer_absences_automatiques(cur)
        elif scope['is_empty']:
            nb = 0
        else:
            nb = generer_absences_automatiques(cur,
                                               departement=scope['department'])
    if nb > 0:
        flash(f"{nb} absence(s) générée(s) automatiquement.", "success")
    else:
        flash("Les absences sont déjà à jour.", "info")
    return redirect(url_for('absences'))


# ==================== PERMISSIONS (MODULE SÉPARÉ, COMME LES CONGÉS) ====================
# Une permission est demandée / approuvée / refusée exactement comme un congé,
# mais elle vit dans sa propre table (`permissions`) et n'a AUCUN impact sur le
# solde de congés (`soldes_conges`).
@app.route('/permissions')
@login_required
def permissions():
    search = request.args.get('search', '').strip()
    statut = request.args.get('statut', '').strip()
    date_debut = request.args.get('date_debut', '').strip()
    date_fin = request.args.get('date_fin', '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 10

    where = ""
    params = []
    if search:
        where += " AND (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s)"
        params += [f"%{search.lower()}%", f"%{search.lower()}%"]
    if statut:
        where += " AND p.statut = %s"; params.append(statut)
    if date_debut:
        where += " AND p.date_debut >= %s"; params.append(date_debut)
    if date_fin:
        where += " AND p.date_fin <= %s"; params.append(date_fin)

    with db_cursor() as (conn, cur):
        scope_where, scope_params = department_scope_sql('e', cur=cur)
        where += f" AND {scope_where}"
        params += scope_params
        from_ = "permissions p JOIN employes e ON p.employe_id = e.id"
        cur.execute(f"SELECT COUNT(*) AS nb FROM {from_} WHERE 1=1{where}", params)
        total = cur.fetchone()['nb']
        pg = pagination_info(total, page, per_page)
        offset = (pg['page'] - 1) * per_page
        cur.execute(f"SELECT p.*, e.nom, e.prenom FROM {from_} WHERE 1=1{where} ORDER BY p.date_demande DESC LIMIT %s OFFSET %s", params + [per_page, offset])
        permissions_list = cur.fetchall()
        cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom",
                    scope_params)
        employees = cur.fetchall()
    filters = {'search': search, 'statut': statut, 'date_debut': date_debut, 'date_fin': date_fin}
    return render_template('permissions.html', permissions=permissions_list, employees=employees, filters=filters,
                           libelles=DEMANDE_LIBELLES, ouvertes=DEMANDE_OUVERTES,
                           peut_decider=_peut_decider_rh(), peut_avis=_peut_donner_avis(),
                           pg=pg, page_items=page_list(pg['page'], pg['pages']),
                           base_qs=urlencode({k: v for k, v in filters.items() if v}))


@app.route('/permissions/add', methods=['GET', 'POST'])
@login_required
def add_permission():
    """Dépôt d'une demande de permission (même circuit que les congés)."""
    moi = get_current_employee()
    gestionnaire = session.get('role') in ('admin', 'rh', 'manager')

    with db_cursor(commit=True) as (conn, cur):
        scope_where, scope_params = department_scope_sql('e', cur=cur)
        if request.method == 'POST':
            employe_id = request.form.get('employe_id', type=int)
            date_debut = request.form.get('date_debut')
            date_fin = request.form.get('date_fin')
            motif = request.form.get('motif', '').strip()

            if not gestionnaire:
                if not moi:
                    flash("Aucun employé n'est lié à votre compte : "
                          "contactez les RH.", "warning")
                    return redirect(url_for('self_service'))
                employe_id = moi['id']

            if employe_id and date_debut and date_fin:
                d1 = datetime.strptime(date_debut, '%Y-%m-%d')
                d2 = datetime.strptime(date_fin, '%Y-%m-%d')
                if d2 < d1:
                    flash("La date de fin ne peut pas être avant la date de début", "danger")
                    cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom", scope_params)
                    return render_template('permission_form.html', employees=cur.fetchall(),
                                           moi=moi, gestionnaire=gestionnaire)
                nombre_jours = (d2 - d1).days + 1
                cur.execute("""
                    INSERT INTO permissions (employe_id, motif, date_debut, date_fin,
                                             nombre_jours, statut, demande_par_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
                """, (employe_id, motif, date_debut, date_fin, nombre_jours,
                      DEMANDE_EN_ATTENTE, session.get('user_id')))
                pid = cur.fetchone()['id']

                nom, dept = _libelle_employe(cur, employe_id)
                titre = "Demande de permission : %s" % nom
                corps = ("%s jour(s) du %s au %s. Votre avis est attendu."
                         % (nombre_jours, d1.strftime('%d/%m/%Y'), d2.strftime('%d/%m/%Y')))
                managers = _managers_du_departement(cur, dept)
                for m in managers:
                    create_notification(m['id'], titre, corps, 'info', cur=cur)
                    queue_email(m.get('email'), titre, corps, cur=cur,
                                event_key=f"permission-a-traiter:{pid}:{m['id']}")
                if not managers:
                    message_rh = corps + " (aucun manager sur ce département)"
                    _notifier_roles(cur, ('admin', 'rh'), titre, message_rh,
                                    'info', sauf=session.get('user_id'))
                    _envoyer_roles(cur, ('admin', 'rh'), titre, message_rh,
                                   f"permission-a-traiter:{pid}")

                log_action(session.get('user_id'), session.get('username'),
                           "Demande de permission", "permission", pid, f"{nombre_jours} j")
                flash("Demande de permission soumise. Vous serez notifié à chaque étape.",
                      "success")
                return redirect(url_for('self_service_conges') if not gestionnaire
                                else url_for('permissions'))
            flash("Veuillez remplir tous les champs obligatoires", "danger")
        cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom", scope_params)
        employees = cur.fetchall()
    return render_template('permission_form.html', employees=employees,
                           moi=moi, gestionnaire=gestionnaire)


@app.route('/permissions/avis/<int:id>', methods=['POST'])
@login_required
@role_required('admin', 'rh', 'manager')
def avis_permission(id):
    """Étape 2 : avis du manager du département."""
    avis = (request.form.get('avis') or '').strip()
    commentaire = (request.form.get('commentaire') or '').strip()
    if avis not in ('favorable', 'defavorable'):
        flash("Avis invalide.", "danger")
        return redirect(url_for('permissions'))
    if avis == 'defavorable' and not commentaire:
        flash("Merci de motiver un avis défavorable.", "danger")
        return redirect(url_for('permissions'))

    with db_cursor(commit=True) as (conn, cur):
        cur.execute("SELECT * FROM permissions WHERE id = %s", (id,))
        pm = cur.fetchone()
        if not pm:
            flash("Demande introuvable.", "danger")
            return redirect(url_for('permissions'))
        if pm['statut'] not in DEMANDE_OUVERTES:
            flash("Cette demande est déjà tranchée.", "warning")
            return redirect(url_for('permissions'))

        cur.execute("""UPDATE permissions SET statut = %s, avis_manager = %s,
                          avis_manager_par = %s, avis_manager_le = CURRENT_DATE,
                          avis_commentaire = %s
                       WHERE id = %s""",
                    (DEMANDE_AVIS_RENDU, avis, session.get('username'),
                     commentaire or None, id))

        nom, _ = _libelle_employe(cur, pm['employe_id'])
        sujet_rh = "Permission à décider : %s" % nom
        message_rh = ("Avis %s du manager %s. %s"
                      % (avis, session.get('username'), commentaire[:120]))
        _notifier_roles(cur, ('admin', 'rh'), sujet_rh, message_rh,
                        'info', sauf=session.get('user_id'))
        _envoyer_roles(cur, ('admin', 'rh'), sujet_rh, message_rh,
                       f"permission-a-decider:{id}")
        uid = _user_id_de_employe(cur, pm['employe_id'])
        if uid:
            create_notification(uid, "Votre demande de permission avance",
                                "Votre manager a rendu un avis %s. "
                                "La décision RH suit." % avis, 'info')

    log_action(session.get('user_id'), session.get('username'),
               "Avis manager permission", "permission", id, avis)
    flash("Avis enregistré : les RH vont trancher.", "success")
    return redirect(url_for('permissions'))


@app.route('/permissions/update/<int:id>', methods=['POST'])
@login_required
@role_required('admin', 'rh')
def update_permission(id):
    """Étape 3 : décision finale des RH."""
    action = request.form.get('action')
    motif_refus = (request.form.get('motif_refus') or '').strip()
    if action == 'refuser' and not motif_refus:
        flash("Merci d'indiquer le motif du refus : l'employé en sera informé.", "danger")
        return redirect(url_for('permissions'))

    with db_cursor(commit=True) as (conn, cur):
        cur.execute("SELECT * FROM permissions WHERE id = %s", (id,))
        pm = cur.fetchone()
        if not pm:
            flash("Demande introuvable.", "danger")
            return redirect(url_for('permissions'))
        if pm['statut'] not in DEMANDE_OUVERTES:
            flash("Cette demande est déjà tranchée.", "warning")
            return redirect(url_for('permissions'))

        if action == 'approuver':
            cur.execute("""UPDATE permissions SET statut = %s, decide_par = %s,
                              decide_le = CURRENT_DATE, motif_refus = NULL
                           WHERE id = %s""",
                        (DEMANDE_APPROUVEE, session.get('username'), id))
            _notifier_employe_evenement(
                cur, pm['employe_id'], "Permission approuvée",
                "Votre permission du %s au %s est approuvée."
                % (pm['date_debut'], pm['date_fin']), 'success',
                cle_evenement=f"permission-decision:{id}:approuve",
            )
            flash("Permission approuvée", "success")
        elif action == 'refuser':
            cur.execute("""UPDATE permissions SET statut = %s, decide_par = %s,
                              decide_le = CURRENT_DATE, motif_refus = %s
                           WHERE id = %s""",
                        (DEMANDE_REFUSEE, session.get('username'), motif_refus, id))
            _notifier_employe_evenement(
                cur, pm['employe_id'], "Permission refusée",
                "Votre demande du %s au %s a été refusée : %s"
                % (pm['date_debut'], pm['date_fin'], motif_refus), 'danger',
                cle_evenement=f"permission-decision:{id}:refuse",
            )
            flash("Permission refusée : l'employé est informé du motif.", "info")
        else:
            flash("Action inconnue.", "danger")
            return redirect(url_for('permissions'))

    log_action(session.get('user_id'), session.get('username'),
               "Décision permission", "permission", id, action)
    return redirect(url_for('permissions'))


@app.route('/permissions/<int:id>/annuler', methods=['POST'])
@login_required
def annuler_permission(id):
    """Retrait d'une demande de permission par son auteur."""
    with db_cursor(commit=True) as (conn, cur):
        cur.execute("SELECT * FROM permissions WHERE id = %s", (id,))
        pm = cur.fetchone()
        if not pm:
            flash("Demande introuvable.", "danger")
            return redirect(url_for('self_service_conges'))
        moi = get_current_employee()
        est_le_mien = moi and moi['id'] == pm['employe_id']
        if not (est_le_mien or _peut_decider_rh()):
            flash("Vous ne pouvez annuler que vos propres demandes.", "danger")
            return redirect(url_for('self_service_conges'))
        if pm['statut'] not in DEMANDE_OUVERTES:
            flash("Cette demande est déjà tranchée : elle ne peut plus être annulée.",
                  "warning")
            return redirect(url_for('self_service_conges') if est_le_mien
                            else url_for('permissions'))

        cur.execute("""UPDATE permissions SET statut = %s, annule_par = %s
                       WHERE id = %s""",
                    (DEMANDE_ANNULEE, session.get('username'), id))
        nom, dept = _libelle_employe(cur, pm['employe_id'])
        for m in _managers_du_departement(cur, dept):
            create_notification(m['id'], "Demande de permission annulée",
                                "%s a retiré sa demande du %s au %s."
                                % (nom, pm['date_debut'], pm['date_fin']), 'info')

    log_action(session.get('user_id'), session.get('username'),
               "Annulation permission", "permission", id, None)
    flash("Demande annulée.", "success")
    return redirect(url_for('self_service_conges') if est_le_mien
                    else url_for('permissions'))


@app.route('/permissions/delete/<int:id>', methods=['POST'])
@login_required
@role_required('admin', 'rh', 'manager')
def delete_permission(id):
    with db_cursor(commit=True) as (conn, cur):
        cur.execute("DELETE FROM permissions WHERE id = %s", (id,))
    flash("Demande de permission supprimée", "success")
    return redirect(url_for('permissions'))


@app.route('/soldes-conges')
@login_required
@role_required('admin', 'rh', 'manager')
def soldes_conges_page():
    """Affiche le solde de congés de chaque employé pour une année donnée.
    jours_utilises est recalculé automatiquement depuis les congés approuvés
    (via recalculer_solde) avant affichage, donc toujours à jour."""
    annee = request.args.get('annee', type=int) or datetime.now().year

    with db_cursor(commit=True) as (conn, cur):
        scope_where, scope_params = department_scope_sql('e', cur=cur)
        cur.execute(f"""SELECT id, nom, prenom FROM employes e
                        WHERE {scope_where} ORDER BY nom, prenom""", scope_params)
        employees = cur.fetchall()
        for emp in employees:
            recalculer_solde(emp['id'], annee, cur=cur)

    soldes = []
    for emp in employees:
        s = get_solde_conges(emp['id'], annee)
        s['employe_id'] = emp['id']
        s['nom'] = emp['nom']
        s['prenom'] = emp['prenom']
        soldes.append(s)

    annee_courante = datetime.now().year
    annees_disponibles = list(range(annee_courante - 2, annee_courante + 2))

    return render_template('soldes_conges.html', soldes=soldes, annee=annee,
                            annees_disponibles=annees_disponibles)


@app.route('/soldes-conges/update/<int:employe_id>', methods=['POST'])
@login_required
@role_required('admin', 'rh')
def update_solde_conges(employe_id):
    """Permet de modifier manuellement le nombre de jours acquis (allocation)
    d'un employé pour une année. jours_utilises reste calculé automatiquement."""
    annee = request.form.get('annee', type=int) or datetime.now().year
    jours_acquis = request.form.get('jours_acquis', type=float)

    if jours_acquis is None or jours_acquis < 0:
        flash("Valeur de jours acquis invalide", "danger")
        return redirect(url_for('soldes_conges_page', annee=annee))

    with db_cursor(commit=True) as (conn, cur):
        cur.execute("""
            INSERT INTO soldes_conges (employe_id, annee, jours_acquis, jours_utilises, jours_acquis_manuel)
            VALUES (%s, %s, %s, 0, TRUE)
            ON CONFLICT (employe_id, annee)
            DO UPDATE SET jours_acquis = %s, jours_acquis_manuel = TRUE
        """, (employe_id, annee, jours_acquis, jours_acquis))
        cur.execute("SELECT nom, prenom FROM employes WHERE id = %s", (employe_id,))
        emp = cur.fetchone()

    if emp:
        log_action(session.get('user_id'), session.get('username'), "UPDATE_SOLDE_CONGES",
                   "solde_conges", employe_id, f"{emp['prenom']} {emp['nom']} → jours_acquis={jours_acquis} ({annee})")

    flash("Solde de congés mis à jour avec succès", "success")
    return redirect(url_for('soldes_conges_page', annee=annee))


@app.route('/audit')
@role_required('admin', 'rh')
def audit():
    with db_cursor() as (conn, cur):
        cur.execute("SELECT a.*, u.username FROM audit_logs a LEFT JOIN users u ON a.user_id = u.id ORDER BY a.timestamp DESC LIMIT 150")
        logs = cur.fetchall()
    return render_template('audit.html', logs=logs)

@app.route('/notifications')
@login_required
def notifications():
    user_id = session.get('user_id')
    notifs = get_all_notifications(user_id, limit=30)
    return render_template('notifications.html', notifications=notifs)
@app.route('/notifications/mark-read', methods=['POST'])
@login_required
def mark_notifications_read():
    user_id = session.get('user_id')
    mark_all_read(user_id)
    flash('Notifications marquées comme lues.', 'success')
    return redirect(url_for('notifications'))


@app.route('/employes')
@login_required
def index():
    conn = get_db()
    cur = get_cursor(conn)
    scope_where, scope_params = department_scope_sql('e', cur=cur)

    search = request.args.get('search', '').strip()
    selected_dept = request.args.get('departement', '').strip()
    sort = request.args.get('sort', 'nom')
    order = request.args.get('order', 'asc')
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 10

    where = f" AND {scope_where}"
    params = list(scope_params)
    if search:
        where += " AND (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s OR LOWER(e.poste) LIKE %s OR LOWER(e.email) LIKE %s)"
        s = f"%{search.lower()}%"
        params += [s, s, s, s]
    if selected_dept:
        where += " AND e.departement = %s"; params.append(selected_dept)

    sort_map = {'nom': 'e.nom, e.prenom', 'salaire': 'COALESCE(e.salaire, 0)', 'date_embauche': 'e.date_embauche', 'poste': 'e.poste'}
    sort_col = sort_map.get(sort, 'nom, prenom')
    direction = 'DESC' if order.lower() == 'desc' else 'ASC'
    order_clause = f" ORDER BY {sort_col} {direction}"

    cur.execute(f"SELECT COUNT(*) AS nb FROM employes e WHERE 1=1{where}", params)
    total = cur.fetchone()['nb']
    pg = pagination_info(total, page, per_page)
    offset = (pg['page'] - 1) * per_page
    # La photo est portée par le compte (users.photo), pas par la fiche employé.
    # Sous-requête scalaire plutôt que JOIN : un même employé peut avoir
    # plusieurs comptes, une jointure dupliquerait la ligne. On privilégie le
    # compte qui a effectivement une photo.
    cur.execute(f"""SELECT e.*, (
                        SELECT u.photo FROM users u
                         WHERE u.employe_id = e.id AND u.photo IS NOT NULL
                         ORDER BY u.id LIMIT 1
                    ) AS photo
                    FROM employes e WHERE 1=1{where}{order_clause} LIMIT %s OFFSET %s""",
                params + [per_page, offset])
    employes = cur.fetchall()

    for emp in employes:
        cur.execute("""
            SELECT date, heure_arrivee, statut
            FROM presences
            WHERE employe_id = %s
            ORDER BY date DESC
            LIMIT 1
        """, (emp['id'],))
        last = cur.fetchone()
        if last:
            emp['last_presence'] = dict(last)
            if emp['last_presence'].get('heure_arrivee'):
                emp['last_presence']['heure_arrivee'] = str(emp['last_presence']['heure_arrivee'])[:5]
        else:
            emp['last_presence'] = None

    dept_where, dept_params = department_scope_sql('d', 'nom', cur)
    cur.execute(f"SELECT DISTINCT d.nom FROM departements d WHERE {dept_where} ORDER BY d.nom",
                dept_params)
    depts = cur.fetchall()

    cur.execute(f"""
        SELECT COUNT(*) as total, COALESCE(AVG(e.salaire), 0) as salaire_moyen,
               COUNT(DISTINCT e.departement) as nb_departements
        FROM employes e WHERE {scope_where}
    """, scope_params)
    stats = cur.fetchone()
    stats = dict(stats) if stats else {'total': 0, 'salaire_moyen': 0, 'nb_departements': 0}

    cur.close()
    conn.close()
    filters = {'search': search, 'departement': selected_dept, 'sort': sort, 'order': order}
    return render_template('index.html', employes=employes, depts=depts, search=search, selected_dept=selected_dept,
                           sort=sort, order=order, stats=stats, pg=pg, page_items=page_list(pg['page'], pg['pages']),
                           base_qs=urlencode({k: v for k, v in filters.items() if v}))
@app.route('/employes/<int:id>')
@login_required
def view_employee(id):
    conn = get_db()
    cur = get_cursor(conn)
    # `photo` vient du compte lié (voir la remarque dans index()).
    cur.execute("""SELECT e.*, (
                       SELECT u.photo FROM users u
                        WHERE u.employe_id = e.id AND u.photo IS NOT NULL
                        ORDER BY u.id LIMIT 1
                   ) AS photo
                   FROM employes e WHERE e.id = %s""", (id,))
    employee = cur.fetchone()
    if not employee:
        cur.close()
        conn.close()
        flash("Employé introuvable", "danger")
        return redirect(url_for('index'))

    cur.execute("SELECT * FROM documents WHERE employe_id = %s ORDER BY date_upload DESC", (id,))
    employee_documents = cur.fetchall()

    # Dernière présence enregistrée (n'était auparavant jamais transmise au
    # template : la carte "Dernière présence" affichait donc toujours "aucune
    # présence enregistrée", même quand des présences existaient).
    cur.execute("""
        SELECT date, heure_arrivee, heure_depart, statut
        FROM presences WHERE employe_id = %s ORDER BY date DESC LIMIT 1
    """, (id,))
    last = cur.fetchone()
    last_presence = None
    if last:
        last_presence = dict(last)
        if last_presence.get('heure_arrivee'):
            last_presence['heure_arrivee'] = str(last_presence['heure_arrivee'])[:5]
        if last_presence.get('heure_depart'):
            last_presence['heure_depart'] = str(last_presence['heure_depart'])[:5]

    # Statistiques présence / absence / congés de l'année en cours
    annee_courante = date.today().year
    cur.execute("""
        SELECT COUNT(*) AS nb FROM presences
        WHERE employe_id = %s AND EXTRACT(YEAR FROM date) = %s
    """, (id, annee_courante))
    nb_presences = cur.fetchone()['nb']

    cur.execute("""
        SELECT heure_arrivee FROM presences
        WHERE employe_id = %s AND EXTRACT(YEAR FROM date) = %s AND heure_arrivee IS NOT NULL
    """, (id, annee_courante))
    nb_retards = sum(1 for r in cur.fetchall() if calculer_retard(r['heure_arrivee']) > 0)

    cur.execute("""
        SELECT COUNT(*) AS nb FROM absences
        WHERE employe_id = %s AND EXTRACT(YEAR FROM date) = %s
          AND COALESCE(statut, 'non_justifiee') <> 'acceptee'
    """, (id, annee_courante))
    nb_absences = cur.fetchone()['nb']

    cur.execute("""
        SELECT COALESCE(SUM(nombre_jours), 0) AS nb FROM conges
        WHERE employe_id = %s AND statut = 'approuvé' AND EXTRACT(YEAR FROM date_debut) = %s
    """, (id, annee_courante))
    nb_jours_conges = cur.fetchone()['nb']

    stats_presence = {
        'annee': annee_courante,
        'presences': nb_presences,
        'retards': nb_retards,
        'absences': nb_absences,
        'jours_conges': nb_jours_conges,
    }

    cur.close()
    conn.close()
    return render_template('detail.html', employee=employee, documents=employee_documents,
                           last_presence=last_presence, stats_presence=stats_presence,
                           today=date.today(),
                           bientot=date.today() + timedelta(days=SEUIL_ALERTE_EXPIRATION_DOCUMENTS_JOURS))

@app.route('/employes/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'rh')
def edit_employee(id):
    conn = get_db()
    cur = get_cursor(conn)

    if request.method == 'POST':
        nom = request.form.get('nom')
        prenom = request.form.get('prenom')
        poste = request.form.get('poste')
        departement = request.form.get('departement')
        email = request.form.get('email')
        telephone = request.form.get('telephone')
        salaire = request.form.get('salaire')
        
        cur.execute("""
            UPDATE employes 
            SET nom=%s, prenom=%s, poste=%s, departement=%s, email=%s, telephone=%s, salaire=%s
            WHERE id = %s
        """, (nom, prenom, poste, departement, email, telephone, salaire, id))
        conn.commit()
        flash("Employé modifié avec succès", "success")
        cur.close()
        conn.close()
        return redirect(url_for('index'))

    cur.execute("SELECT * FROM employes WHERE id = %s", (id,))
    employee = cur.fetchone()
    cur.execute("SELECT DISTINCT nom FROM departements ORDER BY nom")
    depts = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('form.html', employee=employee, depts=depts, title="Modifier l'employé")

@app.route('/employes/<int:id>/delete', methods=['POST'])
@login_required
@role_required('admin', 'rh')
def delete_employee(id):
    conn = get_db()
    cur = get_cursor(conn)
    
    try:
        # 1. Supprimer les présences liées
        cur.execute("DELETE FROM presences WHERE employe_id = %s", (id,))
        
        # 2. Supprimer les congés liés
        cur.execute("DELETE FROM conges WHERE employe_id = %s", (id,))

        # 2b. Supprimer les permissions liées (module séparé)
        cur.execute("DELETE FROM permissions WHERE employe_id = %s", (id,))

        # 2c. Supprimer les absences liées
        cur.execute("DELETE FROM absences WHERE employe_id = %s", (id,))
        
        # 3. Supprimer les soldes de congés liés
        cur.execute("DELETE FROM soldes_conges WHERE employe_id = %s", (id,))
        
        # 4. Supprimer les documents liés
        cur.execute("DELETE FROM documents WHERE employe_id = %s", (id,))
        
        # 5. Supprimer les notifications liées (si la table existe)
        try:
            cur.execute("DELETE FROM notifications WHERE user_id IN (SELECT id FROM users WHERE employe_id = %s)", (id,))
        except:
            pass
        
        # 6. Supprimer les utilisateurs liés (clé étrangère principale)
        cur.execute("DELETE FROM users WHERE employe_id = %s", (id,))
        
        # 7. Enfin supprimer l'employé
        cur.execute("DELETE FROM employes WHERE id = %s", (id,))
        
        conn.commit()
        flash("Employé et toutes ses données associées ont été supprimés avec succès", "success")
        
    except Exception as e:
        conn.rollback()
        flash(f"Erreur lors de la suppression : {str(e)}", "danger")
        logger.error("Erreur delete_employee: %s", e, exc_info=True)
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('index'))

# ==================== RAPPORTS AVANCÉS ====================
@app.route('/rapports')
@login_required
def rapports():
    conn = get_db()
    cur = get_cursor(conn)
    scope_where, scope_params = department_scope_sql('e', cur=cur)
    
    # Filters
    date_debut = request.args.get('date_debut', '')
    date_fin = request.args.get('date_fin', '')
    employe_id = request.args.get('employe_id', '')
    type_rapport = request.args.get('type', 'presences')
    statut = request.args.get('statut', '')
    
    cur.execute(f"SELECT id, prenom, nom FROM employes e WHERE {scope_where} ORDER BY nom, prenom",
                scope_params)
    employees = cur.fetchall()
    
    presences_data = []
    conges_data = []
    total_jours = 0
    
    if type_rapport == 'presences':
        q = f"""SELECT p.*, e.nom, e.prenom FROM presences p
               JOIN employes e ON p.employe_id = e.id WHERE {scope_where} """
        params = list(scope_params)
        if date_debut:
            q += " AND p.date >= %s"
            params.append(date_debut)
        if date_fin:
            q += " AND p.date <= %s"
            params.append(date_fin)
        if employe_id:
            q += " AND p.employe_id = %s"
            params.append(int(employe_id))
        q += " ORDER BY p.date DESC LIMIT 200"
        cur.execute(q, params)
        presences_data = cur.fetchall()
        for p in presences_data:
            p['retard_minutes'] = calculer_retard(p['heure_arrivee'])
    else:
        q = f"""SELECT c.*, e.nom, e.prenom FROM conges c
               JOIN employes e ON c.employe_id = e.id WHERE {scope_where} """
        params = list(scope_params)
        if date_debut:
            q += " AND c.date_debut >= %s"
            params.append(date_debut)
        if date_fin:
            q += " AND c.date_fin <= %s"
            params.append(date_fin)
        if employe_id:
            q += " AND c.employe_id = %s"
            params.append(int(employe_id))
        if statut:
            q += " AND c.statut = %s"
            params.append(statut)
        q += " ORDER BY c.date_debut DESC LIMIT 200"
        cur.execute(q, params)
        conges_data = cur.fetchall()
        total_jours = sum((c['nombre_jours'] or 0) for c in conges_data)
    
    cur.close(); conn.close()
    
    return render_template('rapports.html', 
                           employees=employees,
                           presences=presences_data,
                           conges=conges_data,
                           date_debut=date_debut, date_fin=date_fin,
                           selected_employe=employe_id,
                           type_rapport=type_rapport,
                           statut=statut,
                           total_jours=total_jours)

# ==================== DOCUMENTS ===============================================
# Routes extraites dans blueprints/documents.py (Blueprint ``documents``).

# ==================== MAIN ====================

# ==================== STUB ROUTES (pour compatibilité templates) ====================
# ==================== HISTORIQUE DES PRÉSENCES ==============================
# Route extraite dans blueprints/presences.py.

# ==================== DÉPARTEMENTS ===========================================
# Routes extraites dans blueprints/departements.py.

# ==================== UTILISATEURS ET ACCÈS ==================================
# Routes extraites dans blueprints/utilisateurs.py.

@app.route('/add_employee', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'rh')
def add_employee():
    conn = get_db()
    cur = get_cursor(conn)

    if request.method == 'POST':
        nom = request.form.get('nom')
        prenom = request.form.get('prenom')
        poste = request.form.get('poste')
        departement = request.form.get('departement')
        email = request.form.get('email')
        telephone = request.form.get('telephone')
        salaire = request.form.get('salaire')
        date_embauche = request.form.get('date_embauche')
        
        if nom and prenom and poste:
            cur.execute("""
                INSERT INTO employes (nom, prenom, poste, departement, email, telephone, salaire, date_embauche)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (nom, prenom, poste, departement, email, telephone, salaire, date_embauche))
            employe_id = cur.fetchone()['id']
            libelle = f"{prenom} {nom}"
            _notifier_roles(
                cur, ('admin', 'rh'), "Nouvel employé enregistré",
                f"{libelle} a été ajouté au département {departement or 'non renseigné'}.",
                'success', sauf=session.get('user_id'))
            queue_email(
                email, "Bienvenue — dossier salarié créé",
                f"Bonjour {prenom},\n\nVotre dossier salarié a été créé dans Gestion du Personnel. "
                "Les RH vous communiqueront vos accès à l'espace employé.",
                cur=cur, event_key=f"employe-cree:{employe_id}")
            conn.commit()
            flash("Employé ajouté avec succès ; les personnes concernées ont été informées", "success")
            cur.close()
            conn.close()
            return redirect(url_for('index'))
        else:
            flash("Veuillez remplir les champs obligatoires", "danger")

    cur.execute("SELECT DISTINCT nom FROM departements ORDER BY nom")
    depts = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('form.html', employee=None, depts=depts, title="Nouvel employé")

@app.route('/calendrier-conges')
@login_required
def calendrier_conges():
    annee = request.args.get('annee', type=int) or datetime.now().year

    conn = get_db()
    cur = get_cursor(conn)
    scope_where, scope_params = department_scope_sql('e', cur=cur)
    cur.execute(f"""
        SELECT c.date_debut, c.date_fin, c.nombre_jours, c.statut,
               e.prenom, e.nom
        FROM conges c
        JOIN employes e ON c.employe_id = e.id
        WHERE c.statut = 'approuvé'
          AND EXTRACT(YEAR FROM c.date_debut) = %s
          AND {scope_where}
        ORDER BY c.date_debut
    """, [annee] + scope_params)
    conges = cur.fetchall()
    cur.close(); conn.close()

    # Total des jours approuvés sur l'année
    total_approuves = sum((c['nombre_jours'] or 0) for c in conges)

    # Répartition par mois (1..12)
    mois_noms = ['Janvier', 'Février', 'Mars', 'Avril', 'Mai', 'Juin',
                 'Juillet', 'Août', 'Septembre', 'Octobre', 'Novembre', 'Décembre']
    mois_list = [{'nom': mois_noms[m - 1], 'conges': []} for m in range(1, 13)]
    for c in conges:
        debut = c['date_debut']
        # date (objet datetime.date) ou chaîne 'YYYY-MM-DD'
        mois = debut.month if hasattr(debut, 'month') else int(str(debut)[5:7])
        mois_list[mois - 1]['conges'].append(c)

    return render_template('calendrier_conges.html',
                           annee=annee,
                           total_approuves=total_approuves,
                           mois_list=mois_list)

@app.route('/employes/add', methods=['GET','POST'])
@role_required('admin')
def add_employee_alt():
    return redirect(url_for('index'))


# ==================== PARC MATÉRIEL / INVENTAIRES / MAINTENANCE ==============
# Routes et helpers extraits dans blueprints/parc.py (Blueprint ``parc``).


# Blueprints métier : dépendances partagées injectées explicitement pour éviter
# les imports circulaires avec l'application historique.
app.register_blueprint(creer_blueprint_auth({
    'db_cursor': db_cursor,
    'login_required': login_required,
    'get_current_user_row': get_current_user_row,
    'get_current_employee': get_current_employee,
    'enregistrer_session': enregistrer_session,
    'cloturer_session': cloturer_session,
    'log_action': log_action,
    'get_role_label': get_role_label,
    'enregistrer_photo_profil': enregistrer_photo_profil,
    'supprimer_photo_profil': supprimer_photo_profil,
    'avatar_folder': AVATAR_FOLDER,
}))

app.register_blueprint(creer_blueprint_utilisateurs({
    'db_cursor': db_cursor,
    'login_required': login_required,
    'role_required': role_required,
    'log_action': log_action,
    'get_role_label': get_role_label,
    'create_notification': create_notification,
    'queue_email': queue_email,
    'session_online_window': SESSION_ONLINE_WINDOW_MIN,
    'permanent_session_lifetime': app.config['PERMANENT_SESSION_LIFETIME'],
    'logger': logger,
}))

app.register_blueprint(creer_blueprint_presences({
    'get_db': get_db,
    'get_cursor': get_cursor,
    'db_cursor': db_cursor,
    'login_required': login_required,
    'role_required': role_required,
    'department_scope_sql': department_scope_sql,
    'calculer_retard': calculer_retard,
    'send_retard_email': send_retard_email,
    'notifier_employe_evenement': _notifier_employe_evenement,
    'pagination_info': pagination_info,
    'page_list': page_list,
}))

app.register_blueprint(creer_blueprint_departements({
    'get_db': get_db,
    'get_cursor': get_cursor,
    'login_required': login_required,
    'role_required': role_required,
    'department_scope_sql': department_scope_sql,
}))

app.register_blueprint(creer_blueprint_documents({
    'db_cursor': db_cursor,
    'get_db': get_db,
    'get_cursor': get_cursor,
    'login_required': login_required,
    'role_required': role_required,
    'get_current_employee': get_current_employee,
    'allowed_file': allowed_file,
    'detect_file_type': detect_file_type,
    'log_action': log_action,
    'notifier_employe_evenement': _notifier_employe_evenement,
    'department_scope_sql': department_scope_sql,
    'upload_folder': app.config['UPLOAD_FOLDER'],
    'seuil_expiration': SEUIL_ALERTE_EXPIRATION_DOCUMENTS_JOURS,
}))

parc_bp, parc_api = creer_blueprint_parc({
    'db_cursor': db_cursor,
    'login_required': login_required,
    'role_required': role_required,
    'create_notification': create_notification,
    'notifier_roles': _notifier_roles,
    'log_action': log_action,
    'get_current_employee': get_current_employee,
    'user_id_de_employe': _user_id_de_employe,
    'pagination_info': pagination_info,
    'page_list': page_list,
    'department_scope_sql': department_scope_sql,
    'get_department_scope': get_department_scope,
    'department_access_denied': _department_access_denied,
    'logger': logger,
})
# Compatibilité des tests et des jobs qui appelaient ce helper depuis app.py.
_notifier_stock_bas = parc_api['notifier_stock_bas']
app.register_blueprint(parc_bp)

app.register_blueprint(creer_blueprint_justifications({
    'db_cursor': db_cursor,
    'login_required': login_required,
    'role_required': role_required,
    'get_current_employee': get_current_employee,
    'detect_file_type': detect_file_type,
    'create_notification': create_notification,
    'queue_email': queue_email,
    'log_action': log_action,
}))

app.register_blueprint(creer_blueprint_messagerie({
    'db_cursor': db_cursor,
    'login_required': login_required,
    'detect_file_type': detect_file_type,
    'create_notification': create_notification,
    'queue_email': queue_email,
    'log_action': log_action,
    'department_scope_sql': department_scope_sql,
}))

# Doit s'exécuter que l'app soit lancée directement (python app.py) OU importée
# par un serveur WSGI (gunicorn app:app, cas du déploiement Render) : sinon les
# tables créées via CREATE TABLE IF NOT EXISTS (comme `absences`) n'existent
# jamais en production. Idempotent, donc sans risque même avec plusieurs workers.
init_db()
demarrer_scheduler()

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    if debug_mode and os.environ.get('FLASK_ENV') == 'production':
        raise RuntimeError("FLASK_DEBUG ne doit jamais être activé en production (FLASK_ENV=production).")
    # For development with basic concurrency support (multiple users)
    # For production use: gunicorn -w 4 -b 0.0.0.0:5000 app:app
    app.run(debug=debug_mode, host='0.0.0.0', port=5000, threaded=True)