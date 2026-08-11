from flask import Flask, render_template, request, redirect, url_for, flash, session, make_response, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
import os
import logging
from datetime import date, datetime, timedelta
from functools import wraps
import io
from urllib.parse import urlencode

from flask_mail import Mail, Message
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

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
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
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

# === INITIALISATION SÉCURITÉ ===
csrf = CSRFProtect(app)

# Rate limiter
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
def send_html_email(recipients, subject, html_template, **context):
    try:
        if not app.config.get('MAIL_USERNAME'):
            logger.info(f"[HTML EMAIL DEMO] → {recipients} | {subject}")
            return True
        html_body = render_template(html_template, **context)
        admin = get_admin_email()
        msg = Message(subject=subject, recipients=[recipients] if isinstance(recipients, str) else recipients, cc=[admin], sender=admin)
        msg.html = html_body
        mail.send(msg)
        logger.info("HTML email envoyé")
        return True
    except Exception as e:
        logger.error("Erreur HTML email: %s", e, exc_info=True)
        return False

HEURE_ARRIVEE_ATTENDUE = "09:00"

ROLE_LABELS = {'admin':'Administrateur', 'rh':'Responsable RH', 'manager':'Manager', 'employe':'Employé'}

def get_role_label(role):
    return ROLE_LABELS.get(role, role or 'Employé')

