"""
Configuration pytest partagée.

Utilise une vraie base PostgreSQL de test (pas de mock), pour rester
fidèle au comportement réel de l'appli. Par défaut on pointe vers
`gestion_personnel_test` en local ; surchargez avec la variable
d'environnement TEST_DATABASE_URL si besoin (CI, autre machine...).

Avant de lancer les tests :
    createdb gestion_personnel_test
    pytest
"""
import os
import sys

# Variables d'environnement nécessaires AVANT l'import de app.py,
# car app.py les lit au chargement du module (SECRET_KEY, DATABASE_URL...).
os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
os.environ.setdefault(
    'DATABASE_URL',
    os.environ.get('TEST_DATABASE_URL', 'postgresql://postgres:postgres@localhost:5432/gestion_personnel_test')
)
os.environ.setdefault('FLASK_ENV', 'testing')
os.environ.setdefault('MAIL_USERNAME', '')  # force le mode démo (pas de vrai envoi d'email)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import app as application  # noqa: E402  (import après config des env vars, volontaire)


# Tables réinitialisées avant chaque test pour repartir d'un état propre.
# employes / users / departements restent seedés une seule fois par session
# (les identifiants de test admin/rh/manager/employe doivent rester stables).
MUTABLE_TABLES = [
    'presences', 'conges', 'soldes_conges',
    'audit_logs', 'documents', 'notifications',
]


@pytest.fixture(scope='session', autouse=True)
def _init_database():
    """Crée les tables + données de démo une fois pour toute la session de tests."""
    application.init_db()
    yield


@pytest.fixture(autouse=True)
def _clean_tables():
    """Vide les tables mutables avant chaque test pour l'isolation."""
    with application.db_cursor(commit=True) as (conn, cur):
        for table in MUTABLE_TABLES:
            cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
    yield


@pytest.fixture
def app():
    application.app.config['TESTING'] = True
    application.app.config['WTF_CSRF_ENABLED'] = False   # simplifie les POST dans les tests
    application.app.config['RATELIMIT_ENABLED'] = False   # évite le rate-limit entre tests
    return application.app


@pytest.fixture
def client(app):
    return app.test_client()


def login_as(client, username, password):
    return client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)


@pytest.fixture
def admin_client(client):
    login_as(client, 'admin', 'admin123')
    return client


@pytest.fixture
def employe_client(client):
    login_as(client, 'employe', 'user123')
    return client
