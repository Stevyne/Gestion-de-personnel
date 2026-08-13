#!/usr/bin/env python3
"""Sauvegarde logique PostgreSQL vers un bucket S3 compatible privé.

Pré-requis du job : ``pg_dump`` de version au moins égale au serveur. Les mots
de passe ne sont jamais passés sur la ligne de commande : l'URL est convertie
en variables libpq pour le sous-processus.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.observability import configure_logging, init_sentry


configure_logging()
init_sentry()
logger = logging.getLogger("gestion_personnel.backup")


def _postgres_env(database_url: str) -> dict:
    parsed = urlparse(database_url)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise RuntimeError("DATABASE_URL doit être une URL PostgreSQL.")
    env = os.environ.copy()
    values = {
        "PGHOST": parsed.hostname,
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": unquote(parsed.username or ""),
        "PGPASSWORD": unquote(parsed.password or ""),
        "PGDATABASE": unquote(parsed.path.lstrip("/")),
    }
    env.update({key: value for key, value in values.items() if value})
    query = parse_qs(parsed.query)
    if query.get("sslmode"):
        env["PGSSLMODE"] = query["sslmode"][0]
    return env


def _s3_client():
    import boto3
    from botocore.config import Config

    kwargs = {
        "service_name": "s3",
        "region_name": os.environ.get("BACKUP_S3_REGION")
        or os.environ.get("S3_REGION") or "us-east-1",
        "endpoint_url": os.environ.get("BACKUP_S3_ENDPOINT_URL")
        or os.environ.get("S3_ENDPOINT_URL") or None,
        "config": Config(
            connect_timeout=10,
            read_timeout=120,
            retries={"max_attempts": 4, "mode": "standard"},
            s3={"addressing_style": os.environ.get("S3_ADDRESSING_STYLE", "auto")},
        ),
    }
    access_key = os.environ.get("BACKUP_S3_ACCESS_KEY_ID") or os.environ.get("S3_ACCESS_KEY_ID")
    secret_key = os.environ.get("BACKUP_S3_SECRET_ACCESS_KEY") or os.environ.get("S3_SECRET_ACCESS_KEY")
    if access_key:
        kwargs["aws_access_key_id"] = access_key
    if secret_key:
        kwargs["aws_secret_access_key"] = secret_key
    return boto3.client(**kwargs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_success(database_url: str, key: str, size: int, digest: str,
                    retention: int, deleted: int) -> None:
    import psycopg2

    conn = psycopg2.connect(database_url)
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO backup_runs
            (object_key,size_bytes,sha256,retention_days,deleted_old_backups)
            VALUES (%s,%s,%s,%s,%s)""",
            (key, size, digest, retention, deleted))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _prune(client, bucket: str, prefix: str, retention_days: int, keep_key: str) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    paginator = client.get_paginator("list_objects_v2")
    deleted = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key != keep_key and obj["LastModified"] < cutoff:
                client.delete_object(Bucket=bucket, Key=key)
                deleted += 1
    return deleted


def backup() -> str:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    bucket = (os.environ.get("BACKUP_S3_BUCKET") or os.environ.get("S3_BUCKET") or "").strip()
    prefix = os.environ.get("BACKUP_S3_PREFIX", "backups/postgresql").strip(" /")
    if not database_url:
        raise RuntimeError("DATABASE_URL est obligatoire.")
    if not bucket:
        raise RuntimeError("BACKUP_S3_BUCKET (ou S3_BUCKET) est obligatoire.")
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        raise RuntimeError("pg_dump est introuvable dans le PATH du job de sauvegarde.")

    now = datetime.now(timezone.utc)
    key = f"{prefix}/{now:%Y/%m}/gestion-personnel-{now:%Y%m%dT%H%M%SZ}.dump"
    with tempfile.TemporaryDirectory(prefix="gestion-personnel-backup-") as tmp:
        dump_path = Path(tmp) / "postgresql.dump"
        command = [
            pg_dump,
            "--format=custom",
            "--compress=6",
            "--no-owner",
            "--no-acl",
            "--file",
            str(dump_path),
        ]
        logger.info("début pg_dump", extra={"backup_key": key})
        subprocess.run(
            command,
            env=_postgres_env(database_url),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=int(os.environ.get("BACKUP_TIMEOUT_SECONDS", "3600")),
        )
        size = dump_path.stat().st_size
        digest = _sha256(dump_path)
        if size == 0:
            raise RuntimeError("pg_dump a produit un fichier vide.")

        client = _s3_client()
        extra = {
            "Metadata": {
                "sha256": digest,
                "created-at": now.isoformat(),
                "format": "pg_dump-custom",
            },
            "ContentType": "application/octet-stream",
        }
        sse = os.environ.get(
            "BACKUP_S3_SERVER_SIDE_ENCRYPTION",
            os.environ.get("S3_SERVER_SIDE_ENCRYPTION", "AES256"),
        ).strip()
        if sse:
            extra["ServerSideEncryption"] = sse
            kms = os.environ.get("BACKUP_S3_KMS_KEY_ID") or os.environ.get("S3_KMS_KEY_ID")
            if sse == "aws:kms" and kms:
                extra["SSEKMSKeyId"] = kms
        client.upload_file(str(dump_path), bucket, key, ExtraArgs=extra)
        head = client.head_object(Bucket=bucket, Key=key)
        if int(head.get("ContentLength", -1)) != size:
            raise RuntimeError("La taille de la sauvegarde S3 ne correspond pas au dump local.")
        if (head.get("Metadata") or {}).get("sha256") != digest:
            raise RuntimeError("Le checksum SHA-256 de la sauvegarde S3 est absent ou invalide.")

    retention = max(1, int(os.environ.get("BACKUP_RETENTION_DAYS", "30")))
    deleted = _prune(client, bucket, prefix, retention, key)
    _record_success(database_url, key, size, digest, retention, deleted)
    logger.info(
        "sauvegarde PostgreSQL terminée",
        extra={
            "backup_key": key,
            "size_bytes": size,
            "sha256": digest,
            "retention_days": retention,
            "deleted_old_backups": deleted,
        },
    )
    return key


if __name__ == "__main__":
    try:
        backup()
    except Exception as exc:
        logger.exception("échec de la sauvegarde PostgreSQL")
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
            sentry_sdk.flush(timeout=5)
        except ImportError:
            pass
        raise
