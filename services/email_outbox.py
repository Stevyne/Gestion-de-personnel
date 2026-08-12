"""File d'envoi d'e-mails persistante en PostgreSQL.

Ce module ne dépend pas de l'application Flask. Les accès base et la fonction
d'envoi sont injectés afin de garder le traitement testable et de préparer le
découpage progressif de l'ancien ``app.py`` monolithique.
"""

from datetime import timedelta
import re


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def adresse_email_valide(adresse):
    """Validation volontairement sobre : bloque les valeurs manifestement invalides."""
    return bool(adresse and _EMAIL_RE.match(str(adresse).strip()))


def ajouter_email(cur, destinataire, sujet, corps_texte, corps_html=None,
                   cle_evenement=None):
    """Ajoute un message à l'outbox dans la transaction courante.

    ``cle_evenement`` rend un événement idempotent. PostgreSQL autorise
    plusieurs valeurs NULL dans une contrainte UNIQUE, donc les messages sans
    clé restent libres d'être ajoutés plusieurs fois.
    """
    destinataire = (destinataire or '').strip().lower()
    if not adresse_email_valide(destinataire):
        return None

    cur.execute("""
        INSERT INTO email_outbox
            (destinataire, sujet, corps_texte, corps_html, cle_evenement,
             statut, disponible_le)
        VALUES (%s, %s, %s, %s, %s, 'en_attente', CURRENT_TIMESTAMP)
        ON CONFLICT (cle_evenement) DO NOTHING
        RETURNING id
    """, (destinataire, (sujet or '')[:255], corps_texte or '', corps_html,
          cle_evenement))
    row = cur.fetchone()
    return row['id'] if row else None


def traiter_outbox(db_cursor, envoyer, logger, taille_lot=20,
                    tentatives_max=5, maintenant=None):
    """Réserve puis traite un lot d'e-mails sans doublon entre workers.

    Les lignes sont d'abord revendiquées sous verrou ``SKIP LOCKED`` puis la
    transaction est validée avant l'appel SMTP. Un envoi échoué est replanifié
    avec un délai exponentiel. Les lignes restées ``en_cours`` plus de quinze
    minutes (arrêt brutal d'un worker) sont automatiquement récupérées.
    """
    taille_lot = max(1, int(taille_lot))
    tentatives_max = max(1, int(tentatives_max))

    with db_cursor(commit=True) as (conn, cur):
        # Les colonnes sont des TIMESTAMP sans fuseau. On prend l'heure de la
        # connexion PostgreSQL (configurée sur Indian/Antananarivo) plutôt que
        # l'heure locale potentiellement UTC du processus Python.
        if maintenant is None:
            cur.execute("SELECT LOCALTIMESTAMP AS maintenant")
            maintenant = cur.fetchone()['maintenant']
        cur.execute("""
            UPDATE email_outbox
               SET statut = 'en_attente', verrouille_le = NULL,
                   derniere_erreur = COALESCE(derniere_erreur, '') ||
                       CASE WHEN derniere_erreur IS NULL OR derniere_erreur = ''
                            THEN '' ELSE E'\n' END ||
                       'Reprise après expiration du verrou'
             WHERE statut = 'en_cours'
               AND verrouille_le < %s
        """, (maintenant - timedelta(minutes=15),))
        cur.execute("""
            SELECT id, destinataire, sujet, corps_texte, corps_html,
                   tentatives, cle_evenement
              FROM email_outbox
             WHERE statut = 'en_attente' AND disponible_le <= %s
             ORDER BY date_creation, id
             FOR UPDATE SKIP LOCKED
             LIMIT %s
        """, (maintenant, taille_lot))
        messages = cur.fetchall()
        ids = [m['id'] for m in messages]
        if ids:
            cur.execute("""
                UPDATE email_outbox
                   SET statut = 'en_cours', verrouille_le = %s,
                       tentatives = tentatives + 1
                 WHERE id = ANY(%s)
            """, (maintenant, ids))

    resultat = {'traites': len(messages), 'envoyes': 0,
                'replanifies': 0, 'echecs': 0}
    for message in messages:
        tentative = int(message.get('tentatives') or 0) + 1
        try:
            envoyer(message)
        except Exception as exc:  # l'erreur SMTP ne doit jamais tuer le worker
            terminal = tentative >= tentatives_max
            delai_minutes = min(60, 2 ** max(0, tentative - 1))
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("""
                    UPDATE email_outbox
                       SET statut = %s, verrouille_le = NULL,
                           disponible_le = %s, derniere_erreur = %s
                     WHERE id = %s
                """, ('echec' if terminal else 'en_attente',
                      maintenant + timedelta(minutes=delai_minutes),
                      str(exc)[:2000], message['id']))
            if terminal:
                resultat['echecs'] += 1
            else:
                resultat['replanifies'] += 1
            logger.warning("Échec e-mail outbox #%s (tentative %s/%s): %s",
                           message['id'], tentative, tentatives_max, exc)
        else:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("""
                    UPDATE email_outbox
                       SET statut = 'envoye', envoye_le = %s,
                           verrouille_le = NULL, derniere_erreur = NULL
                     WHERE id = %s
                """, (maintenant, message['id']))
            resultat['envoyes'] += 1

    return resultat
