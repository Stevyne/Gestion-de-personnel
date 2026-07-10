from datetime import date, timedelta

import app as application


def _demande_conge(client, employe_id=1, jours=3):
    debut = date.today() + timedelta(days=10)
    fin = debut + timedelta(days=jours - 1)
    return client.post('/conges/add', data={
        'employe_id': str(employe_id),
        'type_conge': 'payé',
        'date_debut': debut.isoformat(),
        'date_fin': fin.isoformat(),
        'motif': 'Test',
    }, follow_redirects=True)


def test_add_conge_creates_request(admin_client):
    resp = _demande_conge(admin_client, jours=3)
    assert resp.status_code == 200

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM conges WHERE employe_id = 1")
        conge = cur.fetchone()
    assert conge is not None
    assert conge['statut'] == 'en attente'
    assert conge['nombre_jours'] == 3


def test_add_conge_missing_fields_shows_error(admin_client):
    resp = admin_client.post('/conges/add', data={'employe_id': '1'}, follow_redirects=True)
    assert resp.status_code == 200
    assert 'obligatoires'.encode('utf-8') in resp.data


def test_approve_conge_updates_solde(admin_client):
    _demande_conge(admin_client, jours=5)
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM conges WHERE employe_id = 1")
        conge_id = cur.fetchone()['id']

    solde_avant = application.get_solde_conges(1)
    assert solde_avant['jours_utilises'] == 0

    resp = admin_client.post(f'/conges/update/{conge_id}', data={'action': 'approuver'}, follow_redirects=True)
    assert resp.status_code == 200

    solde_apres = application.get_solde_conges(1)
    assert solde_apres['jours_utilises'] == 5
    assert solde_apres['jours_restants'] == solde_apres['jours_acquis'] - 5


def test_refuse_conge_does_not_consume_solde(admin_client):
    _demande_conge(admin_client, jours=5)
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM conges WHERE employe_id = 1")
        conge_id = cur.fetchone()['id']

    admin_client.post(f'/conges/update/{conge_id}', data={'action': 'refuser'}, follow_redirects=True)

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT statut FROM conges WHERE id = %s", (conge_id,))
        statut = cur.fetchone()['statut']
    assert statut == 'refusé'

    solde = application.get_solde_conges(1)
    assert solde['jours_utilises'] == 0


def test_delete_conge_removes_request(admin_client):
    _demande_conge(admin_client, jours=2)
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM conges WHERE employe_id = 1")
        conge_id = cur.fetchone()['id']

    admin_client.post(f'/conges/delete/{conge_id}', follow_redirects=True)

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM conges WHERE id = %s", (conge_id,))
        assert cur.fetchone() is None


def test_solde_conges_defaut_25_jours(admin_client):
    solde = application.get_solde_conges(1)
    assert solde['jours_acquis'] == 25
    assert solde['jours_restants'] == 25
