import re
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

import app as application


def _valeur(html, attribut):
    motif = rb'data-(?:kpi|stat)="' + attribut.encode() + rb'"[^>]*>\s*([^<]+)'
    match = re.search(motif, html)
    assert match, f"Indicateur {attribut!r} absent du tableau de bord"
    return match.group(1).decode().strip()


def _seed_dashboard():
    aujourd_hui = date.today()
    with application.db_cursor(commit=True) as (conn, cur):
        for nom in ('Informatique', 'Ressources Humaines', 'Administration'):
            cur.execute("""INSERT INTO departements (nom) VALUES (%s)
                           ON CONFLICT (nom) DO UPDATE SET nom = EXCLUDED.nom""", (nom,))
        cur.execute("SELECT id, nom FROM departements WHERE nom IN %s",
                    (('Informatique', 'Ressources Humaines', 'Administration'),))
        departements = {row['nom']: row['id'] for row in cur.fetchall()}
        cur.execute("UPDATE employes SET departement = 'Informatique' WHERE id IN (1,3)")
        cur.execute("UPDATE employes SET departement = 'Ressources Humaines' WHERE id = 2")
        cur.execute("UPDATE employes SET departement = 'Administration' WHERE id = 4")

        cur.execute("""INSERT INTO presences
            (employe_id, date, heure_arrivee, heure_depart, statut)
            VALUES
            (1, %s, '08:30', '17:00', 'présent'),
            (3, %s, '09:00', '17:00', 'télétravail'),
            (2, %s, '08:45', '17:00', 'présent')""",
                    (aujourd_hui, aujourd_hui, aujourd_hui))

        for employe_id in (1, 2):
            cur.execute("""INSERT INTO conges
                (employe_id, type_conge, date_debut, date_fin, nombre_jours, statut)
                VALUES (%s, 'congé payé', %s, %s, 1, 'en attente')""",
                        (employe_id, aujourd_hui + timedelta(days=10),
                         aujourd_hui + timedelta(days=10)))
            cur.execute("""INSERT INTO permissions
                (employe_id, motif, date_debut, date_fin, nombre_jours, statut)
                VALUES (%s, 'Test', %s, %s, 1, 'en attente')""",
                        (employe_id, aujourd_hui + timedelta(days=5),
                         aujourd_hui + timedelta(days=5)))

        cur.execute("""INSERT INTO absences (employe_id, date, motif, statut)
                       VALUES (1, %s, 'IT', 'non_justifiee')""",
                    (aujourd_hui - timedelta(days=2),))
        cur.execute("""INSERT INTO absences (employe_id, date, motif, statut)
                       VALUES (2, %s, 'RH 1', 'non_justifiee'),
                              (2, %s, 'RH 2', 'refusee')""",
                    (aujourd_hui - timedelta(days=2), aujourd_hui - timedelta(days=3)))

        for index, employe_id in enumerate((1, 2, 2), start=1):
            cur.execute("""INSERT INTO documents
                (employe_id, titre, nom_fichier, chemin_fichier, type_fichier,
                 taille, date_expiration, contenu)
                VALUES (%s, %s, %s, %s, 'pdf', 4, %s, %s)""",
                        (employe_id, f'Document {index}', f'doc{index}.pdf',
                         f'doc{index}.pdf', aujourd_hui - timedelta(days=1), b'%PDF'))

        cur.execute("""INSERT INTO soldes_conges
            (employe_id, annee, jours_acquis, jours_utilises)
            VALUES (1,%s,20,5),(2,%s,20,5),(3,%s,20,5),(4,%s,20,5)
            ON CONFLICT (employe_id, annee) DO UPDATE
            SET jours_acquis=EXCLUDED.jours_acquis, jours_utilises=EXCLUDED.jours_utilises""",
                    (aujourd_hui.year,) * 4)

        cur.execute("""INSERT INTO materiels
            (nom, categorie, departement_id, quantite, seuil_alerte,
             prix_acquisition, suivi_unitaire)
            VALUES ('PC IT','informatique',%s,3,5,1000,FALSE) RETURNING id""",
                    (departements['Informatique'],))
        materiel_it = cur.fetchone()['id']
        cur.execute("""INSERT INTO materiels
            (nom, categorie, departement_id, quantite, seuil_alerte)
            VALUES ('Papier RH','papeterie',%s,10,2),
                   ('Écran RH','informatique',%s,1,0)""",
                    (departements['Ressources Humaines'],
                     departements['Ressources Humaines']))
        cur.execute("""INSERT INTO materiel_exemplaires
            (materiel_id, numero_inventaire, etat)
            VALUES (%s, 'DASH-IT-001', 'panne') RETURNING id""", (materiel_it,))
        exemplaire = cur.fetchone()['id']
        cur.execute("""INSERT INTO materiel_maintenances
            (exemplaire_id, statut, panne) VALUES (%s, 'signale', 'Test')""",
                    (exemplaire,))
        cur.execute("""INSERT INTO inventaires
            (reference, departement_id, statut) VALUES ('INV-DASH-IT', %s, 'en_cours')""",
                    (departements['Informatique'],))


