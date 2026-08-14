"""Workflow des demandes de congés."""

from datetime import datetime
from urllib.parse import urlencode

from flask import Blueprint, flash, redirect, render_template, request, session, url_for


def creer_blueprint_conges(deps):
    bp = Blueprint('conges', __name__)
    get_db = deps['get_db']
    get_cursor = deps['get_cursor']
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    role_required = deps['role_required']
    department_scope_sql = deps['department_scope_sql']
    get_solde_conges = deps['get_solde_conges']
    get_current_employee = deps['get_current_employee']
    pagination_info = deps['pagination_info']
    page_list = deps['page_list']
    peut_decider_rh = deps['peut_decider_rh']
    peut_donner_avis = deps['peut_donner_avis']
    envoyer_roles = deps['envoyer_roles']
    libelle_employe = deps['libelle_employe']
    managers_du_departement = deps['managers_du_departement']
    notifier_roles = deps['notifier_roles']
    user_id_de_employe = deps['user_id_de_employe']
    notifier_employe_evenement = deps['notifier_employe_evenement']
    create_notification = deps['create_notification']
    queue_email = deps['queue_email']
    log_action = deps['log_action']
    recalculer_solde = deps['recalculer_solde']
    _peut_decider_rh = peut_decider_rh
    _peut_donner_avis = peut_donner_avis
    _envoyer_roles = envoyer_roles
    _libelle_employe = libelle_employe
    _managers_du_departement = managers_du_departement
    _notifier_roles = notifier_roles
    _user_id_de_employe = user_id_de_employe
    _notifier_employe_evenement = notifier_employe_evenement
    DEMANDE_LIBELLES = deps['DEMANDE_LIBELLES']
    DEMANDE_OUVERTES = deps['DEMANDE_OUVERTES']
    DEMANDE_EN_ATTENTE = deps['DEMANDE_EN_ATTENTE']
    DEMANDE_AVIS_RENDU = deps['DEMANDE_AVIS_RENDU']
    DEMANDE_APPROUVEE = deps['DEMANDE_APPROUVEE']
    DEMANDE_REFUSEE = deps['DEMANDE_REFUSEE']
    DEMANDE_ANNULEE = deps['DEMANDE_ANNULEE']
    Q_SOLDE = deps['Q_SOLDE']

    @bp.route('/conges')
    @login_required
    def conges():
        conn = get_db()
        cur = get_cursor(conn)
        scope_where, scope_params = department_scope_sql('e', cur=cur)

        search = request.args.get('search', '').strip()
        statut = request.args.get('statut', '').strip()
        type_conge = request.args.get('type_conge', '').strip()
        date_debut = request.args.get('date_debut', '').strip()
        date_fin = request.args.get('date_fin', '').strip()
        page = max(1, request.args.get('page', 1, type=int))
        per_page = 10

        where = f" AND {scope_where}"
        params = list(scope_params)
        if search:
            where += " AND (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s)"
            params += [f"%{search.lower()}%", f"%{search.lower()}%"]
        if statut:
            where += " AND c.statut = %s"; params.append(statut)
        if type_conge:
            where += " AND c.type_conge = %s"; params.append(type_conge)
        if date_debut:
            where += " AND c.date_debut >= %s"; params.append(date_debut)
        if date_fin:
            where += " AND c.date_fin <= %s"; params.append(date_fin)

        from_ = "conges c JOIN employes e ON c.employe_id = e.id"
        cur.execute(f"SELECT COUNT(*) AS nb FROM {from_} WHERE 1=1{where}", params)
        total = cur.fetchone()['nb']
        pg = pagination_info(total, page, per_page)
        offset = (pg['page'] - 1) * per_page
        cur.execute(f"SELECT c.*, e.nom, e.prenom FROM {from_} WHERE 1=1{where} ORDER BY c.date_demande DESC LIMIT %s OFFSET %s", params + [per_page, offset])
        conges_list = cur.fetchall()

        cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom",
                    scope_params)
        employees = cur.fetchall()

        soldes = {}
        annee_courante = datetime.now().year
        if session.get('role') in ['admin', 'rh', 'manager']:
            for emp in employees:
                s = get_solde_conges(emp['id'], annee_courante)
                s['nom'] = f"{emp['prenom']} {emp['nom']}"
                soldes[emp['id']] = s

        cur.execute(f"""SELECT DISTINCT c.type_conge FROM conges c
                        JOIN employes e ON e.id = c.employe_id
                        WHERE c.type_conge IS NOT NULL AND {scope_where}
                        ORDER BY c.type_conge""", scope_params)
        types = [r['type_conge'] for r in cur.fetchall()]

        cur.close()
        conn.close()
        filters = {'search': search, 'statut': statut, 'type_conge': type_conge, 'date_debut': date_debut, 'date_fin': date_fin}
        return render_template('conges.html', conges=conges_list, employees=employees, soldes=soldes,
                               annee_courante=annee_courante, types=types, filters=filters,
                               libelles=DEMANDE_LIBELLES, ouvertes=DEMANDE_OUVERTES,
                               peut_decider=_peut_decider_rh(), peut_avis=_peut_donner_avis(),
                               pg=pg, page_items=page_list(pg['page'], pg['pages']),
                               base_qs=urlencode({k: v for k, v in filters.items() if v}))

    @bp.route('/conges/add', methods=['GET', 'POST'])
    @login_required
    def add_conge():
        """Dépôt d'une demande de congé.

        Un employé ne peut déposer que pour lui-même ; un gestionnaire peut saisir
        pour n'importe qui (cas des demandes transmises sur papier).
        """
        moi = get_current_employee()
        gestionnaire = session.get('role') in ('admin', 'rh', 'manager')

        with db_cursor(commit=True) as (conn, cur):
            scope_where, scope_params = department_scope_sql('e', cur=cur)
            if request.method == 'POST':
                employe_id = request.form.get('employe_id', type=int)
                type_conge = request.form.get('type_conge')
                date_debut = request.form.get('date_debut')
                date_fin = request.form.get('date_fin')
                motif = request.form.get('motif', '')

                # Un non-gestionnaire ne dépose que pour lui-même : on ignore
                # l'employe_id transmis, qui pourrait être falsifié.
                if not gestionnaire:
                    if not moi:
                        flash("Aucun employé n'est lié à votre compte : "
                              "contactez les RH.", "warning")
                        return redirect(url_for('self_service'))
                    employe_id = moi['id']

                if employe_id and type_conge and date_debut and date_fin:
                    d1 = datetime.strptime(date_debut, '%Y-%m-%d')
                    d2 = datetime.strptime(date_fin, '%Y-%m-%d')
                    if d2 < d1:
                        flash("La date de fin ne peut pas être avant la date de début", "danger")
                        cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom", scope_params)
                        return render_template('conge_form.html', employees=cur.fetchall(),
                                               moi=moi, gestionnaire=gestionnaire)
                    nombre_jours = (d2 - d1).days + 1

                    # Contrôle du solde : on prévient avant d'engager le circuit,
                    # plutôt que de faire refuser la demande trois jours plus tard.
                    annee = d1.year
                    cur.execute(Q_SOLDE, (employe_id, annee))
                    solde = cur.fetchone()
                    # Seuls les congés payés sont décomptés du solde (cf. recalculer_solde).
                    if solde and type_conge == 'congé payé':
                        restant = float(solde['jours_acquis'] or 0) - float(solde['jours_utilises'] or 0)
                        if nombre_jours > restant:
                            flash("Solde insuffisant : %s jour(s) demandé(s) pour %.1f restant(s) en %s."
                                  % (nombre_jours, restant, annee), "danger")
                            cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom", scope_params)
                            return render_template('conge_form.html', employees=cur.fetchall(),
                                                   moi=moi, gestionnaire=gestionnaire)

                    cur.execute("""
                        INSERT INTO conges (employe_id, type_conge, date_debut, date_fin,
                                            nombre_jours, motif, statut, demande_par_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                    """, (employe_id, type_conge, date_debut, date_fin, nombre_jours,
                          motif, DEMANDE_EN_ATTENTE, session.get('user_id')))
                    cid = cur.fetchone()['id']

                    # Étape 1 : router vers le manager du département.
                    nom, dept = _libelle_employe(cur, employe_id)
                    titre = "Demande de congé : %s" % nom
                    corps = ("%s jour(s) du %s au %s. Votre avis est attendu."
                             % (nombre_jours, d1.strftime('%d/%m/%Y'), d2.strftime('%d/%m/%Y')))
                    managers = _managers_du_departement(cur, dept)
                    for m in managers:
                        create_notification(m['id'], titre, corps, 'info', cur=cur)
                        queue_email(m.get('email'), titre, corps, cur=cur,
                                    event_key=f"conge-a-traiter:{cid}:{m['id']}")
                    if not managers:
                        # Aucun manager sur ce département : le RH traite directement.
                        message_rh = corps + " (aucun manager sur ce département)"
                        _notifier_roles(cur, ('admin', 'rh'), titre, message_rh,
                                        'info', sauf=session.get('user_id'))
                        _envoyer_roles(cur, ('admin', 'rh'), titre, message_rh,
                                       f"conge-a-traiter:{cid}")

                    log_action(session.get('user_id'), session.get('username'),
                               "Demande de congé", "conge", cid, f"{nombre_jours} j")
                    flash("Demande de congé soumise. Vous serez notifié à chaque étape.", "success")
                    return redirect(url_for('self_service_conges') if not gestionnaire
                                    else url_for('.conges'))
                else:
                    flash("Veuillez remplir tous les champs obligatoires", "danger")

            cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom", scope_params)
            employees = cur.fetchall()
        return render_template('conge_form.html', employees=employees,
                               moi=moi, gestionnaire=gestionnaire)


    @bp.route('/conges/avis/<int:id>', methods=['POST'])
    @login_required
    @role_required('admin', 'rh', 'manager')
    def avis_conge(id):
        """Étape 2 : le manager du département rend son avis (non décisionnel)."""
        avis = (request.form.get('avis') or '').strip()
        commentaire = (request.form.get('commentaire') or '').strip()
        if avis not in ('favorable', 'defavorable'):
            flash("Avis invalide.", "danger")
            return redirect(url_for('.conges'))
        if avis == 'defavorable' and not commentaire:
            flash("Merci de motiver un avis défavorable.", "danger")
            return redirect(url_for('.conges'))

        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT * FROM conges WHERE id = %s", (id,))
            c = cur.fetchone()
            if not c:
                flash("Demande introuvable.", "danger")
                return redirect(url_for('.conges'))
            if c['statut'] not in DEMANDE_OUVERTES:
                flash("Cette demande est déjà tranchée.", "warning")
                return redirect(url_for('.conges'))

            cur.execute("""UPDATE conges SET statut = %s, avis_manager = %s,
                              avis_manager_par = %s, avis_manager_le = CURRENT_DATE,
                              avis_commentaire = %s
                           WHERE id = %s""",
                        (DEMANDE_AVIS_RENDU, avis, session.get('username'),
                         commentaire or None, id))

            nom, _ = _libelle_employe(cur, c['employe_id'])
            sujet_rh = "Congé à décider : %s" % nom
            message_rh = ("Avis %s du manager %s. %s"
                          % (avis, session.get('username'), commentaire[:120]))
            _notifier_roles(cur, ('admin', 'rh'), sujet_rh, message_rh,
                            'info', sauf=session.get('user_id'))
            _envoyer_roles(cur, ('admin', 'rh'), sujet_rh, message_rh,
                           f"conge-a-decider:{id}")
            uid = _user_id_de_employe(cur, c['employe_id'])
            if uid:
                create_notification(uid, "Votre demande de congé avance",
                                    "Votre manager a rendu un avis %s. "
                                    "La décision RH suit." % avis, 'info')

        log_action(session.get('user_id'), session.get('username'),
                   "Avis manager congé", "conge", id, avis)
        flash("Avis enregistré : les RH vont trancher.", "success")
        return redirect(url_for('.conges'))


    @bp.route('/conges/update/<int:id>', methods=['POST'])
    @login_required
    @role_required('admin', 'rh')
    def update_conge(id):
        """Étape 3 : décision finale des RH (approbation ou refus motivé)."""
        action = request.form.get('action')
        motif_refus = (request.form.get('motif_refus') or '').strip()
        if action == 'refuser' and not motif_refus:
            flash("Merci d'indiquer le motif du refus : l'employé en sera informé.", "danger")
            return redirect(url_for('.conges'))

        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT * FROM conges WHERE id = %s", (id,))
            c = cur.fetchone()
            if not c:
                flash("Demande introuvable.", "danger")
                return redirect(url_for('.conges'))
            if c['statut'] not in DEMANDE_OUVERTES:
                flash("Cette demande est déjà tranchée.", "warning")
                return redirect(url_for('.conges'))

            annee = datetime.strptime(str(c['date_debut']), '%Y-%m-%d').year

            if action == 'approuver':
                cur.execute("""UPDATE conges SET statut = %s, decide_par = %s,
                                  decide_le = CURRENT_DATE, motif_refus = NULL
                               WHERE id = %s""",
                            (DEMANDE_APPROUVEE, session.get('username'), id))
                recalculer_solde(c['employe_id'], annee, cur=cur)
                _notifier_employe_evenement(
                    cur, c['employe_id'], "Congé approuvé",
                    "Votre congé du %s au %s est approuvé."
                    % (c['date_debut'], c['date_fin']), 'success',
                    cle_evenement=f"conge-decision:{id}:approuve",
                )
                flash("Congé approuvé et solde mis à jour", "success")
            elif action == 'refuser':
                cur.execute("""UPDATE conges SET statut = %s, decide_par = %s,
                                  decide_le = CURRENT_DATE, motif_refus = %s
                               WHERE id = %s""",
                            (DEMANDE_REFUSEE, session.get('username'), motif_refus, id))
                recalculer_solde(c['employe_id'], annee, cur=cur)
                _notifier_employe_evenement(
                    cur, c['employe_id'], "Congé refusé",
                    "Votre demande du %s au %s a été refusée : %s"
                    % (c['date_debut'], c['date_fin'], motif_refus), 'danger',
                    cle_evenement=f"conge-decision:{id}:refuse",
                )
                flash("Congé refusé : l'employé est informé du motif.", "info")
            else:
                flash("Action inconnue.", "danger")
                return redirect(url_for('.conges'))

        log_action(session.get('user_id'), session.get('username'),
                   "Décision congé", "conge", id, action)
        return redirect(url_for('.conges'))


    @bp.route('/conges/<int:id>/annuler', methods=['POST'])
    @login_required
    def annuler_conge(id):
        """L'employé retire sa demande tant qu'elle n'est pas tranchée."""
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT * FROM conges WHERE id = %s", (id,))
            c = cur.fetchone()
            if not c:
                flash("Demande introuvable.", "danger")
                return redirect(url_for('self_service_conges'))
            moi = get_current_employee()
            est_le_mien = moi and moi['id'] == c['employe_id']
            if not (est_le_mien or _peut_decider_rh()):
                flash("Vous ne pouvez annuler que vos propres demandes.", "danger")
                return redirect(url_for('self_service_conges'))
            if c['statut'] not in DEMANDE_OUVERTES:
                flash("Cette demande est déjà tranchée : elle ne peut plus être annulée.",
                      "warning")
                return redirect(url_for('self_service_conges') if est_le_mien
                                else url_for('.conges'))

            cur.execute("""UPDATE conges SET statut = %s, annule_par = %s
                           WHERE id = %s""",
                        (DEMANDE_ANNULEE, session.get('username'), id))
            nom, dept = _libelle_employe(cur, c['employe_id'])
            for m in _managers_du_departement(cur, dept):
                create_notification(m['id'], "Demande de congé annulée",
                                    "%s a retiré sa demande du %s au %s."
                                    % (nom, c['date_debut'], c['date_fin']), 'info')

        log_action(session.get('user_id'), session.get('username'),
                   "Annulation congé", "conge", id, None)
        flash("Demande annulée.", "success")
        return redirect(url_for('self_service_conges') if est_le_mien else url_for('.conges'))


    @bp.route('/conges/delete/<int:id>', methods=['POST'])
    @login_required
    @role_required('admin', 'rh', 'manager')
    def delete_conge(id):
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT id FROM absences WHERE conge_id = %s", (id,))
            if cur.fetchone():
                flash("Ce congé maladie provient d'un justificatif accepté et ne peut pas être supprimé.",
                      "warning")
                return redirect(url_for('.conges'))
            cur.execute("DELETE FROM conges WHERE id = %s", (id,))
        flash("Demande de congé supprimée", "success")
        return redirect(url_for('.conges'))

    return bp
