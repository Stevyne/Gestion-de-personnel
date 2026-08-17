from pathlib import Path

import app as application


ROOT = Path(__file__).resolve().parents[1]


def _client_connecte(app, username, password):
    client = app.test_client()
    client.post('/login', data={'username': username, 'password': password})
    return client


def test_declencheurs_et_structure_du_panneau_droit(admin_client):
    response = admin_client.get('/')
    assert response.status_code == 200
    assert b'id="activityPanel"' in response.data
    assert b'id="activityPanelBackdrop"' in response.data
    assert response.data.count(b'js-activity-panel') >= 4
    assert b'data-panel-kind="messages"' in response.data
    assert b'data-panel-kind="notifications"' in response.data
    assert b'/static/activity-panel.js' in response.data
    assert b'/static/activity-panel.css' in response.data


def test_notifications_se_chargent_en_fragment_et_restent_privees(admin_client):
    with application.db_cursor() as (_conn, cur):
        cur.execute("SELECT id FROM users WHERE username='admin'")
        user_id = cur.fetchone()['id']
    application.create_notification(user_id, 'Panneau test', 'Message superposé', 'info')

    panel = admin_client.get('/notifications?panel=1')
    assert panel.status_code == 200
    assert b'<!DOCTYPE html>' not in panel.data
    assert b'data-panel-kind="notifications"' in panel.data
    assert b'Panneau test' in panel.data
    assert b'data-panel-form' in panel.data

    marked_redirect = admin_client.post(
        '/notifications/mark-read', data={'panel': '1'}, follow_redirects=False,
        headers={'X-Activity-Panel': '1', 'X-Requested-With': 'XMLHttpRequest'},
    )
    assert marked_redirect.status_code in (301, 302, 303)
    assert marked_redirect.status_code != 204
    assert 'panel=1' in marked_redirect.headers['Location']
    marked = admin_client.get(marked_redirect.headers['Location'])
    assert marked.status_code == 200
    assert b'data-unread-count="0"' in marked.data
    assert b'<!DOCTYPE html>' not in marked.data

    popup_protocol = admin_client.post(
        '/notifications/mark-read',
        headers={'X-Requested-With': 'XMLHttpRequest'},
        follow_redirects=False,
    )
    assert popup_protocol.status_code == 204
    assert popup_protocol.headers.get('X-Redirect-To')

    anonymous = application.app.test_client()
    denied = anonymous.get('/notifications?panel=1', follow_redirects=False)
    assert denied.status_code in (301, 302)
    assert '/login' in denied.headers['Location']


def test_messagerie_inbox_fil_et_reponse_restent_dans_le_panneau(app):
    employe = _client_connecte(app, 'employe', 'user123')
    response = employe.post('/messages/nouveau', data={
        'type': 'prive', 'destinataires': '3', 'contenu': 'Message panneau',
    }, follow_redirects=False)
    assert response.status_code in (301, 302)
    with application.db_cursor() as (_conn, cur):
        cur.execute('SELECT id FROM conversations ORDER BY id DESC LIMIT 1')
        conversation_id = cur.fetchone()['id']

    inbox = employe.get('/messages?panel=1')
    assert inbox.status_code == 200
    assert b'<!DOCTYPE html>' not in inbox.data
    assert b'data-panel-kind="messages"' in inbox.data
    assert b'data-panel-link' in inbox.data
    assert b'Message panneau' in inbox.data

    thread = employe.get(f'/messages/{conversation_id}?panel=1')
    assert thread.status_code == 200
    assert b'<!DOCTYPE html>' not in thread.data
    assert b'class="activity-panel-back"' in thread.data
    assert b'id="messageComposer"' in thread.data
    assert b'name="panel" value="1"' in thread.data
    assert b'data-panel-form' in thread.data

    reply_redirect = employe.post(
        f'/messages/{conversation_id}/repondre',
        data={'contenu': 'Réponse superposée', 'panel': '1'},
        follow_redirects=False,
        headers={'X-Activity-Panel': '1', 'X-Requested-With': 'XMLHttpRequest'},
    )
    assert reply_redirect.status_code in (301, 302, 303)
    assert reply_redirect.status_code != 204
    assert 'panel=1' in reply_redirect.headers['Location']
    reply = employe.get(reply_redirect.headers['Location'])
    assert reply.status_code == 200
    assert b'<!DOCTYPE html>' not in reply.data
    assert 'Réponse superposée'.encode('utf-8') in reply.data

    compose = employe.get('/messages/nouveau?panel=1')
    assert compose.status_code == 200
    assert b'<!DOCTYPE html>' not in compose.data
    assert b'id="formNouveauMessage"' in compose.data
    assert b'data-panel-form' in compose.data


def test_badge_mobile_messages_se_met_a_jour_et_disparait_apres_lecture(app):
    employe = _client_connecte(app, 'employe', 'user123')
    employe.post('/messages/nouveau', data={
        'type': 'prive', 'destinataires': '3', 'contenu': 'Badge mobile',
    })
    with application.db_cursor() as (_conn, cur):
        cur.execute('SELECT id FROM conversations ORDER BY id DESC LIMIT 1')
        conversation_id = cur.fetchone()['id']

    manager = _client_connecte(app, 'manager', 'manager123')
    count = manager.get('/messages/non-lus')
    assert count.status_code == 200
    assert count.get_json() == {'count': 1}
    assert count.headers['Cache-Control'] == 'private, no-store'
    source = (ROOT / 'blueprints' / 'messagerie.py').read_text(encoding='utf-8')
    assert "@limiter.limit('240 per hour', override_defaults=True)" in source

    mobile_nav = manager.get('/')
    assert b'message-menu-count' in mobile_nav.data
    assert b'data-message-count-url="/messages/non-lus"' in mobile_nav.data

    thread = manager.get(f'/messages/{conversation_id}?panel=1')
    assert thread.status_code == 200
    assert manager.get('/messages/non-lus').get_json() == {'count': 0}


def test_panneau_est_responsive_accessible_et_sans_dependance_externe():
    css = (ROOT / 'static' / 'activity-panel.css').read_text(encoding='utf-8')
    js = (ROOT / 'static' / 'activity-panel.js').read_text(encoding='utf-8')
    assert '.activity-panel' in css
    assert 'transform:translateX(102%)' in css
    assert '@media(max-width:768px)' in css
    assert 'prefers-reduced-motion' in css
    assert "event.key === 'Escape'" in js
    assert "event.key !== 'Tab'" in js
    assert "headers: {'X-Activity-Panel': '1'" in js
    assert 'response.status === 204' in js
    assert "response.headers.get('X-Redirect-To')" in js
    assert "badge.className = 'menu-count message-menu-count'" in js
    assert "window.setInterval(refreshMessageBadge, 30000)" in js
    assert "updateKindBadges('messages', data.count)" in js
    assert 'http://' not in js and 'https://' not in js