def test_manager_ne_voit_que_son_departement(client):
    _seed_dashboard()
    client.post('/login', data={'username': 'manager', 'password': 'manager123'})
    response = client.get('/')
    assert response.status_code == 200
    assert 'Vue départementale'.encode('utf-8') in response.data
    assert b'Informatique' in response.data
    assert _valeur(response.data, 'employees') == '2'
    assert _valeur(response.data, 'present') == '1'
    assert _valeur(response.data, 'remote') == '1'
    assert _valeur(response.data, 'absent') == '0'
    assert _valeur(response.data, 'documents-expired') == '1'
    assert _valeur(response.data, 'absence-open') == '1'
    assert _valeur(response.data, 'material-articles').startswith('1 /')
    assert _valeur(response.data, 'maintenance-open') == '1'
    assert b'data-kpi="salary"' not in response.data
    assert 'Accès et exploitation'.encode('utf-8') not in response.data
    assert 'Sophie Martin'.encode('utf-8') not in response.data


def test_employe_a_le_meme_cloisonnement_departemental(client):
    _seed_dashboard()
    client.post('/login', data={'username': 'employe', 'password': 'user123'})
    response = client.get('/')
    assert response.status_code == 200
    assert _valeur(response.data, 'employees') == '2'
    assert _valeur(response.data, 'documents-expired') == '1'


def test_admin_et_rh_conservent_vue_globale(client):
    _seed_dashboard()
    for username, password in (('admin', 'admin123'), ('rh', 'rh123')):
        client.get('/logout')
        client.post('/login', data={'username': username, 'password': password})
        response = client.get('/')
        assert response.status_code == 200
        assert 'Vue globale'.encode('utf-8') in response.data
        assert _valeur(response.data, 'employees') == '4'
        assert _valeur(response.data, 'documents-expired') == '3'
        assert _valeur(response.data, 'absence-open') == '3'
        assert b'data-kpi="salary"' in response.data
        assert 'Accès et exploitation'.encode('utf-8') in response.data


def test_compte_sans_departement_ne_bascule_jamais_en_global(client):
    _seed_dashboard()
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""INSERT INTO users (username, password_hash, role, employe_id)
                       VALUES ('tech-sans-dept', %s, 'technicien', NULL)""",
                    (generate_password_hash('Secret12!'),))
    client.post('/login', data={'username': 'tech-sans-dept', 'password': 'Secret12!'})
    response = client.get('/')
    assert response.status_code == 200
    assert 'Aucun département rattaché'.encode('utf-8') in response.data
    assert _valeur(response.data, 'employees') == '0'
    assert _valeur(response.data, 'documents-expired') == '0'
    assert _valeur(response.data, 'material-articles').startswith('0 /')


def test_tableau_couvre_tous_les_modules_statistiques(client):
    _seed_dashboard()
    client.post('/login', data={'username': 'admin', 'password': 'admin123'})
    response = client.get('/')
    for titre in (
        'Présences du jour', 'Congés et soldes', 'Permissions', 'Absences',
        'Documents', 'Matériels et parc', 'Maintenance', 'Inventaires',
        'Accès et exploitation',
    ):
        assert titre.encode('utf-8') in response.data
    assert b'<strong>12/12</strong>' in response.data