def role_required(*allowed_roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                flash('Veuillez vous connecter.', 'warning')
                return redirect(url_for('login'))
            role = session.get('role', 'employe')
            if role == 'admin' or role in allowed_roles:
                return f(*args, **kwargs)
            flash('Accès refusé.', 'danger')
            return redirect(url_for('dashboard'))
        return decorated
    return decorator

# ==================== NOTIFICATIONS (Base de données - support multi-utilisateur réel) ====================
def create_notification(user_id, title, message, type_="info"):
    """Crée une notification persistante en base (multi-utilisateur safe)"""
    try:
        conn = get_db()
        cur = get_cursor(conn)
        cur.execute("""
            INSERT INTO notifications (user_id, title, message, type, is_read)
            VALUES (%s, %s, %s, %s, FALSE)
        """, (user_id, title, message, type_))
        conn.commit()
        cur.close()
        conn.close()
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
    return {
        'unread_notifications': unread_count,
        'current_role': session.get('role', 'employe'),
        'role_label': session.get('role_label') or get_role_label(session.get('role', 'employe')),
        'is_modal': is_modal,
        'layout': '_modal_layout.html' if is_modal else 'base.html',
    }

# ==================== RETARD EMAIL (HTML) ====================
def send_retard_email(employee_name, employee_email, retard_minutes, date_str, heure_arrivee):
    admin_email = get_admin_email()
    if not app.config.get('MAIL_USERNAME'):
        logger.info(f"[EMAIL DEMO] De: {admin_email} → {employee_name} +{retard_minutes} min")
        return True
    try:
        subject = f"⚠️ Retard détecté - {employee_name}"
        sent = send_html_email(
            recipients=[employee_email] if employee_email else [admin_email],
            subject=subject,
            html_template="emails/retard.html",
            prenom=employee_name.split()[0] if employee_name else "Employé",
            nom_complet=employee_name,
            date_str=date_str,
            heure_arrivee=heure_arrivee,
            retard_minutes=retard_minutes,
            heure_attendue=HEURE_ARRIVEE_ATTENDUE,
            admin_name="Administrateur Système"
        )
        if sent: return True
        # fallback
        body = f"Bonjour,\n\nRetard détecté : {employee_name} le {date_str} à {heure_arrivee} (+{retard_minutes} min)"
        msg = Message(subject=subject, recipients=[employee_email or admin_email], cc=[admin_email], sender=admin_email)
        msg.body = body
        mail.send(msg)
        return True
    except Exception as e:
        logger.error("Erreur retard email: %s", e, exc_info=True)
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
            return redirect(url_for('login'))
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

def get_current_employee():
    if 'user_id' not in session: return None
    try:
        with db_cursor() as (conn, cur):
            cur.execute("SELECT e.* FROM employes e JOIN users u ON u.employe_id = e.id WHERE u.id = %s LIMIT 1", (session['user_id'],))
            return cur.fetchone()
    except:
        return None

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
    cur.execute('''CREATE TABLE IF NOT EXISTS audit_logs (id SERIAL PRIMARY KEY, user_id INTEGER, username VARCHAR(80), action VARCHAR(100), entity_type VARCHAR(50), entity_id INTEGER, details TEXT, ip_address VARCHAR(45), timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(timestamp DESC)")

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
    # Évite de renvoyer l'alerte de stock bas en boucle : une seule notif tant
    # que le stock n'est pas repassé au-dessus du seuil (remis à FALSE alors).
    cur.execute("ALTER TABLE materiels ADD COLUMN IF NOT EXISTS alerte_envoyee BOOLEAN DEFAULT FALSE")

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
@app.route('/login', methods=['GET','POST'])
def login():
    if 'user_id' in session: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        u = request.form.get('username','').strip()
        p = request.form.get('password','')
        with db_cursor() as (conn, cur):
            cur.execute("SELECT * FROM users WHERE username=%s", (u,))
            user = cur.fetchone()
        if user and check_password_hash(user['password_hash'], p):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['role_label'] = get_role_label(user['role'])
            log_action(user_id=user['id'], username=user['username'], action="LOGIN")
            flash(f'Bienvenue, {user["username"]} !', 'success')
            return redirect(url_for('dashboard'))
        flash('Identifiants ou mot de passe incorrects.', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    log_action(session.get('user_id'), session.get('username'), "LOGOUT")
    session.clear()
    flash('Déconnecté.', 'success')
    return redirect(url_for('login'))

# ==================== SELF-SERVICE ====================
@app.route('/self-service')
@app.route('/mon-espace')
@login_required
def self_service():
    emp = get_current_employee()
    my_presences = []
    my_conges = []
    mon_solde = None
    if emp:
        conn = get_db()
        cur = get_cursor(conn)
        cur.execute("SELECT * FROM presences WHERE employe_id = %s ORDER BY date DESC LIMIT 30", (emp['id'],))
        my_presences = cur.fetchall()
        for p in my_presences: p['retard_minutes'] = calculer_retard(p['heure_arrivee'])
        cur.execute("SELECT * FROM conges WHERE employe_id = %s ORDER BY date_demande DESC LIMIT 15", (emp['id'],))
        my_conges = cur.fetchall()
        mon_solde = get_solde_conges(emp['id'])
        cur.close(); conn.close()
    return render_template('self_service.html', employee=emp, my_presences=my_presences, my_conges=my_conges, mon_solde=mon_solde)

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
    emp = get_current_employee()
    if not emp:
        flash("Aucun employé lié à votre compte.", "warning")
        return redirect(url_for('self_service'))
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM conges WHERE employe_id = %s ORDER BY date_demande DESC", (emp['id'],))
    conges = cur.fetchall()
    cur.close(); conn.close()
    return render_template('self_service_conges.html', conges=conges, employee=emp)

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
    if my_only and emp:
        q += "WHERE p.employe_id = %s "
        params.append(emp['id'])
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
    if my_only and emp:
        q += "WHERE p.employe_id = %s "
        params.append(emp['id'])
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
    if my_only and emp:
        q += "WHERE c.employe_id = %s "
        params.append(emp['id'])
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
    if my_only and emp:
        q += "WHERE c.employe_id = %s "
        params.append(emp['id'])
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
    from datetime import date
    conn = get_db()
    cur = get_cursor(conn)

    today = date.today().strftime('%Y-%m-%d')

    # === Compteurs de base ===
    cur.execute("SELECT COUNT(*) as total FROM employes")
    total_employes = cur.fetchone()['total']

    cur.execute("SELECT COUNT(*) as total FROM departements")
    total_departements = cur.fetchone()['total']

    cur.execute("SELECT AVG(salaire) as avg FROM employes")
    salaire_moyen = cur.fetchone()['avg'] or 0

    # === Présences aujourd'hui ===
    cur.execute("SELECT COUNT(*) as total FROM presences WHERE date = %s", (today,))
    total_presences_aujourdhui = cur.fetchone()['total'] or 0

    # === Retards aujourd'hui ===
    cur.execute("""
        SELECT p.*, e.nom, e.prenom 
        FROM presences p 
        JOIN employes e ON p.employe_id = e.id 
        WHERE p.date = %s
    """, (today,))
    presences_today = cur.fetchall()

    retards_aujourdhui = []
    total_retards_minutes = 0
    for p in presences_today:
        retard = calculer_retard(p.get('heure_arrivee'))
        if retard > 0:
            p['retard_minutes'] = retard
            retards_aujourdhui.append(p)
            total_retards_minutes += retard

    nb_retards = len(retards_aujourdhui)

    # === Stats présences ===
    presences_stat = {
        'present': total_presences_aujourdhui,
        'absent': max(0, total_employes - total_presences_aujourdhui),
        'teletravail': 0
    }
    taux_presence = round((total_presences_aujourdhui / total_employes * 100) if total_employes > 0 else 0, 1)

    # === Stats congés ===
    cur.execute("SELECT statut, COUNT(*) as nb FROM conges GROUP BY statut")
    conges_rows = cur.fetchall()
    conges_stat = {'en_attente': 0, 'approuve': 0, 'refuse': 0}
    for row in conges_rows:
        if row['statut'] in ['en attente', 'en_attente']:
            conges_stat['en_attente'] = row['nb']
        elif row['statut'] == 'approuvé':
            conges_stat['approuve'] = row['nb']
        elif row['statut'] == 'refusé':
            conges_stat['refuse'] = row['nb']

    # === Heures totales (estimation) ===
    cur.execute("SELECT COUNT(*) as total FROM presences")
    total_pointages = cur.fetchone()['total'] or 0
    heures_totales = round(total_pointages * 7.5, 1)

    # === Départements ===
    cur.execute("""
        SELECT d.nom, COUNT(e.id) as nb_employes 
        FROM departements d 
        LEFT JOIN employes e ON e.departement = d.nom 
        GROUP BY d.nom 
        ORDER BY nb_employes DESC 
        LIMIT 8
    """)
    dept_rows = cur.fetchall()
    dept_stats = []
    for row in dept_rows:
        pct = round((row['nb_employes'] / total_employes * 100) if total_employes > 0 else 0, 1)
        dept_stats.append({
            'nom': row['nom'],
            'nb_employes': row['nb_employes'],
            'pourcentage': pct
        })

    # === Activité récente ===
    cur.execute("SELECT p.*, e.nom, e.prenom FROM presences p JOIN employes e ON p.employe_id = e.id ORDER BY p.date DESC LIMIT 5")
    recent_presences = cur.fetchall()

    cur.execute("SELECT c.*, e.nom, e.prenom FROM conges c JOIN employes e ON c.employe_id = e.id ORDER BY c.date_demande DESC LIMIT 5")
    recent_conges = cur.fetchall()

    # === Données pour les graphiques (Chart.js) ===
    cur.execute("""
        SELECT d::date AS jour, COUNT(p.id) AS nb
        FROM generate_series(CURRENT_DATE - INTERVAL '29 days', CURRENT_DATE, INTERVAL '1 day') AS d
        LEFT JOIN presences p ON p.date = d::date
        GROUP BY d
        ORDER BY d
    """)
    tendance_rows = cur.fetchall()
    chart_tendance = {
        'labels': [r['jour'].strftime('%d/%m') for r in tendance_rows],
        'valeurs': [r['nb'] for r in tendance_rows],
    }
    chart_presences_jour = {
        'labels': ['Présent', 'Absent', 'Télétravail'],
        'valeurs': [presences_stat['present'], presences_stat['absent'], presences_stat['teletravail']],
    }
    chart_conges = {
        'labels': ['En attente', 'Approuvés', 'Refusés'],
        'valeurs': [conges_stat['en_attente'], conges_stat['approuve'], conges_stat['refuse']],
    }
    chart_departements = {
        'labels': [d['nom'] for d in dept_stats],
        'valeurs': [d['nb_employes'] for d in dept_stats],
    }

    cur.close()
    conn.close()

    return render_template('dashboard.html',
        total_employes=total_employes,
        total_departements=total_departements,
        salaire_moyen=salaire_moyen,
        total_presences_aujourdhui=total_presences_aujourdhui,
        today=today,
        presences_stat=presences_stat,
        taux_presence=taux_presence,
        conges_stat=conges_stat,
        retards_aujourdhui=retards_aujourdhui,
        nb_retards=nb_retards,
        total_retards_minutes=total_retards_minutes,
        heures_totales=heures_totales,
        dept_stats=dept_stats,
        recent_presences=recent_presences,
        recent_conges=recent_conges,
        chart_tendance=chart_tendance,
        chart_presences_jour=chart_presences_jour,
        chart_conges=chart_conges,
        chart_departements=chart_departements
    )

@app.route('/presences', methods=['GET', 'POST'])
@login_required
def presences():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("SELECT p.*, e.nom, e.prenom FROM presences p JOIN employes e ON p.employe_id = e.id ORDER BY p.date DESC LIMIT 60")
    presences_list = cur.fetchall()

    today = date.today().strftime('%Y-%m-%d')
    
        # === Retards aujourd'hui ===
    cur.execute("""
        SELECT p.*, e.nom, e.prenom 
        FROM presences p 
        JOIN employes e ON p.employe_id = e.id 
        WHERE p.date = %s
    """, (today,))
    presences_today = cur.fetchall()

    retards_aujourdhui = []
    total_retards_minutes = 0
    for p in presences_today:
        retard = calculer_retard(p.get('heure_arrivee'))
        if retard > 0:
            p['retard_minutes'] = retard
            retards_aujourdhui.append(p)
            total_retards_minutes += retard

    nb_retards = len(retards_aujourdhui)

    # === Gestion du pointage rapide (POST) ===
    if request.method == 'POST':
        action = request.form.get('action')
        employe_id = request.form.get('quick_employe_id')
        date_val = datetime.now().strftime('%Y-%m-%d')
        
        if action and employe_id:
            employe_id = int(employe_id)
            
            if action == 'clock_in':
                cur.execute("""
                    INSERT INTO presences (employe_id, date, heure_arrivee, statut)
                    VALUES (%s, %s, CURRENT_TIME, 'présent')
                    ON CONFLICT (employe_id, date) 
                    DO UPDATE SET heure_arrivee = CURRENT_TIME
                """, (employe_id, date_val))
                conn.commit()
                flash('Entrée pointée', 'success')
            
            elif action == 'clock_out':
                cur.execute("""
                    INSERT INTO presences (employe_id, date, heure_depart)
                    VALUES (%s, %s, CURRENT_TIME)
                    ON CONFLICT (employe_id, date) 
                    DO UPDATE SET heure_depart = CURRENT_TIME
                """, (employe_id, date_val))
                conn.commit()
                flash('Sortie pointée', 'success')
            
            cur.close(); conn.close()
            return redirect(url_for('presences'))

        # Normal GET: display the page with filters + pagination
    search_raw = request.args.get('search', '').strip()
    search = search_raw.lower()
    date_filter = request.args.get('date', '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 10

    where = ""
    params = []
    if search:
        where += " AND (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s)"
        params += [f"%{search}%", f"%{search}%"]
    if date_filter:
        where += " AND p.date = %s"; params.append(date_filter)

    from_ = "presences p JOIN employes e ON p.employe_id = e.id"
    cur.execute(f"SELECT COUNT(*) AS nb FROM {from_} WHERE 1=1{where}", params)
    total = cur.fetchone()['nb']
    pg = pagination_info(total, page, per_page)
    offset = (pg['page'] - 1) * per_page
    cur.execute(f"SELECT p.*, e.nom, e.prenom FROM {from_} WHERE 1=1{where} ORDER BY p.date DESC, p.heure_arrivee DESC LIMIT %s OFFSET %s", params + [per_page, offset])
    presences_list = cur.fetchall()

    for p in presences_list:
        if p.get('heure_arrivee'):
            p['heure_arrivee'] = str(p['heure_arrivee'])[:5]
        if p.get('heure_depart'):
            p['heure_depart'] = str(p['heure_depart'])[:5]
        p['retard_minutes'] = calculer_retard(p['heure_arrivee'])
        p['retard'] = p['retard_minutes'] > 0

    cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom")
    employees = cur.fetchall()
    cur.close()
    conn.close()
    filters = {'search': search_raw, 'date': date_filter}
    return render_template('presences.html', presences=presences_list, employees=employees, today=today,
                           retards_aujourdhui=retards_aujourdhui, nb_retards=nb_retards, total_retards_minutes=total_retards_minutes,
                           search=search_raw, date_filter=date_filter, pg=pg, page_items=page_list(pg['page'], pg['pages']),
                           base_qs=urlencode({k: v for k, v in filters.items() if v}))

@app.route('/presences/clock_in/<int:employe_id>', methods=['POST'])
@login_required
def clock_in(employe_id):
    date_val = request.form.get('date') or datetime.now().strftime('%Y-%m-%d')
    with db_cursor(commit=True) as (conn, cur):
        cur.execute("SELECT nom, prenom, email FROM employes WHERE id = %s", (employe_id,))
        emp = cur.fetchone()
        cur.execute("INSERT INTO presences (employe_id, date, heure_arrivee, statut) VALUES (%s, %s, CURRENT_TIME, 'présent') ON CONFLICT (employe_id, date) DO UPDATE SET heure_arrivee = CURRENT_TIME", (employe_id, date_val))
        cur.execute("SELECT heure_arrivee FROM presences WHERE employe_id=%s AND date=%s", (employe_id, date_val))
        res = cur.fetchone()
        heure = str(res['heure_arrivee'])[:5] if res else '09:00'
        retard = calculer_retard(heure)
        if retard > 0 and emp:
            send_retard_email(f"{emp['prenom']} {emp['nom']}", emp.get('email'), retard, date_val, heure)
    flash('Entrée pointée', 'success')
    return redirect(url_for('presences'))

@app.route('/presences/clock_out/<int:employe_id>', methods=['POST'])
@login_required
def clock_out(employe_id):
    date_val = request.form.get('date') or datetime.now().strftime('%Y-%m-%d')
    with db_cursor(commit=True) as (conn, cur):
        cur.execute("""
            INSERT INTO presences (employe_id, date, heure_depart)
            VALUES (%s, %s, CURRENT_TIME)
            ON CONFLICT (employe_id, date) 
            DO UPDATE SET heure_depart = CURRENT_TIME
        """, (employe_id, date_val))
    flash('Sortie pointée', 'success')
    return redirect(url_for('presences'))

@app.route('/presences/add', methods=['GET', 'POST'])
@login_required
def add_presence():
    conn = get_db()
    cur = get_cursor(conn)

    if request.method == 'POST':
        employe_id = request.form.get('employe_id')
        date_val = request.form.get('date')
        heure_arrivee = request.form.get('heure_arrivee')
        heure_depart = request.form.get('heure_depart')
        statut = request.form.get('statut', 'présent')
        commentaire = request.form.get('commentaire', '')

        if employe_id and date_val:
            cur.execute("""
                INSERT INTO presences (employe_id, date, heure_arrivee, heure_depart, statut, commentaire)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (employe_id, date) 
                DO UPDATE SET 
                    heure_arrivee = COALESCE(EXCLUDED.heure_arrivee, presences.heure_arrivee),
                    heure_depart = COALESCE(EXCLUDED.heure_depart, presences.heure_depart),
                    statut = EXCLUDED.statut,
                    commentaire = EXCLUDED.commentaire
            """, (employe_id, date_val, heure_arrivee or None, heure_depart or None, statut, commentaire))
            conn.commit()
            flash("Présence enregistrée / modifiée avec succès", "success")
            cur.close(); conn.close()
            return redirect(url_for('presences'))

    # GET → formulaire
    cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom")
    employees = cur.fetchall()
    cur.close(); conn.close()
    return render_template('presence_form.html', employees=employees)


@app.route('/presences/delete/<int:id>', methods=['POST'])
@login_required
@role_required('admin', 'rh', 'manager')
def delete_presence(id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("DELETE FROM presences WHERE id = %s", (id,))
    conn.commit()
    cur.close(); conn.close()
    flash("Présence supprimée", "success")
    return redirect(url_for('presences'))

@app.route('/conges')
@login_required
def conges():
    conn = get_db()
    cur = get_cursor(conn)

    search = request.args.get('search', '').strip()
    statut = request.args.get('statut', '').strip()
    type_conge = request.args.get('type_conge', '').strip()
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

    cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom")
    employees = cur.fetchall()

    soldes = {}
    annee_courante = datetime.now().year
    if session.get('role') in ['admin', 'rh', 'manager']:
        for emp in employees:
            s = get_solde_conges(emp['id'], annee_courante)
            s['nom'] = f"{emp['prenom']} {emp['nom']}"
            soldes[emp['id']] = s

    cur.execute("SELECT DISTINCT type_conge FROM conges WHERE type_conge IS NOT NULL ORDER BY type_conge")
    types = [r['type_conge'] for r in cur.fetchall()]

    cur.close()
    conn.close()
    filters = {'search': search, 'statut': statut, 'type_conge': type_conge, 'date_debut': date_debut, 'date_fin': date_fin}
    return render_template('conges.html', conges=conges_list, employees=employees, soldes=soldes,
                           annee_courante=annee_courante, types=types, filters=filters,
                           pg=pg, page_items=page_list(pg['page'], pg['pages']),
                           base_qs=urlencode({k: v for k, v in filters.items() if v}))

@app.route('/conges/add', methods=['GET', 'POST'])
@login_required
def add_conge():
    with db_cursor(commit=True) as (conn, cur):
        if request.method == 'POST':
            employe_id = request.form.get('employe_id')
            type_conge = request.form.get('type_conge')
            date_debut = request.form.get('date_debut')
            date_fin = request.form.get('date_fin')
            motif = request.form.get('motif', '')
            
            if employe_id and type_conge and date_debut and date_fin:
                # Calculate days
                from datetime import datetime
                d1 = datetime.strptime(date_debut, '%Y-%m-%d')
                d2 = datetime.strptime(date_fin, '%Y-%m-%d')
                if d2 < d1:
                    flash("La date de fin ne peut pas être avant la date de début", "danger")
                    cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom")
                    employees = cur.fetchall()
                    return render_template('conge_form.html', employees=employees)
                nombre_jours = (d2 - d1).days + 1
                
                cur.execute("""
                    INSERT INTO conges (employe_id, type_conge, date_debut, date_fin, nombre_jours, motif, statut)
                    VALUES (%s, %s, %s, %s, %s, %s, 'en attente')
                """, (employe_id, type_conge, date_debut, date_fin, nombre_jours, motif))
                flash("Demande de congé soumise avec succès", "success")
                return redirect(url_for('conges'))
            else:
                flash("Veuillez remplir tous les champs obligatoires", "danger")
        
        # GET: load employees
        cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom")
        employees = cur.fetchall()
    return render_template('conge_form.html', employees=employees)

@app.route('/conges/update/<int:id>', methods=['POST'])
@login_required
@role_required('admin', 'rh', 'manager')
def update_conge(id):
    action = request.form.get('action')
    with db_cursor(commit=True) as (conn, cur):
        if action == 'approuver':
            cur.execute("UPDATE conges SET statut = 'approuvé' WHERE id = %s", (id,))
            # Update solde
            cur.execute("SELECT employe_id, nombre_jours, date_debut FROM conges WHERE id = %s", (id,))
            conge = cur.fetchone()
            if conge:
                from datetime import datetime
                annee = datetime.strptime(str(conge['date_debut']), '%Y-%m-%d').year
                # recalculer_solde() fait la somme exacte des congés approuvés
                # (plus fiable qu'un delta manuel qui pouvait désynchroniser le solde)
                recalculer_solde(conge['employe_id'], annee, cur=cur)
                flash("Congé approuvé et solde mis à jour", "success")
        elif action == 'refuser':
            cur.execute("UPDATE conges SET statut = 'refusé' WHERE id = %s", (id,))
            cur.execute("SELECT employe_id, date_debut FROM conges WHERE id = %s", (id,))
            conge = cur.fetchone()
            if conge:
                from datetime import datetime
                annee = datetime.strptime(str(conge['date_debut']), '%Y-%m-%d').year
                recalculer_solde(conge['employe_id'], annee, cur=cur)
            flash("Congé refusé", "info")
    return redirect(url_for('conges'))

@app.route('/conges/delete/<int:id>', methods=['POST'])
@login_required
@role_required('admin', 'rh', 'manager')
def delete_conge(id):
    with db_cursor(commit=True) as (conn, cur):
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


def generer_absences_automatiques(cur, date_jusqua=None, date_depuis=None):
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

    cur.execute("SELECT id, date_embauche FROM employes ORDER BY id")
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
                    SELECT d.id, d.titre, d.date_expiration, d.employe_id, e.nom, e.prenom
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
                                                 else "Votre document a expiré", message, "warning")

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
                    f"{details}\n\nConsultez la page Documents pour les renouveler si besoin."
                )
        except Exception:
            logger.exception("Erreur lors du job planifié d'alertes d'expiration de documents")



def _envoyer_email_texte(destinataires, sujet, corps):
    """Envoi d'un email texte simple (pas de template HTML dédié nécessaire ici)."""
    if not app.config.get('MAIL_USERNAME'):
        logger.info(f"[EMAIL DEMO] → {destinataires} | {sujet}")
        return True
    try:
        admin = get_admin_email()
        msg = Message(subject=sujet, recipients=destinataires, sender=admin)
        msg.body = corps
        mail.send(msg)
        return True
    except Exception as e:
        logger.error("Erreur envoi email texte: %s", e, exc_info=True)
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
                    SELECT a.employe_id, a.date, e.nom, e.prenom
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
                    if uid:
                        create_notification(
                            uid, "Absence enregistrée",
                            f"Aucune présence relevée le {a['date']} — une absence a été "
                            f"enregistrée automatiquement.",
                            "warning"
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

    with db_cursor() as (conn, cur):
        from_ = "absences a JOIN employes e ON a.employe_id = e.id"
        cur.execute(f"SELECT COUNT(*) AS nb FROM {from_} WHERE 1=1{where}", params)
        total = cur.fetchone()['nb']
        pg = pagination_info(total, page, per_page)
        offset = (pg['page'] - 1) * per_page
        cur.execute(f"SELECT a.*, e.nom, e.prenom FROM {from_} WHERE 1=1{where} ORDER BY a.date DESC LIMIT %s OFFSET %s", params + [per_page, offset])
        absences_list = cur.fetchall()
        cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom")
        employees = cur.fetchall()
    filters = {'search': search, 'employe_id': employe_id, 'date_debut': date_debut, 'date_fin': date_fin}
    return render_template('absences.html', absences=absences_list, employees=employees, nb_total=total, filters=filters,
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
                    ON CONFLICT (employe_id, date) DO UPDATE SET motif = EXCLUDED.motif
                """, (employe_id, date_val, motif, session.get('user_id')))
            flash("Absence non justifiée enregistrée", "success")
            return redirect(url_for('absences'))
        flash("Employé et date requis", "danger")
    with db_cursor() as (conn, cur):
        cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom")
        employees = cur.fetchall()
    return render_template('absence_form.html', employees=employees)


@app.route('/absences/delete/<int:id>', methods=['POST'])
@login_required
@role_required('admin', 'rh')
def delete_absence(id):
    with db_cursor(commit=True) as (conn, cur):
        # On mémorise la date supprimée pour empêcher la génération automatique
        # de la recréer immédiatement (sinon elle réapparaît au prochain affichage).
        cur.execute("SELECT employe_id, date FROM absences WHERE id = %s", (id,))
        row = cur.fetchone()
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
        nb = generer_absences_automatiques(cur)
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
        from_ = "permissions p JOIN employes e ON p.employe_id = e.id"
        cur.execute(f"SELECT COUNT(*) AS nb FROM {from_} WHERE 1=1{where}", params)
        total = cur.fetchone()['nb']
        pg = pagination_info(total, page, per_page)
        offset = (pg['page'] - 1) * per_page
        cur.execute(f"SELECT p.*, e.nom, e.prenom FROM {from_} WHERE 1=1{where} ORDER BY p.date_demande DESC LIMIT %s OFFSET %s", params + [per_page, offset])
        permissions_list = cur.fetchall()
        cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom")
        employees = cur.fetchall()
    filters = {'search': search, 'statut': statut, 'date_debut': date_debut, 'date_fin': date_fin}
    return render_template('permissions.html', permissions=permissions_list, employees=employees, filters=filters,
                           pg=pg, page_items=page_list(pg['page'], pg['pages']),
                           base_qs=urlencode({k: v for k, v in filters.items() if v}))


@app.route('/permissions/add', methods=['GET', 'POST'])
@login_required
def add_permission():
    with db_cursor(commit=True) as (conn, cur):
        if request.method == 'POST':
            employe_id = request.form.get('employe_id')
            date_debut = request.form.get('date_debut')
            date_fin = request.form.get('date_fin')
            motif = request.form.get('motif', '').strip()
            if employe_id and date_debut and date_fin:
                d1 = datetime.strptime(date_debut, '%Y-%m-%d')
                d2 = datetime.strptime(date_fin, '%Y-%m-%d')
                if d2 < d1:
                    flash("La date de fin ne peut pas être avant la date de début", "danger")
                    cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom")
                    employees = cur.fetchall()
                    return render_template('permission_form.html', employees=employees)
                nombre_jours = (d2 - d1).days + 1
                cur.execute("""
                    INSERT INTO permissions (employe_id, motif, date_debut, date_fin, nombre_jours, statut)
                    VALUES (%s, %s, %s, %s, %s, 'en attente')
                """, (employe_id, motif, date_debut, date_fin, nombre_jours))
                flash("Demande de permission soumise avec succès", "success")
                return redirect(url_for('permissions'))
            flash("Veuillez remplir tous les champs obligatoires", "danger")
        cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom")
        employees = cur.fetchall()
    return render_template('permission_form.html', employees=employees)


@app.route('/permissions/update/<int:id>', methods=['POST'])
@login_required
@role_required('admin', 'rh', 'manager')
def update_permission(id):
    action = request.form.get('action')
    with db_cursor(commit=True) as (conn, cur):
        if action == 'approuver':
            cur.execute("UPDATE permissions SET statut = 'approuvé' WHERE id = %s", (id,))
            flash("Permission approuvée", "success")
        elif action == 'refuser':
            cur.execute("UPDATE permissions SET statut = 'refusé' WHERE id = %s", (id,))
            flash("Permission refusée", "info")
    return redirect(url_for('permissions'))


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
        cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom, prenom")
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

    search = request.args.get('search', '').strip()
    selected_dept = request.args.get('departement', '').strip()
    sort = request.args.get('sort', 'nom')
    order = request.args.get('order', 'asc')
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 10

    where = ""
    params = []
    if search:
        where += " AND (LOWER(nom) LIKE %s OR LOWER(prenom) LIKE %s OR LOWER(poste) LIKE %s OR LOWER(email) LIKE %s)"
        s = f"%{search.lower()}%"
        params += [s, s, s, s]
    if selected_dept:
        where += " AND departement = %s"; params.append(selected_dept)

    sort_map = {'nom': 'nom, prenom', 'salaire': 'COALESCE(salaire, 0)', 'date_embauche': 'date_embauche', 'poste': 'poste'}
    sort_col = sort_map.get(sort, 'nom, prenom')
    direction = 'DESC' if order.lower() == 'desc' else 'ASC'
    order_clause = f" ORDER BY {sort_col} {direction}"

    cur.execute(f"SELECT COUNT(*) AS nb FROM employes WHERE 1=1{where}", params)
    total = cur.fetchone()['nb']
    pg = pagination_info(total, page, per_page)
    offset = (pg['page'] - 1) * per_page
    cur.execute(f"SELECT * FROM employes WHERE 1=1{where}{order_clause} LIMIT %s OFFSET %s", params + [per_page, offset])
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

    cur.execute("SELECT DISTINCT nom FROM departements ORDER BY nom")
    depts = cur.fetchall()

    cur.execute("""
        SELECT
            COUNT(*) as total,
            COALESCE(AVG(salaire), 0) as salaire_moyen,
            (SELECT COUNT(*) FROM departements) as nb_departements
        FROM employes
    """)
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
    cur.execute("SELECT * FROM employes WHERE id = %s", (id,))
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
    
    # Filters
    date_debut = request.args.get('date_debut', '')
    date_fin = request.args.get('date_fin', '')
    employe_id = request.args.get('employe_id', '')
    type_rapport = request.args.get('type', 'presences')
    statut = request.args.get('statut', '')
    
    cur.execute("SELECT id, prenom, nom FROM employes ORDER BY nom, prenom")
    employees = cur.fetchall()
    
    presences_data = []
    conges_data = []
    total_jours = 0
    
    if type_rapport == 'presences':
        q = """SELECT p.*, e.nom, e.prenom FROM presences p 
               JOIN employes e ON p.employe_id = e.id WHERE 1=1 """
        params = []
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
        q = """SELECT c.*, e.nom, e.prenom FROM conges c 
               JOIN employes e ON c.employe_id = e.id WHERE 1=1 """
        params = []
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

# ==================== DOCUMENTS (Upload) ====================
def _traiter_upload_document(conn, cur, employe_id, titre, description, date_expiration):
    """Valide et enregistre un document uploadé (fichier dans request.files).
    Retourne (ok: bool, message: str). Réutilisée par /documents et par
    l'upload direct depuis la fiche employé."""
    if 'fichier' not in request.files or request.files['fichier'].filename == '':
        return False, 'Aucun fichier sélectionné'

    fichier = request.files['fichier']
    if not (fichier and allowed_file(fichier.filename)):
        return False, 'Type de fichier non autorisé'

    # Validation du CONTENU (pas seulement de l'extension) pour éviter qu'un
    # fichier malveillant ne se déguise en image/document.
    detected = detect_file_type(fichier)
    if detected is None:
        return False, 'Le contenu du fichier ne correspond pas à son extension (type non autorisé).'

    filename = secure_filename(fichier.filename)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{timestamp}_{filename}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    fichier.save(filepath)

    cur.execute("""
        INSERT INTO documents (employe_id, titre, nom_fichier, chemin_fichier, type_fichier, taille, description, date_expiration)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (employe_id, titre or filename, filename, filepath,
          filename.rsplit('.', 1)[1].lower(), os.path.getsize(filepath), description, date_expiration))
    conn.commit()
    log_action(session.get('user_id'), session.get('username'), "UPLOAD_DOCUMENT", "document", None, f"{titre} ({filename})")
    return True, 'Document uploadé avec succès'


@app.route('/documents', methods=['GET', 'POST'])
@login_required
def documents():
    emp = get_current_employee()
    conn = get_db()
    cur = get_cursor(conn)
    
    if request.method == 'POST':
        titre = request.form.get('titre', '').strip()
        description = request.form.get('description', '').strip()
        employe_id = request.form.get('employe_id') or (emp['id'] if emp else None)
        date_expiration = request.form.get('date_expiration') or None

        ok, message = _traiter_upload_document(conn, cur, employe_id, titre, description, date_expiration)
        flash(message, 'success' if ok else 'danger')
    
    # List documents
    cur.execute("SELECT id, prenom, nom FROM employes ORDER BY nom")
    employees = cur.fetchall()
    
    cur.execute("""
        SELECT d.*, e.prenom, e.nom 
        FROM documents d 
        LEFT JOIN employes e ON d.employe_id = e.id 
        ORDER BY d.date_upload DESC LIMIT 80
    """)
    docs = cur.fetchall()
    
    cur.close(); conn.close()
    return render_template('documents.html', documents=docs, employees=employees, current_employee=emp,
                           today=date.today(),
                           bientot=date.today() + timedelta(days=SEUIL_ALERTE_EXPIRATION_DOCUMENTS_JOURS))


@app.route('/employes/<int:id>/documents/add', methods=['POST'])
@login_required
@role_required('admin', 'rh')
def add_employee_document(id):
    """Import direct d'un document (ex: contrat) depuis la fiche employé —
    l'employé est déjà connu, pas besoin de choisir dans une liste."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("SELECT id FROM employes WHERE id = %s", (id,))
    if not cur.fetchone():
        cur.close(); conn.close()
        flash("Employé introuvable", "danger")
        return redirect(url_for('index'))

    titre = request.form.get('titre', '').strip()
    description = request.form.get('description', '').strip()
    date_expiration = request.form.get('date_expiration') or None

    ok, message = _traiter_upload_document(conn, cur, id, titre, description, date_expiration)
    flash(message, 'success' if ok else 'danger')
    cur.close(); conn.close()
    return redirect(url_for('view_employee', id=id))


@app.route('/documents/delete/<int:doc_id>', methods=['POST'])
@login_required
@role_required('admin', 'rh')
def delete_document(doc_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("SELECT chemin_fichier FROM documents WHERE id = %s", (doc_id,))
    doc = cur.fetchone()
    if doc:
        try:
            if os.path.exists(doc['chemin_fichier']):
                os.remove(doc['chemin_fichier'])
        except: pass
        cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
        conn.commit()
        flash('Document supprimé', 'success')
    cur.close(); conn.close()
    return redirect(url_for('documents'))

@app.route('/documents/file/<int:doc_id>')
@login_required
def download_document(doc_id):
    with db_cursor() as (conn, cur):
        cur.execute("SELECT nom_fichier, employe_id FROM documents WHERE id = %s", (doc_id,))
        doc = cur.fetchone()
    if not doc:
        flash('Document introuvable.', 'danger')
        return redirect(url_for('documents'))
    # Un simple employé ne peut accéder qu'à SES PROPRES documents ; seuls
    # admin/rh/manager peuvent accéder aux documents de n'importe qui.
    if session.get('role') not in ('admin', 'rh', 'manager'):
        emp = get_current_employee()
        if not emp or doc.get('employe_id') != emp['id']:
            flash('Accès refusé : ce document ne vous appartient pas.', 'danger')
            return redirect(url_for('documents'))
    filename = secure_filename(doc['nom_fichier'])
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    # Anti-path-traversal : on ne sert que depuis le dossier uploads autorisé
    if os.path.dirname(os.path.abspath(filepath)) != os.path.abspath(app.config['UPLOAD_FOLDER']):
        flash('Accès refusé.', 'danger')
        return redirect(url_for('documents'))
    if not os.path.isfile(filepath):
        flash('Fichier indisponible sur le serveur.', 'danger')
        return redirect(url_for('documents'))
    resp = send_file(filepath, as_attachment=True, download_name=filename)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


# ==================== MAIN ====================

# ==================== STUB ROUTES (pour compatibilité templates) ====================
@app.route('/historique')
@login_required
def historique():
    conn = get_db()
    cur = get_cursor(conn)

    selected_employe = request.args.get('employe_id', '').strip()
    date_debut = request.args.get('date_debut', '').strip()
    date_fin = request.args.get('date_fin', '').strip()
    selected_statut = request.args.get('statut', '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 10

    where = ""
    params = []
    if selected_employe and selected_employe.isdigit():
        where += " AND p.employe_id = %s"; params.append(int(selected_employe))
    if date_debut:
        where += " AND p.date >= %s"; params.append(date_debut)
    if date_fin:
        where += " AND p.date <= %s"; params.append(date_fin)
    if selected_statut:
        where += " AND p.statut = %s"; params.append(selected_statut)

    from_ = "presences p JOIN employes e ON p.employe_id = e.id"
    # On récupère tout le jeu filtré (borné) pour les stats, puis on pagine l'affichage
    cur.execute(f"SELECT p.*, e.nom, e.prenom FROM {from_} WHERE 1=1{where} ORDER BY p.date DESC, p.heure_arrivee DESC LIMIT 5000", params)
    all_presences = cur.fetchall()

    total_pointages = len(all_presences)
    total_heures = 0.0
    employes_set = set()
    for p in all_presences:
        if p.get('heure_arrivee'):
            p['heure_arrivee'] = str(p['heure_arrivee'])[:5]
        if p.get('heure_depart'):
            p['heure_depart'] = str(p['heure_depart'])[:5]
        employes_set.add(p.get('employe_id'))
        try:
            if p.get('heure_arrivee') and p.get('heure_depart'):
                ha_parts = str(p['heure_arrivee']).split(':')[:2]
                hd_parts = str(p['heure_depart']).split(':')[:2]
                ha_min = int(ha_parts[0]) * 60 + int(ha_parts[1])
                hd_min = int(hd_parts[0]) * 60 + int(hd_parts[1])
                mins = hd_min - ha_min
                if mins > 0:
                    p['duree_heures'] = round(mins / 60, 1)
                    total_heures += mins / 60
                else:
                    p['duree_heures'] = None
            else:
                p['duree_heures'] = None
        except Exception:
            p['duree_heures'] = None
        p['retard_minutes'] = calculer_retard(p.get('heure_arrivee'))
        p['retard'] = p['retard_minutes'] > 0

    employes_concernes = len(employes_set)
    pg = pagination_info(total_pointages, page, per_page)
    offset = (pg['page'] - 1) * per_page
    presences_list = all_presences[offset:offset + per_page]

    cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom, prenom")
    employees = cur.fetchall()
    cur.close()
    conn.close()

    filters = {'employe_id': selected_employe, 'date_debut': date_debut, 'date_fin': date_fin, 'statut': selected_statut}
    return render_template('historique.html', presences=presences_list, employees=employees,
                           total_pointages=total_pointages, total_heures=round(total_heures, 1),
                           employes_concernes=employes_concernes, selected_employe=selected_employe,
                           date_debut=date_debut, date_fin=date_fin, selected_statut=selected_statut,
                           pg=pg, page_items=page_list(pg['page'], pg['pages']),
                           base_qs=urlencode({k: v for k, v in filters.items() if v}))
@app.route('/departements')
@login_required
def departements():
    conn = get_db()
    cur = get_cursor(conn)
    
    # Get departments with employee count
    cur.execute("""
        SELECT 
            d.id, 
            d.nom, 
            COALESCE(d.description, '') as description, 
            COALESCE(d.responsable, '') as responsable, 
            COUNT(e.id) as nb_employes 
        FROM departements d 
        LEFT JOIN employes e ON e.departement = d.nom 
        GROUP BY d.id, d.nom, d.description, d.responsable 
        ORDER BY d.nom
    """)
    departements = cur.fetchall()
    
    # Get totals
    cur.execute("SELECT COUNT(*) as total FROM departements")
    total_depts = cur.fetchone()['total'] or 0
    
    cur.execute("SELECT COUNT(*) as total FROM employes")
    total_employes = cur.fetchone()['total'] or 0
    
    cur.close()
    conn.close()
    
    return render_template('departements.html', 
                          departements=departements,
                          total_depts=total_depts,
                          total_employes=total_employes)


@app.route('/departements/add', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def add_departement():
    if request.method == 'POST':
        nom = request.form.get('nom', '').strip()
        description = request.form.get('description', '').strip()
        responsable = request.form.get('responsable', '').strip()
        
        if not nom:
            flash("Le nom du département est obligatoire", "danger")
        else:
            conn = get_db()
            cur = get_cursor(conn)
            try:
                cur.execute("""
                    INSERT INTO departements (nom, description, responsable)
                    VALUES (%s, %s, %s)
                """, (nom, description or None, responsable or None))
                conn.commit()
                flash(f"Département '{nom}' créé avec succès", "success")
                cur.close()
                conn.close()
                return redirect(url_for('departements'))
            except Exception as e:
                conn.rollback()
                if "unique" in str(e).lower():
                    flash("Ce nom de département existe déjà", "danger")
                else:
                    flash(f"Erreur : {str(e)}", "danger")
                cur.close()
                conn.close()
    
    return render_template('dept_form.html', dept=None, title="Nouveau département")

@app.route('/departements/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def edit_departement(id):
    conn = get_db()
    cur = get_cursor(conn)
    
    if request.method == 'POST':
        nom = request.form.get('nom', '').strip()
        description = request.form.get('description', '').strip()
        responsable = request.form.get('responsable', '').strip()
        
        if not nom:
            flash("Le nom du département est obligatoire", "danger")
        else:
            try:
                cur.execute("""
                    UPDATE departements 
                    SET nom=%s, description=%s, responsable=%s 
                    WHERE id=%s
                """, (nom, description or None, responsable or None, id))
                conn.commit()
                flash("Département mis à jour", "success")
                cur.close()
                conn.close()
                return redirect(url_for('departements'))
            except Exception as e:
                conn.rollback()
                flash(f"Erreur : {str(e)}", "danger")
    
    # GET: load current department
    cur.execute("SELECT * FROM departements WHERE id = %s", (id,))
    dept = cur.fetchone()
    cur.close()
    conn.close()
    
    if not dept:
        flash("Département introuvable", "danger")
        return redirect(url_for('departements'))
    
    return render_template('dept_form.html', dept=dept, title="Modifier le département")

@app.route('/departements/delete/<int:id>', methods=['POST'])
@login_required
@role_required('admin')
def delete_departement(id):
    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute("DELETE FROM departements WHERE id = %s", (id,))
        conn.commit()
        flash("Département supprimé", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Erreur lors de la suppression : {str(e)}", "danger")
    cur.close()
    conn.close()
    return redirect(url_for('departements'))


@app.route('/utilisateurs')
@login_required
@role_required('admin', 'rh')
def utilisateurs_page():
    """Page de gestion des comptes utilisateurs (admin/rh)."""
    with db_cursor() as (conn, cur):
        cur.execute("""
            SELECT u.id, u.username, u.role, u.employe_id, e.nom, e.prenom
            FROM users u
            LEFT JOIN employes e ON u.employe_id = e.id
            ORDER BY u.role, u.username
        """)
        users_list = cur.fetchall()
        cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom, prenom")
        employees = cur.fetchall()
    return render_template('utilisateurs.html', users=users_list, employees=employees)


@app.route('/utilisateurs/<int:user_id>/edit', methods=['POST'])
@login_required
@role_required('admin', 'rh')
def edit_utilisateur(user_id):
    """Modifie le rôle et/ou l'employé lié d'un utilisateur.
    Seul un admin peut promouvoir/rétrograder vers ou depuis le rôle 'admin'."""
    nouveau_role = request.form.get('role', '').strip()
    employe_id = request.form.get('employe_id') or None
    roles_valides = ['admin', 'rh', 'manager', 'employe']

    if nouveau_role not in roles_valides:
        flash("Rôle invalide.", "danger")
        return redirect(url_for('utilisateurs_page'))

    with db_cursor() as (conn, cur):
        cur.execute("SELECT username, role FROM users WHERE id = %s", (user_id,))
        cible = cur.fetchone()

    if not cible:
        flash("Utilisateur introuvable.", "danger")
        return redirect(url_for('utilisateurs_page'))

    # Seul un admin peut attribuer ou retirer le rôle admin
    if (nouveau_role == 'admin' or cible['role'] == 'admin') and session.get('role') != 'admin':
        flash("Seul un administrateur peut modifier un compte administrateur.", "danger")
        return redirect(url_for('utilisateurs_page'))

    # Empêche de se rétrograder soi-même par erreur (perte d'accès admin)
    if user_id == session.get('user_id') and nouveau_role != cible['role']:
        flash("Vous ne pouvez pas modifier votre propre rôle.", "danger")
        return redirect(url_for('utilisateurs_page'))

    with db_cursor(commit=True) as (conn, cur):
        cur.execute("UPDATE users SET role = %s, employe_id = %s WHERE id = %s",
                    (nouveau_role, employe_id, user_id))

    log_action(session.get('user_id'), session.get('username'), "UPDATE_USER", "user", user_id,
              f"{cible['username']} → rôle={nouveau_role}, employe_id={employe_id}")
    flash(f"Utilisateur '{cible['username']}' mis à jour.", "success")
    return redirect(url_for('utilisateurs_page'))


@app.route('/utilisateurs/<int:user_id>/reset-password', methods=['POST'])
@login_required
@role_required('admin', 'rh')
def reset_password_utilisateur(user_id):
    """Réinitialise le mot de passe d'un utilisateur (admin/rh)."""
    nouveau_mdp = request.form.get('nouveau_mdp', '')

    if len(nouveau_mdp) < 6:
        flash("Le mot de passe doit contenir au moins 6 caractères.", "danger")
        return redirect(url_for('utilisateurs_page'))

    with db_cursor() as (conn, cur):
        cur.execute("SELECT username, role FROM users WHERE id = %s", (user_id,))
        cible = cur.fetchone()

    if not cible:
        flash("Utilisateur introuvable.", "danger")
        return redirect(url_for('utilisateurs_page'))

    if cible['role'] == 'admin' and session.get('role') != 'admin':
        flash("Seul un administrateur peut réinitialiser le mot de passe d'un administrateur.", "danger")
        return redirect(url_for('utilisateurs_page'))

    with db_cursor(commit=True) as (conn, cur):
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                    (generate_password_hash(nouveau_mdp), user_id))

    log_action(session.get('user_id'), session.get('username'), "RESET_PASSWORD", "user", user_id,
              f"Mot de passe réinitialisé pour {cible['username']}")
    flash(f"Mot de passe de '{cible['username']}' réinitialisé.", "success")
    return redirect(url_for('utilisateurs_page'))


@app.route('/utilisateurs/<int:user_id>/delete', methods=['POST'])
@login_required
@role_required('admin', 'rh')
def delete_utilisateur(user_id):
    """Supprime un compte utilisateur, avec garde-fous de sécurité."""
    if user_id == session.get('user_id'):
        flash("Vous ne pouvez pas supprimer votre propre compte.", "danger")
        return redirect(url_for('utilisateurs_page'))

    with db_cursor() as (conn, cur):
        cur.execute("SELECT username, role FROM users WHERE id = %s", (user_id,))
        cible = cur.fetchone()

        if not cible:
            flash("Utilisateur introuvable.", "danger")
            return redirect(url_for('utilisateurs_page'))

        if cible['role'] == 'admin':
            if session.get('role') != 'admin':
                flash("Seul un administrateur peut supprimer un compte administrateur.", "danger")
                return redirect(url_for('utilisateurs_page'))
            cur.execute("SELECT COUNT(*) as total FROM users WHERE role = 'admin'")
            if cur.fetchone()['total'] <= 1:
                flash("Impossible de supprimer le dernier compte administrateur.", "danger")
                return redirect(url_for('utilisateurs_page'))

    with db_cursor(commit=True) as (conn, cur):
        cur.execute("DELETE FROM notifications WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))

    log_action(session.get('user_id'), session.get('username'), "DELETE_USER", "user", user_id,
              f"Utilisateur '{cible['username']}' supprimé")
    flash(f"Utilisateur '{cible['username']}' supprimé.", "success")
    return redirect(url_for('utilisateurs_page'))


@app.route('/register', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'rh')
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        # Validation
        if not username or not password:
            flash("Veuillez remplir tous les champs obligatoires.", "danger")
            return render_template('register.html')
        
        if len(username) < 3:
            flash("Le nom d'utilisateur doit contenir au moins 3 caractères.", "danger")
            return render_template('register.html')
        
        if len(password) < 6:
            flash("Le mot de passe doit contenir au moins 6 caractères.", "danger")
            return render_template('register.html')
        
        if password != confirm_password:
            flash("Les mots de passe ne correspondent pas.", "danger")
            return render_template('register.html')
        
        conn = get_db()
        cur = get_cursor(conn)
        
        try:
            # Check if username already exists
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                flash("Ce nom d'utilisateur est déjà utilisé.", "danger")
                cur.close()
                conn.close()
                return render_template('register.html')
            
            # Create the user (default role = 'employe')
            password_hash = generate_password_hash(password)
            cur.execute(
                "INSERT INTO users (username, password_hash, role, employe_id) VALUES (%s, %s, %s, %s)",
                (username, password_hash, 'employe', None)
            )
            conn.commit()
            
            flash("Compte créé avec succès.", "success")
            cur.close()
            conn.close()
            return redirect(url_for('utilisateurs_page'))
            
        except Exception as e:
            conn.rollback()
            flash(f"Une erreur est survenue lors de la création du compte : {str(e)}", "danger")
            logger.error("Erreur register: %s", e, exc_info=True)
        finally:
            cur.close()
            conn.close()
    
    return render_template('register.html')

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
            """, (nom, prenom, poste, departement, email, telephone, salaire, date_embauche))
            conn.commit()
            flash("Employé ajouté avec succès", "success")
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
    cur.execute("""
        SELECT c.date_debut, c.date_fin, c.nombre_jours, c.statut,
               e.prenom, e.nom
        FROM conges c
        JOIN employes e ON c.employe_id = e.id
        WHERE c.statut = 'approuvé'
          AND EXTRACT(YEAR FROM c.date_debut) = %s
        ORDER BY c.date_debut
    """, (annee,))
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


# ==================== GESTION DES MATÉRIELS ====================
# Stock de fournitures et d'équipements par département (papiers, stylos,
# classeurs, PC...). Chaque entrée/sortie est tracée dans
# `materiels_mouvements`, et le stock `materiels.quantite` en découle.

MATERIEL_CATEGORIES = [
    ('fourniture', '✏️ Fourniture de bureau'),
    ('papeterie', '📄 Papeterie'),
    ('mobilier', '🪑 Mobilier'),
    ('informatique', '💻 Informatique'),
    ('entretien', '🧹 Entretien'),
    ('autre', '📦 Autre'),
]
MATERIEL_CATEGORIES_DICT = dict(MATERIEL_CATEGORIES)

# Catégories dont les articles sont attribuables durablement à un employé
# (un PC se remet à quelqu'un ; une ramette de papier se consomme).
MATERIEL_CAT_ATTRIBUABLES = {'informatique', 'mobilier', 'autre'}


def _peut_gerer_materiels():
    return session.get('role') in ('admin', 'rh', 'manager')


@app.context_processor
def inject_materiel_perms():
    return {'peut_gerer_materiels': _peut_gerer_materiels()}


def _notifier_stock_bas(cur, materiel_id):
    """Notifie les gestionnaires quand un stock passe sous son seuil.

    Anti-spam : `alerte_envoyee` empêche de renvoyer la notification tant que
    le stock n'est pas repassé au-dessus du seuil.
    """
    try:
        cur.execute("""
            SELECT m.id, m.nom, m.quantite, m.seuil_alerte, m.unite,
                   m.alerte_envoyee, COALESCE(d.nom, 'Sans département') AS dept
            FROM materiels m
            LEFT JOIN departements d ON d.id = m.departement_id
            WHERE m.id = %s
        """, (materiel_id,))
        m = cur.fetchone()
        if not m or not m['seuil_alerte']:
            return

        en_alerte = m['quantite'] <= m['seuil_alerte']

        if en_alerte and not m['alerte_envoyee']:
            cur.execute("SELECT id FROM users WHERE role IN ('admin', 'rh', 'manager')")
            for u in cur.fetchall():
                create_notification(
                    u['id'],
                    f"Stock bas : {m['nom']}",
                    f"Il reste {m['quantite']} {m['unite']} de « {m['nom']} » "
                    f"({m['dept']}), seuil d'alerte : {m['seuil_alerte']}.",
                    "warning",
                )
            cur.execute("UPDATE materiels SET alerte_envoyee = TRUE WHERE id = %s", (materiel_id,))
        elif not en_alerte and m['alerte_envoyee']:
            # Stock réapprovisionné : on réarme l'alerte pour la prochaine fois.
            cur.execute("UPDATE materiels SET alerte_envoyee = FALSE WHERE id = %s", (materiel_id,))
    except Exception as e:
        logger.error("Erreur alerte stock matériel %s: %s", materiel_id, e, exc_info=True)


def _enregistrer_mouvement(cur, materiel_id, type_mouvement, quantite,
                           employe_id=None, motif=None):
    """Écrit un mouvement et met à jour le stock. Retourne le nouveau stock.

    Lève ValueError si une sortie dépasse le stock disponible.
    """
    cur.execute("SELECT quantite FROM materiels WHERE id = %s FOR UPDATE", (materiel_id,))
    row = cur.fetchone()
    if not row:
        raise ValueError("Matériel introuvable")

    stock = row['quantite']
    if type_mouvement == 'sortie':
        if quantite > stock:
            raise ValueError(f"Stock insuffisant : il ne reste que {stock} unité(s)")
        nouveau = stock - quantite
    else:
        nouveau = stock + quantite

    cur.execute("UPDATE materiels SET quantite = %s WHERE id = %s", (nouveau, materiel_id))
    cur.execute("""
        INSERT INTO materiels_mouvements
            (materiel_id, type_mouvement, quantite, employe_id, motif, user_id, username)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (materiel_id, type_mouvement, quantite, employe_id, motif or None,
          session.get('user_id'), session.get('username')))

    _notifier_stock_bas(cur, materiel_id)
    return nouveau


@app.route('/materiels')
@login_required
def materiels():
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 10
    search = (request.args.get('search') or '').strip()
    dept_id = request.args.get('departement', type=int)
    categorie = (request.args.get('categorie') or '').strip()
    etat = (request.args.get('etat') or '').strip()  # '' | 'alerte' | 'rupture'

    where, params = [], []
    if search:
        where.append("m.nom ILIKE %s")
        params.append(f"%{search}%")
    if dept_id:
        where.append("m.departement_id = %s")
        params.append(dept_id)
    if categorie:
        where.append("m.categorie = %s")
        params.append(categorie)
    if etat == 'alerte':
        where.append("m.seuil_alerte > 0 AND m.quantite <= m.seuil_alerte")
    elif etat == 'rupture':
        where.append("m.quantite = 0")
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    with db_cursor() as (conn, cur):
        cur.execute(f"SELECT COUNT(*) AS total FROM materiels m {clause}", params)
        total = cur.fetchone()['total'] or 0
        pag = pagination_info(total, page, per_page)

        cur.execute(f"""
            SELECT m.*, COALESCE(d.nom, '—') AS departement_nom,
                   COALESCE((
                       SELECT SUM(a.quantite) FROM materiels_attributions a
                       WHERE a.materiel_id = m.id AND a.date_retour IS NULL
                   ), 0) AS nb_attribues
            FROM materiels m
            LEFT JOIN departements d ON d.id = m.departement_id
            {clause}
            ORDER BY (m.seuil_alerte > 0 AND m.quantite <= m.seuil_alerte) DESC,
                     d.nom NULLS LAST, m.nom
            LIMIT %s OFFSET %s
        """, params + [per_page, (pag['page'] - 1) * per_page])
        liste = cur.fetchall()

        cur.execute("""
            SELECT COUNT(*) AS nb_articles,
                   COALESCE(SUM(quantite), 0) AS stock_total,
                   COUNT(*) FILTER (WHERE seuil_alerte > 0 AND quantite <= seuil_alerte) AS nb_alertes,
                   COUNT(*) FILTER (WHERE quantite = 0) AS nb_ruptures
            FROM materiels
        """)
        stats = cur.fetchone()

        cur.execute("SELECT id, nom FROM departements ORDER BY nom")
        depts = cur.fetchall()

    filters = {'search': search, 'departement': dept_id or '',
               'categorie': categorie, 'etat': etat}
    return render_template('materiels.html',
                           materiels=liste,
                           stats=stats,
                           departements=depts,
                           categories=MATERIEL_CATEGORIES,
                           categories_dict=MATERIEL_CATEGORIES_DICT,
                           pg=pag,
                           page_items=page_list(pag['page'], pag['pages']),
                           base_qs=urlencode({k: v for k, v in filters.items() if v}),
                           filters=filters)


@app.route('/materiels/add', methods=['GET', 'POST'])
@login_required
@role_required('rh', 'manager')
def add_materiel():
    if request.method == 'POST':
        nom = (request.form.get('nom') or '').strip()
        categorie = (request.form.get('categorie') or 'fourniture').strip()
        dept_id = request.form.get('departement_id', type=int)
        quantite = request.form.get('quantite', type=int) or 0
        seuil = request.form.get('seuil_alerte', type=int) or 0
        unite = (request.form.get('unite') or 'unité').strip()
        description = (request.form.get('description') or '').strip()

        erreur = None
        if not nom:
            erreur = "Le nom du matériel est obligatoire"
        elif not dept_id:
            erreur = "Le département est obligatoire"
        elif quantite < 0 or seuil < 0:
            erreur = "Les quantités ne peuvent pas être négatives"

        if erreur:
            flash(erreur, "danger")
        else:
            try:
                with db_cursor(commit=True) as (conn, cur):
                    cur.execute("""
                        INSERT INTO materiels
                            (nom, categorie, departement_id, quantite, seuil_alerte, unite, description)
                        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
                    """, (nom, categorie, dept_id, quantite, seuil, unite, description or None))
                    new_id = cur.fetchone()['id']
                    # Stock initial = premier mouvement d'entrée, pour que
                    # l'historique reste cohérent avec le stock affiché.
                    if quantite > 0:
                        cur.execute("""
                            INSERT INTO materiels_mouvements
                                (materiel_id, type_mouvement, quantite, motif, user_id, username)
                            VALUES (%s, 'entree', %s, 'Stock initial', %s, %s)
                        """, (new_id, quantite, session.get('user_id'), session.get('username')))
                    _notifier_stock_bas(cur, new_id)
                log_action(session.get('user_id'), session.get('username'),
                           "Création matériel", "materiel", new_id, nom)
                flash(f"Matériel « {nom} » ajouté avec succès", "success")
                return redirect(url_for('materiels'))
            except Exception as e:
                logger.error("Erreur ajout matériel: %s", e, exc_info=True)
                flash(f"Erreur : {e}", "danger")

    with db_cursor() as (conn, cur):
        cur.execute("SELECT id, nom FROM departements ORDER BY nom")
        depts = cur.fetchall()
    return render_template('materiel_form.html', materiel=None, departements=depts,
                           categories=MATERIEL_CATEGORIES, title="Nouveau matériel")


@app.route('/materiels/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@role_required('rh', 'manager')
def edit_materiel(id):
    with db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM materiels WHERE id = %s", (id,))
        materiel = cur.fetchone()
        cur.execute("SELECT id, nom FROM departements ORDER BY nom")
        depts = cur.fetchall()

    if not materiel:
        flash("Matériel introuvable", "danger")
        return redirect(url_for('materiels'))

    if request.method == 'POST':
        nom = (request.form.get('nom') or '').strip()
        categorie = (request.form.get('categorie') or 'fourniture').strip()
        dept_id = request.form.get('departement_id', type=int)
        seuil = request.form.get('seuil_alerte', type=int) or 0
        unite = (request.form.get('unite') or 'unité').strip()
        description = (request.form.get('description') or '').strip()

        if not nom or not dept_id:
            flash("Le nom et le département sont obligatoires", "danger")
        else:
            try:
                with db_cursor(commit=True) as (conn, cur):
                    # La quantité n'est volontairement pas modifiable ici :
                    # elle ne change que via les mouvements (traçabilité).
                    cur.execute("""
                        UPDATE materiels
                        SET nom = %s, categorie = %s, departement_id = %s,
                            seuil_alerte = %s, unite = %s, description = %s
                        WHERE id = %s
                    """, (nom, categorie, dept_id, seuil, unite, description or None, id))
                    _notifier_stock_bas(cur, id)
                log_action(session.get('user_id'), session.get('username'),
                           "Modification matériel", "materiel", id, nom)
                flash("Matériel mis à jour", "success")
                return redirect(url_for('materiels'))
            except Exception as e:
                logger.error("Erreur édition matériel: %s", e, exc_info=True)
                flash(f"Erreur : {e}", "danger")

    return render_template('materiel_form.html', materiel=materiel, departements=depts,
                           categories=MATERIEL_CATEGORIES, title="Modifier le matériel")


@app.route('/materiels/<int:id>')
@login_required
def view_materiel(id):
    with db_cursor() as (conn, cur):
        cur.execute("""
            SELECT m.*, COALESCE(d.nom, '—') AS departement_nom
            FROM materiels m
            LEFT JOIN departements d ON d.id = m.departement_id
            WHERE m.id = %s
        """, (id,))
        materiel = cur.fetchone()
        if not materiel:
            flash("Matériel introuvable", "danger")
            return redirect(url_for('materiels'))

        cur.execute("""
            SELECT mv.*, e.nom AS emp_nom, e.prenom AS emp_prenom
            FROM materiels_mouvements mv
            LEFT JOIN employes e ON e.id = mv.employe_id
            WHERE mv.materiel_id = %s
            ORDER BY mv.date_mouvement DESC, mv.id DESC
            LIMIT 50
        """, (id,))
        mouvements = cur.fetchall()

        cur.execute("""
            SELECT a.*, e.nom AS emp_nom, e.prenom AS emp_prenom
            FROM materiels_attributions a
            JOIN employes e ON e.id = a.employe_id
            WHERE a.materiel_id = %s
            ORDER BY a.date_retour NULLS FIRST, a.date_attribution DESC
        """, (id,))
        attributions = cur.fetchall()

        # Employés du même département en priorité pour l'attribution.
        cur.execute("""
            SELECT e.id, e.nom, e.prenom
            FROM employes e
            LEFT JOIN departements d ON d.nom = e.departement
            ORDER BY (d.id = %s) DESC NULLS LAST, e.nom, e.prenom
        """, (materiel['departement_id'],))
        employes = cur.fetchall()

    return render_template('materiel_detail.html',
                           materiel=materiel,
                           mouvements=mouvements,
                           attributions=attributions,
                           employes=employes,
                           categories_dict=MATERIEL_CATEGORIES_DICT,
                           attribuable=materiel['categorie'] in MATERIEL_CAT_ATTRIBUABLES)


@app.route('/materiels/<int:id>/mouvement', methods=['POST'])
@login_required
@role_required('rh', 'manager')
def add_mouvement_materiel(id):
    type_mvt = (request.form.get('type_mouvement') or '').strip()
    quantite = request.form.get('quantite', type=int) or 0
    motif = (request.form.get('motif') or '').strip()
    employe_id = request.form.get('employe_id', type=int)

    if type_mvt not in ('entree', 'sortie'):
        flash("Type de mouvement invalide", "danger")
    elif quantite <= 0:
        flash("La quantité doit être supérieure à zéro", "danger")
    else:
        try:
            with db_cursor(commit=True) as (conn, cur):
                nouveau = _enregistrer_mouvement(cur, id, type_mvt, quantite,
                                                 employe_id or None, motif)
            libelle = "Entrée" if type_mvt == 'entree' else "Sortie"
            log_action(session.get('user_id'), session.get('username'),
                       f"{libelle} stock", "materiel", id, f"{quantite}")
            flash(f"{libelle} de {quantite} enregistrée — nouveau stock : {nouveau}", "success")
        except ValueError as e:
            flash(str(e), "danger")
        except Exception as e:
            logger.error("Erreur mouvement matériel: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")

    return redirect(url_for('view_materiel', id=id))


@app.route('/materiels/<int:id>/attribuer', methods=['POST'])
@login_required
@role_required('rh', 'manager')
def attribuer_materiel(id):
    employe_id = request.form.get('employe_id', type=int)
    quantite = request.form.get('quantite', type=int) or 1
    commentaire = (request.form.get('commentaire') or '').strip()

    if not employe_id:
        flash("Veuillez sélectionner un employé", "danger")
    elif quantite <= 0:
        flash("La quantité doit être supérieure à zéro", "danger")
    else:
        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("SELECT nom, prenom FROM employes WHERE id = %s", (employe_id,))
                emp = cur.fetchone()
                if not emp:
                    raise ValueError("Employé introuvable")
                nom_complet = f"{emp['prenom']} {emp['nom']}"
                # L'attribution retire physiquement l'article du stock.
                _enregistrer_mouvement(cur, id, 'sortie', quantite, employe_id,
                                       f"Attribution à {nom_complet}")
                cur.execute("""
                    INSERT INTO materiels_attributions
                        (materiel_id, employe_id, quantite, commentaire)
                    VALUES (%s, %s, %s, %s)
                """, (id, employe_id, quantite, commentaire or None))
            log_action(session.get('user_id'), session.get('username'),
                       "Attribution matériel", "materiel", id, nom_complet)
            flash(f"Matériel attribué à {nom_complet}", "success")
        except ValueError as e:
            flash(str(e), "danger")
        except Exception as e:
            logger.error("Erreur attribution matériel: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")

    return redirect(url_for('view_materiel', id=id))


@app.route('/materiels/attribution/<int:attribution_id>/retour', methods=['POST'])
@login_required
@role_required('rh', 'manager')
def retour_materiel(attribution_id):
    materiel_id = request.form.get('materiel_id', type=int)
    try:
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("""
                SELECT a.*, e.nom AS emp_nom, e.prenom AS emp_prenom
                FROM materiels_attributions a
                JOIN employes e ON e.id = a.employe_id
                WHERE a.id = %s
            """, (attribution_id,))
            attr = cur.fetchone()
            if not attr:
                raise ValueError("Attribution introuvable")
            if attr['date_retour']:
                raise ValueError("Ce matériel a déjà été retourné")

            materiel_id = attr['materiel_id']
            cur.execute("UPDATE materiels_attributions SET date_retour = CURRENT_DATE WHERE id = %s",
                        (attribution_id,))
            # Le retour réintègre l'article dans le stock.
            _enregistrer_mouvement(
                cur, materiel_id, 'entree', attr['quantite'], attr['employe_id'],
                f"Retour de {attr['emp_prenom']} {attr['emp_nom']}")
        flash("Retour enregistré, stock réintégré", "success")
    except ValueError as e:
        flash(str(e), "danger")
    except Exception as e:
        logger.error("Erreur retour matériel: %s", e, exc_info=True)
        flash(f"Erreur : {e}", "danger")

    return redirect(url_for('view_materiel', id=materiel_id) if materiel_id
                    else url_for('materiels'))


@app.route('/materiels/delete/<int:id>', methods=['POST'])
@login_required
@role_required('rh')
def delete_materiel(id):
    try:
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT nom FROM materiels WHERE id = %s", (id,))
            row = cur.fetchone()
            # ON DELETE CASCADE supprime mouvements et attributions liés.
            cur.execute("DELETE FROM materiels WHERE id = %s", (id,))
        log_action(session.get('user_id'), session.get('username'),
                   "Suppression matériel", "materiel", id, row['nom'] if row else None)
        flash("Matériel supprimé", "success")
    except Exception as e:
        logger.error("Erreur suppression matériel: %s", e, exc_info=True)
        flash(f"Erreur : {e}", "danger")
    return redirect(url_for('materiels'))


@app.route('/departements/<int:id>/materiels')
@login_required
def materiels_departement(id):
    """Raccourci : stock filtré sur un département."""
    return redirect(url_for('materiels', departement=id))


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