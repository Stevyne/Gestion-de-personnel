from flask import Flask, render_template, request, redirect, url_for, flash, session, make_response, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2.extras import RealDictCursor
import os
import logging
from datetime import date, datetime, timedelta
from functools import wraps
import io

from flask_mail import Mail, Message

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

@app.context_processor
def inject_context():
    try:
        user_id = session.get('user_id')
        unread_count = len(get_unread_notifications(user_id)) if user_id else 0
    except:
        unread_count = 0
    return {
        'unread_notifications': unread_count,
        'current_role': session.get('role', 'employe'),
        'role_label': session.get('role_label') or get_role_label(session.get('role', 'employe'))
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
                cur.execute("""
                    INSERT INTO soldes_conges (employe_id, annee, jours_acquis, jours_utilises)
                    VALUES (%s, %s, 25, 0)
                    RETURNING *
                """, (employe_id, annee))
                solde = cur.fetchone()

            acquis = float(solde.get('jours_acquis') or 25)
            utilises = float(solde.get('jours_utilises') or 0)
            return {
                'jours_acquis': acquis,
                'jours_utilises': utilises,
                'jours_restants': round(acquis - utilises, 1),
                'annee': annee
            }
    except Exception as e:
        logger.error("Erreur get_solde_conges: %s", e, exc_info=True)
        return {'jours_acquis': 25, 'jours_utilises': 0, 'jours_restants': 25, 'annee': annee}


def mettre_a_jour_solde(employe_id, jours_delta, annee=None):
    """Ajoute ou soustrait des jours du solde (appelé lors de l'approbation/refus)"""
    if annee is None:
        annee = datetime.now().year
    try:
        conn = get_db()
        cur = get_cursor(conn)
        cur.execute("""
            INSERT INTO soldes_conges (employe_id, annee, jours_acquis, jours_utilises)
            VALUES (%s, %s, 25, %s)
            ON CONFLICT (employe_id, annee) 
            DO UPDATE SET jours_utilises = GREATEST(0, soldes_conges.jours_utilises + %s)
        """, (employe_id, annee, jours_delta, jours_delta))
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
              AND EXTRACT(YEAR FROM date_debut) = %s
        """, (employe_id, annee))
        total = float(cur.fetchone()['total'] or 0)

        cur.execute("""
            INSERT INTO soldes_conges (employe_id, annee, jours_acquis, jours_utilises)
            VALUES (%s, %s, 25, %s)
            ON CONFLICT (employe_id, annee) 
            DO UPDATE SET jours_utilises = %s
        """, (employe_id, annee, total, total))
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

    cur.execute('''CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username VARCHAR(80) UNIQUE, password_hash VARCHAR(255), role VARCHAR(20) DEFAULT 'employe', employe_id INTEGER REFERENCES employes(id))''')
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
        cur.execute("SELECT id FROM employes")
        for emp in cur.fetchall():
            cur.execute("""
                INSERT INTO soldes_conges (employe_id, annee, jours_acquis, jours_utilises)
                VALUES (%s, %s, 25, 0)
                ON CONFLICT (employe_id, annee) DO NOTHING
            """, (emp['id'], annee_courante))

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
        recent_conges=recent_conges
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

    # Normal GET: display the page with filters
    search = request.args.get('search', '').strip().lower()
    date_filter = request.args.get('date', '').strip()

    # Construction requête filtrée
    q = "SELECT p.*, e.nom, e.prenom FROM presences p JOIN employes e ON p.employe_id = e.id "
    params = []
    conditions = []

    if search:
        conditions.append("(LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])

    if date_filter:
        conditions.append("p.date = %s")
        params.append(date_filter)

    if conditions:
        q += " WHERE " + " AND ".join(conditions)

    q += " ORDER BY p.date DESC, p.heure_arrivee DESC LIMIT 100"

    cur.execute(q, params)
    presences_list = cur.fetchall()

    for p in presences_list:
        # Convert datetime.time → string (ex: "09:15")
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
    return render_template('presences.html', presences=presences_list, employees=employees, today=today, retards_aujourdhui=retards_aujourdhui, nb_retards=nb_retards, total_retards_minutes=total_retards_minutes)

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
    cur.execute("SELECT c.*, e.nom, e.prenom FROM conges c JOIN employes e ON c.employe_id = e.id ORDER BY c.date_demande DESC")
    conges_list = cur.fetchall()
    
    # Always fetch employees (needed for the "+ Nouvelle demande" button)
    cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom")
    employees = cur.fetchall()
    
    # Soldes de congés pour les rôles privilégiés (admin/rh/manager)
    soldes = {}
    annee_courante = datetime.now().year
    if session.get('role') in ['admin', 'rh', 'manager']:
        for emp in employees:
            s = get_solde_conges(emp['id'], annee_courante)
            s['nom'] = f"{emp['prenom']} {emp['nom']}"
            soldes[emp['id']] = s
    
    cur.close()
    conn.close()
    return render_template('conges.html', conges=conges_list, employees=employees, soldes=soldes, annee_courante=annee_courante)

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
            INSERT INTO soldes_conges (employe_id, annee, jours_acquis, jours_utilises)
            VALUES (%s, %s, %s, 0)
            ON CONFLICT (employe_id, annee)
            DO UPDATE SET jours_acquis = %s
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

    cur.execute("SELECT * FROM employes ORDER BY nom, prenom")
    employes = cur.fetchall()

    search = request.args.get('search', '').strip()
    selected_dept = request.args.get('departement', '').strip()
    sort = request.args.get('sort', 'nom')
    order = request.args.get('order', 'asc')
    
    # Dynamic filter query
    query = "SELECT * FROM employes WHERE 1=1"
    params = []
    
    if search:
        query += """ AND (
            LOWER(nom) LIKE %s OR 
            LOWER(prenom) LIKE %s OR 
            LOWER(poste) LIKE %s OR 
            LOWER(email) LIKE %s
        )"""
        s = f"%{search.lower()}%"
        params.extend([s, s, s, s])
    
    if selected_dept:
        query += " AND departement = %s"
        params.append(selected_dept)
    
    # Sorting
    sort_map = {
        'nom': 'nom, prenom',
        'salaire': 'COALESCE(salaire, 0)',
        'date_embauche': 'date_embauche',
        'poste': 'poste'
    }
    sort_col = sort_map.get(sort, 'nom, prenom')
    direction = 'DESC' if order.lower() == 'desc' else 'ASC'
    query += f" ORDER BY {sort_col} {direction}"
    
    cur.execute(query, params)
    employes = cur.fetchall()
    
        # Enrich with last presence info (for better view)
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

    # Requête corrigée avec les bons alias
    cur.execute("""
        SELECT 
            COUNT(*) as total,
            COALESCE(AVG(salaire), 0) as salaire_moyen,
            (SELECT COUNT(*) FROM departements) as nb_departements
        FROM employes
    """)
    stats = cur.fetchone()

    # Conversion en dict pour éviter les erreurs RealDictRow
    stats = dict(stats) if stats else {
        'total': 0,
        'salaire_moyen': 0,
        'nb_departements': 0
    }

    cur.close()
    conn.close()

    return render_template('index.html',
                           employes=employes,
                           depts=depts,
                           search='',
                           selected_dept='',
                           stats=stats)

@app.route('/employes/<int:id>')
@login_required
def view_employee(id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM employes WHERE id = %s", (id,))
    employee = cur.fetchone()
    cur.close()
    conn.close()
    if not employee:
        flash("Employé introuvable", "danger")
        return redirect(url_for('index'))
    return render_template('detail.html', employee=employee)

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
        
        if 'fichier' not in request.files:
            flash('Aucun fichier sélectionné', 'danger')
            return redirect(url_for('documents'))
        
        fichier = request.files['fichier']
        if fichier.filename == '':
            flash('Aucun fichier sélectionné', 'danger')
            return redirect(url_for('documents'))
        
        if fichier and allowed_file(fichier.filename):
            # Validation du CONTENU (pas seulement de l'extension) pour éviter
            # qu'un fichier malveillant ne se déguise en image/document.
            detected = detect_file_type(fichier)
            if detected is None:
                flash('Le contenu du fichier ne correspond pas à son extension (type non autorisé).', 'danger')
            else:
                filename = secure_filename(fichier.filename)
                # Unique filename
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                filename = f"{timestamp}_{filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                fichier.save(filepath)

                # Insert into DB
                cur.execute("""
                    INSERT INTO documents (employe_id, titre, nom_fichier, chemin_fichier, type_fichier, taille, description)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (employe_id, titre or filename, filename, filepath,
                      filename.rsplit('.', 1)[1].lower(), os.path.getsize(filepath), description))
                conn.commit()
                log_action(session.get('user_id'), session.get('username'), "UPLOAD_DOCUMENT", "document", None, f"{titre} ({filename})")
                flash('Document uploadé avec succès', 'success')
        else:
            flash('Type de fichier non autorisé', 'danger')
    
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
    return render_template('documents.html', documents=docs, employees=employees, current_employee=emp)

