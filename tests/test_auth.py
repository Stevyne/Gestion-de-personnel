from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_login_page_moderne_et_accessible(client):
    resp = client.get('/login')
    assert resp.status_code == 200
    assert b'class="auth-page"' in resp.data
    assert b'autocomplete="username"' in resp.data
    assert b'autocomplete="current-password"' in resp.data
    assert b'aria-controls="password"' in resp.data
    assert b'/static/login.js' in resp.data
    assert 'Ravi de vous revoir'.encode('utf-8') in resp.data


def test_login_assets_responsives_et_sans_dependance_externe():
    css = (ROOT / 'static/style.css').read_text(encoding='utf-8')
    js = (ROOT / 'static/login.js').read_text(encoding='utf-8')
    assert '.login-shell' in css
    assert '@media (max-width: 820px)' in css
    assert '.login-password-toggle' in css
    assert 'prefers-reduced-motion' in css
    assert "getModifierState('CapsLock')" in js
    assert "submit.classList.add('is-loading')" in js
    assert 'http://' not in js and 'https://' not in js


def test_login_valid_credentials(client):
    resp = client.post('/login', data={'username': 'admin', 'password': 'admin123'}, follow_redirects=True)
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get('username') == 'admin'
        assert sess.get('role') == 'admin'
        assert sess.permanent is True


def test_login_invalid_password_conserve_seulement_identifiant(client):
    secret = 'mauvais_mdp-ne-jamais-refleter'
    resp = client.post(
        '/login', data={'username': 'admin', 'password': secret},
        follow_redirects=True,
    )
    with client.session_transaction() as sess:
        assert 'user_id' not in sess
    assert 'incorrects'.encode() in resp.data or resp.status_code == 200
    assert b'value="admin"' in resp.data
    assert secret.encode() not in resp.data
    assert b'aria-invalid="true"' in resp.data


def test_login_unknown_user(client):
    client.post('/login', data={'username': 'inconnu', 'password': 'x'}, follow_redirects=True)
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
