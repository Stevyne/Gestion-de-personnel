"""Accès PostgreSQL communs, indépendants des Blueprints."""

from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor


def creer_acces_postgres(database_url):
    def get_db():
        conn = psycopg2.connect(database_url)
        with conn.cursor() as cur:
            cur.execute("SET timezone TO 'Indian/Antananarivo'")
        return conn

    def get_cursor(conn):
        return conn.cursor(cursor_factory=RealDictCursor)

    @contextmanager
    def db_cursor(commit=False):
        """Fournit ``(connexion, curseur)`` et ferme toujours les ressources."""
        conn = get_db()
        cur = get_cursor(conn)
        try:
            yield conn, cur
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    return get_db, db_cursor, get_cursor
