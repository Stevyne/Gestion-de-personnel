"""Présences, pointages et historique du temps de travail."""

from datetime import date, datetime
from urllib.parse import urlencode

from flask import Blueprint, flash, redirect, render_template, request, url_for


def creer_blueprint_presences(deps):
    bp = Blueprint('presences', __name__)
    get_db = deps['get_db']
    get_cursor = deps['get_cursor']
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    role_required = deps['role_required']
    department_scope_sql = deps['department_scope_sql']
    calculer_retard = deps['calculer_retard']
    send_retard_email = deps['send_retard_email']
    _notifier_employe_evenement = deps['notifier_employe_evenement']
    pagination_info = deps['pagination_info']
    page_list = deps['page_list']

    @bp.route('/presences', methods=['GET', 'POST'])
    @login_required
    def presences():
        conn = get_db()
        cur = get_cursor(conn)
        scope_where, scope_params = department_scope_sql('e', cur=cur)

        today = date.today().strftime('%Y-%m-%d')

            # === Retards aujourd'hui ===
        cur.execute("""
            SELECT p.*, e.nom, e.prenom
            FROM presences p
            JOIN employes e ON p.employe_id = e.id
            WHERE p.date = %s AND """ + scope_where,
            [today] + scope_params)
        presences_today = cur.fetchall()

        retards_aujourdhui = []
        total_retards_minutes = 0
        for p in presences_today:
            retard = calculer_retard(p.get('heure_arrivee'))
            if retard > 0:
                p['retard_minutes'] = retard
                retards_aujourdhui.append(p)
                total_retards_minutes += retard

        nb_retards = len(retards_aujourdhui)

        # === Gestion du pointage rapide (POST) ===
        if request.method == 'POST':
            action = request.form.get('action')
            employe_id = request.form.get('quick_employe_id')
            date_val = datetime.now().strftime('%Y-%m-%d')

            if action and employe_id:
                employe_id = int(employe_id)

                if action == 'clock_in':
                    cur.execute("""
                        INSERT INTO presences (employe_id, date, heure_arrivee, statut)
                        VALUES (%s, %s, CURRENT_TIME, 'présent')
                        ON CONFLICT (employe_id, date)
                        DO UPDATE SET heure_arrivee = CURRENT_TIME
                    """, (employe_id, date_val))
                    conn.commit()
                    flash('Entrée pointée', 'success')

                elif action == 'clock_out':
                    cur.execute("""
                        INSERT INTO presences (employe_id, date, heure_depart)
                        VALUES (%s, %s, CURRENT_TIME)
                        ON CONFLICT (employe_id, date)
                        DO UPDATE SET heure_depart = CURRENT_TIME
                    """, (employe_id, date_val))
                    conn.commit()
                    flash('Sortie pointée', 'success')

                cur.close(); conn.close()
                return redirect(url_for('presences.presences'))

            # Normal GET: display the page with filters + pagination
        search_raw = request.args.get('search', '').strip()
        search = search_raw.lower()
        date_filter = request.args.get('date', '').strip()
        page = max(1, request.args.get('page', 1, type=int))
        per_page = 10

        where = f" AND {scope_where}"
        params = list(scope_params)
        if search:
            where += " AND (LOWER(e.nom) LIKE %s OR LOWER(e.prenom) LIKE %s)"
            params += [f"%{search}%", f"%{search}%"]
        if date_filter:
            where += " AND p.date = %s"; params.append(date_filter)

        from_ = "presences p JOIN employes e ON p.employe_id = e.id"
        cur.execute(f"SELECT COUNT(*) AS nb FROM {from_} WHERE 1=1{where}", params)
        total = cur.fetchone()['nb']
        pg = pagination_info(total, page, per_page)
        offset = (pg['page'] - 1) * per_page
        cur.execute(f"SELECT p.*, e.nom, e.prenom FROM {from_} WHERE 1=1{where} ORDER BY p.date DESC, p.heure_arrivee DESC LIMIT %s OFFSET %s", params + [per_page, offset])
        presences_list = cur.fetchall()

        for p in presences_list:
            if p.get('heure_arrivee'):
                p['heure_arrivee'] = str(p['heure_arrivee'])[:5]
            if p.get('heure_depart'):
                p['heure_depart'] = str(p['heure_depart'])[:5]
            p['retard_minutes'] = calculer_retard(p['heure_arrivee'])
            p['retard'] = p['retard_minutes'] > 0

        cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom",
                    scope_params)
        employees = cur.fetchall()
        cur.close()
        conn.close()
        filters = {'search': search_raw, 'date': date_filter}
        return render_template('presences.html', presences=presences_list, employees=employees, today=today,
                               retards_aujourdhui=retards_aujourdhui, nb_retards=nb_retards, total_retards_minutes=total_retards_minutes,
                               search=search_raw, date_filter=date_filter, pg=pg, page_items=page_list(pg['page'], pg['pages']),
                               base_qs=urlencode({k: v for k, v in filters.items() if v}))

    @bp.route('/presences/clock_in/<int:employe_id>', methods=['POST'])
    @login_required
    def clock_in(employe_id):
        date_val = request.form.get('date') or datetime.now().strftime('%Y-%m-%d')
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT nom, prenom, email FROM employes WHERE id = %s", (employe_id,))
            emp = cur.fetchone()
            cur.execute("INSERT INTO presences (employe_id, date, heure_arrivee, statut) VALUES (%s, %s, CURRENT_TIME, 'présent') ON CONFLICT (employe_id, date) DO UPDATE SET heure_arrivee = CURRENT_TIME", (employe_id, date_val))
            cur.execute("SELECT heure_arrivee FROM presences WHERE employe_id=%s AND date=%s", (employe_id, date_val))
            res = cur.fetchone()
            heure = str(res['heure_arrivee'])[:5] if res else '09:00'
            retard = calculer_retard(heure)
            if retard > 0 and emp:
                send_retard_email(f"{emp['prenom']} {emp['nom']}", emp.get('email'), retard, date_val, heure)
        flash('Entrée pointée', 'success')
        return redirect(url_for('presences.presences'))

    @bp.route('/presences/clock_out/<int:employe_id>', methods=['POST'])
    @login_required
    def clock_out(employe_id):
        date_val = request.form.get('date') or datetime.now().strftime('%Y-%m-%d')
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("""
                INSERT INTO presences (employe_id, date, heure_depart)
                VALUES (%s, %s, CURRENT_TIME)
                ON CONFLICT (employe_id, date)
                DO UPDATE SET heure_depart = CURRENT_TIME
            """, (employe_id, date_val))
        flash('Sortie pointée', 'success')
        return redirect(url_for('presences.presences'))

    @bp.route('/presences/add', methods=['GET', 'POST'])
    @login_required
    def add_presence():
        conn = get_db()
        cur = get_cursor(conn)

        if request.method == 'POST':
            employe_id = request.form.get('employe_id')
            date_val = request.form.get('date')
            heure_arrivee = request.form.get('heure_arrivee')
            heure_depart = request.form.get('heure_depart')
            statut = request.form.get('statut', 'présent')
            commentaire = request.form.get('commentaire', '')

            if employe_id and date_val:
                cur.execute("""
                    INSERT INTO presences (employe_id, date, heure_arrivee, heure_depart, statut, commentaire)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (employe_id, date)
                    DO UPDATE SET
                        heure_arrivee = COALESCE(EXCLUDED.heure_arrivee, presences.heure_arrivee),
                        heure_depart = COALESCE(EXCLUDED.heure_depart, presences.heure_depart),
                        statut = EXCLUDED.statut,
                        commentaire = EXCLUDED.commentaire
                """, (employe_id, date_val, heure_arrivee or None, heure_depart or None, statut, commentaire))
                _notifier_employe_evenement(
                    cur, int(employe_id), "Présence enregistrée",
                    f"Votre présence du {date_val} a été enregistrée avec le statut « {statut} ». ",
                    'info',
                    cle_evenement=(f"presence:{employe_id}:{date_val}:{statut}:"
                                   f"{heure_arrivee}:{heure_depart}"),
                )
                conn.commit()
                flash("Présence enregistrée / modifiée avec succès", "success")
                cur.close(); conn.close()
                return redirect(url_for('presences.presences'))

        # GET → formulaire limité au département courant
        scope_where, scope_params = department_scope_sql('e', cur=cur)
        cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom",
                    scope_params)
        employees = cur.fetchall()
        cur.close(); conn.close()
        return render_template('presence_form.html', employees=employees)


    @bp.route('/presences/delete/<int:id>', methods=['POST'])
    @login_required
    @role_required('admin', 'rh', 'manager')
    def delete_presence(id):
        conn = get_db()
        cur = get_cursor(conn)
        cur.execute("DELETE FROM presences WHERE id = %s", (id,))
        conn.commit()
        cur.close(); conn.close()
        flash("Présence supprimée", "success")
        return redirect(url_for('presences.presences'))


    @bp.route('/historique')
    @login_required
    def historique():
        conn = get_db()
        cur = get_cursor(conn)
        scope_where, scope_params = department_scope_sql('e', cur=cur)

        selected_employe = request.args.get('employe_id', '').strip()
        date_debut = request.args.get('date_debut', '').strip()
        date_fin = request.args.get('date_fin', '').strip()
        selected_statut = request.args.get('statut', '').strip()
        page = max(1, request.args.get('page', 1, type=int))
        per_page = 10

        where = f" AND {scope_where}"
        params = list(scope_params)
        if selected_employe and selected_employe.isdigit():
            where += " AND p.employe_id = %s"; params.append(int(selected_employe))
        if date_debut:
            where += " AND p.date >= %s"; params.append(date_debut)
        if date_fin:
            where += " AND p.date <= %s"; params.append(date_fin)
        if selected_statut:
            where += " AND p.statut = %s"; params.append(selected_statut)

        from_ = "presences p JOIN employes e ON p.employe_id = e.id"
        # On récupère tout le jeu filtré (borné) pour les stats, puis on pagine l'affichage
        cur.execute(f"SELECT p.*, e.nom, e.prenom FROM {from_} WHERE 1=1{where} ORDER BY p.date DESC, p.heure_arrivee DESC LIMIT 5000", params)
        all_presences = cur.fetchall()

        total_pointages = len(all_presences)
        total_heures = 0.0
        employes_set = set()
        for p in all_presences:
            if p.get('heure_arrivee'):
                p['heure_arrivee'] = str(p['heure_arrivee'])[:5]
            if p.get('heure_depart'):
                p['heure_depart'] = str(p['heure_depart'])[:5]
            employes_set.add(p.get('employe_id'))
            try:
                if p.get('heure_arrivee') and p.get('heure_depart'):
                    ha_parts = str(p['heure_arrivee']).split(':')[:2]
                    hd_parts = str(p['heure_depart']).split(':')[:2]
                    ha_min = int(ha_parts[0]) * 60 + int(ha_parts[1])
                    hd_min = int(hd_parts[0]) * 60 + int(hd_parts[1])
                    mins = hd_min - ha_min
                    if mins > 0:
                        p['duree_heures'] = round(mins / 60, 1)
                        total_heures += mins / 60
                    else:
                        p['duree_heures'] = None
                else:
                    p['duree_heures'] = None
            except Exception:
                p['duree_heures'] = None
            p['retard_minutes'] = calculer_retard(p.get('heure_arrivee'))
            p['retard'] = p['retard_minutes'] > 0

        employes_concernes = len(employes_set)
        pg = pagination_info(total_pointages, page, per_page)
        offset = (pg['page'] - 1) * per_page
        presences_list = all_presences[offset:offset + per_page]

        cur.execute(f"SELECT id, nom, prenom FROM employes e WHERE {scope_where} ORDER BY nom, prenom",
                    scope_params)
        employees = cur.fetchall()
        cur.close()
        conn.close()

        filters = {'employe_id': selected_employe, 'date_debut': date_debut, 'date_fin': date_fin, 'statut': selected_statut}
        return render_template('historique.html', presences=presences_list, employees=employees,
                               total_pointages=total_pointages, total_heures=round(total_heures, 1),
                               employes_concernes=employes_concernes, selected_employe=selected_employe,
                               date_debut=date_debut, date_fin=date_fin, selected_statut=selected_statut,
                               pg=pg, page_items=page_list(pg['page'], pg['pages']),
                               base_qs=urlencode({k: v for k, v in filters.items() if v}))

    return bp
