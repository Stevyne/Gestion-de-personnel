from datetime import datetime

import app as application


def test_clock_in_creates_presence(admin_client):
    # employé seedé par init_db : id=1 (voir seed dans app.py)
    resp = admin_client.post('/presences/clock_in/1', data={}, follow_redirects=True)
    assert resp.status_code == 200

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM presences WHERE employe_id = 1")
        row = cur.fetchone()
    assert row is not None
    assert row['heure_arrivee'] is not None


def test_clock_out_updates_presence(admin_client):
    admin_client.post('/presences/clock_in/1', data={})
    resp = admin_client.post('/presences/clock_out/1', data={}, follow_redirects=True)
    assert resp.status_code == 200

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM presences WHERE employe_id = 1")
        row = cur.fetchone()
    assert row['heure_depart'] is not None


def test_calculer_retard_avant_neuf_heures():
    assert application.calculer_retard("08:45") == 0


def test_calculer_retard_apres_neuf_heures():
    # 09:20 -> 20 minutes de retard par rapport à HEURE_ARRIVEE_ATTENDUE = "09:00"
    assert application.calculer_retard("09:20") == 20


def test_clock_in_requires_login(client):
    resp = client.post('/presences/clock_in/1', data={}, follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert '/login' in resp.headers.get('Location', '')
