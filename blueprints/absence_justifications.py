"""Circuit self-service de justification des absences.

Premier périmètre extrait de ``app.py`` sous forme de Blueprint. Les dépendances
(base, authentification, notifications, e-mails et audit) sont injectées par la
fabrique pour éviter tout import circulaire avec l'application historique.
"""

import io
import os

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.utils import secure_filename


ABSENCE_NON_JUSTIFIEE = 'non_justifiee'
ABSENCE_JUSTIFICATIF_DEPOSE = 'justificatif_depose'
ABSENCE_ACCEPTEE = 'acceptee'
ABSENCE_REFUSEE = 'refusee'

ABSENCE_STATUT_LABELS = {
    ABSENCE_NON_JUSTIFIEE: 'Non justifiée',
    ABSENCE_JUSTIFICATIF_DEPOSE: 'Justificatif déposé — décision RH attendue',
    ABSENCE_ACCEPTEE: 'Justificatif accepté — congé maladie créé',
    ABSENCE_REFUSEE: 'Justificatif refusé',
}

JUSTIFICATIF_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}
JUSTIFICATIF_MAX_BYTES = 8 * 1024 * 1024


def _lire_justificatif(fichier, detect_file_type):
    """Valide le nom, la taille et les magic-bytes d'un justificatif."""
    if not fichier or not fichier.filename:
        return None, "Sélectionnez un justificatif."
    ext = fichier.filename.rsplit('.', 1)[-1].lower() if '.' in fichier.filename else ''
    if ext not in JUSTIFICATIF_EXTENSIONS:
        return None, "Format non accepté. Utilisez un PDF, un PNG ou un JPEG."

    fichier.stream.seek(0, os.SEEK_END)
    taille = fichier.stream.tell()
    fichier.stream.seek(0)
    if taille <= 0:
        return None, "Le justificatif est vide."
    if taille > JUSTIFICATIF_MAX_BYTES:
        return None, "Le justificatif dépasse la taille maximale de 8 Mo."
    if detect_file_type(fichier) not in JUSTIFICATIF_EXTENSIONS:
        return None, "Le contenu du justificatif ne correspond pas à son extension."

    fichier.stream.seek(0)
    contenu = fichier.stream.read()
    nom = secure_filename(fichier.filename)[:255] or f"justificatif.{ext}"
    return {
        'nom': nom,
        'type': ext,
        'taille': len(contenu),
        'contenu': contenu,
    }, None


