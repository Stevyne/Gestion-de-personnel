import io
from datetime import date, timedelta

import app as application


def _creer_absence(employe_id=1):
    jour = date.today() - timedelta(days=1)
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""
            INSERT INTO absences (employe_id, date, motif)
            VALUES (%s, %s, 'Absence test') RETURNING id
        """, (employe_id, jour))
        return cur.fetchone()['id']


def _deposer(client, absence_id):
    return client.post(
        f'/self-service/absences/{absence_id}/justificatif',
        data={
            'commentaire': 'Arrêt médical',
            'justificatif': (io.BytesIO(b'%PDF-1.4\njustificatif-test'), 'arret.pdf'),
        },
        content_type='multipart/form-data',
        follow_redirects=True,
    )


def test_employe_depose_justificatif_persistant(employe_client):
    absence_id = _creer_absence()
    response = _deposer(employe_client, absence_id)
    assert response.status_code == 200
    assert 'Justificatif déposé'.encode('utf-8') in response.data

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM absences WHERE id = %s", (absence_id,))
        absence = cur.fetchone()
        cur.execute("""SELECT COUNT(*) AS n FROM notifications n
                       JOIN users u ON u.id = n.user_id
                       WHERE u.role IN ('admin','rh')
                         AND n.title = 'Justificatif d''absence à traiter'""")
        notifications_rh = cur.fetchone()['n']
    assert absence['statut'] == 'justificatif_depose'
    assert bytes(absence['justificatif_contenu']).startswith(b'%PDF')
    assert absence['justificatif_nom'] == 'arret.pdf'
    assert notifications_rh >= 1


def test_rh_accepte_et_requalifie_en_conge_maladie(employe_client):
    absence_id = _creer_absence()
    _deposer(employe_client, absence_id)
    employe_client.get('/logout')
    employe_client.post('/login', data={'username': 'rh', 'password': 'rh123'})

    response = employe_client.post(
        f'/absences/{absence_id}/decision',
        data={'decision': 'accepter'},
        follow_redirects=True,
    )
    assert response.status_code == 200

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT statut, conge_id FROM absences WHERE id = %s", (absence_id,))
        absence = cur.fetchone()
        cur.execute("SELECT * FROM conges WHERE id = %s", (absence['conge_id'],))
        conge = cur.fetchone()
    assert absence['statut'] == 'acceptee'
    assert conge['statut'] == 'approuvé'
    assert conge['type_conge'] == 'congé maladie'
    assert conge['nombre_jours'] == 1


def test_refus_exige_un_motif(employe_client):
    absence_id = _creer_absence()
    _deposer(employe_client, absence_id)
    employe_client.get('/logout')
    employe_client.post('/login', data={'username': 'rh', 'password': 'rh123'})

    employe_client.post(f'/absences/{absence_id}/decision',
                        data={'decision': 'refuser'})
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT statut FROM absences WHERE id = %s", (absence_id,))
        assert cur.fetchone()['statut'] == 'justificatif_depose'

    employe_client.post(f'/absences/{absence_id}/decision', data={
        'decision': 'refuser', 'motif_refus': 'Document illisible',
    })
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT statut, motif_refus FROM absences WHERE id = %s", (absence_id,))
        absence = cur.fetchone()
    assert absence['statut'] == 'refusee'
    assert absence['motif_refus'] == 'Document illisible'


def test_employe_ne_peut_pas_justifier_absence_autrui(employe_client):
    absence_id = _creer_absence(employe_id=2)
    response = _deposer(employe_client, absence_id)
    assert response.status_code == 404


def test_justificatif_prive_inaccessible_au_manager(employe_client):
    absence_id = _creer_absence()
    _deposer(employe_client, absence_id)
    employe_client.get('/logout')
    employe_client.post('/login', data={'username': 'manager', 'password': 'manager123'})
    response = employe_client.get(f'/absences/{absence_id}/justificatif')
    assert response.status_code == 403
