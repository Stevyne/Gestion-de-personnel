import io
from datetime import date, timedelta

import openpyxl
import psycopg2
import pytest

import app as application


def _initier_depart(client, employe_id=1):
    return client.post(f'/employes/{employe_id}/depart/initier', data={
        'date_depart_prevue': (date.today() + timedelta(days=10)).isoformat(),
        'motif_depart': 'Fin de collaboration',
    }, follow_redirects=True)


def _materiel_generique_attribue(client, employe_id=1):
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""INSERT INTO departements(nom) VALUES ('Phase2 IT')
                       ON CONFLICT(nom) DO UPDATE SET nom=EXCLUDED.nom RETURNING id""")
        dept = cur.fetchone()['id']
        cur.execute("""INSERT INTO materiels(nom,categorie,departement_id,quantite,suivi_unitaire)
                       VALUES ('Badge Phase2','autre',%s,2,FALSE) RETURNING id""", (dept,))
        materiel = cur.fetchone()['id']
    client.post(f'/materiels/{materiel}/attribuer', data={
        'employe_id': employe_id, 'quantite': 1,
    })
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM materiels_attributions WHERE materiel_id=%s", (materiel,))
        attribution = cur.fetchone()['id']
    return materiel, attribution


def _exemplaire_phase2(numero='P2-001'):
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""INSERT INTO departements(nom) VALUES ('Maintenance P2')
                       ON CONFLICT(nom) DO UPDATE SET nom=EXCLUDED.nom RETURNING id""")
        dept = cur.fetchone()['id']
        cur.execute("""INSERT INTO materiels(nom,categorie,departement_id,quantite,suivi_unitaire)
                       VALUES ('PC Maintenance P2','informatique',%s,1,TRUE) RETURNING id""", (dept,))
        materiel = cur.fetchone()['id']
        cur.execute("""INSERT INTO materiel_exemplaires(materiel_id,numero_inventaire,etat)
                       VALUES (%s,%s,'bon') RETURNING id""", (materiel, numero))
        return cur.fetchone()['id']


def test_depart_bloque_jusqu_au_retour_de_tout_materiel(admin_client):
    _, attribution = _materiel_generique_attribue(admin_client)
    _initier_depart(admin_client)
    bloque = admin_client.post('/employes/1/depart/finaliser', data={
        'date_depart_effective': date.today().isoformat(),
    }, follow_redirects=True)
    assert 'Clôture impossible'.encode('utf-8') in bloque.data

    admin_client.post(f'/materiels/attribution/{attribution}/retour', data={})
    final = admin_client.post('/employes/1/depart/finaliser', data={
        'date_depart_effective': date.today().isoformat(),
    }, follow_redirects=True)
    assert 'Départ finalisé'.encode('utf-8') in final.data
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT actif,statut_depart,date_depart_effective FROM employes WHERE id=1")
        employe = cur.fetchone()
        cur.execute("SELECT actif FROM users WHERE username='employe'")
        compte = cur.fetchone()['actif']
    assert employe['actif'] is False and employe['statut_depart'] == 'finalise'
    assert compte is False


def test_compte_archive_ne_peut_plus_se_connecter(client, admin_client):
    _initier_depart(admin_client)
    admin_client.post('/employes/1/depart/finaliser', data={
        'date_depart_effective': date.today().isoformat(),
    })
    client.get('/logout')
    client.post('/login', data={'username': 'employe', 'password': 'user123'})
    with client.session_transaction() as session:
        assert 'user_id' not in session


def test_trigger_postgresql_interdit_archivage_avec_materiel(admin_client):
    _materiel_generique_attribue(admin_client)
    with pytest.raises(psycopg2.errors.RaiseException):
        with application.db_cursor(commit=True) as (conn, cur):
            cur.execute("UPDATE employes SET actif=FALSE WHERE id=1")


def test_employe_archive_refuse_nouvelle_operation_metier(admin_client):
    _initier_depart(admin_client)
    admin_client.post('/employes/1/depart/finaliser', data={
        'date_depart_effective': date.today().isoformat(),
    })
    with pytest.raises(psycopg2.errors.RaiseException):
        with application.db_cursor(commit=True) as (conn, cur):
            cur.execute("""INSERT INTO presences(employe_id,date,statut)
                           VALUES (1,CURRENT_DATE,'présent')""")


def test_ticket_maintenance_reference_priorite_et_sla(admin_client):
    exemplaire = _exemplaire_phase2()
    response = admin_client.post(f'/exemplaires/{exemplaire}/panne', data={
        'panne': 'Serveur indisponible', 'priorite': 'critique',
    }, follow_redirects=True)
    assert response.status_code == 200
    with application.db_cursor() as (conn, cur):
        cur.execute("""SELECT *, sla_echeance-date_creation AS delai_sla
                       FROM materiel_maintenances WHERE exemplaire_id=%s""", (exemplaire,))
        ticket = cur.fetchone()
    assert ticket['reference'].startswith(f'MAINT-{date.today().year}-')
    assert ticket['priorite'] == 'critique'
    assert ticket['delai_sla'] == timedelta(hours=4)


