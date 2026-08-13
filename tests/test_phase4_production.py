import io
from pathlib import Path

import app as application


ROOT = Path(__file__).resolve().parents[1]


class FakeS3Body(io.BytesIO):
    pass


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = {
            'body': bytes(Body),
            'metadata': kwargs.get('Metadata', {}),
            'content_type': kwargs.get('ContentType'),
        }
        return {'ETag': '"etag-test"'}

    def head_object(self, Bucket, Key):
        obj = self.objects[(Bucket, Key)]
        return {
            'ETag': '"etag-test"',
            'Metadata': obj['metadata'],
            'ContentLength': len(obj['body']),
            'ContentType': obj['content_type'],
        }

    def get_object(self, Bucket, Key):
        head = self.head_object(Bucket, Key)
        return {**head, 'Body': FakeS3Body(self.objects[(Bucket, Key)]['body'])}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)
        return {}

    def head_bucket(self, Bucket):
        return {}


def _activer_fake_s3(monkeypatch):
    fake = FakeS3()
    storage = application.object_storage
    monkeypatch.setattr(storage, 'enabled', True)
    monkeypatch.setattr(storage, 'required', True)
    monkeypatch.setattr(storage, 'bucket', 'bucket-test')
    monkeypatch.setattr(storage, 'prefix', 'tests')
    monkeypatch.setattr(storage, 'threshold_bytes', 1)
    monkeypatch.setattr(storage, '_client_instance', fake)
    return fake


def test_schema_phase4_stockage_et_heartbeat():
    with application.db_cursor() as (_conn, cur):
        cur.execute("""
            SELECT table_name, column_name
              FROM information_schema.columns
             WHERE (table_name='documents' AND column_name='storage_key')
                OR (table_name='contrats' AND column_name='storage_key')
                OR (table_name='absences' AND column_name='justificatif_storage_key')
                OR (table_name='messages' AND column_name='piece_jointe_storage_key')
                OR (table_name='scheduler_heartbeats' AND column_name='last_seen')
                OR (table_name='backup_runs' AND column_name='completed_at')
        """)
        columns = {(row['table_name'], row['column_name']) for row in cur.fetchall()}
    assert columns == {
        ('documents', 'storage_key'),
        ('contrats', 'storage_key'),
        ('absences', 'justificatif_storage_key'),
        ('messages', 'piece_jointe_storage_key'),
        ('scheduler_heartbeats', 'last_seen'),
        ('backup_runs', 'completed_at'),
    }


