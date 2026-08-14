"""Configuration d'environnement de l'application Flask.

Ce module ne connaît aucune route métier. Il centralise les validations de
production, les cookies, PostgreSQL/SQLAlchemy, le proxy Render et SMTP.
"""

import os

from werkzeug.middleware.proxy_fix import ProxyFix


def _bool_env(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {
        '1', 'true', 'yes', 'on',
    }


def configurer_application(app, logger):
    secret_key = os.environ.get('SECRET_KEY')
    if not secret_key:
        if os.environ.get('FLASK_ENV') == 'production':
            raise RuntimeError(
                "SECRET_KEY doit être défini dans l'environnement en production. "
                "Générez-en une avec: python -c \"import secrets; "
                "print(secrets.token_hex(32))\""
            )
        secret_key = 'dev-only-insecure-key-do-not-use-in-production'
        logger.warning(
            "SECRET_KEY absente de l'environnement, utilisation d'une clé "
            "de dev non sécurisée."
        )

    app.secret_key = secret_key
    app.config.update(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=_bool_env('SESSION_COOKIE_SECURE', False),
        PERMANENT_SESSION_LIFETIME=int(
            os.environ.get('PERMANENT_SESSION_LIFETIME', 3600)
        ),
        MAX_CONTENT_LENGTH=int(
            os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024)
        ),
    )

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        if os.environ.get('FLASK_ENV') == 'production':
            raise RuntimeError(
                "DATABASE_URL doit être défini dans l'environnement en "
                "production."
            )
        database_url = (
            'postgresql://postgres:postgres@localhost:5432/gestion_personnel'
        )
        logger.warning(
            "DATABASE_URL absente de l'environnement, utilisation du "
            "fallback de développement local."
        )

    try:
        trusted_proxies = max(
            0, int(os.environ.get('TRUSTED_PROXY_COUNT', '0'))
        )
    except ValueError as exc:
        raise RuntimeError("TRUSTED_PROXY_COUNT doit être un entier.") from exc
    if trusted_proxies:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=trusted_proxies,
            x_proto=trusted_proxies,
            x_host=trusted_proxies,
        )

    app.config.update(
        AUTO_INIT_DB=_bool_env(
            'AUTO_INIT_DB', os.environ.get('FLASK_ENV') != 'production'
        ),
        MAIL_SERVER=os.environ.get('MAIL_SERVER', 'smtp.gmail.com'),
        MAIL_PORT=int(os.environ.get('MAIL_PORT', 587)),
        MAIL_USE_TLS=_bool_env('MAIL_USE_TLS', True),
        MAIL_USERNAME=os.environ.get('MAIL_USERNAME', ''),
        MAIL_PASSWORD=os.environ.get('MAIL_PASSWORD', ''),
        MAIL_DEFAULT_SENDER=os.environ.get(
            'MAIL_DEFAULT_SENDER', 'gestion.personnel@entreprise.fr'
        ),
        ADMIN_EMAIL=os.environ.get('ADMIN_EMAIL', 'admin@entreprise.fr'),
        EMAIL_ENABLED=_bool_env('EMAIL_ENABLED', False),
        EMAIL_BATCH_SIZE=int(os.environ.get('EMAIL_BATCH_SIZE', 20)),
        EMAIL_MAX_ATTEMPTS=int(os.environ.get('EMAIL_MAX_ATTEMPTS', 5)),
        EMAIL_POLL_SECONDS=int(os.environ.get('EMAIL_POLL_SECONDS', 60)),
    )

    return database_url
