"""Blueprint « Objectifs » : gestion des objectifs individuels.

URL publiques :
    /objectifs                              liste (périmètre de l'utilisateur)
    /objectifs/nouveau                      créer un objectif
    /objectifs/<id>                         consulter / modifier le détail
    /objectifs/<id>/modifier                modifier titre / dates / description
    /objectifs/<id>/progression             ajouter un point de situation
    /objectifs/<id>/soumettre               employé soumet comme atteint
    /objectifs/<id>/valider                 manager valide comme atteint
    /objectifs/<id>/non-atteint             manager clôt comme non atteint
    /objectifs/<id>/annuler                 annule (employé, manager ou RH)
    /objectifs/<id>/reactiver               rouvrir un objectif (manager/RH)

Politique d'accès :
    * admin/RH           : tous les objectifs, toutes actions sauf déclarer
                          progression à la place de l'employé ;
    * manager            : CRUD complet sur les objectifs de ses subordonnés,
                          peut initier un objectif pour eux, valider/refuser ;
    * employé/technicien : crée ses propres objectifs (brouillon), met à jour
                          sa progression, peut soumettre comme atteint ;
                          ne voit que ses objectifs.
"""
from datetime import date, datetime

from flask import (
    Blueprint, abort, flash, redirect, render_template, request,
    session, url_for,
)

from services.objectifs import (
    CATEGORIES_DEFAUT, PRIORITES, PRIORITE_LABELS, STATUT_ANNULE,
    STATUT_ATTEINT, STATUT_BADGES, STATUT_BROUILLON, STATUT_EN_COURS,
    STATUT_LABELS, STATUT_NON_ATTEINT, progression_couleur, statut_final,
)


def _parse_date(v, champ):
    if not v:
        return None
    try:
        return datetime.strptime(v, '%Y-%m-%d').date()
    except ValueError:
        raise ValueError(f"Format de date invalide pour {champ}.")


def _parse_int(v, mini, maxi, defaut=None):
    if v in (None, ''):
        return defaut
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError("Valeur numérique invalide.")
    if not mini <= n <= maxi:
        raise ValueError(f"La valeur doit être comprise entre {mini} et {maxi}.")
    return n


