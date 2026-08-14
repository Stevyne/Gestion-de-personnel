"""Recherche globale multi-domaine et palette de navigation."""

from flask import Blueprint, flash, jsonify, render_template, request, session, url_for


RECHERCHE_ACCES = {
    'employe':     None,
    'departement': None,
    'materiel':    None,
    'exemplaire':  None,
    'conge':       ('admin', 'rh', 'manager'),
    'absence':     ('admin', 'rh', 'manager'),
    'document':    None,
    'utilisateur': ('admin', 'rh'),
    'page':        None,
}

# Pages de l'application atteignables depuis la recherche : taper « congé »
# doit proposer d'aller sur la page des congés, pas seulement lister des demandes.
RECHERCHE_PAGES = [
    ('Tableau de bord',        'dashboard.dashboard',  None,                        'dashboard'),
    ('Employés',               'index',                None,                        'users'),
    ('Recrutement',            'recrutement.tableau_recrutement', ('admin','rh','manager'), 'clipboard'),
    ('Départements',           'departements.departements', None,                    'building'),
    ('Matériels',              'parc.materiels',       None,                        'box'),
    ('Inventaire physique',    'parc.inventaires',     None,                        'box'),
    ('Maintenance',            'parc.maintenances',    None,                        'box'),
    ('Présences',              'presences.presences',  None,                        'clock'),
    ('Historique',             'presences.historique', None,                        'history'),
    ('Absences',               'absences.absences',    ('admin', 'rh', 'manager'),  'user-x'),
    ('Congés',                 'conges.conges',         None,                        'palm'),
    ('Calendrier des congés',  'calendrier_conges',    None,                        'calendar'),
    ('Soldes de congés',       'soldes_conges_page',   None,                        'wallet'),
    ('Permissions',            'permissions',          None,                        'file'),
    ('Documents',              'documents.documents',  None,                        'file'),
    ('Contrats',               'contrats.contrats_liste', None,                      'file'),
    ('Départs',                'departs.departs_liste', ('admin', 'rh'),             'logout'),
    ('Utilisateurs',           'utilisateurs.utilisateurs_page', ('admin', 'rh'),            'shield'),
    ('Notifications',          'notifications.notifications', None,                 'bell'),
    ('Mon espace',             'auth.mon_profil',      None,                        'user'),
]



