import app as application


def test_notifications_page_accessible(admin_client):
    resp = admin_client.get('/notifications')
    assert resp.status_code == 200


def test_mark_notifications_read(admin_client):
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM users WHERE username = 'admin'")
        user_id = cur.fetchone()['id']

    application.create_notification(
        user_id=user_id, title="Test", message="Test notification", type_="info"
    )

    resp = admin_client.post('/notifications/mark-read', follow_redirects=True)
    assert resp.status_code == 200

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS c FROM notifications WHERE user_id = %s AND is_read = FALSE", (user_id,))
        unread = cur.fetchone()['c']
    assert unread == 0


def test_notifications_template_has_csrf_token():
    """Vérifie que le formulaire 'Marquer tout comme lu' embarque bien le token CSRF
    (régression du fix appliqué sur templates/notifications.html)."""
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates', 'notifications.html')
    with open(path, encoding='utf-8') as f:
        content = f.read()
    assert 'csrf_token' in content


def test_notification_longue_reste_complete_et_recoit_classes_responsives(admin_client):
    titre = 'TitreTresLongSansEspace' * 8
    message = 'MessageTresLongSansEspace' * 20
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM users WHERE username='admin'")
        user_id = cur.fetchone()['id']
    application.create_notification(user_id, titre, message, 'info')

    response = admin_client.get('/notifications')
    assert response.status_code == 200
    assert titre.encode() in response.data
    assert message.encode() in response.data
    assert b'notification-message-cell' in response.data
    assert b'notification-content' in response.data
    assert b'notification-title' in response.data
    assert b'notification-text' in response.data


def test_css_notifications_mobile_tronque_sans_debordement():
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / 'static' / 'style.css').read_text(encoding='utf-8')
    assert '.notifications-table .notification-content' in css
    assert 'overflow-wrap: anywhere' in css
    assert '-webkit-line-clamp: 2' in css
    assert 'text-overflow: ellipsis' in css
