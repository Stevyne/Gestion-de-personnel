"""Authentification et espace personnel de l'utilisateur connecté."""

import io
import os

from flask import (Blueprint, flash, redirect, render_template, request,
                   send_file, session, url_for)
import psycopg2
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

CHAMPS_PROFIL_MODIFIABLES = ('nom', 'prenom', 'email', 'telephone')


def creer_blueprint_auth(deps):
    bp = Blueprint('auth', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    get_current_user_row = deps['get_current_user_row']
    get_current_employee = deps['get_current_employee']
    enregistrer_session = deps['enregistrer_session']
    cloturer_session = deps['cloturer_session']
    log_action = deps['log_action']
    get_role_label = deps['get_role_label']
    enregistrer_photo_profil = deps['enregistrer_photo_profil']
    supprimer_photo_profil = deps['supprimer_photo_profil']
    avatar_folder = deps['avatar_folder']
    limiter = deps['limiter']
    login_rate_limit = os.environ.get('LOGIN_RATE_LIMIT', '5 per minute;20 per hour')

    @bp.route('/login', methods=['GET','POST'])
    @limiter.limit(login_rate_limit, methods=['POST'], override_defaults=False)
    def login():
        if 'user_id' in session: return redirect(url_for('dashboard'))
        if request.method == 'POST':
            u = request.form.get('username','').strip()
            p = request.form.get('password','')
            with db_cursor() as (conn, cur):
                cur.execute("SELECT * FROM users WHERE username=%s", (u,))
                user = cur.fetchone()
            if user and user.get('actif', True) and check_password_hash(user['password_hash'], p):
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['role'] = user['role']
                session['role_label'] = get_role_label(user['role'])
                # Inscription au registre des sessions : permet l'indicateur de
                # présence et la déconnexion à distance par un administrateur.
                session['sid'] = enregistrer_session(user['id'], user['username'])
                log_action(user_id=user['id'], username=user['username'], action="LOGIN")
                flash(f'Bienvenue, {user["username"]} !', 'success')
                return redirect(url_for('dashboard'))
            flash('Identifiants ou mot de passe incorrects.', 'danger')
        return render_template('login.html')

    @bp.route('/logout')
    def logout():
        log_action(session.get('user_id'), session.get('username'), "LOGOUT")
        cloturer_session(session.get('sid'), par='(déconnexion volontaire)')
        session.clear()
        flash('Déconnecté.', 'success')
        return redirect(url_for('auth.login'))


    @bp.route('/mon-profil')
    @login_required
    def mon_profil():
        """Espace personnel : consultation de ses informations et de sa photo."""
        user = get_current_user_row()
        emp = get_current_employee()
        return render_template('mon_profil.html', user=user, emp=emp)


    @bp.route('/mon-profil/infos', methods=['POST'])
    @login_required
    def mon_profil_infos():
        """Mise à jour des informations personnelles de l'utilisateur connecté."""
        user = get_current_user_row()
        if not user or not user['employe_id']:
            flash("Aucune fiche employé n'est liée à votre compte.", "warning")
            return redirect(url_for('auth.mon_profil'))

        valeurs = {c: (request.form.get(c) or '').strip() for c in CHAMPS_PROFIL_MODIFIABLES}

        if not valeurs['nom'] or not valeurs['prenom']:
            flash("Le nom et le prénom sont obligatoires.", "danger")
            return redirect(url_for('auth.mon_profil'))

        email = valeurs['email']
        if email and ('@' not in email or '.' not in email.split('@')[-1]):
            flash("L'adresse email n'est pas valide.", "danger")
            return redirect(url_for('auth.mon_profil'))

        with db_cursor(commit=True) as (conn, cur):
            cur.execute("""
                UPDATE employes
                   SET nom = %s, prenom = %s, email = %s, telephone = %s
                 WHERE id = %s
            """, (valeurs['nom'], valeurs['prenom'], email or None,
                  valeurs['telephone'] or None, user['employe_id']))

        log_action(session.get('user_id'), session.get('username'),
                   "UPDATE_PROFIL", "employe", user['employe_id'],
                   "Mise à jour de ses informations personnelles")
        flash("Vos informations ont été enregistrées.", "success")
        return redirect(url_for('auth.mon_profil'))


    @bp.route('/mon-profil/photo', methods=['POST'])
    @login_required
    def mon_profil_photo():
        """Envoi ou remplacement de la photo de profil."""
        user = get_current_user_row()
        if not user:
            return redirect(url_for('auth.login'))

        nom, erreur, contenu = enregistrer_photo_profil(request.files.get('photo'), user['id'])
        if erreur:
            flash(erreur, "danger")
            return redirect(url_for('auth.mon_profil'))

        ancienne = user['photo']
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("UPDATE users SET photo = %s, photo_contenu = %s WHERE id = %s",
                        (nom, psycopg2.Binary(contenu), user['id']))
        supprimer_photo_profil(ancienne)   # on ne la retire qu'après la mise à jour

        log_action(session.get('user_id'), session.get('username'),
                   "UPDATE_PHOTO", "user", user['id'], "Photo de profil modifiée")
        flash("Votre photo de profil a été mise à jour.", "success")
        return redirect(url_for('auth.mon_profil'))


    @bp.route('/mon-profil/photo/supprimer', methods=['POST'])
    @login_required
    def mon_profil_photo_supprimer():
        """Retire la photo de profil et revient aux initiales."""
        user = get_current_user_row()
        if not user:
            return redirect(url_for('auth.login'))
        if not user['photo']:
            flash("Vous n'avez pas de photo de profil.", "info")
            return redirect(url_for('auth.mon_profil'))

        with db_cursor(commit=True) as (conn, cur):
            cur.execute("UPDATE users SET photo = NULL, photo_contenu = NULL WHERE id = %s", (user['id'],))
        supprimer_photo_profil(user['photo'])

        log_action(session.get('user_id'), session.get('username'),
                   "DELETE_PHOTO", "user", user['id'], "Photo de profil supprimée")
        flash("Votre photo de profil a été supprimée.", "success")
        return redirect(url_for('auth.mon_profil'))


    @bp.route('/avatar/<path:filename>')
    @login_required
    def avatar_image(filename):
        """Sert une photo de profil. Lit d'abord en base (persistant, survit à un
        redémarrage du service), puis retombe sur le disque local si besoin
        (photo uploadée avant ce correctif et jamais perdue depuis)."""
        filename = secure_filename(filename)
        with db_cursor() as (conn, cur):
            cur.execute("SELECT photo_contenu FROM users WHERE photo = %s LIMIT 1", (filename,))
            row = cur.fetchone()

        mimetype = 'image/png' if filename.lower().endswith('.png') else 'image/jpeg'

        if row and row.get('photo_contenu') is not None:
            resp = send_file(io.BytesIO(bytes(row['photo_contenu'])), mimetype=mimetype)
            resp.headers['Cache-Control'] = 'private, max-age=86400'
            return resp

        chemin = os.path.join(avatar_folder, filename)
        if os.path.dirname(os.path.abspath(chemin)) == os.path.abspath(avatar_folder) and os.path.isfile(chemin):
            resp = send_file(chemin, mimetype=mimetype)
            resp.headers['Cache-Control'] = 'private, max-age=86400'
            return resp

        # Photo perdue (redémarrage du service avant ce correctif) : image par
        # défaut plutôt qu'une icône cassée dans le navigateur.
        return redirect(url_for('static', filename='Logo.png'))


    @bp.route('/mon-profil/mot-de-passe', methods=['POST'])
    @login_required
    def mon_profil_mot_de_passe():
        """Changement de son propre mot de passe (ancien mot de passe exigé)."""
        actuel = request.form.get('mdp_actuel', '')
        nouveau = request.form.get('nouveau_mdp', '')
        confirmation = request.form.get('confirmer_mdp', '')

        if len(nouveau) < 6:
            flash("Le nouveau mot de passe doit contenir au moins 6 caractères.", "danger")
            return redirect(url_for('auth.mon_profil'))
        if nouveau != confirmation:
            flash("La confirmation ne correspond pas au nouveau mot de passe.", "danger")
            return redirect(url_for('auth.mon_profil'))

        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT password_hash FROM users WHERE id = %s", (session['user_id'],))
            row = cur.fetchone()
            if not row or not check_password_hash(row['password_hash'], actuel):
                flash("Votre mot de passe actuel est incorrect.", "danger")
                return redirect(url_for('auth.mon_profil'))
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                        (generate_password_hash(nouveau), session['user_id']))

        log_action(session.get('user_id'), session.get('username'),
                   "CHANGE_PASSWORD", "user", session['user_id'],
                   "Changement de son propre mot de passe")
        flash("Votre mot de passe a été modifié.", "success")
        return redirect(url_for('auth.mon_profil'))

    return bp
