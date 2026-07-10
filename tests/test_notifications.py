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
