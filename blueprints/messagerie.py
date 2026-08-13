"""Messagerie interne : messages privés, discussions de groupe, annonces RH.

Un « petit réseau social » interne à l'application : chaque utilisateur peut
démarrer une conversation privée avec un collègue, créer un groupe à plusieurs,
ou (pour admin/rh) diffuser une annonce à tous ou à un rôle donné. Les pièces
jointes sont stockées en base (BYTEA), comme les documents et les photos de
profil : le disque local du service est éphémère sur Render.

Suit le même patron que ``blueprints/absence_justifications.py`` : les
dépendances (base, auth, notifications, e-mails, audit) sont injectées par la
fabrique pour éviter tout import circulaire avec ``app.py``.
"""

import io
import psycopg2

from flask import (Blueprint, abort, flash, redirect, render_template,
                    request, send_file, session, url_for)
from werkzeug.utils import secure_filename


PIECE_JOINTE_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'doc', 'docx', 'xls', 'xlsx', 'txt'}
PIECE_JOINTE_MAX_BYTES = 8 * 1024 * 1024  # 8 Mo, cohérent avec les autres pièces jointes de l'app

ROLES_VALIDES = ('admin', 'rh', 'manager', 'employe')


def _lire_piece_jointe(fichier, detect_file_type):
    """Valide le nom, la taille et les magic-bytes d'une pièce jointe.
    Retourne (dict, None) ou (None, message_erreur). dict est None si aucun
    fichier n'a été fourni (une pièce jointe est toujours optionnelle ici)."""
    if not fichier or not fichier.filename:
        return None, None
    ext = fichier.filename.rsplit('.', 1)[-1].lower() if '.' in fichier.filename else ''
    if ext not in PIECE_JOINTE_EXTENSIONS:
        return None, "Format de pièce jointe non autorisé."

    fichier.stream.seek(0, 2)
    taille = fichier.stream.tell()
    fichier.stream.seek(0)
    if taille <= 0:
        return None, None
    if taille > PIECE_JOINTE_MAX_BYTES:
        return None, f"Pièce jointe trop volumineuse (max {PIECE_JOINTE_MAX_BYTES // (1024 * 1024)} Mo)."
    if detect_file_type(fichier) is None:
        return None, "Le contenu de la pièce jointe ne correspond pas à son extension."

    fichier.stream.seek(0)
    contenu = fichier.stream.read()
    nom = secure_filename(fichier.filename)[:255] or f"fichier.{ext}"
    return {'nom': nom, 'type': ext, 'taille': len(contenu), 'contenu': contenu}, None


