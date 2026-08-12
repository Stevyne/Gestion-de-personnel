from datetime import date, timedelta

import app as application


def _demande_permission(client, employe_id=1, jours=1):
    debut = date.today() + timedelta(days=10)
    fin = debut + timedelta(days=jours - 1)
    return client.post('/permissions/add', data={
        'employe_id': str(employe_id),
        'date_debut': debut.isoformat(),
        'date_fin': fin.isoformat(),
        'motif': 'Rendez-vous administratif',
    }, follow_redirects=True)


def test_add_permission_creates_request(admin_client):
    resp = _demande_permission(admin_client, jours=2)
    assert resp.status_code == 200

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM permissions WHERE employe_id = 1")
        perm = cur.fetchone()
    assert perm is not None
    assert perm['statut'] == 'en attente'
    assert perm['nombre_jours'] == 2


def test_add_permission_missing_fields_shows_error(admin_client):
    resp = admin_client.post('/permissions/add', data={'employe_id': '1'}, follow_redirects=True)
    assert resp.status_code == 200
    assert 'obligatoires'.encode('utf-8') in resp.data


def test_approve_permission_changes_statut(admin_client):
    _demande_permission(admin_client, jours=1)
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM permissions WHERE employe_id = 1")
        perm_id = cur.fetchone()['id']

    admin_client.post(f'/permissions/update/{perm_id}', data={'action': 'approuver'}, follow_redirects=True)

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT statut FROM permissions WHERE id = %s", (perm_id,))
        assert cur.fetchone()['statut'] == 'approuvé'


def test_permission_does_not_affect_solde_conges(admin_client):
    """Une permission est indépendante des congés : l'approuver ne modifie
    JAMAIS le solde de congés (soldes_conges)."""
    _demande_permission(admin_client, jours=3)
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM permissions WHERE employe_id = 1")
        perm_id = cur.fetchone()['id']

    solde_avant = application.get_solde_conges(1)
    assert solde_avant['jours_utilises'] == 0

    admin_client.post(f'/permissions/update/{perm_id}', data={'action': 'approuver'}, follow_redirects=True)

    solde_apres = application.get_solde_conges(1)
    assert solde_apres['jours_utilises'] == 0
    assert solde_apres['jours_restants'] == solde_avant['jours_restants']


def test_refuse_permission(admin_client):
    _demande_permission(admin_client, jours=1)
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM permissions WHERE employe_id = 1")
        perm_id = cur.fetchone()['id']

    # Un refus sans motif est rejeté : la demande reste ouverte.
    admin_client.post(f'/permissions/update/{perm_id}', data={'action': 'refuser'}, follow_redirects=True)
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT statut FROM permissions WHERE id = %s", (perm_id,))
        assert cur.fetchone()['statut'] in ('en attente', 'avis rendu')

    admin_client.post(f'/permissions/update/{perm_id}',
                      data={'action': 'refuser', 'motif_refus': 'Service non couvert'},
                      follow_redirects=True)

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT statut, motif_refus FROM permissions WHERE id = %s", (perm_id,))
        row = cur.fetchone()
        assert row['statut'] == 'refusé'
        assert row['motif_refus'] == 'Service non couvert'


def test_delete_permission_removes_request(admin_client):
    _demande_permission(admin_client, jours=1)
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM permissions WHERE employe_id = 1")
        perm_id = cur.fetchone()['id']

    admin_client.post(f'/permissions/delete/{perm_id}', follow_redirects=True)

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM permissions WHERE id = %s", (perm_id,))
        assert cur.fetchone() is None


def test_permissions_page_requires_login(client):
    resp = client.get('/permissions', follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert '/login' in resp.headers.get('Location', '')
