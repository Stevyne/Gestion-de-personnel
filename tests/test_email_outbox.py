import app as application


def test_email_desactive_est_un_noop(app):
    app.config['EMAIL_ENABLED'] = False
    assert application.queue_email(
        'destinataire@example.test', 'Sujet', 'Corps') is None
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM email_outbox")
        assert cur.fetchone()['n'] == 0


def test_outbox_envoie_un_message_sans_smtp_dans_la_requete(app, monkeypatch):
    app.config['EMAIL_ENABLED'] = True
    app.config['EMAIL_BATCH_SIZE'] = 5
    app.config['EMAIL_MAX_ATTEMPTS'] = 3
    envoyes = []
    monkeypatch.setattr(application, '_send_outbox_message',
                        lambda message: envoyes.append(dict(message)))

    email_id = application.queue_email(
        'destinataire@example.test', 'Décision rendue', 'Votre demande est approuvée.',
        event_key='test-decision:1')
    assert email_id is not None
    assert envoyes == []  # la route ne contacte jamais SMTP

    resultat = application.job_traiter_file_emails()
    assert resultat['envoyes'] == 1
    assert envoyes[0]['destinataire'] == 'destinataire@example.test'
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT statut, tentatives FROM email_outbox WHERE id = %s", (email_id,))
        row = cur.fetchone()
    assert row['statut'] == 'envoye'
    assert row['tentatives'] == 1


def test_cle_evenement_empeche_doublon(app):
    app.config['EMAIL_ENABLED'] = True
    premier = application.queue_email(
        'a@example.test', 'Sujet', 'Corps', event_key='evenement-unique')
    second = application.queue_email(
        'a@example.test', 'Sujet', 'Corps', event_key='evenement-unique')
    assert premier is not None
    assert second is None
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM email_outbox")
        assert cur.fetchone()['n'] == 1


def test_erreur_outbox_ne_casse_pas_transaction_metier(app):
    app.config['EMAIL_ENABLED'] = True
    with application.db_cursor(commit=True) as (conn, cur):
        # Dépasse volontairement VARCHAR(200) : le SAVEPOINT doit absorber
        # l'erreur SQL et laisser la transaction utilisable.
        assert application.queue_email(
            'a@example.test', 'Sujet', 'Corps', cur=cur,
            event_key='x' * 500) is None
        cur.execute("INSERT INTO audit_logs (action) VALUES ('ACTION_METIER')")
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action = 'ACTION_METIER'")
        assert cur.fetchone()['n'] == 1