def creer_blueprint_justifications(deps):
    """Construit le Blueprint avec les services fournis par ``app.py``."""
    bp = Blueprint('absence_justifications', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    role_required = deps['role_required']
    get_current_employee = deps['get_current_employee']
    detect_file_type = deps['detect_file_type']
    create_notification = deps['create_notification']
    queue_email = deps['queue_email']
    log_action = deps['log_action']

    def _cible_employe(cur, employe_id):
        cur.execute("""
            SELECT e.nom, e.prenom, e.email, u.id AS user_id
              FROM employes e
              LEFT JOIN users u ON u.employe_id = e.id
             WHERE e.id = %s
             ORDER BY u.id NULLS LAST LIMIT 1
        """, (employe_id,))
        return cur.fetchone()

    @bp.route('/self-service/absences')
    @login_required
    def self_service_absences():
        employe = get_current_employee()
        if not employe:
            flash("Aucun employé n'est lié à votre compte.", 'warning')
            return redirect(url_for('self_service'))
        with db_cursor() as (conn, cur):
            cur.execute("""
                SELECT id, date, motif, statut, date_enregistrement,
                       justificatif_nom, justificatif_taille, date_depot_justificatif,
                       motif_refus, decide_le, conge_id
                  FROM absences
                 WHERE employe_id = %s
                 ORDER BY date DESC
            """, (employe['id'],))
            dossiers = cur.fetchall()
        return render_template('self_service_absences.html', employee=employe,
                               absences=dossiers,
                               statut_labels=ABSENCE_STATUT_LABELS,
                               peut_deposer=(ABSENCE_NON_JUSTIFIEE, ABSENCE_REFUSEE))

    @bp.route('/self-service/absences/<int:absence_id>/justificatif', methods=['POST'])
    @login_required
    def deposer_justificatif(absence_id):
        employe = get_current_employee()
        if not employe:
            flash("Aucun employé n'est lié à votre compte.", 'warning')
            return redirect(url_for('self_service'))

        justificatif, erreur = _lire_justificatif(
            request.files.get('justificatif'), detect_file_type)
        commentaire = (request.form.get('commentaire') or '').strip()
        if erreur:
            flash(erreur, 'danger')
            return redirect(url_for('.self_service_absences'))

        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT * FROM absences WHERE id = %s FOR UPDATE",
                        (absence_id,))
            absence = cur.fetchone()
            if not absence or absence['employe_id'] != employe['id']:
                abort(404)
            if absence['statut'] not in (ABSENCE_NON_JUSTIFIEE, ABSENCE_REFUSEE):
                flash("Cette absence ne peut plus recevoir de justificatif.", 'warning')
                return redirect(url_for('.self_service_absences'))

            cur.execute("""
                UPDATE absences
                   SET statut = %s, justificatif_nom = %s,
                       justificatif_type = %s, justificatif_taille = %s,
                       justificatif_contenu = %s,
                       date_depot_justificatif = CURRENT_TIMESTAMP,
                       justification_commentaire = %s,
                       motif_refus = NULL, decide_par = NULL, decide_le = NULL
                 WHERE id = %s
            """, (ABSENCE_JUSTIFICATIF_DEPOSE, justificatif['nom'],
                  justificatif['type'], justificatif['taille'],
                  justificatif['contenu'], commentaire or None, absence_id))

            nom = f"{employe['prenom']} {employe['nom']}"
            cur.execute("""
                SELECT u.id, e.email
                  FROM users u LEFT JOIN employes e ON e.id = u.employe_id
                 WHERE u.role IN ('admin', 'rh')
            """)
            for rh in cur.fetchall():
                create_notification(
                    rh['id'], "Justificatif d'absence à traiter",
                    f"{nom} a déposé un justificatif pour l'absence du {absence['date']}.",
                    'info', cur=cur)
                queue_email(
                    rh.get('email'), "Justificatif d'absence à traiter",
                    f"{nom} a déposé un justificatif pour son absence du "
                    f"{absence['date']}. Connectez-vous à l'application pour rendre la décision.",
                    cur=cur,
                    event_key=(f"absence-justificatif:{absence_id}:{rh['id']}:"
                               f"{absence.get('decide_le') or 'initial'}"))

        log_action(session.get('user_id'), session.get('username'),
                   "Dépôt justificatif absence", 'absence', absence_id,
                   justificatif['nom'])
        flash("Justificatif déposé. Les RH ont été informées.", 'success')
        return redirect(url_for('.self_service_absences'))

    @bp.route('/absences/<int:absence_id>/justificatif')
    @login_required
    def telecharger_justificatif(absence_id):
        employe = get_current_employee()
        with db_cursor() as (conn, cur):
            cur.execute("""
                SELECT employe_id, justificatif_nom, justificatif_type,
                       justificatif_contenu
                  FROM absences WHERE id = %s
            """, (absence_id,))
            absence = cur.fetchone()
        if not absence or absence.get('justificatif_contenu') is None:
            abort(404)

        est_proprietaire = bool(employe and employe['id'] == absence['employe_id'])
        est_rh = session.get('role') in ('admin', 'rh')
        if not (est_proprietaire or est_rh):
            abort(403)

        reponse = send_file(
            io.BytesIO(bytes(absence['justificatif_contenu'])),
            as_attachment=True,
            download_name=secure_filename(absence['justificatif_nom']) or 'justificatif',
        )
        reponse.headers['X-Content-Type-Options'] = 'nosniff'
        reponse.headers['Cache-Control'] = 'private, no-store'
        return reponse

    @bp.route('/absences/<int:absence_id>/decision', methods=['POST'])
    @login_required
    @role_required('rh')
    def decider_justificatif(absence_id):
        decision = (request.form.get('decision') or '').strip()
        motif_refus = (request.form.get('motif_refus') or '').strip()
        if decision not in ('accepter', 'refuser'):
            flash("Décision invalide.", 'danger')
            return redirect(url_for('absences'))
        if decision == 'refuser' and not motif_refus:
            flash("Le motif du refus est obligatoire.", 'danger')
            return redirect(url_for('absences'))

        cible = None
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT * FROM absences WHERE id = %s FOR UPDATE",
                        (absence_id,))
            absence = cur.fetchone()
            if not absence:
                abort(404)
            if absence['statut'] != ABSENCE_JUSTIFICATIF_DEPOSE:
                flash("Ce justificatif a déjà été traité ou n'a pas été déposé.", 'warning')
                return redirect(url_for('absences'))

            cible = _cible_employe(cur, absence['employe_id'])
            if decision == 'accepter':
                # L'absence reste dans son dossier d'audit, mais elle est
                # requalifiée en congé maladie approuvé pour les calendriers et
                # rapports RH. Ce type ne consomme pas le solde de congés payés.
                cur.execute("""
                    INSERT INTO conges
                        (employe_id, type_conge, date_debut, date_fin,
                         nombre_jours, motif, statut, date_demande,
                         demande_par_id, decide_par, decide_le)
                    VALUES (%s, 'congé maladie', %s, %s, 1, %s, 'approuvé',
                            CURRENT_DATE, %s, %s, CURRENT_DATE)
                    RETURNING id
                """, (absence['employe_id'], absence['date'], absence['date'],
                      f"Requalification de l'absence #{absence_id} après justificatif accepté",
                      cible.get('user_id') if cible else None,
                      session.get('username')))
                conge_id = cur.fetchone()['id']
                cur.execute("""
                    UPDATE absences
                       SET statut = %s, decide_par = %s,
                           decide_le = CURRENT_TIMESTAMP, motif_refus = NULL,
                           conge_id = %s
                     WHERE id = %s
                """, (ABSENCE_ACCEPTEE, session.get('user_id'), conge_id,
                      absence_id))
                titre = "Justificatif d'absence accepté"
                message = (f"Votre justificatif pour le {absence['date']} a été accepté. "
                           "L'absence est requalifiée en congé maladie.")
                type_notification = 'success'
            else:
                cur.execute("""
                    UPDATE absences
                       SET statut = %s, decide_par = %s,
                           decide_le = CURRENT_TIMESTAMP, motif_refus = %s,
                           conge_id = NULL
                     WHERE id = %s
                """, (ABSENCE_REFUSEE, session.get('user_id'), motif_refus,
                      absence_id))
                titre = "Justificatif d'absence refusé"
                message = (f"Votre justificatif pour le {absence['date']} a été refusé : "
                           f"{motif_refus}")
                type_notification = 'danger'

            if cible and cible.get('user_id'):
                create_notification(cible['user_id'], titre, message,
                                    type_notification, cur=cur)
            if cible:
                queue_email(
                    cible.get('email'), titre, message, cur=cur,
                    event_key=f"absence-decision:{absence_id}:{decision}")

        log_action(session.get('user_id'), session.get('username'),
                   "Décision justificatif absence", 'absence', absence_id,
                   decision if decision == 'accepter' else f"refuser: {motif_refus[:120]}")
        flash("Justificatif accepté et absence requalifiée en congé maladie."
              if decision == 'accepter'
              else "Justificatif refusé : l'employé a été informé.",
              'success' if decision == 'accepter' else 'info')
        return redirect(url_for('absences'))

    return bp