def creer_blueprint_recherche(deps):
    bp = Blueprint('recherche', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    department_scope_sql = deps['department_scope_sql']
    get_role_label = deps['get_role_label']
    logger = deps['logger']

    def _date_courte(valeur):
        """Formate une date pour l'affichage des résultats (tolère None)."""
        try:
            return valeur.strftime('%d/%m/%Y')
        except Exception:
            return str(valeur or '')


    def _total_exact(lignes, defaut=0):
        """Total réel renvoyé par COUNT(*) OVER() (calculé avant le LIMIT)."""
        return lignes[0]['_total'] if lignes else defaut


    def _recherche_autorise(categorie, role):
        """Le rôle a-t-il le droit de voir cette catégorie ? (admin voit tout)"""
        roles = RECHERCHE_ACCES.get(categorie)
        return roles is None or role == 'admin' or role in roles


    def recherche_globale(terme, role, limite_par_categorie=5):
        """Cherche `terme` dans tout le contenu métier visible par `role`.

        Renvoie une liste de groupes [{categorie, libelle, icone, resultats[], total}].
        Chaque résultat porte un titre, un sous-titre et une URL de destination.
        """
        terme = (terme or '').strip()
        if len(terme) < 2:          # en dessous, le bruit dépasse l'utilité
            return []

        # LIKE insensible à la casse. On échappe les jokers SQL pour qu'un terme
        # contenant % ou _ soit cherché littéralement au lieu de tout retourner.
        motif = '%' + terme.lower().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        groupes = []

        def ajouter(categorie, libelle, icone, lignes, total, url_liste=None):
            if lignes:
                groupes.append({
                    'categorie': categorie, 'libelle': libelle, 'icone': icone,
                    'resultats': lignes, 'total': total, 'url_liste': url_liste,
                })

        with db_cursor() as (conn, cur):
            employee_scope, employee_scope_params = department_scope_sql('e', cur=cur)
            department_scope, department_scope_params = department_scope_sql('d', 'nom', cur)
            # ---- Employés ----
            if _recherche_autorise('employe', role):
                cur.execute(f"""
                    SELECT COUNT(*) OVER() AS _total,
                           e.id, e.nom, e.prenom, e.poste, e.departement, e.email, (
                        SELECT u.photo FROM users u
                         WHERE u.employe_id = e.id AND u.photo IS NOT NULL
                         ORDER BY u.id LIMIT 1
                    ) AS photo
                      FROM employes e
                     WHERE (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s
                        OR LOWER(e.nom || ' ' || e.prenom) LIKE %s
                        OR LOWER(e.prenom || ' ' || e.nom) LIKE %s
                        OR LOWER(COALESCE(e.poste, '')) LIKE %s
                        OR LOWER(COALESCE(e.email, '')) LIKE %s
                        OR LOWER(COALESCE(e.telephone, '')) LIKE %s)
                       AND {employee_scope}
                     ORDER BY e.nom, e.prenom LIMIT %s
                """, [motif] * 7 + employee_scope_params + [limite_par_categorie + 1])
                lignes = cur.fetchall()
                total = _total_exact(lignes)
                ajouter('employe', 'Employés', 'users', [{
                    'titre': f"{r['prenom']} {r['nom']}",
                    'sous_titre': ' · '.join(x for x in [r.get('poste'), r.get('departement')] if x) or r.get('email') or '',
                    'url': url_for('view_employee', id=r['id']),
                    'photo': r.get('photo'),
                } for r in lignes[:limite_par_categorie]], total, url_for('index', search=terme))

            # ---- Départements ----
            if _recherche_autorise('departement', role):
                cur.execute(f"""
                    SELECT COUNT(*) OVER() AS _total, d.id, d.nom, d.description, d.responsable
                      FROM departements d
                     WHERE (LOWER(COALESCE(d.nom, '')) LIKE %s
                        OR LOWER(COALESCE(d.description, '')) LIKE %s
                        OR LOWER(COALESCE(d.responsable, '')) LIKE %s)
                       AND {department_scope}
                     ORDER BY d.nom LIMIT %s
                """, [motif, motif, motif] + department_scope_params +
                      [limite_par_categorie + 1])
                lignes = cur.fetchall()
                ajouter('departement', 'Départements', 'building', [{
                    'titre': r['nom'],
                    'sous_titre': (f"Responsable : {r['responsable']}" if r.get('responsable') else (r.get('description') or '')),
                    'url': url_for('parc.materiels_departement', id=r['id']),
                } for r in lignes[:limite_par_categorie]], _total_exact(lignes), url_for('departements.departements'))

            # ---- Matériels ----
            if _recherche_autorise('materiel', role):
                cur.execute(f"""
                    SELECT COUNT(*) OVER() AS _total,
                           m.id, m.nom, m.categorie, m.quantite, m.unite, m.departement_id, d.nom AS dept
                      FROM materiels m LEFT JOIN departements d ON d.id = m.departement_id
                     WHERE (LOWER(m.nom) LIKE %s
                        OR LOWER(COALESCE(m.description, '')) LIKE %s
                        OR LOWER(COALESCE(m.categorie, '')) LIKE %s)
                       AND {department_scope}
                     ORDER BY m.nom LIMIT %s
                """, [motif, motif, motif] + department_scope_params +
                      [limite_par_categorie + 1])
                lignes = cur.fetchall()
                ajouter('materiel', 'Matériels', 'box', [{
                    'titre': r['nom'],
                    'sous_titre': ' · '.join(x for x in [
                        r.get('dept'), f"{r['quantite']} {r.get('unite') or ''}".strip()] if x),
                    'url': (url_for('parc.materiels_departement', id=r['departement_id'])
                            if r.get('departement_id') else url_for('parc.materiels')),
                } for r in lignes[:limite_par_categorie]], _total_exact(lignes), url_for('parc.materiels'))

            # ---- Exemplaires / numéros d'inventaire ----
            if _recherche_autorise('exemplaire', role):
                cur.execute(f"""SELECT COUNT(*) OVER() AS _total,
                           ex.id,ex.numero_inventaire,ex.numero_serie,ex.etat,
                           m.nom AS materiel_nom,d.nom AS departement
                      FROM materiel_exemplaires ex JOIN materiels m ON m.id=ex.materiel_id
                      LEFT JOIN departements d ON d.id=m.departement_id
                     WHERE (LOWER(ex.numero_inventaire) LIKE %s
                        OR LOWER(COALESCE(ex.numero_serie,'')) LIKE %s
                        OR LOWER(m.nom) LIKE %s) AND {department_scope}
                     ORDER BY ex.numero_inventaire LIMIT %s""",
                            [motif,motif,motif] + department_scope_params + [limite_par_categorie+1])
                lignes = cur.fetchall()
                ajouter('exemplaire','Exemplaires','box',[{
                    'titre': r['numero_inventaire'],
                    'sous_titre': ' · '.join(x for x in [r['materiel_nom'],r.get('departement'),r.get('etat')] if x),
                    'url': url_for('parc.view_exemplaire', id=r['id']),
                } for r in lignes[:limite_par_categorie]], _total_exact(lignes), url_for('parc.materiels', search=terme))

            # ---- Congés ----
            if _recherche_autorise('conge', role):
                cur.execute(f"""
                    SELECT COUNT(*) OVER() AS _total,
                           c.id, c.type_conge, c.statut, c.date_debut, c.date_fin, c.motif,
                           e.nom, e.prenom
                      FROM conges c JOIN employes e ON c.employe_id = e.id
                     WHERE (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s
                        OR LOWER(COALESCE(c.motif, '')) LIKE %s
                        OR LOWER(COALESCE(c.type_conge, '')) LIKE %s
                        OR LOWER(COALESCE(c.statut, '')) LIKE %s)
                       AND {employee_scope}
                     ORDER BY c.date_debut DESC LIMIT %s
                """, [motif] * 5 + employee_scope_params + [limite_par_categorie + 1])
                lignes = cur.fetchall()
                ajouter('conge', 'Congés', 'palm', [{
                    'titre': f"{r['prenom']} {r['nom']} — {r.get('type_conge') or 'congé'}",
                    'sous_titre': f"{_date_courte(r['date_debut'])} → {_date_courte(r['date_fin'])} · {r.get('statut') or ''}",
                    'url': url_for('conges.conges', search=f"{r['prenom']} {r['nom']}"),
                } for r in lignes[:limite_par_categorie]], _total_exact(lignes), url_for('conges.conges', search=terme))

            # ---- Absences ----
            if _recherche_autorise('absence', role):
                cur.execute(f"""
                    SELECT COUNT(*) OVER() AS _total, a.id, a.date, a.motif, e.nom, e.prenom
                      FROM absences a JOIN employes e ON a.employe_id = e.id
                     WHERE (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s
                        OR LOWER(COALESCE(a.motif, '')) LIKE %s)
                       AND {employee_scope}
                     ORDER BY a.date DESC LIMIT %s
                """, [motif, motif, motif] + employee_scope_params +
                      [limite_par_categorie + 1])
                lignes = cur.fetchall()
                ajouter('absence', 'Absences', 'user-x', [{
                    'titre': f"{r['prenom']} {r['nom']}",
                    'sous_titre': f"{_date_courte(r['date'])}" + (f" · {r['motif']}" if r.get('motif') else ''),
                    'url': url_for('absences.absences', search=f"{r['prenom']} {r['nom']}"),
                } for r in lignes[:limite_par_categorie]], _total_exact(lignes), url_for('absences.absences', search=terme))

            # ---- Documents ----
            if _recherche_autorise('document', role):
                cur.execute(f"""
                    SELECT COUNT(*) OVER() AS _total, d.id, d.titre, d.nom_fichier, d.description, e.nom, e.prenom
                      FROM documents d LEFT JOIN employes e ON d.employe_id = e.id
                     WHERE (LOWER(d.titre) LIKE %s
                        OR LOWER(COALESCE(d.description, '')) LIKE %s
                        OR LOWER(COALESCE(d.nom_fichier, '')) LIKE %s)
                       AND {employee_scope}
                     ORDER BY d.date_upload DESC LIMIT %s
                """, [motif, motif, motif] + employee_scope_params +
                      [limite_par_categorie + 1])
                lignes = cur.fetchall()
                ajouter('document', 'Documents', 'file', [{
                    'titre': r['titre'],
                    'sous_titre': (f"{r['prenom']} {r['nom']}" if r.get('nom') else (r.get('nom_fichier') or '')),
                    'url': url_for('documents.documents'),
                } for r in lignes[:limite_par_categorie]], _total_exact(lignes), url_for('documents.documents'))

            # ---- Comptes utilisateurs ----
            if _recherche_autorise('utilisateur', role):
                cur.execute("""
                    SELECT COUNT(*) OVER() AS _total, u.id, u.username, u.role, u.photo, e.nom, e.prenom
                      FROM users u LEFT JOIN employes e ON u.employe_id = e.id
                     WHERE LOWER(u.username) LIKE %s
                        OR LOWER(COALESCE(e.nom, '')) LIKE %s
                        OR LOWER(COALESCE(e.prenom, '')) LIKE %s
                     ORDER BY u.username LIMIT %s
                """, [motif, motif, motif, limite_par_categorie + 1])
                lignes = cur.fetchall()
                ajouter('utilisateur', 'Comptes', 'shield', [{
                    'titre': r['username'],
                    'sous_titre': ' · '.join(x for x in [
                        get_role_label(r.get('role')),
                        (f"{r['prenom']} {r['nom']}" if r.get('nom') else None)] if x),
                    'url': url_for('utilisateurs.utilisateurs_page'),
                    'photo': r.get('photo'),
                } for r in lignes[:limite_par_categorie]], _total_exact(lignes), url_for('utilisateurs.utilisateurs_page'))

        # ---- Pages de l'application ----
        terme_bas = terme.lower()
        pages = []
        for libelle, endpoint, roles, icone in RECHERCHE_PAGES:
            if roles and role != 'admin' and role not in roles:
                continue
            if terme_bas in libelle.lower():
                try:
                    pages.append({'titre': libelle, 'sous_titre': 'Aller à la page',
                                  'url': url_for(endpoint)})
                except Exception:
                    continue        # endpoint absent : on ignore la page
        if pages:
            groupes.append({'categorie': 'page', 'libelle': 'Navigation', 'icone': 'dashboard',
                            'resultats': pages[:limite_par_categorie], 'total': len(pages),
                            'url_liste': None})

        return groupes


    @bp.route('/api/recherche')
    @login_required
    def api_recherche():
        """Aperçu instantané : renvoie les résultats en JSON pour le panneau déroulant."""
        terme = request.args.get('q', '').strip()
        role = session.get('role', 'employe')
        if len(terme) < 2:
            return jsonify({'terme': terme, 'groupes': [], 'total': 0})
        try:
            groupes = recherche_globale(terme, role, limite_par_categorie=4)
        except Exception as e:
            logger.error("Erreur de recherche globale : %s", e, exc_info=True)
            return jsonify({'terme': terme, 'groupes': [], 'total': 0, 'erreur': True}), 200
        total = sum(g['total'] for g in groupes)
        return jsonify({'terme': terme, 'groupes': groupes, 'total': total})


    @bp.route('/recherche')
    @login_required
    def recherche_page():
        """Page listant tous les résultats, groupés par catégorie."""
        terme = request.args.get('q', '').strip()
        role = session.get('role', 'employe')
        groupes = []
        if len(terme) >= 2:
            try:
                groupes = recherche_globale(terme, role, limite_par_categorie=20)
            except Exception as e:
                logger.error("Erreur de recherche globale : %s", e, exc_info=True)
                flash("La recherche a échoué. Réessayez.", "danger")
        total = sum(g['total'] for g in groupes)
        return render_template('recherche.html', terme=terme, groupes=groupes, total=total)


    return bp, {'recherche_globale': recherche_globale}