def creer_blueprint_objectifs(deps):
    bp = Blueprint('objectifs', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    get_department_scope = deps['get_department_scope']
    create_notification = deps.get('create_notification')
    log_action = deps['log_action']
    pagination_info = deps.get('pagination_info')
    page_list = deps.get('page_list')

    def _role_courant():
        return session.get('role', 'employe')

    def _est_global():
        return _role_courant() in ('admin', 'rh')

    def _est_manager():
        return _role_courant() in ('admin', 'rh', 'manager')

    def _scope_employes_ids(cur):
        """Renvoie la liste des ids employés dans le périmètre de l'utilisateur.

        admin/RH  -> tous les employés ;
        manager   -> employés du même département ;
        employé   -> lui-même uniquement.
        """
        scope = get_department_scope(cur)
        uid = session.get('user_id')
        if scope['is_global']:
            cur.execute("SELECT id FROM employes WHERE actif IS NOT FALSE ORDER BY nom, prenom")
            return [r['id'] for r in cur.fetchall()]
        if scope['is_empty']:
            # Sans département : on voit uniquement ses propres objectifs si
            # l'utilisateur est rattaché à un employé.
            cur.execute("SELECT employe_id FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
            return [row['employe_id']] if row and row['employe_id'] else []
        if _role_courant() == 'manager':
            cur.execute("SELECT id FROM employes WHERE departement=%s AND actif IS NOT FALSE ORDER BY nom, prenom",
                        (scope['department'],))
            ids = [r['id'] for r in cur.fetchall()]
            # Inclure aussi le manager lui-même s'il a une fiche employé
            cur.execute("SELECT employe_id FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
            if row and row['employe_id'] and row['employe_id'] not in ids:
                ids.append(row['employe_id'])
            return ids
        # Employé/technicien : lui-même
        cur.execute("SELECT employe_id FROM users WHERE id=%s", (uid,))
        row = cur.fetchone()
        return [row['employe_id']] if row and row['employe_id'] else []

    def _charger_objectif(cur, oid):
        cur.execute("""SELECT o.*,
                          e.nom AS emp_nom, e.prenom AS emp_prenom,
                          e.poste AS emp_poste, e.departement AS emp_dept,
                          c.nom AS comp_nom,
                          uc.username AS cree_par_nom,
                          uv.username AS valide_par_nom,
                          ucl.username AS cloture_par_nom
                       FROM objectifs o
                       JOIN employes e ON e.id = o.employe_id
                       LEFT JOIN competences c ON c.id = o.competence_id
                       LEFT JOIN users uc ON uc.id = o.cree_par
                       LEFT JOIN users uv ON uv.id = o.valide_par
                       LEFT JOIN users ucl ON ucl.id = o.cloture_par
                       WHERE o.id = %s""", (oid,))
        obj = cur.fetchone()
        if not obj:
            abort(404)
        return obj

    def _verifier_lecture(cur, obj):
        scope = get_department_scope(cur)
        if scope['is_global']:
            return True
        uid = session.get('user_id')
        cur.execute("SELECT employe_id FROM users WHERE id=%s", (uid,))
        row = cur.fetchone()
        mon_emp = row['employe_id'] if row else None
        if obj['employe_id'] == mon_emp:
            return True
        if _role_courant() == 'manager' and not scope['is_empty'] \
                and obj['emp_dept'] == scope['department']:
            return True
        abort(403)

    def _verifier_ecriture(cur, obj, action='modifier'):
        """Vérifie les droits d'écriture selon le statut et le rôle.

        Renvoie True si autorisé, False si l'utilisateur n'a aucun droit (403),
        et lève un ValueError avec message flash si c'est une erreur fonctionnelle.
        """
        scope = get_department_scope(cur)
        uid = session.get('user_id')
        cur.execute("SELECT employe_id, role FROM users WHERE id=%s", (uid,))
        me = cur.fetchone() or {}
        mon_emp = me.get('employe_id')
        mon_role = me.get('role', 'employe')
        est_proprietaire = (mon_emp == obj['employe_id'])
        est_manage_dept = (
            mon_role == 'manager' and not scope['is_empty']
            and obj['emp_dept'] == scope['department']
        )
        est_admin = scope['is_global']
        st = obj['statut']

        # Actions qui ne sont autorisées que sur un objectif non finalisé
        if action != 'reactiver' and statut_final(st):
            raise ValueError("Cet objectif est clôturé, il ne peut plus être modifié.")

        if action == 'modifier':
            if est_admin or est_manage_dept:
                return True
            if est_proprietaire:
                return True
            return False

        if action == 'progression':
            if est_proprietaire:
                return True
            if est_admin or est_manage_dept:
                return True
            return False

        if action == 'soumettre':
            return bool(est_proprietaire and st == STATUT_EN_COURS)

        if action in ('valider', 'non_atteint'):
            return bool(est_admin or est_manage_dept)

        if action == 'annuler':
            if est_admin:
                return True
            if est_proprietaire:
                return True
            if est_manage_dept:
                return True
            return False

        if action == 'reactiver':
            if not statut_final(st):
                raise ValueError("Cet objectif n'est pas clôturé.")
            return bool(est_admin or est_manage_dept)

        return False

    def _notifier(cur, user_id, titre, message, type_='info'):
        if create_notification and user_id:
            try:
                create_notification(user_id, titre, message, type_, cur=cur)
            except Exception:
                pass

    def _notifier_manager_departement(cur, dept, titre, message, sauf=None, type_='info'):
        if not dept:
            return
        cur.execute("SELECT id FROM users WHERE role IN ('manager','admin','rh') "
                    "AND employe_id IN (SELECT id FROM employes WHERE departement=%s)",
                    (dept,))
        for row in cur.fetchall():
            if sauf and row['id'] == sauf:
                continue
            _notifier(cur, row['id'], titre, message, type_)
        # Et systématiquement les RH globaux
        cur.execute("SELECT id FROM users WHERE role IN ('admin','rh')")
        for row in cur.fetchall():
            if sauf and row['id'] == sauf:
                continue
            _notifier(cur, row['id'], titre, message, type_)

    def _user_id_employe(cur, emp_id):
        cur.execute("SELECT id FROM users WHERE employe_id=%s ORDER BY id LIMIT 1", (emp_id,))
        row = cur.fetchone()
        return row['id'] if row else None

    def _require(cur, obj, action):
        """Vérifie les droits ; abort(403) si refus, flash+redirect si état incohérent."""
        from flask import redirect as _redir
        try:
            if not _verifier_ecriture(cur, obj, action):
                abort(403)
        except ValueError as exc:
            flash(str(exc), "warning")
            return _redir(url_for('objectifs.objectif_detail', oid=obj['id']))
        return None

    # --------------------------------------------------------------- list / detail

    @bp.route('/objectifs')
    @login_required
    def objectifs_liste():
        filtre_statut = (request.args.get('statut') or '').strip()
        filtre_employe = request.args.get('employe_id', type=int)
        filtre_miens = request.args.get('miens') == '1'
        page = max(1, request.args.get('page', 1, type=int))
        per_page = 20

        with db_cursor() as (conn, cur):
            ids = _scope_employes_ids(cur)
            if filtre_miens:
                cur.execute("SELECT employe_id FROM users WHERE id=%s", (session.get('user_id'),))
                m = cur.fetchone()
                if m and m['employe_id']:
                    ids = [i for i in ids if i == m['employe_id']]
            if filtre_employe and filtre_employe in ids:
                ids = [filtre_employe]
            if not ids:
                return render_template('objectifs/objectifs_liste.html',
                                       objectifs=[], comptes={},
                                       employes=[],
                                       filtres={'statut': filtre_statut,
                                                'employe_id': filtre_employe,
                                                'miens': filtre_miens},
                                       pg=None, page_items=[], base_qs='',
                                       labels=STATUT_LABELS, badges=STATUT_BADGES,
                                       priorites=PRIORITE_LABELS,
                                       progression_couleur=progression_couleur,
                                       peut_creer=True)

            placeholders = ','.join(['%s'] * len(ids))
            where = [f"o.employe_id IN ({placeholders})"]
            params = list(ids)
            if filtre_statut:
                where.append("o.statut=%s")
                params.append(filtre_statut)

            where_sql = " AND ".join(where)
            cur.execute(f"SELECT COUNT(*) AS nb FROM objectifs o WHERE {where_sql}", params)
            total = cur.fetchone()['nb']
            pg = pagination_info(total, page, per_page) if pagination_info else None
            offset = (page - 1) * per_page
            cur.execute(
                f"""SELECT o.*, e.nom AS emp_nom, e.prenom AS emp_prenom,
                           c.nom AS comp_nom
                    FROM objectifs o
                    JOIN employes e ON e.id=o.employe_id
                    LEFT JOIN competences c ON c.id=o.competence_id
                    WHERE {where_sql}
                    ORDER BY
                      CASE o.statut
                        WHEN 'en_cours' THEN 1
                        WHEN 'brouillon' THEN 2
                        WHEN 'atteint' THEN 3
                        ELSE 4 END,
                      o.date_echeance NULLS LAST, o.id DESC
                    LIMIT %s OFFSET %s""",
                params + [per_page, offset],
            )
            objectifs = cur.fetchall()
            # Employés du périmètre pour le filtre
            cur.execute(f"SELECT id, nom, prenom FROM employes WHERE id IN ({placeholders}) "
                        "ORDER BY nom, prenom", ids)
            employes = cur.fetchall()

        # Petit résumé par statut pour l'interface
        comptes = {'total': 0, 'brouillon': 0, 'en_cours': 0,
                   'atteint': 0, 'non_atteint': 0, 'annule': 0,
                   'en_retard': 0}
        for o in objectifs:
            comptes['total'] += 1
            if o['statut'] in comptes:
                comptes[o['statut']] += 1
            if (o['statut'] == STATUT_EN_COURS and o['date_echeance']
                    and o['date_echeance'] < date.today()):
                comptes['en_retard'] += 1
        # Totaux tout statut
        with db_cursor() as (conn, cur):
            cur.execute(
                f"SELECT statut, COUNT(*) AS nb FROM objectifs o "
                f"WHERE employe_id IN ({placeholders}) GROUP BY statut",
                ids,
            )
            for row in cur.fetchall():
                comptes[row['statut']] = row['nb']
            cur.execute(
                f"SELECT COUNT(*) AS nb FROM objectifs o WHERE employe_id IN "
                f"({placeholders}) AND statut='en_cours' AND date_echeance IS NOT NULL "
                f"AND date_echeance < CURRENT_DATE",
                ids,
            )
            comptes['en_retard'] = cur.fetchone()['nb']

        from urllib.parse import urlencode
        base_qs = urlencode({k: v for k, v in
                             {'statut': filtre_statut,
                              'employe_id': filtre_employe,
                              'miens': '1' if filtre_miens else ''}.items()
                             if v and v != ''})

        return render_template('objectifs/objectifs_liste.html',
                               objectifs=objectifs, comptes=comptes,
                               employes=employes,
                               filtres={'statut': filtre_statut,
                                        'employe_id': filtre_employe,
                                        'miens': filtre_miens},
                               pg=pg, page_items=(page_list(pg['page'], pg['pages'])
                                                  if pg and page_list else []),
                               base_qs=base_qs,
                               labels=STATUT_LABELS, badges=STATUT_BADGES,
                               priorites=PRIORITE_LABELS,
                               progression_couleur=progression_couleur,
                               peut_creer=True,
                               today=date.today())

    @bp.route('/objectifs/nouveau', methods=['GET', 'POST'])
    @login_required
    def objectif_nouveau():
        with db_cursor(commit=True) as (conn, cur):
            ids = _scope_employes_ids(cur)
            if not ids:
                flash("Aucun collaborateur dans votre périmètre.", "warning")
                return redirect(url_for('objectifs.objectifs_liste'))

            if request.method == 'POST':
                try:
                    # L'employé ne peut créer que pour lui-même
                    role = _role_courant()
                    if role in ('employe', 'technicien'):
                        cur.execute("SELECT employe_id FROM users WHERE id=%s",
                                    (session['user_id'],))
                        m = cur.fetchone()
                        if not m or not m['employe_id']:
                            raise ValueError("Votre compte n'est pas rattaché à un employé.")
                        employe_id = m['employe_id']
                    else:
                        employe_id = request.form.get('employe_id', type=int)
                        if employe_id not in ids:
                            raise ValueError("Collaborateur hors périmètre.")
                    titre = (request.form.get('titre') or '').strip()
                    if not titre:
                        raise ValueError("Le titre est obligatoire.")
                    if len(titre) > 200:
                        raise ValueError("Le titre est trop long (max 200 caractères).")
                    description = (request.form.get('description') or '').strip() or None
                    categorie = (request.form.get('categorie') or '').strip() or None
                    priorite = (request.form.get('priorite') or 'normale').strip()
                    if priorite not in PRIORITES:
                        priorite = 'normale'
                    date_debut = _parse_date(request.form.get('date_debut'), 'date de début')
                    date_echeance = _parse_date(request.form.get('date_echeance'), 'date d\'échéance')
                    if date_echeance and date_debut and date_echeance < date_debut:
                        raise ValueError("La date d'échéance ne peut être avant la date de début.")
                    competence_id = request.form.get('competence_id', type=int) or None
                    if competence_id:
                        cur.execute("SELECT id FROM competences WHERE id=%s AND active IS TRUE",
                                    (competence_id,))
                        if not cur.fetchone():
                            competence_id = None

                    # Un manager peut valider d'emblée (objectif créé en_cours)
                    start_status = STATUT_BROUILLON
                    if role in ('admin', 'rh', 'manager') and request.form.get('valider_immediat') == '1':
                        start_status = STATUT_EN_COURS

                    cur.execute(
                        """INSERT INTO objectifs
                           (employe_id, titre, description, categorie, priorite,
                            statut, progression, date_debut, date_echeance,
                            cree_par, cree_par_role, competence_id)
                           VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s)
                           RETURNING id""",
                        (employe_id, titre, description, categorie, priorite,
                         start_status, date_debut, date_echeance,
                         session.get('user_id'), role, competence_id),
                    )
                    oid = cur.fetchone()['id']
                    log_action(session.get('user_id'), session.get('username'),
                               'OBJECTIF_CREER', 'objectif', oid, titre)

                    # Si créé par le manager directement en en_cours, notifier l'employé
                    if start_status == STATUT_EN_COURS:
                        uid_emp = _user_id_employe(cur, employe_id)
                        if uid_emp and uid_emp != session.get('user_id'):
                            _notifier(cur, uid_emp, "🎯 Nouvel objectif assigné",
                                      f"Un nouvel objectif « {titre} » vous a été assigné.",
                                      'info')
                    else:
                        # Si créé par l'employé, prévenir le manager
                        cur.execute("SELECT departement FROM employes WHERE id=%s", (employe_id,))
                        dept_row = cur.fetchone()
                        if dept_row and dept_row['departement']:
                            _notifier_manager_departement(
                                cur, dept_row['departement'],
                                "📝 Nouveau brouillon d'objectif",
                                f"Un objectif « {titre} » a été créé comme brouillon "
                                f"et attend votre validation.",
                                sauf=session.get('user_id'),
                            )
                    flash("Objectif créé.", "success")
                    return redirect(url_for('objectifs.objectif_detail', oid=oid))
                except ValueError as exc:
                    flash(str(exc), "danger")

            # Charger les listes
            placeholders = ','.join(['%s'] * len(ids))
            cur.execute(f"SELECT id, nom, prenom FROM employes WHERE id IN ({placeholders}) "
                        "ORDER BY nom, prenom", ids)
            employes = cur.fetchall()
            cur.execute("SELECT id, nom, categorie FROM competences WHERE active IS TRUE "
                        "ORDER BY categorie NULLS LAST, nom")
            competences = cur.fetchall()
            return render_template('objectifs/objectif_form.html',
                                   objectif=None, employes=employes,
                                   competences=competences,
                                   categories=CATEGORIES_DEFAUT,
                                   priorites=PRIORITES,
                                   priorite_labels=PRIORITE_LABELS,
                                   is_editing=False)

    @bp.route('/objectifs/<int:oid>')
    @login_required
    def objectif_detail(oid):
        with db_cursor() as (conn, cur):
            obj = _charger_objectif(cur, oid)
            _verifier_lecture(cur, obj)
            cur.execute("""SELECT p.*, u.username AS auteur_nom
                           FROM objectifs_points p
                           LEFT JOIN users u ON u.id=p.auteur_id
                           WHERE p.objectif_id=%s
                           ORDER BY p.date_creation ASC""", (oid,))
            points = cur.fetchall()
            cur.execute("SELECT id, nom, categorie FROM competences WHERE active IS TRUE "
                        "ORDER BY categorie NULLS LAST, nom")
            competences = cur.fetchall()
            uid = session.get('user_id')
            cur.execute("SELECT employe_id FROM users WHERE id=%s", (uid,))
            droits = {}
            for action in ('modifier', 'progression', 'soumettre',
                           'valider', 'non_atteint', 'annuler', 'reactiver'):
                try:
                    droits[action] = bool(_verifier_ecriture(cur, obj, action))
                except ValueError:
                    droits[action] = False

        return render_template('objectifs/objectif_detail.html',
                               o=obj, points=points,
                               competences=competences,
                               categories=CATEGORIES_DEFAUT,
                               priorites=PRIORITES,
                               priorite_labels=PRIORITE_LABELS,
                               labels=STATUT_LABELS, badges=STATUT_BADGES,
                               progression_couleur=progression_couleur,
                               statut_final=statut_final,
                               droits=droits,
                               today=date.today())

    # ---- modifier les champs statiques

    @bp.route('/objectifs/<int:oid>/modifier', methods=['GET', 'POST'])
    @login_required
    def objectif_modifier(oid):
        with db_cursor(commit=True) as (conn, cur):
            obj = _charger_objectif(cur, oid)
            redir = _require(cur, obj, 'modifier')
            if redir is not None: return redir

            if request.method == 'GET':
                # Charger les listes pour le formulaire d'édition (popup ou page)
                cur.execute("SELECT id, nom, categorie FROM competences WHERE active IS TRUE "
                            "ORDER BY categorie NULLS LAST, nom")
                competences = cur.fetchall()
                return render_template('objectifs/objectif_form.html',
                                       objectif=obj, employes=[],
                                       competences=competences,
                                       categories=CATEGORIES_DEFAUT,
                                       priorites=PRIORITES,
                                       priorite_labels=PRIORITE_LABELS,
                                       is_editing=True)

            try:
                titre = (request.form.get('titre') or '').strip()
                if not titre:
                    raise ValueError("Le titre est obligatoire.")
                description = (request.form.get('description') or '').strip() or None
                categorie = (request.form.get('categorie') or '').strip() or None
                priorite = (request.form.get('priorite') or 'normale').strip()
                if priorite not in PRIORITES:
                    priorite = 'normale'
                date_debut = _parse_date(request.form.get('date_debut'), 'date de début')
                date_echeance = _parse_date(request.form.get('date_echeance'),
                                            'date d\'échéance')
                if date_echeance and date_debut and date_echeance < date_debut:
                    raise ValueError("La date d'échéance ne peut être avant la date de début.")
                competence_id = request.form.get('competence_id', type=int) or None
                if competence_id:
                    cur.execute("SELECT id FROM competences WHERE id=%s AND active IS TRUE",
                                (competence_id,))
                    if not cur.fetchone():
                        competence_id = None
                cur.execute("""UPDATE objectifs SET titre=%s, description=%s, categorie=%s,
                               priorite=%s, date_debut=%s, date_echeance=%s,
                               competence_id=%s, date_modification=CURRENT_TIMESTAMP
                               WHERE id=%s""",
                            (titre, description, categorie, priorite,
                             date_debut, date_echeance, competence_id, oid))
                log_action(session.get('user_id'), session.get('username'),
                           'OBJECTIF_MODIFIER', 'objectif', oid, titre)
                flash("Objectif mis à jour.", "success")
                return redirect(url_for('objectifs.objectif_detail', oid=oid))
            except ValueError as exc:
                flash(str(exc), "danger")

        # En cas d'erreur de validation, on ré-affiche le formulaire (dans la
        # popup si le POST venait de ?modal=1) plutôt que de rediriger.
        with db_cursor(commit=True) as (conn, cur):
            obj = _charger_objectif(cur, oid)
            cur.execute("SELECT id, nom, categorie FROM competences WHERE active IS TRUE "
                        "ORDER BY categorie NULLS LAST, nom")
            competences = cur.fetchall()
        return render_template('objectifs/objectif_form.html',
                               objectif=obj, employes=[],
                               competences=competences,
                               categories=CATEGORIES_DEFAUT,
                               priorites=PRIORITES,
                               priorite_labels=PRIORITE_LABELS,
                               is_editing=True)

    # ---- soumettre / valider / non atteint / annuler / réactiver

    @bp.route('/objectifs/<int:oid>/progression', methods=['POST'])
    @login_required
    def objectif_progression(oid):
        with db_cursor(commit=True) as (conn, cur):
            obj = _charger_objectif(cur, oid)
            redir = _require(cur, obj, 'progression')
            if redir is not None: return redir
            try:
                progression = _parse_int(request.form.get('progression'), 0, 100)
                if progression is None:
                    raise ValueError("La progression est obligatoire.")
                commentaire = (request.form.get('commentaire') or '').strip() or None
                # Si le brouillon reçoit une progression >= 0, on passe en_cours
                # uniquement si l'auteur est manager/RH (l'employé reste en brouillon
                # tant qu'il n'a pas explicitement « démarré »).
                new_status = obj['statut']
                if obj['statut'] == STATUT_BROUILLON and _est_manager():
                    new_status = STATUT_EN_COURS
                if progression >= 100 and obj['statut'] == STATUT_EN_COURS:
                    # L'employé doit explicitement soumettre, donc on reste à 100%
                    # mais pas automatiquement en statut atteint.
                    progression = 100
                cur.execute("""UPDATE objectifs SET progression=%s, statut=%s,
                               date_modification=CURRENT_TIMESTAMP WHERE id=%s""",
                            (progression, new_status, oid))
                cur.execute("""INSERT INTO objectifs_points
                               (objectif_id, auteur_id, progression, commentaire)
                               VALUES (%s,%s,%s,%s)""",
                            (oid, session.get('user_id'), progression, commentaire))
                log_action(session.get('user_id'), session.get('username'),
                           'OBJECTIF_PROGRESSION', 'objectif', oid,
                           f"{progression}%")
                # Notifications
                uid_emp = _user_id_employe(cur, obj['employe_id'])
                me = session.get('user_id')
                if new_status != obj['statut'] and new_status == STATUT_EN_COURS:
                    if uid_emp and uid_emp != me:
                        _notifier(cur, uid_emp, "🎯 Objectif démarré",
                                  f"Votre objectif « {obj['titre']} » est maintenant en cours.",
                                  'success')
                flash("Progression mise à jour.", "success")
            except ValueError as exc:
                flash(str(exc), "danger")
        return redirect(url_for('objectifs.objectif_detail', oid=oid))

    @bp.route('/objectifs/<int:oid>/demarrer', methods=['POST'])
    @login_required
    def objectif_demarrer(oid):
        """L'employé démarre un objectif en brouillon (le passe en_cours)."""
        with db_cursor(commit=True) as (conn, cur):
            obj = _charger_objectif(cur, oid)
            uid = session.get('user_id')
            cur.execute("SELECT employe_id, role FROM users WHERE id=%s", (uid,))
            me = cur.fetchone() or {}
            est_proprio = me.get('employe_id') == obj['employe_id']
            est_mgr_or_rh = _est_manager() or get_department_scope(cur)['is_global']
            if not (est_proprio or est_mgr_or_rh):
                abort(403)
            if obj['statut'] != STATUT_BROUILLON:
                flash("Cet objectif n'est pas en brouillon.", "warning")
                return redirect(url_for('objectifs.objectif_detail', oid=oid))
            cur.execute("""UPDATE objectifs SET statut=%s, date_debut=COALESCE(date_debut, CURRENT_DATE),
                           valide_par=%s, valide_le=CURRENT_DATE,
                           date_modification=CURRENT_TIMESTAMP WHERE id=%s""",
                        (STATUT_EN_COURS, uid if est_mgr_or_rh else None, oid))
            log_action(uid, session.get('username'),
                       'OBJECTIF_DEMARRER', 'objectif', oid, '')
            # Notifier le manager qu'un brouillon est parti
            if est_proprio:
                _notifier_manager_departement(
                    cur, obj['emp_dept'],
                    "🎯 Objectif démarré par l'employé",
                    f"« {obj['titre']} » est passé en cours.",
                    sauf=uid)
            else:
                uid_emp = _user_id_employe(cur, obj['employe_id'])
                _notifier(cur, uid_emp, "🎯 Objectif validé",
                          f"Votre objectif « {obj['titre']} » est maintenant en cours.",
                          'success')
            flash("Objectif démarré.", "success")
        return redirect(url_for('objectifs.objectif_detail', oid=oid))

    @bp.route('/objectifs/<int:oid>/soumettre', methods=['POST'])
    @login_required
    def objectif_soumettre(oid):
        with db_cursor(commit=True) as (conn, cur):
            obj = _charger_objectif(cur, oid)
            redir = _require(cur, obj, 'soumettre')
            if redir is not None: return redir
            cur.execute("""UPDATE objectifs SET progression=100, statut=%s,
                           date_realisation=CURRENT_DATE,
                           date_modification=CURRENT_TIMESTAMP WHERE id=%s""",
                        (STATUT_ATTEINT, oid))
            cur.execute("""INSERT INTO objectifs_points
                           (objectif_id, auteur_id, progression, commentaire)
                           VALUES (%s,%s,100,%s)""",
                        (oid, session.get('user_id'),
                         "Objectif marqué comme atteint par l'employé."))
            log_action(session.get('user_id'), session.get('username'),
                       'OBJECTIF_SOUMETTRE', 'objectif', oid, '')
            _notifier_manager_departement(
                cur, obj['emp_dept'],
                "✅ Objectif à valider",
                f"« {obj['titre']} » a été marqué comme atteint — à valider.",
                sauf=session.get('user_id'),
                type_='success')
            flash("Objectif soumis pour validation.", "success")
        return redirect(url_for('objectifs.objectif_detail', oid=oid))

    @bp.route('/objectifs/<int:oid>/valider', methods=['POST'])
    @login_required
    def objectif_valider(oid):
        with db_cursor(commit=True) as (conn, cur):
            obj = _charger_objectif(cur, oid)
            redir = _require(cur, obj, 'valider')
            if redir is not None: return redir
            commentaire = (request.form.get('commentaire') or '').strip() or None
            # Valider un objectif : s'il est en_cours on force progression=100.
            cur.execute("""UPDATE objectifs SET statut=%s, progression=100,
                           cloture_par=%s, cloture_le=CURRENT_DATE,
                           cloture_commentaire=%s,
                           date_realisation=COALESCE(date_realisation, CURRENT_DATE),
                           date_modification=CURRENT_TIMESTAMP
                           WHERE id=%s""",
                        (STATUT_ATTEINT, session.get('user_id'), commentaire, oid))
            cur.execute("""INSERT INTO objectifs_points
                           (objectif_id, auteur_id, progression, commentaire)
                           VALUES (%s,%s,100,%s)""",
                        (oid, session.get('user_id'),
                         commentaire or "Objectif validé comme atteint."))
            log_action(session.get('user_id'), session.get('username'),
                       'OBJECTIF_VALIDER', 'objectif', oid, '')
            uid_emp = _user_id_employe(cur, obj['employe_id'])
            _notifier(cur, uid_emp, "🎉 Objectif validé",
                      f"Votre objectif « {obj['titre']} » a été validé comme atteint !",
                      'success')
            flash("Objectif validé comme atteint.", "success")
        return redirect(url_for('objectifs.objectif_detail', oid=oid))

    @bp.route('/objectifs/<int:oid>/non-atteint', methods=['POST'])
    @login_required
    def objectif_non_atteint(oid):
        with db_cursor(commit=True) as (conn, cur):
            obj = _charger_objectif(cur, oid)
            redir = _require(cur, obj, 'non_atteint')
            if redir is not None: return redir
            commentaire = (request.form.get('commentaire') or '').strip()
            if not commentaire:
                flash("Merci de motiver la non-atteinte de l'objectif.", "danger")
                return redirect(url_for('objectifs.objectif_detail', oid=oid))
            cur.execute("""UPDATE objectifs SET statut=%s, cloture_par=%s,
                           cloture_le=CURRENT_DATE, cloture_commentaire=%s,
                           date_modification=CURRENT_TIMESTAMP
                           WHERE id=%s""",
                        (STATUT_NON_ATTEINT, session.get('user_id'), commentaire, oid))
            cur.execute("""INSERT INTO objectifs_points
                           (objectif_id, auteur_id, progression, commentaire)
                           VALUES (%s,%s,(SELECT progression FROM objectifs WHERE id=%s),%s)""",
                        (oid, session.get('user_id'), oid,
                         f"Objectif clos comme non atteint : {commentaire}"))
            log_action(session.get('user_id'), session.get('username'),
                       'OBJECTIF_NON_ATTEINT', 'objectif', oid, commentaire)
            uid_emp = _user_id_employe(cur, obj['employe_id'])
            _notifier(cur, uid_emp, "⚠️ Objectif clos comme non atteint",
                      f"Votre objectif « {obj['titre']} » a été clos comme non atteint : {commentaire}",
                      'warning')
            flash("Objectif clos comme non atteint.", "info")
        return redirect(url_for('objectifs.objectif_detail', oid=oid))

    @bp.route('/objectifs/<int:oid>/annuler', methods=['POST'])
    @login_required
    def objectif_annuler(oid):
        with db_cursor(commit=True) as (conn, cur):
            obj = _charger_objectif(cur, oid)
            redir = _require(cur, obj, 'annuler')
            if redir is not None: return redir
            commentaire = (request.form.get('commentaire') or '').strip() or None
            cur.execute("""UPDATE objectifs SET statut=%s, cloture_par=%s,
                           cloture_le=CURRENT_DATE, cloture_commentaire=%s,
                           date_modification=CURRENT_TIMESTAMP
                           WHERE id=%s""",
                        (STATUT_ANNULE, session.get('user_id'), commentaire, oid))
            log_action(session.get('user_id'), session.get('username'),
                       'OBJECTIF_ANNULER', 'objectif', oid, commentaire or '')
            # Notification croisée
            uid_emp = _user_id_employe(cur, obj['employe_id'])
            if uid_emp and uid_emp != session.get('user_id'):
                _notifier(cur, uid_emp, "Objectif annulé",
                          f"Votre objectif « {obj['titre']} » a été annulé.",
                          'warning')
            else:
                _notifier_manager_departement(
                    cur, obj['emp_dept'], "Objectif annulé",
                    f"« {obj['titre']} » a été annulé.",
                    sauf=session.get('user_id'))
            flash("Objectif annulé.", "info")
        return redirect(url_for('objectifs.objectif_detail', oid=oid))

    @bp.route('/objectifs/<int:oid>/reactiver', methods=['POST'])
    @login_required
    def objectif_reactiver(oid):
        with db_cursor(commit=True) as (conn, cur):
            obj = _charger_objectif(cur, oid)
            redir = _require(cur, obj, 'reactiver')
            if redir is not None: return redir
            # Repasse en_cours en conservant la dernière progression
            cur.execute("""UPDATE objectifs SET statut=%s, cloture_par=NULL,
                           cloture_le=NULL, cloture_commentaire=NULL,
                           date_realisation=NULL,
                           date_modification=CURRENT_TIMESTAMP
                           WHERE id=%s""",
                        (STATUT_EN_COURS, oid))
            log_action(session.get('user_id'), session.get('username'),
                       'OBJECTIF_REACTIVER', 'objectif', oid, '')
            uid_emp = _user_id_employe(cur, obj['employe_id'])
            if uid_emp and uid_emp != session.get('user_id'):
                _notifier(cur, uid_emp, "Objectif rouvert",
                          f"Votre objectif « {obj['titre']} » a été rouvert.",
                          'info')
            flash("Objectif rouvert.", "success")
        return redirect(url_for('objectifs.objectif_detail', oid=oid))

    return bp
