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


def _deposer(client, absence_id, nature=None):
    donnees = {
        'commentaire': 'Arrêt médical',
        'justificatif': (io.BytesIO(b'%PDF-1.4\njustificatif-test'), 'arret.pdf'),
    }
    if nature is not None:
        donnees['justification_type'] = nature
    return client.post(
        f'/self-service/absences/{absence_id}/justificatif',
        data=donnees,
        content_type='multipart/form-data',
        follow_redirects=True,
    )


def _lire_conge(absence_id):
    """Le congé créé par une acceptation, ou None s'il n'y en a pas."""
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM absences WHERE id = %s", (absence_id,))
        absence = cur.fetchone()
        if not absence or not absence['conge_id']:
            return absence, None
        cur.execute("SELECT * FROM conges WHERE id = %s", (absence['conge_id'],))
        return absence, cur.fetchone()


def _rh(client):
    """Reprend la session en tant que RH."""
    client.get('/logout')
    client.post('/login', data={'username': 'rh', 'password': 'rh123'})
    return client


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


def test_rh_accepte_et_requalifie_en_maladie_par_defaut(employe_client):
    """Sans nature proposée, la maladie reste le défaut.

    Attention, la valeur a changé de forme : le circuit écrivait
    « congé maladie », qui ne correspondait à aucune entrée de la liste
    des types de congé (le formulaire propose « maladie »). Les deux
    écritures se retrouvaient côte à côte dans les calendriers.
    """
    absence_id = _creer_absence()
    _deposer(employe_client, absence_id)
    _rh(employe_client)

    response = employe_client.post(
        f'/absences/{absence_id}/decision',
        data={'decision': 'accepter'},
        follow_redirects=True,
    )
    assert response.status_code == 200

    absence, conge = _lire_conge(absence_id)
    assert absence['statut'] == 'acceptee'
    assert conge['statut'] == 'approuvé'
    assert conge['type_conge'] == 'maladie'
    assert conge['nombre_jours'] == 1


def test_rh_retient_la_nature_proposee_par_lemploye(employe_client):
    """L'employé propose une nature, les RH l'acceptent telle quelle."""
    absence_id = _creer_absence()
    _deposer(employe_client, absence_id, nature='récupération')

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT justification_type FROM absences WHERE id = %s", (absence_id,))
        assert cur.fetchone()['justification_type'] == 'récupération'

    _rh(employe_client)
    # La proposition doit être pré-sélectionnée dans la liste des RH.
    page = employe_client.get('/absences').data.decode('utf-8')
    assert '<option value="récupération" selected>' in page

    employe_client.post(
        f'/absences/{absence_id}/decision',
        data={'decision': 'accepter', 'type_justification': 'récupération'},
        follow_redirects=True,
    )
    absence, conge = _lire_conge(absence_id)
    assert absence['statut'] == 'acceptee'
    assert conge['type_conge'] == 'récupération'


def test_rh_peut_corriger_la_nature_proposee(employe_client):
    """La proposition n'engage pas : les RH gardent le dernier mot.

    On conserve les deux valeurs — la proposition de l'employé et la
    nature retenue (portée par le congé) — pour garder la trace de
    l'arbitrage.
    """
    absence_id = _creer_absence()
    _deposer(employe_client, absence_id, nature='autre')
    _rh(employe_client)
    employe_client.post(
        f'/absences/{absence_id}/decision',
        data={'decision': 'accepter', 'type_justification': 'maladie'},
        follow_redirects=True,
    )
    absence, conge = _lire_conge(absence_id)
    assert conge['type_conge'] == 'maladie'
    assert absence['justification_type'] == 'autre'


def test_rh_peut_accepter_sans_creer_de_conge(employe_client):
    """« Aucune » excuse l'absence sans rien écrire dans les congés."""
    absence_id = _creer_absence()
    _deposer(employe_client, absence_id, nature='congé sans solde')
    _rh(employe_client)
    employe_client.post(
        f'/absences/{absence_id}/decision',
        data={'decision': 'accepter', 'type_justification': 'aucun'},
        follow_redirects=True,
    )
    absence, conge = _lire_conge(absence_id)
    assert absence['statut'] == 'acceptee'
    assert conge is None
    assert absence['conge_id'] is None


def test_nature_hors_liste_est_ignoree_sans_bloquer_le_depot(employe_client):
    """Une valeur fabriquée ne doit pas empêcher le dépôt de la pièce."""
    absence_id = _creer_absence()
    response = _deposer(employe_client, absence_id, nature='__invention__')
    assert response.status_code == 200

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT statut, justification_type FROM absences WHERE id = %s",
                    (absence_id,))
        absence = cur.fetchone()
    assert absence['statut'] == 'justificatif_depose'
    assert absence['justification_type'] is None


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
