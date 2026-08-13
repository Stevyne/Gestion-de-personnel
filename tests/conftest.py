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
os.environ.setdefault('RATELIMIT_ENABLED', 'false')
os.environ.setdefault('MAIL_USERNAME', '')  # force le mode démo (pas de vrai envoi d'email)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import app as application  # noqa: E402  (import après config des env vars, volontaire)


# Tables réinitialisées avant chaque test pour repartir d'un état propre.
# employes / users / departements restent seedés une seule fois par session
# (les identifiants de test admin/rh/manager/employe doivent rester stables).
MUTABLE_TABLES = [
    'presences', 'conges', 'permissions', 'absences', 'absences_exclues',
    'soldes_conges', 'audit_logs', 'documents', 'documents_alertes',
    'notifications', 'email_outbox', 'scheduler_runs', 'sessions_actives',
    'conversations',  # CASCADE : messages, membres et lectures d'annonces
    # Les modules parc/maintenance sont désormais couverts eux aussi.
    'inventaires', 'materiel_maintenances', 'materiel_exemplaires',
    'materiels_attributions', 'materiels_mouvements', 'materiel_compteurs',
    'materiels', 'prestataires',
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
        # Conserver uniquement les quatre comptes et employés seedés : les
        # scénarios de création ne doivent pas polluer les tests suivants.
        cur.execute("DELETE FROM users WHERE id > 4")
        cur.execute("DELETE FROM employes WHERE id > 4")
        comptes_seed = {
            1: ('admin', 4), 2: ('rh', 2),
            3: ('manager', 3), 4: ('employe', 1),
        }
        for user_id, (role, employe_id) in comptes_seed.items():
            cur.execute("UPDATE users SET role=%s, employe_id=%s WHERE id=%s",
                        (role, employe_id, user_id))
        # Certains tests d'absences déplacent les dates d'embauche. Sans remise
        # à zéro, leur ordre modifiait le résultat des tests de soldes.
        employes_seed = {
            1: ('2023-01-15', 'Informatique'),
            2: ('2022-06-01', 'Ressources Humaines'),
            3: ('2021-09-10', 'Informatique'),
            4: ('2022-01-01', 'Administration'),
        }
        for employe_id, (date_embauche, departement) in employes_seed.items():
            cur.execute("""UPDATE employes SET date_embauche = %s, departement = %s
                           WHERE id = %s""",
                        (date_embauche, departement, employe_id))
    yield


@pytest.fixture
def app():
    application.app.config['TESTING'] = True
    application.app.config['WTF_CSRF_ENABLED'] = False   # simplifie les POST dans les tests
    application.app.config['RATELIMIT_ENABLED'] = False   # évite le rate-limit entre tests
    application.app.config['EMAIL_ENABLED'] = False        # aucun SMTP pendant les tests
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
