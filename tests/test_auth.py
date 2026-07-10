def test_login_page_accessible(client):
    resp = client.get('/login')
    assert resp.status_code == 200


def test_login_valid_credentials(client):
    resp = client.post('/login', data={'username': 'admin', 'password': 'admin123'}, follow_redirects=True)
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get('username') == 'admin'
        assert sess.get('role') == 'admin'


def test_login_invalid_password(client):
    resp = client.post('/login', data={'username': 'admin', 'password': 'mauvais_mdp'}, follow_redirects=True)
    with client.session_transaction() as sess:
        assert 'user_id' not in sess
    assert 'incorrects'.encode() in resp.data or resp.status_code == 200


def test_login_unknown_user(client):
    resp = client.post('/login', data={'username': 'inconnu', 'password': 'x'}, follow_redirects=True)
    with client.session_transaction() as sess:
        assert 'user_id' not in sess


def test_logout_clears_session(admin_client):
    admin_client.get('/logout')
    with admin_client.session_transaction() as sess:
        assert 'user_id' not in sess


def test_dashboard_requires_login(client):
    """Une route protégée doit rediriger vers /login si non connecté."""
    resp = client.get('/', follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert '/login' in resp.headers.get('Location', '')


def test_dashboard_accessible_once_logged_in(admin_client):
    resp = admin_client.get('/')
    assert resp.status_code == 200


def test_audit_forbidden_for_employe(employe_client):
    """/audit est réservé à admin/rh : role_required redirige vers le dashboard
    avec un message flash 'Accès refusé', il ne renvoie pas de 403."""
    resp = employe_client.get('/audit', follow_redirects=True)
    assert resp.status_code == 200
    assert 'Accès refusé'.encode('utf-8') in resp.data


def test_audit_allowed_for_admin(admin_client):
    resp = admin_client.get('/audit')
    assert resp.status_code == 200
