import re
from datetime import date, timedelta

import app as application


def _dernier_jour_ouvre_passe():
    """Retourne le plus récent jour ouvré (lun-ven) strictement avant aujourd'hui."""
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:  # 5=samedi, 6=dimanche
        d -= timedelta(days=1)
    return d


def _preparer_periodes(cur, employe_id, embauche, autres_embauche):
    """Fixe les dates d'embauche pour limiter la portée du calcul et nettoie
    les données liées : tous les autres employés sont 'embauchés' aujourd'hui
    (hors période) pour ne pas polluer le test."""
    cur.execute("UPDATE employes SET date_embauche = %s", (autres_embauche,))
    cur.execute("UPDATE employes SET date_embauche = %s WHERE id = %s", (embauche, employe_id))
    cur.execute("DELETE FROM presences WHERE employe_id = %s", (employe_id,))
    cur.execute("DELETE FROM conges WHERE employe_id = %s", (employe_id,))
    cur.execute("DELETE FROM permissions WHERE employe_id = %s", (employe_id,))
    cur.execute("DELETE FROM absences")


def test_jour_sans_presence_devient_absence(admin_client):
    hier = _dernier_jour_ouvre_passe()
    today = date.today()
    with application.db_cursor(commit=True) as (conn, cur):
        _preparer_periodes(cur, 1, hier, today)
        nb = application.generer_absences_automatiques(cur, date_jusqua=hier)
    assert nb == 1
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM absences WHERE employe_id = 1")
        assert cur.fetchone()['n'] == 1


def test_presence_empeche_absence(admin_client):
    hier = _dernier_jour_ouvre_passe()
    today = date.today()
    with application.db_cursor(commit=True) as (conn, cur):
        _preparer_periodes(cur, 1, hier, today)
        # l'employé a pointé ce jour-là -> pas d'absence
        cur.execute(
            "INSERT INTO presences (employe_id, date, heure_arrivee, statut) VALUES (%s, %s, '08:30', 'présent')",
            (1, hier),
        )
        nb = application.generer_absences_automatiques(cur, date_jusqua=hier)
    assert nb == 0
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM absences WHERE employe_id = 1")
        assert cur.fetchone()['n'] == 0


def test_conge_approuve_empeche_absence(admin_client):
    hier = _dernier_jour_ouvre_passe()
    today = date.today()
    with application.db_cursor(commit=True) as (conn, cur):
        _preparer_periodes(cur, 1, hier, today)
        cur.execute(
            "INSERT INTO conges (employe_id, type_conge, date_debut, date_fin, nombre_jours, statut) "
            "VALUES (1, 'congé payé', %s, %s, 1, 'approuvé')",
            (hier, hier),
        )
        nb = application.generer_absences_automatiques(cur, date_jusqua=hier)
    assert nb == 0
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM absences WHERE employe_id = 1")
        assert cur.fetchone()['n'] == 0


def test_permission_approuvee_empeche_absence(admin_client):
    """Lien entre les deux modules : une permission approuvée couvre le jour,
    donc il n'est pas marqué comme absence non justifiée."""
    hier = _dernier_jour_ouvre_passe()
    today = date.today()
    with application.db_cursor(commit=True) as (conn, cur):
        _preparer_periodes(cur, 1, hier, today)
        cur.execute(
            "INSERT INTO permissions (employe_id, motif, date_debut, date_fin, nombre_jours, statut) "
            "VALUES (1, 'démarche', %s, %s, 1, 'approuvé')",
            (hier, hier),
        )
        nb = application.generer_absences_automatiques(cur, date_jusqua=hier)
    assert nb == 0


def test_generation_est_idempotente(admin_client):
    hier = _dernier_jour_ouvre_passe()
    today = date.today()
    with application.db_cursor(commit=True) as (conn, cur):
        _preparer_periodes(cur, 1, hier, today)
        n1 = application.generer_absences_automatiques(cur, date_jusqua=hier)
        n2 = application.generer_absences_automatiques(cur, date_jusqua=hier)
    assert n1 == 1
    assert n2 == 0  # déjà créé -> rien de nouveau


def test_absences_page_employe_limitee_a_son_propre_dossier(employe_client):
    """/absences est ouverte à l'employé, mais il n'y voit que ses lignes.

    Le cloisonnement par département ne suffit pas ici : il laisserait
    l'employé lire les absences de ses collègues, ce qui ne le regarde pas.
    La vue restreint donc sur son identifiant, et le filtre « employe_id »
    de la querystring ne doit pas permettre de contourner cela.
    """
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("SELECT employe_id FROM users WHERE username = %s", ('employe',))
        mon_id = cur.fetchone()['employe_id']
        cur.execute("SELECT id FROM employes WHERE id != %s ORDER BY id LIMIT 1", (mon_id,))
        autre = cur.fetchone()
        autre_id = autre['id'] if autre else None
        cur.execute("DELETE FROM absences WHERE motif LIKE %s", ('TEST-H %',))
        cur.execute("""
            INSERT INTO absences (employe_id, date, motif, statut)
            VALUES (%s, DATE '2020-03-02', 'TEST-H mienne', 'non_justifiee')
        """, (mon_id,))
        if autre_id:
            cur.execute("""
                INSERT INTO absences (employe_id, date, motif, statut)
                VALUES (%s, DATE '2020-03-03', 'TEST-H collegue', 'non_justifiee')
            """, (autre_id,))

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT prenom, nom FROM employes WHERE id = %s", (mon_id,))
        moi = cur.fetchone()
    mon_nom = f"{moi['prenom']} {moi['nom']}"

    def _noms_lignes(page):
        return {m.strip() for m in re.findall(
            r'<td data-label="Employé"><strong>(.*?)</strong></td>', page)}

    resp = employe_client.get('/absences')
    assert resp.status_code == 200
    page = resp.data.decode('utf-8')
    # Son absence y figure, avec de quoi la justifier lui-même.
    assert 'Justifier cette absence' in page
    # Celles de ses collègues n'y figurent jamais.
    assert _noms_lignes(page) == {mon_nom}

    # Passer ?employe_id=<un collègue> ne change rien.
    if autre_id:
        resp = employe_client.get(f'/absences?employe_id={autre_id}')
        assert _noms_lignes(resp.data.decode('utf-8')) == {mon_nom}

    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("DELETE FROM absences WHERE motif LIKE %s", ('TEST-H %',))
