"""Adresse IP du poste : résolution, usurpation et robustesse (patch M).

Le point sensible est X-Forwarded-For : cet en-tête est fourni par le
client, donc falsifiable. L'application ne doit s'en servir que par
l'intermédiaire de ProxyFix, qui seul sait combien de mandataires sont
dignes de confiance (TRUSTED_PROXY_COUNT). Le lire directement permettrait
à n'importe qui de choisir la provenance inscrite au journal.
"""
import logging

import pytest

import app as application

# Largeur de la colonne audit_logs.ip_address (services/schema.py).
LARGEUR_COLONNE = 45


@pytest.fixture
def avertissements(monkeypatch):
    """Capture les avertissements et réarme l'avertissement unique."""
    class Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    capture = Capture()
    journal = logging.getLogger('gestion_personnel')
    niveau = journal.level
    journal.addHandler(capture)
    journal.setLevel(logging.WARNING)
    # L'avertissement de mauvaise configuration n'est émis qu'une fois par
    # processus : sans remise à zéro, un seul test pourrait l'observer.
    monkeypatch.setattr(application, '_AVERTISSEMENT_PROXY_EMIS', False)
    yield capture
    journal.removeHandler(capture)
    journal.setLevel(niveau)


def _derniere_connexion():
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT ip_address FROM audit_logs WHERE action = 'LOGIN' "
                    "ORDER BY id DESC LIMIT 1")
        entree = cur.fetchone()
    return entree['ip_address'] if entree else None


def _connecter(client, **kwargs):
    kwargs.setdefault('follow_redirects', True)
    return client.post('/login', data={'username': 'admin', 'password': 'admin123'},
                       **kwargs)


def test_adresse_ip_est_none_hors_contexte_de_requete():
    assert application._client_ip() is None


def test_journalisation_hors_requete_ne_perd_pas_l_entree():
    """Hors requête il n'y a pas d'adresse, mais l'entrée doit survivre."""
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("DELETE FROM audit_logs WHERE action = 'TEST_HORS_REQUETE'")
    application.log_action(username='sonde', action='TEST_HORS_REQUETE')
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id, ip_address FROM audit_logs "
                    "WHERE action = 'TEST_HORS_REQUETE'")
        entree = cur.fetchone()
    assert entree is not None, "l'entrée doit être enregistrée même sans IP"
    assert entree['ip_address'] is None


def test_adresse_trop_longue_est_tronquee(app):
    """Au-delà de la largeur de la colonne, l'INSERT échouerait et
    l'entrée d'audit disparaîtrait entièrement avec lui."""
    with app.test_request_context('/', environ_base={'REMOTE_ADDR': 'a' * 60}):
        assert len(application._client_ip()) == LARGEUR_COLONNE


def test_en_tete_x_forwarded_for_n_est_pas_recopie(client):
    """Un en-tête falsifié ne doit pas devenir la provenance enregistrée.

    Aucun mandataire n'étant déclaré de confiance, l'adresse retenue est
    celle du pair immédiat : c'est la seule observable sans topologie sûre.
    """
    _connecter(client, headers={'X-Forwarded-For': '9.9.9.9'})
    assert _derniere_connexion() == '127.0.0.1'


def test_sessions_actives_ignorent_l_en_tete_falsifie(client):
    """Le registre des sessions lisait X-Forwarded-For sans précaution :
    n'importe qui pouvait y faire inscrire la provenance de son choix.
    Le journal d'audit, lui, s'appuyait déjà sur remote_addr — d'où
    l'incohérence entre les deux tables avant le correctif."""
    _connecter(client, headers={'X-Forwarded-For': '9.9.9.9'},
               environ_base={'REMOTE_ADDR': '127.0.0.1'})
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT ip_address FROM sessions_actives "
                    "WHERE username = 'admin' ORDER BY id DESC LIMIT 1")
        session = cur.fetchone()
    assert session is not None
    assert session['ip_address'] == '127.0.0.1'


def test_mauvaise_configuration_de_proxy_est_signalee_une_seule_fois(client, avertissements):
    """Trois requêtes ne doivent produire qu'un avertissement : le signaler
    à chaque fois noierait les journaux sans rien apprendre de plus."""
    for _ in range(3):
        _connecter(client, headers={'X-Forwarded-For': '203.0.113.5'},
                   environ_base={'REMOTE_ADDR': '127.0.0.1'})
    pertinents = [m for m in avertissements.messages if 'X-Forwarded-For' in m]
    assert len(pertinents) == 1, f"attendu 1 avertissement, obtenu {len(pertinents)}"
    assert 'TRUSTED_PROXY_COUNT' in pertinents[0]


def test_journal_et_sessions_actives_concordent(client):
    """Une même requête ne doit pas produire deux provenances différentes."""
    _connecter(client, environ_base={'REMOTE_ADDR': '127.0.0.1'})
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT ip_address FROM sessions_actives "
                    "WHERE username = 'admin' ORDER BY id DESC LIMIT 1")
        session = cur.fetchone()
    assert session is not None
    assert session['ip_address'] == _derniere_connexion()
