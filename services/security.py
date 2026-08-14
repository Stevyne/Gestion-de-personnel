"""Extensions de sécurité HTTP : CSRF, rate limiting et en-têtes."""

import os

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFProtect


def _bool_env(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {
        '1', 'true', 'yes', 'on',
    }


def configurer_securite_http(app, logger):
    csrf = CSRFProtect(app)
    app.config['RATELIMIT_ENABLED'] = _bool_env('RATELIMIT_ENABLED', True)
    storage_uri = (
        os.environ.get('RATELIMIT_STORAGE_URI')
        or os.environ.get('REDIS_URL')
        or 'memory://'
    )
    if (
        os.environ.get('FLASK_ENV') == 'production'
        and app.config['RATELIMIT_ENABLED']
        and storage_uri == 'memory://'
        and _bool_env('REQUIRE_REDIS_RATE_LIMIT', True)
    ):
        raise RuntimeError(
            "REDIS_URL (ou RATELIMIT_STORAGE_URI) est obligatoire pour le "
            "rate limiting multi-worker en production."
        )

    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=['200 per day', '50 per hour'],
        storage_uri=storage_uri,
        storage_options={'socket_connect_timeout': 2, 'socket_timeout': 2}
        if storage_uri.startswith(('redis://', 'rediss://')) else None,
        enabled=app.config['RATELIMIT_ENABLED'],
        in_memory_fallback_enabled=False,
        key_prefix=os.environ.get('RATELIMIT_KEY_PREFIX', 'gestion-personnel'),
    )

    csp = {
        'default-src': "'self'",
        'script-src': ["'self'", "'unsafe-inline'"],
        'style-src': ["'self'", "'unsafe-inline'"],
        'img-src': ["'self'", 'data:'],
    }
    talisman = Talisman(
        app,
        force_https=_bool_env(
            'FORCE_HTTPS', os.environ.get('FLASK_ENV') == 'production'
        ),
        frame_options='DENY',
        content_security_policy=csp,
        referrer_policy='strict-origin-when-cross-origin',
        session_cookie_secure=app.config['SESSION_COOKIE_SECURE'],
    )
    logger.info("Sécurité activée : CSRF + RateLimit + Talisman")
    return csrf, limiter, talisman
