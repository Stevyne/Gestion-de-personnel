"""Workflow strict de départ d'un employé avec archivage."""

from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, session, url_for


def creer_blueprint_departs(deps):
    bp = Blueprint('departs', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    role_required = deps['role_required']
    create_notification = deps['create_notification']
    queue_email = deps['queue_email']
    log_action = deps['log_action']

    def _materiels_restants(cur, employe_id):
        cur.execute("""SELECT a.id, m.nom, a.quantite, ex.numero_inventaire
                       FROM materiels_attributions a
                       JOIN materiels m ON m.id=a.materiel_id
                       LEFT JOIN materiel_exemplaires ex ON ex.id=a.exemplaire_id
                       WHERE a.employe_id=%s AND a.date_retour IS NULL
                       ORDER BY m.nom""", (employe_id,))
        attributions = cur.fetchall()
        cur.execute("""SELECT ex.id, m.nom, ex.numero_inventaire
                       FROM materiel_exemplaires ex JOIN materiels m ON m.id=ex.materiel_id
                       WHERE ex.employe_id=%s
                         AND NOT EXISTS (SELECT 1 FROM materiels_attributions a
                                         WHERE a.exemplaire_id=ex.id AND a.date_retour IS NULL)""",
                    (employe_id,))
        exemplaires_orphelins = cur.fetchall()
        return attributions, exemplaires_orphelins

    @bp.route('/departs')
    @login_required
    @role_required('rh')
    def departs_liste():
        with db_cursor() as (conn, cur):
            cur.execute("""SELECT e.*,
                     ((SELECT COUNT(*) FROM materiels_attributions a
                        WHERE a.employe_id=e.id AND a.date_retour IS NULL) +
                      (SELECT COUNT(*) FROM materiel_exemplaires ex
                        WHERE ex.employe_id=e.id AND NOT EXISTS
                          (SELECT 1 FROM materiels_attributions a2
                           WHERE a2.exemplaire_id=ex.id AND a2.date_retour IS NULL))) AS materiels_restants,
                     (SELECT COUNT(*) FROM users u WHERE u.employe_id=e.id AND u.actif) AS comptes_actifs
                    FROM employes e
                    ORDER BY CASE e.statut_depart WHEN 'preparation' THEN 0 WHEN 'aucun' THEN 1 ELSE 2 END,
                             e.date_depart_prevue NULLS LAST, e.nom, e.prenom""")
            employes = cur.fetchall()
        return render_template('departs.html', employes=employes, today=date.today())

    @bp.route('/employes/<int:id>/depart/initier', methods=['POST'])
    @login_required
    @role_required('rh')
    def depart_initier(id):
        date_prevue_raw = (request.form.get('date_depart_prevue') or '').strip()
        motif = (request.form.get('motif_depart') or '').strip()
        try:
            date_prevue = datetime.strptime(date_prevue_raw, '%Y-%m-%d').date()
        except ValueError:
            flash("Date de départ prévue invalide.", 'danger')
            return redirect(url_for('departs.departs_liste'))
        if not motif:
            flash("Le motif du départ est obligatoire.", 'danger')
            return redirect(url_for('departs.departs_liste'))

        user_id = None
        email = None
        nom = None
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT * FROM employes WHERE id=%s FOR UPDATE", (id,))
            employe = cur.fetchone()
            if not employe:
                flash("Employé introuvable.", 'danger')
                return redirect(url_for('departs.departs_liste'))
            if not employe['actif']:
                flash("Cet employé est déjà archivé.", 'warning')
                return redirect(url_for('departs.departs_liste'))
            cur.execute("""UPDATE employes SET statut_depart='preparation',
                           date_depart_prevue=%s, motif_depart=%s,
                           depart_initie_par=%s, depart_initie_le=CURRENT_TIMESTAMP
                           WHERE id=%s""",
                        (date_prevue, motif, session.get('user_id'), id))
            cur.execute("SELECT id FROM users WHERE employe_id=%s ORDER BY id LIMIT 1", (id,))
            compte = cur.fetchone()
            user_id = compte['id'] if compte else None
            email = employe.get('email')
            nom = f"{employe['prenom']} {employe['nom']}"
            cur.execute("""INSERT INTO depart_employe_logs
                (employe_id,evenement,details,acteur_id,acteur_nom)
                VALUES (%s,'initie',%s,%s,%s)""",
                        (id, f"Prévu le {date_prevue}: {motif}", session.get('user_id'),
                         session.get('username')))
            if user_id:
                create_notification(user_id, "Préparation de votre départ",
                                    f"Votre départ est prévu le {date_prevue}. Les matériels doivent être restitués.",
                                    'warning', cur=cur)
            queue_email(email, "Préparation de votre départ",
                        f"Bonjour {employe['prenom']},\n\nVotre départ est prévu le {date_prevue}. "
                        "Merci de restituer tous les matériels avant la clôture.",
                        cur=cur, event_key=f"depart-initie:{id}:{date_prevue}")
        log_action(session.get('user_id'), session.get('username'), 'DEPART_INITIE',
                   'employe', id, f"{nom} — {date_prevue}")
        flash("Départ initié. La clôture restera bloquée jusqu'au retour de tous les matériels.", 'success')
        return redirect(url_for('departs.departs_liste'))

    @bp.route('/employes/<int:id>/depart/finaliser', methods=['POST'])
    @login_required
    @role_required('rh')
    def depart_finaliser(id):
        date_effective_raw = (request.form.get('date_depart_effective') or '').strip()
        date_effective = date.today()
        if date_effective_raw:
            try:
                date_effective = datetime.strptime(date_effective_raw, '%Y-%m-%d').date()
            except ValueError:
                flash("Date de départ effective invalide.", 'danger')
                return redirect(url_for('departs.departs_liste'))

        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT * FROM employes WHERE id=%s FOR UPDATE", (id,))
            employe = cur.fetchone()
            if not employe:
                flash("Employé introuvable.", 'danger')
                return redirect(url_for('departs.departs_liste'))
            if employe['statut_depart'] != 'preparation' or not employe['actif']:
                flash("Le départ doit d'abord être initié.", 'warning')
                return redirect(url_for('departs.departs_liste'))
            attributions, exemplaires = _materiels_restants(cur, id)
            if attributions or exemplaires:
                noms = [a['numero_inventaire'] or a['nom'] for a in attributions]
                noms += [e['numero_inventaire'] for e in exemplaires]
                flash("Clôture impossible : matériel restant — " + ", ".join(noms), 'danger')
                return redirect(url_for('departs.departs_liste'))

            cur.execute("""UPDATE employes SET actif=FALSE, statut_depart='finalise',
                           date_depart_effective=%s, depart_finalise_par=%s,
                           depart_finalise_le=CURRENT_TIMESTAMP WHERE id=%s""",
                        (date_effective, session.get('user_id'), id))
            cur.execute("UPDATE users SET actif=FALSE WHERE employe_id=%s", (id,))
            cur.execute("""UPDATE sessions_actives SET revoked_at=CURRENT_TIMESTAMP,
                           revoked_by=%s WHERE user_id IN
                           (SELECT id FROM users WHERE employe_id=%s) AND revoked_at IS NULL""",
                        (session.get('username'), id))
            cur.execute("""INSERT INTO depart_employe_logs
                (employe_id,evenement,details,acteur_id,acteur_nom)
                VALUES (%s,'finalise',%s,%s,%s)""",
                        (id, f"Départ effectif le {date_effective}", session.get('user_id'),
                         session.get('username')))
            nom = f"{employe['prenom']} {employe['nom']}"
        log_action(session.get('user_id'), session.get('username'), 'DEPART_FINALISE',
                   'employe', id, f"{nom} — {date_effective}")
        flash("Départ finalisé : fiche archivée, comptes désactivés et sessions fermées.", 'success')
        return redirect(url_for('departs.departs_liste'))

    @bp.route('/employes/<int:id>/depart/annuler', methods=['POST'])
    @login_required
    @role_required('rh')
    def depart_annuler(id):
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT statut_depart FROM employes WHERE id=%s FOR UPDATE", (id,))
            employe = cur.fetchone()
            if not employe or employe['statut_depart'] != 'preparation':
                flash("Aucun départ en préparation pour cet employé.", 'warning')
                return redirect(url_for('departs.departs_liste'))
            cur.execute("""UPDATE employes SET statut_depart='annule', date_depart_prevue=NULL,
                           depart_initie_par=NULL, depart_initie_le=NULL WHERE id=%s""", (id,))
            cur.execute("""INSERT INTO depart_employe_logs
                (employe_id,evenement,details,acteur_id,acteur_nom)
                VALUES (%s,'annule','Workflow annulé',%s,%s)""",
                        (id, session.get('user_id'), session.get('username')))
        log_action(session.get('user_id'), session.get('username'), 'DEPART_ANNULE',
                   'employe', id, None)
        flash("Préparation du départ annulée.", 'info')
        return redirect(url_for('departs.departs_liste'))

    return bp
