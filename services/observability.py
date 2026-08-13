"""Journalisation structurée, Sentry et sondes de santé."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
import time
from uuid import uuid4

from flask import g, jsonify, request, session


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,80}$")
_STANDARD_LOG_FIELDS = set(logging.makeLogRecord({}).__dict__) | {
    "message", "asctime",
}


class JsonFormatter(logging.Formatter):
    """Format JSON compact, exploitable par Render et les agrégateurs de logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": os.environ.get("SERVICE_NAME", "gestion-personnel"),
            "environment": os.environ.get("SENTRY_ENVIRONMENT")
            or os.environ.get("FLASK_ENV", "development"),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_FIELDS and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = str(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging() -> None:
    level = getattr(
        logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO
    )
    handler = logging.StreamHandler()
    if os.environ.get("LOG_FORMAT", "json").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def init_sentry() -> bool:
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        logging.getLogger(__name__).warning(
            "SENTRY_DSN est défini mais sentry-sdk n'est pas installé."
        )
        return False

    def _float_env(name: str, default: float) -> float:
        try:
            return min(1.0, max(0.0, float(os.environ.get(name, default))))
        except ValueError:
            return default

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT")
        or os.environ.get("FLASK_ENV", "development"),
        release=os.environ.get("SENTRY_RELEASE") or None,
        integrations=[
            FlaskIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        traces_sample_rate=_float_env("SENTRY_TRACES_SAMPLE_RATE", 0.05),
        profiles_sample_rate=_float_env("SENTRY_PROFILES_SAMPLE_RATE", 0.0),
        send_default_pii=False,
        max_request_body_size="small",
    )
    return True


def _bool_env(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


def register_observability(app, get_db, object_storage, *, alembic_revision: str):
    """Ajoute corrélation des requêtes et endpoints live/readiness."""
    access_logger = logging.getLogger("gestion_personnel.http")

    @app.before_request
    def _start_observed_request():
        supplied = request.headers.get("X-Request-ID", "")
        g.request_id = supplied if _REQUEST_ID_RE.fullmatch(supplied) else uuid4().hex
        g.request_started_at = time.perf_counter()

    @app.after_request
    def _finish_observed_request(response):
        request_id = getattr(g, "request_id", uuid4().hex)
        response.headers["X-Request-ID"] = request_id
        if request.endpoint != "static":
            duration_ms = round(
                (time.perf_counter() - getattr(g, "request_started_at", time.perf_counter()))
                * 1000,
                2,
            )
            access_logger.info(
                "requête HTTP",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                    "user_id": session.get("user_id"),
                    "remote_addr": request.remote_addr,
                },
            )
        return response

    @app.get("/health/live")
    def health_live():
        return jsonify(status="ok", service="gestion-personnel"), 200

    @app.get("/health/ready")
    def health_ready():
        checks = {}
        failures = []

        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            conn.close()
            checks["postgresql"] = "ok"
        except Exception as exc:
            checks["postgresql"] = f"error:{type(exc).__name__}"
            failures.append("postgresql")

        redis_url = os.environ.get("REDIS_URL", "").strip()
        if redis_url:
            try:
                import redis

                redis.Redis.from_url(
                    redis_url,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                    health_check_interval=30,
                ).ping()
                checks["redis"] = "ok"
            except Exception as exc:
                checks["redis"] = f"error:{type(exc).__name__}"
                failures.append("redis")
        else:
            checks["redis"] = "disabled"

        if object_storage.enabled and _bool_env("HEALTHCHECK_OBJECT_STORAGE", False):
            try:
                object_storage.healthcheck()
                checks["object_storage"] = "ok"
            except Exception as exc:
                checks["object_storage"] = f"error:{type(exc).__name__}"
                failures.append("object_storage")
        else:
            checks["object_storage"] = (
                "configured" if object_storage.enabled else "disabled"
            )

        # Le schéma Alembic est critique en production. En développement, le
        # bootstrap historique reste utilisable sans table alembic_version.
        require_migration = _bool_env(
            "REQUIRE_ALEMBIC_CURRENT",
            os.environ.get("FLASK_ENV") == "production",
        )
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT version_num FROM alembic_version LIMIT 1")
            row = cur.fetchone()
            cur.close()
            conn.close()
            current = row[0] if row else None
            checks["migration"] = current or "missing"
            if require_migration and current != alembic_revision:
                failures.append("migration")
        except Exception as exc:
            checks["migration"] = f"error:{type(exc).__name__}"
            if require_migration:
                failures.append("migration")

        # Le heartbeat est exposé même s'il n'est pas rendu bloquant. Cela évite
        # qu'un déploiement web échoue pendant que le worker Render redémarre.
        require_scheduler = _bool_env("REQUIRE_SCHEDULER_HEARTBEAT", False)
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - MAX(last_seen)))
                  FROM scheduler_heartbeats WHERE service_name='scheduler'
            """)
            row = cur.fetchone()
            cur.close()
            conn.close()
            age = float(row[0]) if row and row[0] is not None else None
            max_age = int(os.environ.get("SCHEDULER_HEARTBEAT_MAX_AGE", "180"))
            checks["scheduler"] = (
                {"status": "ok" if age is not None and age <= max_age else "stale",
                 "age_seconds": round(age, 1) if age is not None else None}
            )
            if require_scheduler and (age is None or age > max_age):
                failures.append("scheduler")
        except Exception as exc:
            checks["scheduler"] = f"error:{type(exc).__name__}"
            if require_scheduler:
                failures.append("scheduler")

        require_backup = _bool_env("REQUIRE_BACKUP_FRESHNESS", False)
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - MAX(completed_at)))
                  FROM backup_runs
            """)
            row = cur.fetchone()
            cur.close()
            conn.close()
            age = float(row[0]) if row and row[0] is not None else None
            max_age = int(os.environ.get("BACKUP_MAX_AGE_SECONDS", "129600"))
            checks["backup"] = {
                "status": "ok" if age is not None and age <= max_age else "stale",
                "age_seconds": round(age, 1) if age is not None else None,
            }
            if require_backup and (age is None or age > max_age):
                failures.append("backup")
        except Exception as exc:
            checks["backup"] = f"error:{type(exc).__name__}"
            if require_backup:
                failures.append("backup")

        status = "ready" if not failures else "not_ready"
        return jsonify(status=status, checks=checks, failures=failures), (
            200 if not failures else 503
        )

    return health_live, health_ready
