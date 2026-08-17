"""Blueprint « Compétences » : référentiel et niveaux par employé.

URL publiques exposées par ce blueprint :

    /competences                       → catalogue des compétences
    /competences/nouvelle              → ajout (admin/RH)
    /competences/<id>                  → détail d'une compétence (employés associés)
    /competences/<id>/modifier         → modification (admin/RH)
    /competences/<id>/archiver         → archiver (admin/RH)
    /competences/<id>/reactiver        → réactiver (admin/RH)
    /employes/<id>/competences         → compétences d'un employé
    /employes/<id>/competences/ajouter → associer une compétence à un employé
    /employes/<id>/competences/<cid>/modifier  → modifier le niveau
    /employes/<id>/competences/<cid>/supprimer → retirer une compétence

Politique d'accès :
    * admin/RH : référentiel complet + tous les employés (portée globale).
    * manager   : référentiel en lecture seule + niveaux des employés de
                  son propre département en lecture/écriture.
    * technicien / employé : lecture seule de son propre profil.
"""
from flask import (
    Blueprint, abort, flash, redirect, render_template, request,
    session, url_for,
)

from services.competences import libelle_niveau, lignes_texte


def creer_blueprint_competences(deps):
    bp = Blueprint('competences', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    role_required = deps['role_required']
    get_department_scope = deps['get_department_scope']
    log_action = deps['log_action']
    create_notification = deps.get('create_notification')

    # ------------------------------------------------------------------ helpers

    def _role_courant():
        return session.get('role', 'employe')

    def _est_global():
        return _role_courant() in ('admin', 'rh')

    def _est_manager():
        return _role_courant() in ('admin', 'rh', 'manager')

    def _scope_departement(cur):
        """Renvoie le(s) nom(s) de département accessibles ou None = global."""
        scope = get_department_scope(cur)
        if scope['is_global']:
            return None
        if scope['is_empty']:
            return '__EMPTY__'
        return scope['department']

    def _employes_portee(cur):
        """Renvoie la liste des employés de la portée courante pour les
        formulaires d'association. Les managers ne voient que leur département.
        """
        scope = get_department_scope(cur)
        if scope['is_global']:
            cur.execute("""SELECT e.id, e.nom, e.prenom, e.poste, e.departement
                           FROM employes e WHERE e.actif IS NOT FALSE
                           ORDER BY e.departement, e.nom, e.prenom""")
        elif scope['is_empty']:
            return []
        else:
            cur.execute("""SELECT e.id, e.nom, e.prenom, e.poste, e.departement
                           FROM employes e
                           WHERE e.departement = %s AND e.actif IS NOT FALSE
                           ORDER BY e.nom, e.prenom""",
                        (scope['department'],))
        return cur.fetchall()

    def _charger_competence(cur, cid):
        cur.execute("SELECT * FROM competences WHERE id = %s", (cid,))
        row = cur.fetchone()
        if not row:
            abort(404)
        return row

    def _charger_employe(cur, eid):
        cur.execute("SELECT * FROM employes WHERE id = %s", (eid,))
        emp = cur.fetchone()
        if not emp:
            abort(404)
        return emp

    def _verifier_acces_employe(cur, emp):
        """Un utilisateur ne peut modifier les compétences d'un employé que
        si c'est admin/RH (global), manager du même département, ou l'employé
        lui-même (ce dernier en lecture seule : l'écriture est refusée).
        """
        scope = get_department_scope(cur)
        if scope['is_global']:
            return 'write'
        if scope.get('employee_id') == emp['id']:
            return 'read'
        if _role_courant() == 'manager' and emp['departement'] == scope['department']:
            return 'write'
        abort(403)

    def _competences_actives(cur):
        cur.execute("""SELECT * FROM competences
                       WHERE active IS TRUE
                       ORDER BY categorie NULLS LAST, nom""")
        return cur.fetchall()

    def _categories(cur):
        cur.execute("""SELECT DISTINCT categorie FROM competences
                       WHERE categorie IS NOT NULL AND categorie <> ''
                       ORDER BY categorie""")
        return [r['categorie'] for r in cur.fetchall()]

    def _parse_niveau(raw):
        try:
            n = int(raw)
        except (TypeError, ValueError):
            raise ValueError("Le niveau doit être un entier entre 0 et 100.")
        if not 0 <= n <= 100:
            raise ValueError("Le niveau doit être compris entre 0 et 100.")
        return n

    # ----------------------------------------------------------- liste catalogue

    @bp.route('/competences')
    @login_required
    def competences_liste():
        """Catalogue des compétences.

        Tous les utilisateurs connectés peuvent le consulter ; seul admin/RH
        voit les actions de gestion. Les managers ont accès au catalogue mais
        ne peuvent pas y toucher.
        """
        show_inactives = (request.args.get('inactifs') == '1' and _est_global())
        with db_cursor() as (conn, cur):
            if show_inactives:
                cur.execute("""SELECT c.*,
                               (SELECT COUNT(*) FROM employe_competences ec
                                 WHERE ec.competence_id = c.id) AS nb_employes
                               FROM competences c
                               ORDER BY c.active DESC,
                                        c.categorie NULLS LAST, c.nom""")
            else:
                cur.execute("""SELECT c.*,
                               (SELECT COUNT(*) FROM employe_competences ec
                                 WHERE ec.competence_id = c.id) AS nb_employes
                               FROM competences c
                               WHERE c.active IS TRUE
                               ORDER BY c.categorie NULLS LAST, c.nom""")
            competences = cur.fetchall()
        return render_template(
            'competences/competences_liste.html',
            competences=competences,
            peut_gerer=_est_global(),
            show_inactives=show_inactives,
            libelle_niveau=libelle_niveau,
        )

    @bp.route('/competences/nouvelle', methods=['GET', 'POST'])
    @login_required
    @role_required('admin', 'rh')
    def competence_ajouter():
        if request.method == 'POST':
            nom = (request.form.get('nom') or '').strip()
            description = (request.form.get('description') or '').strip() or None
            categorie = (request.form.get('categorie') or '').strip() or None
            if not nom:
                flash("Le nom de la compétence est obligatoire.", "danger")
            else:
                with db_cursor(commit=True) as (conn, cur):
                    try:
                        cur.execute("""INSERT INTO competences
                                       (nom, description, categorie)
                                       VALUES (%s, %s, %s) RETURNING id""",
                                    (nom, description, categorie))
                        cid = cur.fetchone()['id']
                    except Exception as exc:
                        conn.rollback()
                        # unique violation
                        flash(f"Impossible d'ajouter cette compétence : {exc}",
                              "danger")
                    else:
                        log_action(session.get('user_id'),
                                   session.get('username'),
                                   'COMPETENCE_CREER', 'competence', cid, nom)
                        flash(f"Compétence « {nom} » ajoutée.", "success")
                        return redirect(url_for('competences.competences_liste'))
        with db_cursor() as (conn, cur):
            cats = _categories(cur)
        return render_template('competences/competence_form.html',
                               competence=None, categories=cats)

    @bp.route('/competences/<int:cid>')
    @login_required
    def competence_detail(cid):
        with db_cursor() as (conn, cur):
            comp = _charger_competence(cur, cid)
            scope = get_department_scope(cur)
            if scope['is_global']:
                cur.execute("""SELECT ec.*, e.nom, e.prenom, e.poste,
                                      e.departement, u.username AS modifie_par
                               FROM employe_competences ec
                               JOIN employes e ON e.id = ec.employe_id
                               LEFT JOIN users u ON u.id = ec.modifie_par
                               WHERE ec.competence_id = %s
                               ORDER BY e.departement, e.nom, e.prenom""",
                            (cid,))
            elif scope['is_empty']:
                cur.execute("""SELECT ec.*, e.nom, e.prenom, e.poste,
                                      e.departement, u.username AS modifie_par
                               FROM employe_competences ec
                               JOIN employes e ON e.id = ec.employe_id
                               LEFT JOIN users u ON u.id = ec.modifie_par
                               WHERE 1 = 0""")
            else:
                cur.execute("""SELECT ec.*, e.nom, e.prenom, e.poste,
                                      e.departement, u.username AS modifie_par
                               FROM employe_competences ec
                               JOIN employes e ON e.id = ec.employe_id
                               LEFT JOIN users u ON u.id = ec.modifie_par
                               WHERE ec.competence_id = %s
                                 AND e.departement = %s
                               ORDER BY e.nom, e.prenom""",
                            (cid, scope['department']))
            associations = cur.fetchall()
        return render_template('competences/competence_detail.html',
                               comp=comp,
                               associations=associations,
                               libelle_niveau=libelle_niveau,
                               peut_gerer=_est_global(),
                               is_global=scope['is_global'])

    @bp.route('/competences/<int:cid>/modifier', methods=['GET', 'POST'])
    @login_required
    @role_required('admin', 'rh')
    def competence_modifier(cid):
        with db_cursor(commit=True) as (conn, cur):
            comp = _charger_competence(cur, cid)
            if request.method == 'POST':
                nom = (request.form.get('nom') or '').strip()
                description = (request.form.get('description') or '').strip() or None
                categorie = (request.form.get('categorie') or '').strip() or None
                if not nom:
                    flash("Le nom est obligatoire.", "danger")
                else:
                    try:
                        cur.execute("""UPDATE competences
                                       SET nom=%s, description=%s, categorie=%s,
                                           date_modification=CURRENT_TIMESTAMP
                                       WHERE id=%s""",
                                    (nom, description, categorie, cid))
                    except Exception as exc:
                        conn.rollback()
                        flash(f"Erreur : {exc}", "danger")
                    else:
                        log_action(session.get('user_id'),
                                   session.get('username'),
                                   'COMPETENCE_MODIFIER', 'competence', cid,
                                   nom)
                        flash("Compétence mise à jour.", "success")
                        return redirect(url_for('competences.competence_detail',
                                                cid=cid))
                    comp = _charger_competence(cur, cid)
            cats = _categories(cur)
        return render_template('competences/competence_form.html',
                               competence=comp, categories=cats)

    @bp.route('/competences/<int:cid>/archiver', methods=['POST'])
    @login_required
    @role_required('admin', 'rh')
    def competence_archiver(cid):
        with db_cursor(commit=True) as (conn, cur):
            comp = _charger_competence(cur, cid)
            cur.execute("""UPDATE competences SET active=FALSE,
                           date_modification=CURRENT_TIMESTAMP WHERE id=%s""",
                        (cid,))
            log_action(session.get('user_id'), session.get('username'),
                       'COMPETENCE_ARCHIVER', 'competence', cid,
                       comp['nom'])
        flash(f"Compétence « {comp['nom']} » archivée.", "info")
        return redirect(url_for('competences.competences_liste'))

    @bp.route('/competences/<int:cid>/reactiver', methods=['POST'])
    @login_required
    @role_required('admin', 'rh')
    def competence_reactiver(cid):
        with db_cursor(commit=True) as (conn, cur):
            comp = _charger_competence(cur, cid)
            cur.execute("""UPDATE competences SET active=TRUE,
                           date_modification=CURRENT_TIMESTAMP WHERE id=%s""",
                        (cid,))
            log_action(session.get('user_id'), session.get('username'),
                       'COMPETENCE_REACTIVER', 'competence', cid,
                       comp['nom'])
        flash(f"Compétence « {comp['nom']} » réactivée.", "success")
        return redirect(url_for('competences.competence_detail', cid=cid))

    # ---------------------------------------------- compétences d'un employé

    @bp.route('/employes/<int:eid>/competences')
    @login_required
    def employe_competences(eid):
        with db_cursor() as (conn, cur):
            emp = _charger_employe(cur, eid)
            acces = _verifier_acces_employe(cur, emp)
            scope = get_department_scope(cur)
            cur.execute("""SELECT ec.*, c.nom AS comp_nom, c.categorie,
                                  u.username AS modifie_par
                           FROM employe_competences ec
                           JOIN competences c ON c.id = ec.competence_id
                           LEFT JOIN users u ON u.id = ec.modifie_par
                           WHERE ec.employe_id = %s
                           ORDER BY c.categorie NULLS LAST, c.nom""",
                        (eid,))
            competences_employe = cur.fetchall()

            # Compétences disponibles pour ajouter (celles non déjà associées
            # et actives). Si portée globale on montre toutes les catégories,
            # sinon rien à ajouter sauf si le manager a le droit.
            if acces == 'write':
                cur.execute("""SELECT id, nom, categorie FROM competences
                               WHERE active IS TRUE
                                 AND id NOT IN (
                                   SELECT competence_id FROM employe_competences
                                    WHERE employe_id = %s
                                 )
                               ORDER BY categorie NULLS LAST, nom""", (eid,))
                disponibles = cur.fetchall()
            else:
                disponibles = []

        return render_template(
            'competences/employe_competences.html',
            emp=emp,
            competences=competences_employe,
            disponibles=disponibles,
            acces=acces,
            peut_ecrire=(acces == 'write'),
            libelle_niveau=libelle_niveau,
            lignes_texte=lignes_texte,
            is_self=(scope.get('employee_id') == eid),
        )

    @bp.route('/employes/<int:eid>/competences/ajouter', methods=['POST'])
    @login_required
    def employe_competence_ajouter(eid):
        with db_cursor(commit=True) as (conn, cur):
            emp = _charger_employe(cur, eid)
            if _verifier_acces_employe(cur, emp) != 'write':
                abort(403)
            competence_id = request.form.get('competence_id', type=int)
            if not competence_id:
                flash("Choisissez une compétence dans la liste.", "danger")
                return redirect(url_for('competences.employe_competences',
                                        eid=eid))
            try:
                niveau = _parse_niveau(request.form.get('niveau'))
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for('competences.employe_competences',
                                        eid=eid))
            notes = (request.form.get('notes') or '').strip() or None

            cur.execute("SELECT * FROM competences WHERE id=%s AND active IS TRUE",
                        (competence_id,))
            comp = cur.fetchone()
            if not comp:
                flash("Compétence invalide ou archivée.", "danger")
                return redirect(url_for('competences.employe_competences',
                                        eid=eid))
            try:
                cur.execute("""INSERT INTO employe_competences
                               (employe_id, competence_id, niveau, notes,
                                ajoute_par, modifie_par)
                               VALUES (%s, %s, %s, %s, %s, %s)""",
                            (eid, competence_id, niveau, notes,
                             session.get('user_id'), session.get('user_id')))
            except Exception as exc:
                conn.rollback()
                flash(f"Impossible d'associer la compétence : {exc}", "danger")
            else:
                log_action(session.get('user_id'), session.get('username'),
                           'EMPLOYE_COMPETENCE_AJOUTER', 'employe', eid,
                           f"{comp['nom']}={niveau}")
                flash(f"Compétence « {comp['nom']} » ajoutée.", "success")
                if create_notification:
                    uid = _user_id_employe(cur, eid)
                    if uid and uid != session.get('user_id'):
                        create_notification(
                            uid,
                            "Vos compétences ont été mises à jour",
                            f"Le niveau « {comp['nom']} » a été ajouté à "
                            f"votre profil ({niveau}/100).",
                            'info', cur=cur)
        return redirect(url_for('competences.employe_competences', eid=eid))

    @bp.route('/employes/<int:eid>/competences/<int:ecid>/modifier',
              methods=['POST'])
    @login_required
    def employe_competence_modifier(eid, ecid):
        with db_cursor(commit=True) as (conn, cur):
            emp = _charger_employe(cur, eid)
            if _verifier_acces_employe(cur, emp) != 'write':
                abort(403)
            cur.execute("""SELECT ec.*, c.nom AS comp_nom
                           FROM employe_competences ec
                           JOIN competences c ON c.id = ec.competence_id
                           WHERE ec.id = %s AND ec.employe_id = %s""",
                        (ecid, eid))
            assoc = cur.fetchone()
            if not assoc:
                abort(404)
            try:
                niveau = _parse_niveau(request.form.get('niveau'))
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for('competences.employe_competences',
                                        eid=eid))
            notes = (request.form.get('notes') or '').strip() or None
            cur.execute("""UPDATE employe_competences
                           SET niveau=%s, notes=%s, modifie_par=%s,
                               date_modification=CURRENT_TIMESTAMP
                           WHERE id=%s""",
                        (niveau, notes, session.get('user_id'), ecid))
            log_action(session.get('user_id'), session.get('username'),
                       'EMPLOYE_COMPETENCE_MODIFIER', 'employe', eid,
                       f"{assoc['comp_nom']}={niveau}")
            flash("Niveau mis à jour.", "success")
            uid = _user_id_employe(cur, eid)
            if create_notification and uid and uid != session.get('user_id'):
                create_notification(
                    uid,
                    "Vos compétences ont été mises à jour",
                    f"Le niveau de « {assoc['comp_nom']} » a été "
                    f"modifié ({niveau}/100).",
                    'info', cur=cur)
        return redirect(url_for('competences.employe_competences', eid=eid))

    @bp.route('/employes/<int:eid>/competences/<int:ecid>/supprimer',
              methods=['POST'])
    @login_required
    def employe_competence_supprimer(eid, ecid):
        with db_cursor(commit=True) as (conn, cur):
            emp = _charger_employe(cur, eid)
            if _verifier_acces_employe(cur, emp) != 'write':
                abort(403)
            cur.execute("""SELECT ec.*, c.nom AS comp_nom
                           FROM employe_competences ec
                           JOIN competences c ON c.id = ec.competence_id
                           WHERE ec.id = %s AND ec.employe_id = %s""",
                        (ecid, eid))
            assoc = cur.fetchone()
            if not assoc:
                abort(404)
            cur.execute("DELETE FROM employe_competences WHERE id=%s", (ecid,))
            log_action(session.get('user_id'), session.get('username'),
                       'EMPLOYE_COMPETENCE_SUPPRIMER', 'employe', eid,
                       assoc['comp_nom'])
            flash(f"Compétence « {assoc['comp_nom']} » retirée.", "info")
        return redirect(url_for('competences.employe_competences', eid=eid))

    # ----------------------------------------------------------- helpers privés

    def _user_id_employe(cur, employe_id):
        cur.execute("SELECT id FROM users WHERE employe_id = %s LIMIT 1",
                    (employe_id,))
        row = cur.fetchone()
        return row['id'] if row else None

    def scope_has_user_id(cur, employe_id):
        return _user_id_employe(cur, employe_id) is not None

    # On expose les helpers et le label pour les templates hors blueprint
    # (ex. lien depuis la fiche employé).
    bp.emp_competences_url = lambda eid: url_for(  # noqa: E731
        'competences.employe_competences', eid=eid)

    return bp
