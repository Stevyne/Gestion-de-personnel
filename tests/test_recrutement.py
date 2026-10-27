import io
from datetime import date, timedelta

import app as application


def _dans(jours):
    """Date relative à aujourd'hui : évite les tests qui périment."""
    return (date.today() + timedelta(days=jours)).isoformat()


def _login(client, username, password):
    response = client.post('/login', data={'username': username, 'password': password})
    assert response.status_code in (301, 302)


def _departement_id(nom):
    with application.db_cursor() as (_conn, cur):
        cur.execute("SELECT id FROM departements WHERE nom=%s", (nom,))
        return cur.fetchone()['id']


def test_workflow_complet_recrutement_jusqua_embauche(app):
    manager = app.test_client()
    admin = app.test_client()
    _login(manager, 'manager', 'manager123')
    _login(admin, 'admin', 'admin123')
    # Le recrutement est sorti du périmètre du manager (accès strict) :
    # le circuit est porté de bout en bout par l'admin ou les RH.
    assert manager.get('/recrutement', follow_redirects=False).status_code in (301, 302)
    assert admin.get('/recrutement').status_code == 200
    informatique = _departement_id('Informatique')

    # Le besoin est exprimé pour le département, par l'admin.
    response = admin.post('/recrutement/demandes/nouvelle', data={
        'poste': 'Développeur Flask', 'departement_id': str(informatique),
        'nombre_postes': '2', 'type_contrat': 'cdi',
        'date_souhaitee': '2026-10-01', 'salaire_min': '50000',
        'salaire_max': '70000', 'motif': 'Remplacement',
        'competences': 'Python\nFlask\nPostgreSQL',
    })
    assert response.status_code in (301, 302)
    with application.db_cursor() as (_conn, cur):
        cur.execute("SELECT * FROM recrutement_demandes")
        demande = cur.fetchone()
    assert demande['statut'] == 'en_attente'
    assert demande['reference'].startswith('REC-')

    # Seules les RH/admin décident puis créent l'offre.
    assert manager.post(
        f"/recrutement/demandes/{demande['id']}/decision",
        data={'decision': 'validee'},
    ).status_code in (301, 302)
    admin.post(f"/recrutement/demandes/{demande['id']}/decision",
               data={'decision': 'validee'})
    response = admin.post(
        f"/recrutement/offres/nouvelle?demande_id={demande['id']}",
        data={
            'demande_id': str(demande['id']), 'titre': 'Développeur Flask',
            'description': 'Développer et maintenir les applications RH.',
            'departement_id': str(informatique), 'poste': 'Développeur Flask',
            'competences': 'Python\nFlask\nPostgreSQL',
            'niveau_experience': '3 ans', 'diplome_requis': 'Bac+3',
            'type_contrat': 'cdi', 'salaire_min': '50000',
            'salaire_max': '70000', 'localisation': 'Antananarivo',
            'date_limite': _dans(30), 'nombre_postes': '2',
        },
    )
    assert response.status_code in (301, 302)
    with application.db_cursor() as (_conn, cur):
        cur.execute("SELECT * FROM recrutement_offres")
        offre = cur.fetchone()
        cur.execute("SELECT * FROM recrutement_criteres WHERE offre_id=%s ORDER BY id",
                    (offre['id'],))
        criteres = cur.fetchall()
    assert offre['statut'] == 'brouillon'
    assert round(sum(float(c['poids']) for c in criteres), 2) == 100
    assert admin.get(f"/recrutement/offres/{offre['id']}").status_code == 200
    admin.post(f"/recrutement/offres/{offre['id']}/statut",
               data={'statut': 'publiee'})

    # Le candidat reste une entité indépendante de l'employé.
    pdf = b'%PDF-1.4\nCV recrutement test'
    response = admin.post('/recrutement/candidats/nouveau', data={
        'nom': 'Dupont', 'prenom': 'Jean', 'email': 'jean.candidat@example.test',
        'telephone': '0340000000', 'diplome': 'Bac+5',
        'experience_annees': '4', 'experience': 'Développeur Python',
        'competences': 'Python\nFlask\nPostgreSQL',
        'cv': (io.BytesIO(pdf), 'jean_dupont.pdf'),
    }, content_type='multipart/form-data')
    assert response.status_code in (301, 302)
    with application.db_cursor() as (_conn, cur):
        cur.execute("SELECT * FROM recrutement_candidats")
        candidat = cur.fetchone()
    assert candidat['employe_id'] is None
    assert bytes(candidat['cv_contenu']) == pdf
    assert admin.get(f"/recrutement/candidats/{candidat['id']}").status_code == 200
    assert admin.get(
        f"/recrutement/candidats/{candidat['id']}/fichier/cv"
    ).data == pdf
    assert manager.get(
        f"/recrutement/candidats/{candidat['id']}/fichier/cv"
    ).status_code in (301, 302)

    admin.post(f"/recrutement/candidats/{candidat['id']}/candidature",
               data={'offre_id': str(offre['id'])})
    with application.db_cursor() as (_conn, cur):
        cur.execute("SELECT * FROM recrutement_candidatures")
        candidature = cur.fetchone()
    assert admin.get(
        f"/recrutement/candidatures/{candidature['id']}"
    ).status_code == 200
    assert admin.get(
        f"/recrutement/candidatures/{candidature['id']}/evaluation"
    ).status_code == 200

    # Notes pondérées : le score aide, mais ne change pas seul le statut.
    notes = {}
    for critere in criteres:
        notes[f"note_{critere['id']}"] = '80'
        notes[f"commentaire_{critere['id']}"] = 'Évaluation RH'
    admin.post(f"/recrutement/candidatures/{candidature['id']}/evaluation",
               data=notes)
    with application.db_cursor() as (_conn, cur):
        cur.execute("SELECT * FROM recrutement_candidatures WHERE id=%s",
                    (candidature['id'],))
        candidature = cur.fetchone()
    assert float(candidature['score_dossier']) == 80
    assert candidature['statut'] == 'evaluation'

    # Entretien réel et score global 40 % dossier / 60 % entretien.
    admin.post(f"/recrutement/candidatures/{candidature['id']}/entretiens/nouveau",
               data={'date_entretien': '2026-08-20', 'heure_entretien': '10:00',
                     'type_entretien': 'presentiel', 'lieu_ou_lien': 'Bureau IT'})
    with application.db_cursor() as (_conn, cur):
        cur.execute("SELECT id FROM recrutement_entretiens")
        entretien_id = cur.fetchone()['id']
    assert admin.get(
        f"/recrutement/entretiens/{entretien_id}/evaluer"
    ).status_code == 200
    evaluation_entretien = {f'note_{nom}': '90' for nom in (
        'Technique', 'Communication', 'Motivation',
        'Travail en équipe', 'Adaptabilité',
    )}
    evaluation_entretien['notes'] = 'Très bon entretien'
    admin.post(f"/recrutement/entretiens/{entretien_id}/evaluer",
               data=evaluation_entretien)
    with application.db_cursor() as (_conn, cur):
        cur.execute("SELECT * FROM recrutement_candidatures WHERE id=%s",
                    (candidature['id'],))
        candidature = cur.fetchone()
    assert float(candidature['score_entretien']) == 90
    assert float(candidature['score_global']) == 86
    assert candidature['statut'] != 'acceptee'
    comparison = admin.get(f"/recrutement/offres/{offre['id']}/comparer")
    assert comparison.status_code == 200
    assert b'Dupont' in comparison.data

    # Décision humaine, puis conversion transactionnelle employé + contrat.
    admin.post(f"/recrutement/candidatures/{candidature['id']}/statut",
               data={'statut': 'acceptee'})
    assert admin.get(
        f"/recrutement/candidatures/{candidature['id']}/embaucher"
    ).status_code == 200
    response = admin.post(f"/recrutement/candidatures/{candidature['id']}/embaucher",
                          data={'poste': 'Développeur Flask',
                                'departement_id': str(informatique),
                                'date_embauche': '2026-10-01', 'salaire': '62000',
                                'type_contrat': 'cdi',
                                'date_debut_contrat': '2026-10-01'})
    assert response.status_code in (301, 302)
    with application.db_cursor() as (_conn, cur):
        cur.execute("SELECT * FROM employes WHERE email='jean.candidat@example.test'")
        employe = cur.fetchone()
        cur.execute("SELECT * FROM contrats WHERE employe_id=%s", (employe['id'],))
        contrat = cur.fetchone()
        cur.execute("SELECT employe_id FROM recrutement_candidats WHERE id=%s",
                    (candidat['id'],))
        lien = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS nb FROM users WHERE employe_id=%s", (employe['id'],))
        comptes = cur.fetchone()['nb']
    assert contrat['type_contrat'] == 'cdi'
    assert lien['employe_id'] == employe['id']
    assert comptes == 0


def test_manager_ne_peut_plus_creer_de_demande_recrutement(app):
    """Le recrutement n'est plus de son ressort : sa demande est refusée.

    Ce test vérifiait auparavant qu'il ne pouvait demander que pour son
    propre département. Le périmètre strict lui ayant retiré le module
    entier, il vérifie désormais le refus. L'ancienne version continuait
    de passer, mais pour la mauvaise raison : le POST était bloqué par
    l'ACL avant même d'atteindre le contrôle de département, si bien
    qu'elle n'a plus jamais prouvé ce que son nom annonçait.
    """
    manager = app.test_client(); _login(manager, 'manager', 'manager123')
    rh_dept = _departement_id('Ressources Humaines')
    response = manager.post('/recrutement/demandes/nouvelle', data={
        'poste': 'Intrusion', 'departement_id': str(rh_dept),
        'nombre_postes': '1', 'type_contrat': 'cdi', 'motif': 'Test',
    })
    assert response.status_code in (301, 302)
    assert manager.get('/recrutement', follow_redirects=False).status_code in (301, 302)
    with application.db_cursor() as (_conn, cur):
        cur.execute("SELECT COUNT(*) AS nb FROM recrutement_demandes")
        assert cur.fetchone()['nb'] == 0
