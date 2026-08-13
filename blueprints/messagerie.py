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
MESSAGE_MAX_CHARS = 20_000
TITRE_MAX_CHARS = 200

ROLES_VALIDES = ('admin', 'rh', 'manager', 'technicien', 'employe')
ROLES_GLOBAUX = ('admin', 'rh')


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
    department_scope_sql = deps['department_scope_sql']

    # ---------------------------------------------------------------- utils
    def _est_membre(cur, conversation_id, user_id):
        cur.execute("""
            SELECT 1 FROM conversation_membres
            WHERE conversation_id = %s AND user_id = %s
        """, (conversation_id, user_id))
        return cur.fetchone() is not None

    def _peut_voir_annonce(conv, role, user_id):
        # Admin/RH administrent toutes les annonces, y compris celles destinées
        # à un autre rôle. Le créateur conserve également toujours l'accès.
        return (role in ROLES_GLOBAUX or conv.get('cree_par') == user_id
                or conv['cible_role'] is None or conv['cible_role'] == role)

    def _peut_acceder(cur, conv, user_id, role):
        if conv['type'] == 'annonce':
            return _peut_voir_annonce(conv, role, user_id)
        return _est_membre(cur, conv['id'], user_id)

    def _ids_destinataires_autorises(cur, valeurs, sender_id):
        """Valide tous les destinataires, sans ignorer silencieusement les ID
        invalides ou hors département."""
        demandes = set()
        for valeur in valeurs:
            try:
                uid = int(valeur)
            except (TypeError, ValueError):
                return None
            if uid == sender_id:
                return None
            demandes.add(uid)
        if not demandes:
            return []

        scope_sql, scope_params = department_scope_sql('e', 'departement', cur)
        cur.execute(f"""
            SELECT u.id FROM users u
            LEFT JOIN employes e ON e.id = u.employe_id
            WHERE u.id = ANY(%s) AND u.id != %s AND {scope_sql}
        """, [list(demandes), sender_id] + scope_params)
        autorises = {row['id'] for row in cur.fetchall()}
        return sorted(autorises) if autorises == demandes else None

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
                      AND m.sender_id IS DISTINCT FROM %s
                      AND (cm.dernier_message_lu_id IS NULL OR m.id > cm.dernier_message_lu_id)
                )
                UNION
                SELECT c.id
                FROM conversations c
                WHERE c.type = 'annonce'
                  AND (%s IN ('admin','rh') OR c.cree_par = %s
                       OR c.cible_role IS NULL OR c.cible_role = %s)
                  AND NOT EXISTS (
                      SELECT 1 FROM annonce_lues al
                      WHERE al.conversation_id = c.id AND al.user_id = %s
                  )
                  AND EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)
            ) t
        """, (user_id, user_id, role, user_id, role, user_id))
        return cur.fetchone()['nb']

    def _charger_conversations(cur, user_id, role):
        """Charge la colonne de gauche commune à la boîte, au fil et à la
        composition d'un message."""
        cur.execute("""
            SELECT c.id, c.type, c.titre, c.cible_role, c.date_creation,
                   cm.dernier_message_lu_id,
                   (SELECT contenu FROM messages WHERE conversation_id = c.id
                    ORDER BY id DESC LIMIT 1) AS dernier_message,
                   (SELECT date_envoi FROM messages WHERE conversation_id = c.id
                    ORDER BY id DESC LIMIT 1) AS date_dernier_message,
                   (SELECT MAX(id) FROM messages WHERE conversation_id = c.id) AS dernier_message_id,
                   (SELECT string_agg(u.username, ', ' ORDER BY u.username)
                      FROM conversation_membres cm2 JOIN users u ON u.id = cm2.user_id
                     WHERE cm2.conversation_id = c.id AND cm2.user_id != %s) AS autres_membres,
                   (SELECT u.photo FROM conversation_membres cm3
                      JOIN users u ON u.id = cm3.user_id
                     WHERE cm3.conversation_id = c.id AND cm3.user_id != %s
                     ORDER BY u.username LIMIT 1) AS avatar_photo,
                   (SELECT u.username FROM conversation_membres cm4
                      JOIN users u ON u.id = cm4.user_id
                     WHERE cm4.conversation_id = c.id AND cm4.user_id != %s
                     ORDER BY u.username LIMIT 1) AS avatar_username
              FROM conversations c
              JOIN conversation_membres cm ON cm.conversation_id = c.id AND cm.user_id = %s
             WHERE c.type IN ('prive', 'groupe')
            UNION ALL
            SELECT c.id, c.type, c.titre, c.cible_role, c.date_creation,
                   NULL::INTEGER, (SELECT contenu FROM messages WHERE conversation_id=c.id ORDER BY id DESC LIMIT 1),
                   (SELECT date_envoi FROM messages WHERE conversation_id=c.id ORDER BY id DESC LIMIT 1),
                   (SELECT MAX(id) FROM messages WHERE conversation_id=c.id),
                   NULL, NULL, NULL
              FROM conversations c
             WHERE c.type='annonce'
               AND (%s IN ('admin','rh') OR c.cree_par=%s
                    OR c.cible_role IS NULL OR c.cible_role=%s)
             ORDER BY date_dernier_message DESC NULLS LAST
        """, (user_id, user_id, user_id, user_id, role, user_id, role))
        conversations = cur.fetchall()
        cur.execute("SELECT conversation_id FROM annonce_lues WHERE user_id=%s", (user_id,))
        annonces_lues = {r['conversation_id'] for r in cur.fetchall()}
        for conversation in conversations:
            apercu = conversation.get('dernier_message') or ''
            if len(apercu) > 80:
                apercu = apercu[:80] + '…'
            conversation['dernier_message'] = apercu
            if conversation['type'] == 'annonce':
                conversation['non_lu'] = bool(conversation['dernier_message_id']) and conversation['id'] not in annonces_lues
                conversation['libelle'] = conversation.get('titre') or 'Annonce RH'
                conversation['avatar_initiale'] = '📢'
            else:
                conversation['non_lu'] = bool(conversation['dernier_message_id']) and (
                    conversation['dernier_message_lu_id'] is None
                    or conversation['dernier_message_id'] > conversation['dernier_message_lu_id'])
                conversation['libelle'] = (conversation.get('titre') if conversation['type'] == 'groupe'
                                           else None) or conversation.get('autres_membres') or 'Conversation'
                conversation['avatar_initiale'] = ('👥' if conversation['type'] == 'groupe'
                                                    else (conversation.get('avatar_username') or '?')[:1].upper())
        return conversations

    # ------------------------------------------------------------- routes
    @bp.route('/messages')
    @login_required
    def messagerie_inbox():
        user_id = session['user_id']
        role = session.get('role', 'employe')
        with db_cursor() as (conn, cur):
            conversations = _charger_conversations(cur, user_id, role)
        return render_template('messagerie_inbox.html', conversations=conversations,
                               active_conversation_id=None)

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
            if len(titre) > TITRE_MAX_CHARS:
                flash(f"Le titre ne peut pas dépasser {TITRE_MAX_CHARS} caractères.", "danger")
                return redirect(url_for('messagerie.messagerie_nouveau'))
            if len(contenu) > MESSAGE_MAX_CHARS:
                flash(f"Le message ne peut pas dépasser {MESSAGE_MAX_CHARS} caractères.", "danger")
                return redirect(url_for('messagerie.messagerie_nouveau'))
            if cible_role and cible_role not in ROLES_VALIDES:
                flash("Rôle destinataire invalide.", "danger")
                return redirect(url_for('messagerie.messagerie_nouveau'))

            piece, erreur = _lire_piece_jointe(request.files.get('piece_jointe'), detect_file_type)
            if erreur:
                flash(erreur, "danger")
                return redirect(url_for('messagerie.messagerie_nouveau'))
            if not contenu and not piece:
                flash("Ajoutez un message ou une pièce jointe.", "danger")
                return redirect(url_for('messagerie.messagerie_nouveau'))

            if type_conv in ('prive', 'groupe') and not destinataires:
                flash("Choisissez au moins un destinataire.", "danger")
                return redirect(url_for('messagerie.messagerie_nouveau'))

            with db_cursor(commit=True) as (conn, cur):
                membres_ids = [session['user_id']]
                if type_conv in ('prive', 'groupe'):
                    autorises = _ids_destinataires_autorises(
                        cur, destinataires, session['user_id'])
                    if autorises is None:
                        abort(403)
                    if not autorises:
                        flash("Choisissez au moins un destinataire autorisé.", "danger")
                        return redirect(url_for('messagerie.messagerie_nouveau'))
                    if type_conv == 'prive' and len(autorises) > 1:
                        type_conv = 'groupe'
                    membres_ids.extend(autorises)

                cur.execute("""
                    INSERT INTO conversations (type, titre, cible_role, cree_par)
                    VALUES (%s, %s, %s, %s) RETURNING id
                """, (type_conv, titre or None, cible_role if type_conv == 'annonce' else None,
                      session['user_id']))
                conv_id = cur.fetchone()['id']

                if type_conv in ('prive', 'groupe'):
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
                    # Le créateur vient de lire son propre message : ne pas lui
                    # afficher un badge non-lu juste après l'envoi.
                    cur.execute("""
                        INSERT INTO annonce_lues (conversation_id, user_id)
                        VALUES (%s, %s) ON CONFLICT DO NOTHING
                    """, (conv_id, session['user_id']))
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
            scope_sql, scope_params = department_scope_sql('e', 'departement', cur)
            cur.execute(f"""
                SELECT u.id, u.username, u.role, e.nom, e.prenom, e.departement
                FROM users u LEFT JOIN employes e ON e.id = u.employe_id
                WHERE u.id != %s AND {scope_sql}
                ORDER BY u.username
            """, [session['user_id']] + scope_params)
            utilisateurs = cur.fetchall()
            conversations = _charger_conversations(cur, session['user_id'], role)

        return render_template('messagerie_nouveau.html', utilisateurs=utilisateurs,
                               conversations=conversations, active_conversation_id=None,
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
                       COALESCE(u.username, 'Utilisateur supprimé') AS sender_username,
                       u.photo AS sender_photo
                FROM messages m LEFT JOIN users u ON u.id = m.sender_id
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

            conversations = _charger_conversations(cur, user_id, role)
            active_conversation = next(
                (c for c in conversations if c['id'] == id),
                {'id': id, 'type': conv['type'], 'libelle': conv.get('titre') or 'Conversation',
                 'avatar_initiale': '💬', 'avatar_photo': None})

        return render_template('messagerie_thread.html', conv=conv, messages=messages,
                               membres=membres, user_id=user_id,
                               conversations=conversations,
                               active_conversation=active_conversation,
                               active_conversation_id=id)

    @bp.route('/messages/<int:id>/repondre', methods=['POST'])
    @login_required
    def messagerie_repondre(id):
        user_id = session['user_id']
        role = session.get('role', 'employe')
        contenu = request.form.get('contenu', '').strip()
        if len(contenu) > MESSAGE_MAX_CHARS:
            flash(f"Le message ne peut pas dépasser {MESSAGE_MAX_CHARS} caractères.", "danger")
            return redirect(url_for('messagerie.messagerie_voir', id=id))

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
            else:  # réponse admin/RH à une annonce
                # La mise à jour redevient non lue pour la cible ; son auteur
                # reste marqué comme lecteur et les destinataires sont notifiés.
                cur.execute("DELETE FROM annonce_lues WHERE conversation_id = %s", (id,))
                cur.execute("""INSERT INTO annonce_lues (conversation_id, user_id)
                               VALUES (%s,%s) ON CONFLICT DO NOTHING""", (id, user_id))
                query = """SELECT u.id, e.email FROM users u
                           LEFT JOIN employes e ON e.id=u.employe_id
                           WHERE u.id != %s"""
                params = [user_id]
                if conv.get('cible_role'):
                    query += " AND u.role = %s"
                    params.append(conv['cible_role'])
                cur.execute(query, params)
                for destinataire in cur.fetchall():
                    create_notification(
                        destinataire['id'],
                        f"📢 Annonce mise à jour : {conv.get('titre') or 'Annonce'}",
                        (contenu or '📎 Nouvelle pièce jointe')[:120], 'info', cur=cur)
                    if destinataire.get('email'):
                        queue_email(
                            destinataire['email'],
                            f"📢 {conv.get('titre') or 'Annonce'} — mise à jour",
                            contenu or 'Une nouvelle pièce jointe est disponible.', cur=cur)


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
        resp.headers['Cache-Control'] = 'private, no-store'
        return resp

    @bp.route('/messages/<int:id>/quitter', methods=['POST'])
    @login_required
    def messagerie_quitter(id):
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("SELECT type FROM conversations WHERE id = %s", (id,))
            conv = cur.fetchone()
            if not conv or conv['type'] != 'groupe':
                abort(404)
            if not _est_membre(cur, id, session['user_id']):
                abort(403)
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