@app.route('/documents/delete/<int:doc_id>', methods=['POST'])
@login_required
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
        cur.execute("SELECT nom_fichier FROM documents WHERE id = %s", (doc_id,))
        doc = cur.fetchone()
    if not doc:
        flash('Document introuvable.', 'danger')
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

    # Récupérer les filtres
    selected_employe = request.args.get('employe_id', '').strip()
    date_debut = request.args.get('date_debut', '').strip()
    date_fin = request.args.get('date_fin', '').strip()
    selected_statut = request.args.get('statut', '').strip()

    # Construction de la requête
    query = """
        SELECT p.*, e.nom, e.prenom 
        FROM presences p 
        JOIN employes e ON p.employe_id = e.id 
    """
    params = []
    conditions = []

    if selected_employe:
        conditions.append("p.employe_id = %s")
        params.append(int(selected_employe))

    if date_debut:
        conditions.append("p.date >= %s")
        params.append(date_debut)

    if date_fin:
        conditions.append("p.date <= %s")
        params.append(date_fin)

    if selected_statut:
        conditions.append("p.statut = %s")
        params.append(selected_statut)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY p.date DESC, p.heure_arrivee DESC LIMIT 500"

    cur.execute(query, params)
    presences_list = cur.fetchall()

    # Traitement des données + calculs
    total_pointages = len(presences_list)
    total_heures = 0.0
    employes_set = set()

    for p in presences_list:
        # Normaliser les heures
        if p.get('heure_arrivee'):
            p['heure_arrivee'] = str(p['heure_arrivee'])[:5]
        if p.get('heure_depart'):
            p['heure_depart'] = str(p['heure_depart'])[:5]

        employes_set.add(p.get('employe_id'))

        # Calcul durée
        try:
            if p.get('heure_arrivee') and p.get('heure_depart'):
                ha_parts = str(p['heure_arrivee']).split(':')[:2]
                hd_parts = str(p['heure_depart']).split(':')[:2]
                ha_min = int(ha_parts[0]) * 60 + int(ha_parts[1])
                hd_min = int(hd_parts[0]) * 60 + int(hd_parts[1])
                mins = hd_min - ha_min
                if mins > 0:
                    duree = round(mins / 60, 1)
                    p['duree_heures'] = duree
                    total_heures += duree
                else:
                    p['duree_heures'] = None
            else:
                p['duree_heures'] = None
        except Exception:
            p['duree_heures'] = None

        # Retard (pour cohérence)
        p['retard_minutes'] = calculer_retard(p.get('heure_arrivee'))
        p['retard'] = p['retard_minutes'] > 0

    employes_concernes = len(employes_set)

    # Liste employés pour le filtre
    cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom, prenom")
    employees = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        'historique.html',
        presences=presences_list,
        employees=employees,
        total_pointages=total_pointages,
        total_heures=round(total_heures, 1),
        employes_concernes=employes_concernes,
        selected_employe=selected_employe,
        date_debut=date_debut,
        date_fin=date_fin,
        selected_statut=selected_statut
    )
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


if __name__ == '__main__':
    init_db()
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    if debug_mode and os.environ.get('FLASK_ENV') == 'production':
        raise RuntimeError("FLASK_DEBUG ne doit jamais être activé en production (FLASK_ENV=production).")
    # For development with basic concurrency support (multiple users)
    # For production use: gunicorn -w 4 -b 0.0.0.0:5000 app:app
    app.run(debug=debug_mode, host='0.0.0.0', port=5000, threaded=True)