def creer_blueprint_messagerie(deps):
    """Construit le Blueprint avec les services fournis par ``app.py``."""
    bp = Blueprint('messagerie', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    detect_file_type = deps['detect_file_type']
    create_notification = deps['create_notification']
    queue_email = deps['queue_email']
    log_action = deps['log_action']

    # ---------------------------------------------------------------- utils
    def _est_membre(cur, conversation_id, user_id):
        cur.execute("""
            SELECT 1 FROM conversation_membres
            WHERE conversation_id = %s AND user_id = %s
        """, (conversation_id, user_id))
        return cur.fetchone() is not None

    def _peut_voir_annonce(conv, role):
        return conv['cible_role'] is None or conv['cible_role'] == role

    def _peut_acceder(cur, conv, user_id, role):
        if conv['type'] == 'annonce':
            return _peut_voir_annonce(conv, role)
        return _est_membre(cur, conv['id'], user_id)

    def _nb_non_lus(cur, user_id, role):
        """Nombre total de conversations (privé/groupe + annonces) avec au
        moins un message non lu, pour le badge de la navbar."""
        cur.execute("""
            SELECT COUNT(*) AS nb FROM (
                SELECT c.id
                FROM conversations c
                JOIN conversation_membres cm ON cm.conversation_id = c.id AND cm.user_id = %s
                WHERE EXISTS (
                    SELECT 1 FROM messages m
                    WHERE m.conversation_id = c.id
                      AND m.sender_id != %s
                      AND (cm.dernier_message_lu_id IS NULL OR m.id > cm.dernier_message_lu_id)
                )
                UNION
                SELECT c.id
                FROM conversations c
                WHERE c.type = 'annonce' AND (c.cible_role IS NULL OR c.cible_role = %s)
                  AND NOT EXISTS (
                      SELECT 1 FROM annonce_lues al
                      WHERE al.conversation_id = c.id AND al.user_id = %s
                  )
                  AND EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)
            ) t
        """, (user_id, user_id, role, user_id))
        return cur.fetchone()['nb']

    # ------------------------------------------------------------- routes
    @bp.route('/messages')
    @login_required
    def messagerie_inbox():
        user_id = session['user_id']
        role = session.get('role', 'employe')
        with db_cursor() as (conn, cur):
            cur.execute("""
                SELECT c.id, c.type, c.titre, c.cible_role, c.date_creation,
                       cm.dernier_message_lu_id,
                       (SELECT contenu FROM messages WHERE conversation_id = c.id
                        ORDER BY id DESC LIMIT 1) AS dernier_message,
                       (SELECT date_envoi FROM messages WHERE conversation_id = c.id
                        ORDER BY id DESC LIMIT 1) AS date_dernier_message,
                       (SELECT MAX(id) FROM messages WHERE conversation_id = c.id) AS dernier_message_id,
                       (SELECT string_agg(u.username, ', ') FROM conversation_membres cm2
                        JOIN users u ON u.id = cm2.user_id
                        WHERE cm2.conversation_id = c.id AND cm2.user_id != %s) AS autres_membres
                FROM conversations c
                JOIN conversation_membres cm ON cm.conversation_id = c.id AND cm.user_id = %s
                WHERE c.type IN ('prive', 'groupe')
                UNION ALL
                SELECT c.id, c.type, c.titre, c.cible_role, c.date_creation,
                       NULL::INTEGER AS dernier_message_lu_id,
                       (SELECT contenu FROM messages WHERE conversation_id = c.id
                        ORDER BY id DESC LIMIT 1) AS dernier_message,
                       (SELECT date_envoi FROM messages WHERE conversation_id = c.id
                        ORDER BY id DESC LIMIT 1) AS date_dernier_message,
                       (SELECT MAX(id) FROM messages WHERE conversation_id = c.id) AS dernier_message_id,
                       NULL AS autres_membres
                FROM conversations c
                WHERE c.type = 'annonce' AND (c.cible_role IS NULL OR c.cible_role = %s)
                ORDER BY date_dernier_message DESC NULLS LAST
            """, (user_id, user_id, role))
            conversations = cur.fetchall()

            # Lu/non-lu par conversation (annonces : table dédiée, pas de pointeur)
            cur.execute("SELECT conversation_id FROM annonce_lues WHERE user_id = %s", (user_id,))
            annonces_lues = {r['conversation_id'] for r in cur.fetchall()}

        for c in conversations:
            if c['dernier_message'] and len(c['dernier_message']) > 80:
                c['dernier_message'] = c['dernier_message'][:80] + '…'
            if c['type'] == 'annonce':
                c['non_lu'] = bool(c['dernier_message_id']) and c['id'] not in annonces_lues
            else:
                c['non_lu'] = bool(c['dernier_message_id']) and (
                    c['dernier_message_lu_id'] is None or c['dernier_message_id'] > c['dernier_message_lu_id']
                )

        return render_template('messagerie_inbox.html', conversations=conversations)

    @bp.route('/messages/nouveau', methods=['GET', 'POST'])
    @login_required
    def messagerie_nouveau():
        role = session.get('role', 'employe')
        peut_annoncer = role in ('admin', 'rh')

        if request.method == 'POST':
            type_conv = request.form.get('type', 'prive').strip()
            if type_conv not in ('prive', 'groupe', 'annonce'):
                type_conv = 'prive'
            if type_conv == 'annonce' and not peut_annoncer:
                abort(403)

            titre = request.form.get('titre', '').strip()
            contenu = request.form.get('contenu', '').strip()
            destinataires = request.form.getlist('destinataires')  # user_id (str) pour prive/groupe
            cible_role = request.form.get('cible_role', '').strip() or None
            if cible_role and cible_role not in ROLES_VALIDES:
                cible_role = None

            if not contenu:
                flash("Le message ne peut pas être vide.", "danger")
                return redirect(url_for('messagerie.messagerie_nouveau'))

            if type_conv in ('prive', 'groupe') and not destinataires:
                flash("Choisissez au moins un destinataire.", "danger")
                return redirect(url_for('messagerie.messagerie_nouveau'))

            if type_conv == 'prive' and len(destinataires) > 1:
                type_conv = 'groupe'  # plusieurs destinataires = groupe, même sans le demander explicitement

            piece, erreur = _lire_piece_jointe(request.files.get('piece_jointe'), detect_file_type)
            if erreur:
                flash(erreur, "danger")
                return redirect(url_for('messagerie.messagerie_nouveau'))

            with db_cursor(commit=True) as (conn, cur):
                cur.execute("""
                    INSERT INTO conversations (type, titre, cible_role, cree_par)
                    VALUES (%s, %s, %s, %s) RETURNING id
                """, (type_conv, titre or None, cible_role if type_conv == 'annonce' else None,
                      session['user_id']))
                conv_id = cur.fetchone()['id']

                membres_ids = [session['user_id']]
                if type_conv in ('prive', 'groupe'):
                    for uid in destinataires:
                        try:
                            uid = int(uid)
                        except (TypeError, ValueError):
                            continue
                        if uid not in membres_ids:
                            membres_ids.append(uid)
                    for uid in membres_ids:
                        cur.execute("""
                            INSERT INTO conversation_membres (conversation_id, user_id)
                            VALUES (%s, %s) ON CONFLICT DO NOTHING
                        """, (conv_id, uid))

                cur.execute("""
                    INSERT INTO messages (conversation_id, sender_id, contenu,
                                          piece_jointe_nom, piece_jointe_type,
                                          piece_jointe_taille, piece_jointe_contenu)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
                """, (conv_id, session['user_id'], contenu,
                      piece['nom'] if piece else None, piece['type'] if piece else None,
                      piece['taille'] if piece else None,
                      psycopg2.Binary(piece['contenu']) if piece else None))
                message_id = cur.fetchone()['id']

                if type_conv in ('prive', 'groupe'):
                    cur.execute("""
                        UPDATE conversation_membres SET dernier_message_lu_id = %s
                        WHERE conversation_id = %s AND user_id = %s
                    """, (message_id, conv_id, session['user_id']))
                    for uid in membres_ids:
                        if uid == session['user_id']:
                            continue
                        create_notification(
                            uid, f"Nouveau message de {session.get('username')}",
                            contenu[:120], "info", cur=cur
                        )
                        cur.execute("""
                            SELECT e.email FROM users u LEFT JOIN employes e ON e.id = u.employe_id
                            WHERE u.id = %s
                        """, (uid,))
                        r = cur.fetchone()
                        if r and r.get('email'):
                            queue_email(r['email'], f"Nouveau message de {session.get('username')}",
                                       contenu, cur=cur)
                else:  # annonce
                    query = "SELECT u.id, e.email FROM users u LEFT JOIN employes e ON e.id = u.employe_id"
                    params = ()
                    if cible_role:
                        query += " WHERE u.role = %s"
                        params = (cible_role,)
                    cur.execute(query, params)
                    for r in cur.fetchall():
                        if r['id'] == session['user_id']:
                            continue
                        create_notification(
                            r['id'], f"📢 Annonce : {titre or 'Nouvelle annonce'}",
                            contenu[:120], "info", cur=cur
                        )
                        if r.get('email'):
                            queue_email(r['email'], f"📢 {titre or 'Annonce'}", contenu, cur=cur)

                log_action(session.get('user_id'), session.get('username'),
                          "CREATE_CONVERSATION", "conversation", conv_id,
                          f"{type_conv} : {titre or contenu[:40]}")

            flash("Message envoyé.", "success")
            return redirect(url_for('messagerie.messagerie_voir', id=conv_id))

        with db_cursor() as (conn, cur):
            cur.execute("""
                SELECT u.id, u.username, u.role, e.nom, e.prenom
                FROM users u LEFT JOIN employes e ON e.id = u.employe_id
                WHERE u.id != %s ORDER BY u.username
            """, (session['user_id'],))
            utilisateurs = cur.fetchall()

        return render_template('messagerie_nouveau.html', utilisateurs=utilisateurs,
                               peut_annoncer=peut_annoncer, roles=ROLES_VALIDES)

    @bp.route('/messages/<int:id>')
    @login_required
    def messagerie_voir(id):
        user_id = session['user_id']
        role = session.get('role', 'employe')
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT * FROM conversations WHERE id = %s", (id,))
            conv = cur.fetchone()
            if not conv:
                abort(404)
            if not _peut_acceder(cur, conv, user_id, role):
                flash("Vous n'avez pas accès à cette conversation.", "danger")
                return redirect(url_for('messagerie.messagerie_inbox'))

            cur.execute("""
                SELECT m.id, m.contenu, m.date_envoi, m.sender_id,
                       m.piece_jointe_nom, m.piece_jointe_taille,
                       u.username AS sender_username
                FROM messages m JOIN users u ON u.id = m.sender_id
                WHERE m.conversation_id = %s ORDER BY m.id ASC
            """, (id,))
            messages = cur.fetchall()

            dernier_id = messages[-1]['id'] if messages else None
            if conv['type'] == 'annonce':
                if dernier_id:
                    cur.execute("""
                        INSERT INTO annonce_lues (conversation_id, user_id)
                        VALUES (%s, %s) ON CONFLICT (conversation_id, user_id) DO NOTHING
                    """, (id, user_id))
            else:
                if dernier_id:
                    cur.execute("""
                        UPDATE conversation_membres SET dernier_message_lu_id = %s
                        WHERE conversation_id = %s AND user_id = %s
                    """, (dernier_id, id, user_id))

            membres = []
            if conv['type'] in ('prive', 'groupe'):
                cur.execute("""
                    SELECT u.username FROM conversation_membres cm
                    JOIN users u ON u.id = cm.user_id
                    WHERE cm.conversation_id = %s ORDER BY u.username
                """, (id,))
                membres = [r['username'] for r in cur.fetchall()]

        return render_template('messagerie_thread.html', conv=conv, messages=messages,
                               membres=membres, user_id=user_id)

    @bp.route('/messages/<int:id>/repondre', methods=['POST'])
    @login_required
    def messagerie_repondre(id):
        user_id = session['user_id']
        role = session.get('role', 'employe')
        contenu = request.form.get('contenu', '').strip()

        piece, erreur = _lire_piece_jointe(request.files.get('piece_jointe'), detect_file_type)
        if erreur:
            flash(erreur, "danger")
            return redirect(url_for('messagerie.messagerie_voir', id=id))
        if not contenu and not piece:
            return redirect(url_for('messagerie.messagerie_voir', id=id))

        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT * FROM conversations WHERE id = %s", (id,))
            conv = cur.fetchone()
            if not conv or not _peut_acceder(cur, conv, user_id, role):
                abort(403)
            if conv['type'] == 'annonce' and role not in ('admin', 'rh'):
                # Une annonce est à sens unique : seuls admin/rh peuvent y répondre
                # (ce qui ajoute un message visible par toute la cible).
                abort(403)

            cur.execute("""
                INSERT INTO messages (conversation_id, sender_id, contenu,
                                      piece_jointe_nom, piece_jointe_type,
                                      piece_jointe_taille, piece_jointe_contenu)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (id, user_id, contenu or '',
                  piece['nom'] if piece else None, piece['type'] if piece else None,
                  piece['taille'] if piece else None,
                  psycopg2.Binary(piece['contenu']) if piece else None))
            message_id = cur.fetchone()['id']

            if conv['type'] in ('prive', 'groupe'):
                cur.execute("""
                    UPDATE conversation_membres SET dernier_message_lu_id = %s
                    WHERE conversation_id = %s AND user_id = %s
                """, (message_id, id, user_id))
                cur.execute("""
                    SELECT u.id, e.email FROM conversation_membres cm
                    JOIN users u ON u.id = cm.user_id
                    LEFT JOIN employes e ON e.id = u.employe_id
                    WHERE cm.conversation_id = %s AND cm.user_id != %s
                """, (id, user_id))
                for r in cur.fetchall():
                    create_notification(r['id'], f"Nouveau message de {session.get('username')}",
                                        (contenu or '📎 Pièce jointe')[:120], "info", cur=cur)
                    if r.get('email'):
                        queue_email(r['email'], f"Nouveau message de {session.get('username')}",
                                   contenu or 'Pièce jointe envoyée.', cur=cur)
            else:  # réponse d'admin/rh à une annonce : on retire tout le monde du "lu"
                # pour que la cible voie qu'il y a une mise à jour à consulter.
                cur.execute("""
                    DELETE FROM annonce_lues WHERE conversation_id = %s AND user_id != %s
                """, (id, user_id))

            log_action(session.get('user_id'), session.get('username'),
                      "REPLY_MESSAGE", "conversation", id, contenu[:60] if contenu else "Pièce jointe")

        return redirect(url_for('messagerie.messagerie_voir', id=id))

    @bp.route('/messages/piece-jointe/<int:message_id>')
    @login_required
    def messagerie_piece_jointe(message_id):
        user_id = session['user_id']
        role = session.get('role', 'employe')
        with db_cursor() as (conn, cur):
            cur.execute("""
                SELECT m.piece_jointe_nom, m.piece_jointe_contenu, m.conversation_id
                FROM messages m WHERE m.id = %s
            """, (message_id,))
            msg = cur.fetchone()
            if not msg or msg.get('piece_jointe_contenu') is None:
                abort(404)
            cur.execute("SELECT * FROM conversations WHERE id = %s", (msg['conversation_id'],))
            conv = cur.fetchone()
            if not conv or not _peut_acceder(cur, conv, user_id, role):
                abort(403)

        filename = secure_filename(msg['piece_jointe_nom'])
        resp = send_file(io.BytesIO(bytes(msg['piece_jointe_contenu'])),
                         as_attachment=True, download_name=filename)
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        return resp

    @bp.route('/messages/<int:id>/quitter', methods=['POST'])
    @login_required
    def messagerie_quitter(id):
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT type FROM conversations WHERE id = %s", (id,))
            conv = cur.fetchone()
            if not conv or conv['type'] != 'groupe':
                abort(404)
            cur.execute("""
                DELETE FROM conversation_membres WHERE conversation_id = %s AND user_id = %s
            """, (id, session['user_id']))
        flash("Vous avez quitté la discussion.", "success")
        return redirect(url_for('messagerie.messagerie_inbox'))

    @bp.app_context_processor
    def inject_messagerie_badge():
        if 'user_id' not in session:
            return {'messages_non_lus': 0}
        try:
            with db_cursor() as (conn, cur):
                nb = _nb_non_lus(cur, session['user_id'], session.get('role', 'employe'))
        except Exception:
            nb = 0
        return {'messages_non_lus': nb}

    return bp