def test_assignation_et_resolution_renseignent_sla(admin_client):
    exemplaire = _exemplaire_phase2()
    admin_client.post(f'/exemplaires/{exemplaire}/panne', data={
        'panne': 'Écran noir', 'priorite': 'haute',
    })
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM materiel_maintenances WHERE exemplaire_id=%s", (exemplaire,))
        ticket = cur.fetchone()['id']
    admin_client.post(f'/maintenances/{ticket}/assigner', data={
        'cible': 'interne', 'assigne_user_id': '1',
    })
    admin_client.post(f'/maintenances/{ticket}/cloturer', data={
        'resultat': 'repare', 'diagnostic': 'Réparé', 'etat_retour': 'bon',
    })
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT date_prise_en_charge,date_resolution,sla_respecte FROM materiel_maintenances WHERE id=%s", (ticket,))
        row = cur.fetchone()
    assert row['date_prise_en_charge'] is not None
    assert row['date_resolution'] is not None
    assert row['sla_respecte'] is True


def test_export_materiel_pdf_et_excel_respecte_departement(client):
    with application.db_cursor(commit=True) as (conn, cur):
        for dept, marker in (('Informatique','VISIBLE-P2'),('Ressources Humaines','SECRET-P2')):
            cur.execute("INSERT INTO departements(nom) VALUES (%s) ON CONFLICT(nom) DO UPDATE SET nom=EXCLUDED.nom RETURNING id", (dept,))
            dept_id = cur.fetchone()['id']
            cur.execute("INSERT INTO materiels(nom,categorie,departement_id,quantite) VALUES (%s,'autre',%s,3)", (marker,dept_id))
    client.post('/login', data={'username': 'manager', 'password': 'manager123'})
    pdf = client.get('/export/materiels/pdf')
    assert pdf.status_code == 200 and pdf.data.startswith(b'%PDF')
    excel = client.get('/export/materiels/excel')
    assert excel.status_code == 200
    workbook = openpyxl.load_workbook(io.BytesIO(excel.data))
    values = ' '.join(str(c.value or '') for row in workbook.active.iter_rows() for c in row)
    assert 'VISIBLE-P2' in values
    assert 'SECRET-P2' not in values


def test_contrat_complet_document_et_acces_proprietaire(admin_client, app):
    pdf = b'%PDF-1.4\ncontrat-phase2'
    response = admin_client.post('/employes/1/contrats/nouveau', data={
        'type_contrat': 'cdd', 'reference': 'CDD-2026-001',
        'date_debut': date.today().isoformat(),
        'date_fin': (date.today()+timedelta(days=30)).isoformat(),
        'notes': 'Contrat test', 'fichier': (io.BytesIO(pdf),'contrat.pdf'),
    }, content_type='multipart/form-data', follow_redirects=False)
    assert response.status_code in (301,302)
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id,contenu FROM contrats WHERE employe_id=1")
        contrat = cur.fetchone()
    assert bytes(contrat['contenu']) == pdf

    employe = app.test_client()
    employe.post('/login', data={'username':'employe','password':'user123'})
    assert employe.get(f"/contrats/{contrat['id']}").status_code == 200
    assert employe.get(f"/contrats/{contrat['id']}/fichier").data == pdf
    manager = app.test_client()
    manager.post('/login', data={'username':'manager','password':'manager123'})
    assert manager.get(f"/contrats/{contrat['id']}").status_code == 403


def test_renouvellement_contrat_versionne(admin_client):
    admin_client.post('/employes/1/contrats/nouveau', data={
        'type_contrat':'cdd','date_debut':date.today().isoformat(),
        'date_fin':(date.today()+timedelta(days=5)).isoformat(),
    })
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM contrats WHERE employe_id=1")
        ancien = cur.fetchone()['id']
    admin_client.post(f'/contrats/{ancien}/renouveler', data={
        'type_contrat':'cdd','date_debut':(date.today()+timedelta(days=6)).isoformat(),
        'date_fin':(date.today()+timedelta(days=365)).isoformat(),
    })
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT statut FROM contrats WHERE id=%s", (ancien,))
        assert cur.fetchone()['statut'] == 'renouvele'
        cur.execute("SELECT renouvelle_depuis FROM contrats WHERE renouvelle_depuis=%s", (ancien,))
        assert cur.fetchone()['renouvelle_depuis'] == ancien


def test_alertes_contrats_30_7_expiration_idempotentes(admin_client):
    with application.db_cursor(commit=True) as (conn, cur):
        for index, jours in enumerate((30,7,-1), start=1):
            cur.execute("""INSERT INTO contrats
                (employe_id,type_contrat,reference,date_debut,date_fin,statut)
                VALUES (1,'cdd',%s,%s,%s,'actif')""",
                        (f'ALERTE-{index}', date.today()-timedelta(days=30),
                         date.today()+timedelta(days=jours)))
    assert application.job_alertes_contrats() == 3
    assert application.job_alertes_contrats() == 0
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM contrats_alertes")
        assert cur.fetchone()['n'] == 3
        cur.execute("SELECT COUNT(*) AS n FROM notifications WHERE title='Échéance de contrat'")
        assert cur.fetchone()['n'] >= 3
        cur.execute("SELECT statut FROM contrats WHERE reference='ALERTE-3'")
        assert cur.fetchone()['statut'] == 'expire'
