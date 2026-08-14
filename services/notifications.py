"""Persistance des notifications internes."""


def creer_service_notifications(db_cursor, logger):
    def create_notification(user_id, title, message, type_='info', cur=None):
        try:
            if cur is not None:
                cur.execute("SAVEPOINT ajout_notification")
                try:
                    cur.execute("""
                        INSERT INTO notifications
                            (user_id, title, message, type, is_read)
                        VALUES (%s, %s, %s, %s, FALSE)
                    """, (user_id, title, message, type_))
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT ajout_notification")
                    cur.execute("RELEASE SAVEPOINT ajout_notification")
                    raise
                cur.execute("RELEASE SAVEPOINT ajout_notification")
                return True
            with db_cursor(commit=True) as (_conn, notification_cur):
                notification_cur.execute("""
                    INSERT INTO notifications
                        (user_id, title, message, type, is_read)
                    VALUES (%s, %s, %s, %s, FALSE)
                """, (user_id, title, message, type_))
            return True
        except Exception as exc:
            logger.error("Erreur create_notification DB: %s", exc, exc_info=True)
            return False

    def get_unread_notifications(user_id=None):
        try:
            with db_cursor() as (_conn, cur):
                if user_id is None:
                    cur.execute("""SELECT * FROM notifications
                        WHERE is_read=FALSE ORDER BY timestamp DESC LIMIT 50""")
                else:
                    cur.execute("""SELECT * FROM notifications
                        WHERE user_id=%s AND is_read=FALSE
                        ORDER BY timestamp DESC LIMIT 50""", (user_id,))
                return cur.fetchall()
        except Exception as exc:
            logger.error("Erreur get_unread_notifications: %s", exc, exc_info=True)
            return []

    def mark_all_read(user_id=None):
        try:
            with db_cursor(commit=True) as (_conn, cur):
                if user_id is None:
                    cur.execute("UPDATE notifications SET is_read=TRUE")
                else:
                    cur.execute(
                        "UPDATE notifications SET is_read=TRUE WHERE user_id=%s",
                        (user_id,),
                    )
            return True
        except Exception as exc:
            logger.error("Erreur mark_all_read: %s", exc, exc_info=True)
            return False

    def get_all_notifications(user_id=None, limit=30):
        try:
            with db_cursor() as (_conn, cur):
                if user_id is None:
                    cur.execute("""SELECT * FROM notifications
                        ORDER BY timestamp DESC LIMIT %s""", (limit,))
                else:
                    cur.execute("""SELECT * FROM notifications WHERE user_id=%s
                        ORDER BY timestamp DESC LIMIT %s""", (user_id, limit))
                return cur.fetchall()
        except Exception as exc:
            logger.error("Erreur get_all_notifications: %s", exc, exc_info=True)
            return []

    return (
        create_notification, get_unread_notifications,
        mark_all_read, get_all_notifications,
    )
