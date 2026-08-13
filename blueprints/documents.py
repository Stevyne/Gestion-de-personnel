"""Documents RH : dépôt, liste, suppression et téléchargement protégé."""

from datetime import date, datetime, timedelta
import os

from flask import (Blueprint, flash, redirect, render_template, request,
                   send_file, session, url_for)
import psycopg2
from werkzeug.utils import secure_filename

from services.object_storage import ObjectStorageError

GLOBAL_DATA_ROLES = ('admin', 'rh')


def creer_blueprint_documents(deps):
    """Construit le Blueprint Documents avec ses services partagés."""
    bp = Blueprint('documents', __name__)
    db_cursor = deps['db_cursor']
    get_db = deps['get_db']
    get_cursor = deps['get_cursor']
    login_required = deps['login_required']
    role_required = deps['role_required']
    get_current_employee = deps['get_current_employee']
    allowed_file = deps['allowed_file']
    detect_file_type = deps['detect_file_type']
    log_action = deps['log_action']
    _notifier_employe_evenement = deps['notifier_employe_evenement']
    department_scope_sql = deps['department_scope_sql']
    upload_folder = deps['upload_folder']
    seuil_expiration = deps['seuil_expiration']
    object_storage = deps['object_storage']

    def _traiter_upload_document(conn, cur, employe_id, titre, description, date_expiration):
        """Valide et enregistre un document uploadé (fichier dans request.files).
        Retourne (ok: bool, message: str). Réutilisée par /documents et par
        l'upload direct depuis la fiche employé.

        Le stockage est hybride : petit BYTEA PostgreSQL ou objet S3 privé au-
        delà du seuil configuré. Le disque local Render n'est jamais utilisé
        pour un nouvel upload.
        """
        if 'fichier' not in request.files or request.files['fichier'].filename == '':
            return False, 'Aucun fichier sélectionné'

        fichier = request.files['fichier']
        if not (fichier and allowed_file(fichier.filename)):
            return False, 'Type de fichier non autorisé'

        # Validation du CONTENU (pas seulement de l'extension) pour éviter qu'un
        # fichier malveillant ne se déguise en image/document.
        detected = detect_file_type(fichier)
        if detected is None:
            return False, 'Le contenu du fichier ne correspond pas à son extension (type non autorisé).'

        filename = secure_filename(fichier.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{timestamp}_{filename}"
        contenu = fichier.stream.read()
        try:
            stored = object_storage.store(
                'documents', contenu, filename,
                content_type=f'application/{detected}' if detected == 'pdf' else None,
            )
        except ObjectStorageError as exc:
            return False, str(exc)

        try:
            cur.execute("""
                INSERT INTO documents
                    (employe_id, titre, nom_fichier, chemin_fichier, type_fichier,
                     taille, description, date_expiration, contenu, storage_key,
                     storage_etag, storage_sha256)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (employe_id, titre or filename, filename, filename,
                  filename.rsplit('.', 1)[1].lower(), len(contenu), description,
                  date_expiration,
                  psycopg2.Binary(stored.content) if stored.content is not None else None,
                  stored.storage_key, stored.storage_etag, stored.storage_sha256))
            document_id = cur.fetchone()['id']
            if employe_id:
                expiration = f" Il expire le {date_expiration}." if date_expiration else ""
                _notifier_employe_evenement(
                    cur, int(employe_id), "Nouveau document dans votre dossier",
                    f"Le document « {titre or filename} » a été ajouté à votre dossier.{expiration}",
                    'info', cle_evenement=f"document-ajoute:{document_id}",
                )
            conn.commit()
        except Exception:
            conn.rollback()
            if stored.external:
                object_storage.delete(stored.storage_key)
            raise
        log_action(session.get('user_id'), session.get('username'), "UPLOAD_DOCUMENT", "document", document_id, f"{titre} ({filename})")
        return True, 'Document uploadé avec succès'


    @bp.route('/documents', methods=['GET', 'POST'])
    @login_required
    def documents():
        emp = get_current_employee()
        conn = get_db()
        cur = get_cursor(conn)
        employee_scope, employee_scope_params = department_scope_sql('e', cur=cur)
        if session.get('role') in GLOBAL_DATA_ROLES:
            document_scope, document_scope_params = 'TRUE', []
        elif session.get('role') == 'manager':
            document_scope, document_scope_params = employee_scope, employee_scope_params
        elif emp:
            document_scope, document_scope_params = 'd.employe_id = %s', [emp['id']]
        else:
            document_scope, document_scope_params = 'FALSE', []

        if request.method == 'POST':
            titre = request.form.get('titre', '').strip()
            description = request.form.get('description', '').strip()
            employe_id = request.form.get('employe_id') or (emp['id'] if emp else None)
            date_expiration = request.form.get('date_expiration') or None

            ok, message = _traiter_upload_document(conn, cur, employe_id, titre, description, date_expiration)
            flash(message, 'success' if ok else 'danger')

        # Liste des employés et documents dans la portée autorisée.
        cur.execute(f"SELECT id, prenom, nom FROM employes e WHERE {employee_scope} ORDER BY nom",
                    employee_scope_params)
        employees = cur.fetchall()

        cur.execute(f"""
            SELECT d.*, e.prenom, e.nom
            FROM documents d
            LEFT JOIN employes e ON d.employe_id = e.id
            WHERE {document_scope}
            ORDER BY d.date_upload DESC LIMIT 80
        """, document_scope_params)
        docs = cur.fetchall()

        cur.close(); conn.close()
        return render_template('documents.html', documents=docs, employees=employees, current_employee=emp,
                               today=date.today(),
                               bientot=date.today() + timedelta(days=seuil_expiration))


    @bp.route('/employes/<int:id>/documents/add', methods=['POST'])
    @login_required
    @role_required('admin', 'rh')
    def add_employee_document(id):
        """Import direct d'un document (ex: contrat) depuis la fiche employé —
        l'employé est déjà connu, pas besoin de choisir dans une liste."""
        conn = get_db()
        cur = get_cursor(conn)
        cur.execute("SELECT id FROM employes WHERE id = %s", (id,))
        if not cur.fetchone():
            cur.close(); conn.close()
            flash("Employé introuvable", "danger")
            return redirect(url_for('index'))

        titre = request.form.get('titre', '').strip()
        description = request.form.get('description', '').strip()
        date_expiration = request.form.get('date_expiration') or None

        ok, message = _traiter_upload_document(conn, cur, id, titre, description, date_expiration)
        flash(message, 'success' if ok else 'danger')
        cur.close(); conn.close()
        return redirect(url_for('view_employee', id=id))


    @bp.route('/documents/delete/<int:doc_id>', methods=['POST'])
    @login_required
    @role_required('admin', 'rh')
    def delete_document(doc_id):
        conn = get_db()
        cur = get_cursor(conn)
        cur.execute("SELECT chemin_fichier, storage_key FROM documents WHERE id = %s", (doc_id,))
        doc = cur.fetchone()
        if doc:
            try:
                if os.path.exists(doc['chemin_fichier']):
                    os.remove(doc['chemin_fichier'])
            except OSError:
                pass
            cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
            conn.commit()
            object_storage.delete(doc.get('storage_key'))
            flash('Document supprimé', 'success')
        cur.close(); conn.close()
        return redirect(url_for('documents.documents'))

    @bp.route('/documents/file/<int:doc_id>')
    @login_required
    def download_document(doc_id):
        with db_cursor() as (conn, cur):
            cur.execute("""SELECT nom_fichier, employe_id, contenu, type_fichier,
                                  storage_key
                             FROM documents WHERE id = %s""", (doc_id,))
            doc = cur.fetchone()
        if not doc:
            flash('Document introuvable.', 'danger')
            return redirect(url_for('documents.documents'))
        # Un simple employé ne peut accéder qu'à SES PROPRES documents ; seuls
        # admin/rh/manager peuvent accéder aux documents de n'importe qui.
        if session.get('role') not in ('admin', 'rh', 'manager'):
            emp = get_current_employee()
            if not emp or doc.get('employe_id') != emp['id']:
                flash('Accès refusé : ce document ne vous appartient pas.', 'danger')
                return redirect(url_for('documents.documents'))

        filename = secure_filename(doc['nom_fichier'])

        # Lecture rétrocompatible : objet privé S3 en priorité, puis BYTEA.
        if doc.get('storage_key') or doc.get('contenu') is not None:
            try:
                return object_storage.download_response(
                    content=doc.get('contenu'),
                    storage_key=doc.get('storage_key'),
                    filename=filename,
                )
            except ObjectStorageError as exc:
                flash(str(exc), 'danger')
                return redirect(url_for('documents.documents'))

        # Repli pour un document uploadé AVANT ce correctif : son fichier n'a
        # peut-être jamais survécu à un redémarrage du service. On tente quand
        # même le disque local (cas où le service n'a jamais redémarré depuis).
        filepath = os.path.join(upload_folder, filename)
        if os.path.dirname(os.path.abspath(filepath)) != os.path.abspath(upload_folder):
            flash('Accès refusé.', 'danger')
            return redirect(url_for('documents.documents'))
        if os.path.isfile(filepath):
            resp = send_file(filepath, as_attachment=True, download_name=filename)
            resp.headers['X-Content-Type-Options'] = 'nosniff'
            return resp

        flash("Ce fichier a été perdu suite à un redémarrage du service (ancien document, "
              "uploadé avant la correction du stockage). Merci de le réimporter.", 'danger')
        return redirect(url_for('documents.documents'))

    return bp
