import io
from datetime import date

import app as application


def _notifications_employe(titre):
    with application.db_cursor() as (conn, cur):
        cur.execute("""
            SELECT COUNT(*) AS n FROM notifications n
            JOIN users u ON u.id = n.user_id
            WHERE u.employe_id = 1 AND n.title = %s
        """, (titre,))
        return cur.fetchone()['n']


def test_ajout_absence_notifie_employe(admin_client):
    response = admin_client.post('/absences/add', data={
        'employe_id': '1', 'date': date.today().isoformat(),
        'motif': 'Absence signalée',
    }, follow_redirects=True)
    assert response.status_code == 200
    assert _notifications_employe('Absence enregistrée') == 1


def test_ajout_presence_notifie_employe(admin_client):
    response = admin_client.post('/presences/add', data={
        'employe_id': '1', 'date': date.today().isoformat(),
        'heure_arrivee': '08:30', 'heure_depart': '17:00',
        'statut': 'présent', 'commentaire': '',
    }, follow_redirects=True)
    assert response.status_code == 200
    assert _notifications_employe('Présence enregistrée') == 1


def test_ajout_document_notifie_employe(admin_client):
    response = admin_client.post('/documents', data={
        'employe_id': '1', 'titre': 'Attestation',
        'description': 'Test', 'date_expiration': '',
        'fichier': (io.BytesIO(b'%PDF-1.4\ndocument-test'), 'attestation.pdf'),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert response.status_code == 200
    assert _notifications_employe('Nouveau document dans votre dossier') == 1


def test_ajout_employe_notifie_les_rh(admin_client):
    response = admin_client.post('/add_employee', data={
        'nom': 'Rakoto', 'prenom': 'Mia', 'poste': 'Comptable',
        'departement': 'Finance', 'email': 'mia.rakoto@example.test',
        'telephone': '', 'salaire': '1000',
        'date_embauche': date.today().isoformat(),
    }, follow_redirects=True)
    assert response.status_code == 200
    with application.db_cursor() as (conn, cur):
        cur.execute("""
            SELECT COUNT(*) AS n FROM notifications n
            JOIN users u ON u.id = n.user_id
            WHERE u.role = 'rh' AND n.title = 'Nouvel employé enregistré'
        """)
        assert cur.fetchone()['n'] == 1
