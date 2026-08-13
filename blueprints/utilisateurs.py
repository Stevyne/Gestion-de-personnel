"""Administration des comptes, rôles, sessions et création d'utilisateurs."""

import re

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
import psycopg2
from werkzeug.security import generate_password_hash


def creer_blueprint_utilisateurs(deps):
    bp = Blueprint('utilisateurs', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    role_required = deps['role_required']
    log_action = deps['log_action']
    get_role_label = deps['get_role_label']
    create_notification = deps['create_notification']
    queue_email = deps['queue_email']
    session_online_window = deps['session_online_window']
    permanent_session_lifetime = deps['permanent_session_lifetime']
    logger = deps['logger']

    @bp.route('/utilisateurs')
    @login_required
    @role_required('admin', 'rh')
    def utilisateurs_page():
        """Page de gestion des comptes utilisateurs (admin/rh)."""
        with db_cursor() as (conn, cur):
            # On joint le registre des sessions pour connaître l'état de connexion
            # de chaque compte : nombre de sessions ouvertes et dernière activité.
            cur.execute("""
                SELECT u.id, u.username, u.role, u.employe_id, u.photo, e.nom, e.prenom,
                       COALESCE(s.nb_sessions, 0) AS nb_sessions,
                       s.last_seen,
                       (s.last_seen > CURRENT_TIMESTAMP - (%s * INTERVAL '1 minute')) AS en_ligne
                FROM users u
                LEFT JOIN employes e ON u.employe_id = e.id
                LEFT JOIN (
                    SELECT user_id, COUNT(*) AS nb_sessions, MAX(last_seen) AS last_seen
                      FROM sessions_actives
                     WHERE revoked_at IS NULL
                       -- Au-delà de la durée de vie d'une session Flask, le cookie
                       -- n'est plus valable : la session ne compte plus comme ouverte.
                       AND last_seen > CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                     GROUP BY user_id
                ) s ON s.user_id = u.id
                ORDER BY u.role, u.username
            """, (session_online_window, permanent_session_lifetime))
            users_list = cur.fetchall()
            cur.execute("SELECT id, nom, prenom FROM employes ORDER BY nom, prenom")
            employees = cur.fetchall()
        return render_template('utilisateurs.html', users=users_list, employees=employees)


    @bp.route('/utilisateurs/<int:user_id>/edit', methods=['POST'])
    @login_required
    @role_required('admin', 'rh')
    def edit_utilisateur(user_id):
        """Modifie le rôle et/ou l'employé lié d'un utilisateur.
        Seul un admin peut promouvoir/rétrograder vers ou depuis le rôle 'admin'."""
        nouveau_role = request.form.get('role', '').strip()
        employe_id = request.form.get('employe_id') or None
        roles_valides = ['admin', 'rh', 'manager', 'technicien', 'employe']

        if nouveau_role not in roles_valides:
            flash("Rôle invalide.", "danger")
            return redirect(url_for('utilisateurs.utilisateurs_page'))

        with db_cursor() as (conn, cur):
            cur.execute("SELECT username, role FROM users WHERE id = %s", (user_id,))
            cible = cur.fetchone()

        if not cible:
            flash("Utilisateur introuvable.", "danger")
            return redirect(url_for('utilisateurs.utilisateurs_page'))

        # Seul un admin peut attribuer ou retirer le rôle admin
        if (nouveau_role == 'admin' or cible['role'] == 'admin') and session.get('role') != 'admin':
            flash("Seul un administrateur peut modifier un compte administrateur.", "danger")
            return redirect(url_for('utilisateurs.utilisateurs_page'))

        # Empêche de se rétrograder soi-même par erreur (perte d'accès admin)
        if user_id == session.get('user_id') and nouveau_role != cible['role']:
            flash("Vous ne pouvez pas modifier votre propre rôle.", "danger")
            return redirect(url_for('utilisateurs.utilisateurs_page'))

        with db_cursor(commit=True) as (conn, cur):
            cur.execute("UPDATE users SET role = %s, employe_id = %s WHERE id = %s",
                        (nouveau_role, employe_id, user_id))

        log_action(session.get('user_id'), session.get('username'), "UPDATE_USER", "user", user_id,
                  f"{cible['username']} → rôle={nouveau_role}, employe_id={employe_id}")
        flash(f"Utilisateur '{cible['username']}' mis à jour.", "success")
        return redirect(url_for('utilisateurs.utilisateurs_page'))


    @bp.route('/utilisateurs/<int:user_id>/reset-password', methods=['POST'])
    @login_required
    @role_required('admin', 'rh')
    def reset_password_utilisateur(user_id):
        """Réinitialise le mot de passe d'un utilisateur (admin/rh)."""
        nouveau_mdp = request.form.get('nouveau_mdp', '')

        if len(nouveau_mdp) < 6:
            flash("Le mot de passe doit contenir au moins 6 caractères.", "danger")
            return redirect(url_for('utilisateurs.utilisateurs_page'))

        with db_cursor() as (conn, cur):
            cur.execute("SELECT username, role FROM users WHERE id = %s", (user_id,))
            cible = cur.fetchone()

        if not cible:
            flash("Utilisateur introuvable.", "danger")
            return redirect(url_for('utilisateurs.utilisateurs_page'))

        if cible['role'] == 'admin' and session.get('role') != 'admin':
            flash("Seul un administrateur peut réinitialiser le mot de passe d'un administrateur.", "danger")
            return redirect(url_for('utilisateurs.utilisateurs_page'))

        with db_cursor(commit=True) as (conn, cur):
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                        (generate_password_hash(nouveau_mdp), user_id))

        log_action(session.get('user_id'), session.get('username'), "RESET_PASSWORD", "user", user_id,
                  f"Mot de passe réinitialisé pour {cible['username']}")
        flash(f"Mot de passe de '{cible['username']}' réinitialisé.", "success")
        return redirect(url_for('utilisateurs.utilisateurs_page'))


    @bp.route('/utilisateurs/<int:user_id>/deconnecter', methods=['POST'])
    @login_required
    @role_required('admin')
    def deconnecter_utilisateur(user_id):
        """Ferme toutes les sessions ouvertes d'un utilisateur (admin uniquement).

        La révocation prend effet à la requête suivante de l'intéressé : son cookie
        reste dans son navigateur, mais il n'est plus accepté par le serveur.
        """
        if user_id == session.get('user_id'):
            flash("Utilisez le bouton Déconnexion pour fermer votre propre session.", "warning")
            return redirect(url_for('utilisateurs.utilisateurs_page'))

        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT username FROM users WHERE id = %s", (user_id,))
            cible = cur.fetchone()
            if not cible:
                flash("Utilisateur introuvable.", "danger")
                return redirect(url_for('utilisateurs.utilisateurs_page'))

            cur.execute("""
                UPDATE sessions_actives
                   SET revoked_at = CURRENT_TIMESTAMP, revoked_by = %s
                 WHERE user_id = %s AND revoked_at IS NULL
            """, (session.get('username'), user_id))
            nb = cur.rowcount

        if nb:
            log_action(session.get('user_id'), session.get('username'),
                       "FORCE_LOGOUT", "user", user_id,
                       f"{nb} session(s) fermée(s) pour {cible['username']}")
            create_notification(user_id, "Session fermée",
                                f"Votre session a été fermée par {session.get('username')}.",
                                "warning")
            flash(f"{nb} session(s) fermée(s) pour « {cible['username']} ».", "success")
        else:
            flash(f"« {cible['username']} » n'a aucune session ouverte.", "info")
        return redirect(url_for('utilisateurs.utilisateurs_page'))


    @bp.route('/utilisateurs/<int:user_id>/delete', methods=['POST'])
    @login_required
    @role_required('admin', 'rh')
    def delete_utilisateur(user_id):
        """Supprime un compte utilisateur, avec garde-fous de sécurité."""
        if user_id == session.get('user_id'):
            flash("Vous ne pouvez pas supprimer votre propre compte.", "danger")
            return redirect(url_for('utilisateurs.utilisateurs_page'))

        with db_cursor() as (conn, cur):
            cur.execute("SELECT username, role FROM users WHERE id = %s", (user_id,))
            cible = cur.fetchone()

            if not cible:
                flash("Utilisateur introuvable.", "danger")
                return redirect(url_for('utilisateurs.utilisateurs_page'))

            if cible['role'] == 'admin':
                if session.get('role') != 'admin':
                    flash("Seul un administrateur peut supprimer un compte administrateur.", "danger")
                    return redirect(url_for('utilisateurs.utilisateurs_page'))
                cur.execute("SELECT COUNT(*) as total FROM users WHERE role = 'admin'")
                if cur.fetchone()['total'] <= 1:
                    flash("Impossible de supprimer le dernier compte administrateur.", "danger")
                    return redirect(url_for('utilisateurs.utilisateurs_page'))

        with db_cursor(commit=True) as (conn, cur):
            cur.execute("DELETE FROM notifications WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))

        log_action(session.get('user_id'), session.get('username'), "DELETE_USER", "user", user_id,
                  f"Utilisateur '{cible['username']}' supprimé")
        flash(f"Utilisateur '{cible['username']}' supprimé.", "success")
        return redirect(url_for('utilisateurs.utilisateurs_page'))


    ROLES_CREATION_UTILISATEUR = [
        ('employe', 'Employé'),
        ('manager', 'Manager'),
        ('technicien', 'Technicien'),
        ('rh', 'Responsable RH'),
        ('admin', 'Administrateur'),
    ]


    def _contexte_creation_utilisateur(username='', role='employe', employe_id=''):
        """Données du formulaire, en excluant les salariés déjà liés à un compte."""
        with db_cursor() as (conn, cur):
            cur.execute("""
                SELECT e.id, e.nom, e.prenom, e.poste, e.departement
                  FROM employes e
                 WHERE NOT EXISTS (
                       SELECT 1 FROM users u WHERE u.employe_id = e.id
                 )
                 ORDER BY e.nom, e.prenom
            """)
            employes_disponibles = cur.fetchall()

        roles = ROLES_CREATION_UTILISATEUR
        if session.get('role') != 'admin':
            roles = [r for r in roles if r[0] != 'admin']
        return {
            'employees': employes_disponibles,
            'roles_creation': roles,
            'form_values': {
                'username': username,
                'role': role,
                'employe_id': str(employe_id or ''),
            },
        }


    @bp.route('/register', methods=['GET', 'POST'])
    @login_required
    @role_required('admin', 'rh')
    def register():
        """Crée en une étape un compte opérationnel, avec rôle et salarié lié."""
        if request.method == 'GET':
            return render_template('register.html', **_contexte_creation_utilisateur())

        # Les identifiants sont normalisés en minuscules : la connexion est ainsi
        # prévisible et la contrainte UNIQUE bloque aussi les variantes de casse.
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        role = request.form.get('role', 'employe').strip()
        employe_brut = request.form.get('employe_id', '').strip()
        employe_id = int(employe_brut) if employe_brut.isdigit() else None
        contexte = lambda: _contexte_creation_utilisateur(username, role, employe_brut)

        if not username or not password or not confirm_password:
            flash("Veuillez remplir tous les champs obligatoires.", "danger")
            return render_template('register.html', **contexte())
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{2,79}', username):
            flash("L'identifiant doit contenir 3 à 80 caractères : lettres non accentuées, "
                  "chiffres, point, tiret ou soulignement.", "danger")
            return render_template('register.html', **contexte())
        if len(password) < 8 or len(password) > 128:
            flash("Le mot de passe doit contenir entre 8 et 128 caractères.", "danger")
            return render_template('register.html', **contexte())
        if password != confirm_password:
            flash("Les mots de passe ne correspondent pas.", "danger")
            return render_template('register.html', **contexte())

        roles_valides = {code for code, _ in ROLES_CREATION_UTILISATEUR}
        if role not in roles_valides:
            flash("Rôle invalide.", "danger")
            return render_template('register.html', **contexte())
        if role == 'admin' and session.get('role') != 'admin':
            flash("Seul un administrateur peut créer un compte administrateur.", "danger")
            return render_template('register.html', **contexte())
        if employe_brut and employe_id is None:
            flash("Employé invalide.", "danger")
            return render_template('register.html', **contexte())

        user_id = None
        try:
            with db_cursor(commit=True) as (conn, cur):
                # Le verrou sur la fiche salarié sérialise deux créations
                # concurrentes qui tenteraient de rattacher la même personne.
                employe = None
                if employe_id is not None:
                    cur.execute("""
                        SELECT id, nom, prenom, email FROM employes
                         WHERE id = %s FOR UPDATE
                    """, (employe_id,))
                    employe = cur.fetchone()
                    if not employe:
                        flash("Employé introuvable.", "danger")
                        return render_template('register.html', **contexte())
                    cur.execute("SELECT username FROM users WHERE employe_id = %s LIMIT 1",
                                (employe_id,))
                    compte_existant = cur.fetchone()
                    if compte_existant:
                        flash("Cet employé est déjà lié au compte "
                              f"« {compte_existant['username']} ».", "danger")
                        return render_template('register.html', **contexte())

                cur.execute("SELECT id FROM users WHERE LOWER(username) = %s", (username,))
                if cur.fetchone():
                    flash("Ce nom d'utilisateur est déjà utilisé.", "danger")
                    return render_template('register.html', **contexte())

                cur.execute("""
                    INSERT INTO users (username, password_hash, role, employe_id)
                    VALUES (%s, %s, %s, %s) RETURNING id
                """, (username, generate_password_hash(password), role, employe_id))
                user_id = cur.fetchone()['id']
                create_notification(
                    user_id, "Votre compte a été créé",
                    "Votre accès à Gestion du Personnel est actif. Pensez à modifier "
                    "votre mot de passe après votre première connexion.",
                    'success', cur=cur)
                if employe:
                    queue_email(
                        employe.get('email'), "Votre accès Gestion du Personnel est créé",
                        f"Bonjour {employe['prenom']},\n\nVotre compte « {username} » est actif. "
                        "Contactez les RH pour recevoir votre mot de passe initial, puis "
                        "modifiez-le après votre première connexion.",
                        cur=cur, event_key=f"compte-cree:{user_id}")
        except psycopg2.errors.UniqueViolation:
            # Garde-fou pour deux requêtes strictement simultanées.
            flash("Ce nom d'utilisateur est déjà utilisé.", "danger")
            return render_template('register.html', **contexte())
        except Exception as e:
            logger.error("Erreur register: %s", e, exc_info=True)
            flash("Le compte n'a pas pu être créé. Réessayez ou contactez l'administrateur.",
                  "danger")
            return render_template('register.html', **contexte())

        log_action(session.get('user_id'), session.get('username'),
                   "CREATE_USER", "user", user_id,
                   f"username={username}, role={role}, employe_id={employe_id}")
        flash(f"Compte « {username} » créé avec le rôle {get_role_label(role)}.", "success")
        return redirect(url_for('utilisateurs.utilisateurs_page'))

    return bp