def test_document_volumineux_externalise_et_telecharge(admin_client, monkeypatch):
    fake = _activer_fake_s3(monkeypatch)
    pdf = b'%PDF-1.4\nphase4-document'
    response = admin_client.post('/documents', data={
        'employe_id': '1', 'titre': 'Document S3', 'description': '',
        'date_expiration': '', 'fichier': (io.BytesIO(pdf), 'phase4.pdf'),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert response.status_code == 200
    with application.db_cursor() as (_conn, cur):
        cur.execute("SELECT id, contenu, storage_key, storage_sha256 FROM documents")
        row = cur.fetchone()
    assert row['contenu'] is None
    assert row['storage_key'].startswith('tests/documents/')
    assert len(row['storage_sha256']) == 64
    assert admin_client.get(f"/documents/file/{row['id']}").data == pdf
    assert fake.objects


def test_contrat_et_piece_jointe_externalises(admin_client, app, monkeypatch):
    _activer_fake_s3(monkeypatch)
    pdf = b'%PDF-1.4\nphase4-contrat'
    response = admin_client.post('/employes/1/contrats/nouveau', data={
        'type_contrat': 'cdd', 'reference': 'P4', 'date_debut': '2026-08-01',
        'date_fin': '2026-12-31', 'notes': '',
        'fichier': (io.BytesIO(pdf), 'contrat-phase4.pdf'),
    }, content_type='multipart/form-data')
    assert response.status_code in (301, 302)
    with application.db_cursor() as (_conn, cur):
        cur.execute("SELECT id, contenu, storage_key FROM contrats")
        contrat = cur.fetchone()
    assert contrat['contenu'] is None and contrat['storage_key']
    assert admin_client.get(f"/contrats/{contrat['id']}/fichier").data == pdf

    employe = app.test_client()
    employe.post('/login', data={'username': 'employe', 'password': 'user123'})
    piece = b'%PDF-1.4\nphase4-message'
    response = employe.post('/messages/nouveau', data={
        'type': 'prive', 'destinataires': '3', 'contenu': '', 'titre': '',
        'piece_jointe': (io.BytesIO(piece), 'message-phase4.pdf'),
    }, content_type='multipart/form-data')
    assert response.status_code in (301, 302)
    with application.db_cursor() as (_conn, cur):
        cur.execute("""SELECT id, piece_jointe_contenu, piece_jointe_storage_key
                         FROM messages ORDER BY id DESC LIMIT 1""")
        message = cur.fetchone()
    assert message['piece_jointe_contenu'] is None
    assert message['piece_jointe_storage_key']
    assert employe.get(f"/messages/piece-jointe/{message['id']}").data == piece


def test_justificatif_externalise_reste_confidentiel(employe_client, app, monkeypatch):
    _activer_fake_s3(monkeypatch)
    manager_client = app.test_client()
    manager_client.post('/login', data={'username': 'manager', 'password': 'manager123'})
    with application.db_cursor(commit=True) as (_conn, cur):
        cur.execute("""INSERT INTO absences(employe_id,date,motif)
                       VALUES (1,CURRENT_DATE-1,'Phase 4') RETURNING id""")
        absence_id = cur.fetchone()['id']
    pdf = b'%PDF-1.4\nphase4-justificatif'
    response = employe_client.post(
        f'/self-service/absences/{absence_id}/justificatif',
        data={'commentaire': '', 'justificatif': (io.BytesIO(pdf), 'arret-p4.pdf')},
        content_type='multipart/form-data',
    )
    assert response.status_code in (301, 302)
    with application.db_cursor() as (_conn, cur):
        cur.execute("""SELECT justificatif_contenu, justificatif_storage_key
                         FROM absences WHERE id=%s""", (absence_id,))
        row = cur.fetchone()
    assert row['justificatif_contenu'] is None and row['justificatif_storage_key']
    assert employe_client.get(f'/absences/{absence_id}/justificatif').data == pdf
    assert manager_client.get(f'/absences/{absence_id}/justificatif').status_code == 403


def test_commandes_flask_migration_stockage(app):
    result = app.test_cli_runner().invoke(args=['storage', 'status'])
    assert result.exit_code == 0, result.output
    assert 'documents:' in result.output
    assert 'backend=postgresql' in result.output


def test_healthchecks_publics(client):
    live = client.get('/health/live')
    ready = client.get('/health/ready')
    assert live.status_code == 200
    assert live.get_json()['status'] == 'ok'
    assert ready.status_code == 200
    assert ready.get_json()['checks']['postgresql'] == 'ok'
    assert live.headers.get('X-Request-ID')


def test_configuration_production_versionnee():
    render = (ROOT / 'render.yaml').read_text(encoding='utf-8')
    workflow = (ROOT / '.github/workflows/tests.yml').read_text(encoding='utf-8')
    deploy = (ROOT / '.github/workflows/deploy-render.yml').read_text(encoding='utf-8')
    assert 'type: keyvalue' in render
    assert 'python scheduler_worker.py' in render
    assert 'flask bootstrap-db && flask db upgrade' in render
    assert 'scripts/backup_postgres.py' in render
    assert 'redis:8-alpine' in workflow
    assert 'flask db upgrade' in workflow
    assert 'RENDER_WEB_DEPLOY_HOOK_URL' in deploy
    assert (ROOT / 'migrations/versions/20260813_phase4_production.py').exists()
