"""Consultation, saisie et synchronisation des absences."""

from urllib.parse import urlencode

from flask import Blueprint, flash, redirect, render_template, request, session, url_for


def creer_blueprint_absences(deps):
    bp = Blueprint('absences', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    role_required = deps['role_required']
    department_scope_sql = deps['department_scope_sql']
    pagination_info = deps['pagination_info']
    page_list = deps['page_list']
    notifier_employe_evenement = deps['notifier_employe_evenement']
    generer_absences_automatiques = deps['generer_absences_automatiques']
    get_department_scope = deps['get_department_scope']
    ABSENCE_STATUT_LABELS = deps['ABSENCE_STATUT_LABELS']
    ABSENCE_ACCEPTEE = deps['ABSENCE_ACCEPTEE']
    _notifier_employe_evenement = notifier_employe_evenement

    @bp.route('/absences')
    @login_required
    @role_required('admin', 'rh', 'manager')
    def absences():
        # NOTE : la génération automatique se faisait ici, à CHAQUE affichage de la
        # page. Conséquence : supprimer une absence "aucune présence enregistrée"
        # ne servait à rien, puisque la condition (toujours aucune présence ce
        # jour-là) redevenait vraie au rechargement suivant et la ligne était
        # aussitôt recréée. La génération se fait maintenant uniquement via le
        # bouton "Synchroniser" (route /absences/synchroniser), de façon explicite.

        search = request.args.get('search', '').strip()
        employe_id = request.args.get('employe_id', '').strip()
        date_debut = request.args.get('date_debut', '').strip()
        date_fin = request.args.get('date_fin', '').strip()
        statut = request.args.get('statut', '').strip()
        page = max(1, request.args.get('page', 1, type=int))
        per_page = 10

        where = ""
        params = []
        if search:
            where += " AND (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s)"
            params += [f"%{search.lower()}%", f"%{search.lower()}%"]
        if employe_id and employe_id.isdigit():
            where += " AND a.employe_id = %s"; params.append(int(employe_id))
        if date_debut:
            where += " AND a.date >= %s"; params.append(date_debut)
        if date_fin:
            where += " AND a.date <= %s"; params.append(date_fin)
        if statut in ABSENCE_STATUT_LABELS:
            where += " AND a.statut = %s"; params.append(statut)

        with db_cursor() as (conn, cur):
            scope_where, scope_params = department_scope_sql('e', cur=cur)
            where += f" AND {scope_where}"
            params += scope_params
            from_ = "absences a JOIN employes e ON a.employe_id = e.id"
            cur.execute(f"SELECT COUNT(*) AS nb FROM {from_} WHERE 1=1{where}", params)
            total = cur.fetchone()['nb']
            pg = pagination_info(total, page, per_page)
            offset = (pg['page'] - 1) * per_page
            cur.execute(f"SELECT a.*, e.nom, e.prenom FROM {from_} WHERE 1=1{where} ORDER BY a.date DESC LIMIT %s OFFSET %s", params + [per_page, offset])
            absences_list = cur.fetchall()
            cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom",
                        scope_params)
            employees = cur.fetchall()
        filters = {'search': search, 'employe_id': employe_id, 'date_debut': date_debut,
                   'date_fin': date_fin, 'statut': statut}
        return render_template('absences.html', absences=absences_list, employees=employees,
                               nb_total=total, filters=filters,
                               statut_labels=ABSENCE_STATUT_LABELS,
                               pg=pg, page_items=page_list(pg['page'], pg['pages']),
                               base_qs=urlencode({k: v for k, v in filters.items() if v}))


    @bp.route('/absences/add', methods=['GET', 'POST'])
    @login_required
    @role_required('admin', 'rh', 'manager')
    def add_absence():
        if request.method == 'POST':
            employe_id = request.form.get('employe_id')
            date_val = request.form.get('date')
            motif = request.form.get('motif', '')
            if employe_id and date_val:
                with db_cursor(commit=True) as (conn, cur):
                    cur.execute("""
                        INSERT INTO absences (employe_id, date, motif, enregistre_par)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (employe_id, date) DO UPDATE SET
                            motif = EXCLUDED.motif,
                            enregistre_par = EXCLUDED.enregistre_par
                    """, (employe_id, date_val, motif, session.get('user_id')))
                    message = (f"Une absence non justifiée a été enregistrée pour le {date_val}. "
                               "Vous pouvez déposer un justificatif depuis votre espace employé.")
                    _notifier_employe_evenement(
                        cur, int(employe_id), "Absence enregistrée", message, 'warning',
                        cle_evenement=f"absence-manuelle:{employe_id}:{date_val}",
                    )
                flash("Absence non justifiée enregistrée et employé informé", "success")
                return redirect(url_for('.absences'))
            flash("Employé et date requis", "danger")
        with db_cursor() as (conn, cur):
            scope_where, scope_params = department_scope_sql('e', cur=cur)
            cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom",
                        scope_params)
            employees = cur.fetchall()
        return render_template('absence_form.html', employees=employees)


    @bp.route('/absences/delete/<int:id>', methods=['POST'])
    @login_required
    @role_required('admin', 'rh')
    def delete_absence(id):
        with db_cursor(commit=True) as (conn, cur):
            # On mémorise la date supprimée pour empêcher la génération automatique
            # de la recréer immédiatement (sinon elle réapparaît au prochain affichage).
            cur.execute("SELECT employe_id, date, statut, conge_id FROM absences WHERE id = %s", (id,))
            row = cur.fetchone()
            if row and row.get('statut') == ABSENCE_ACCEPTEE:
                flash("Une absence déjà requalifiée en congé maladie ne peut pas être supprimée.",
                      "warning")
                return redirect(url_for('.absences'))
            if row:
                cur.execute("""
                    INSERT INTO absences_exclues (employe_id, date) VALUES (%s, %s)
                    ON CONFLICT (employe_id, date) DO NOTHING
                """, (row['employe_id'], row['date']))
            cur.execute("DELETE FROM absences WHERE id = %s", (id,))
        flash("Absence supprimée", "success")
        return redirect(url_for('.absences'))


    @bp.route('/absences/synchroniser', methods=['POST'])
    @login_required
    @role_required('admin', 'rh', 'manager')
    def synchroniser_absences():
        """Recalcule explicitement toutes les absences (depuis la date d'embauche
        jusqu'à la veille) et renvoie le nombre d'absences nouvellement créées."""
        with db_cursor(commit=True) as (conn, cur):
            scope = get_department_scope(cur)
            if scope['is_global']:
                nb = generer_absences_automatiques(cur)
            elif scope['is_empty']:
                nb = 0
            else:
                nb = generer_absences_automatiques(cur,
                                                   departement=scope['department'])
        if nb > 0:
            flash(f"{nb} absence(s) générée(s) automatiquement.", "success")
        else:
            flash("Les absences sont déjà à jour.", "info")
        return redirect(url_for('.absences'))

    return bp
