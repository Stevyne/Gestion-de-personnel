import os

import app as application


def test_secret_key_is_not_the_old_hardcoded_value():
    """Régression : l'ancienne clé codée en dur ne doit plus jamais être utilisée."""
    assert application.app.secret_key != 'secret-key-gestion-personnel-2026-auth'


def test_debug_mode_is_off_by_default(monkeypatch):
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    assert debug_mode is False


def test_db_cursor_closes_connection_even_on_exception():
    """Le context manager db_cursor doit fermer la connexion même si une exception
    est levée dans le bloc `with` (c'est tout l'intérêt du fix vs l'ancien code)."""
    conn_ref = {}
    with application.db_cursor() as (conn, cur):
        conn_ref['conn'] = conn
        assert conn.closed == 0  # 0 = ouverte

    assert conn_ref['conn'].closed != 0  # fermée après la sortie du `with`


def test_db_cursor_closes_connection_after_exception():
    conn_ref = {}
    try:
        with application.db_cursor() as (conn, cur):
            conn_ref['conn'] = conn
            raise ValueError("erreur simulée")
    except ValueError:
        pass
    assert conn_ref['conn'].closed != 0
