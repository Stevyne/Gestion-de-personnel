#!/usr/bin/env python3
"""Restauration contrôlée d'une sauvegarde S3 créée par backup_postgres.py."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backup_postgres import _postgres_env, _s3_client, _sha256
from services.observability import configure_logging


configure_logging()
logger = logging.getLogger("gestion_personnel.restore")


def _latest_key(client, bucket: str, prefix: str) -> str:
    paginator = client.get_paginator("list_objects_v2")
    latest = None
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", []):
            if latest is None or obj["LastModified"] > latest["LastModified"]:
                latest = obj
    if latest is None:
        raise RuntimeError("Aucune sauvegarde n'a été trouvée.")
    return latest["Key"]


def restore(key: str | None, confirmation: str) -> None:
    target_url = os.environ.get("RESTORE_DATABASE_URL", "").strip()
    bucket = (os.environ.get("BACKUP_S3_BUCKET") or os.environ.get("S3_BUCKET") or "").strip()
    prefix = os.environ.get("BACKUP_S3_PREFIX", "backups/postgresql").strip(" /")
    expected = os.environ.get("RESTORE_CONFIRMATION", "RESTAURER-GESTION-PERSONNEL")
    if confirmation != expected:
        raise RuntimeError("Confirmation de restauration incorrecte.")
    if not target_url:
        raise RuntimeError("RESTORE_DATABASE_URL est obligatoire (jamais DATABASE_URL implicitement).")
    if not bucket:
        raise RuntimeError("BACKUP_S3_BUCKET (ou S3_BUCKET) est obligatoire.")
    pg_restore = shutil.which("pg_restore")
    if not pg_restore:
        raise RuntimeError("pg_restore est introuvable dans le PATH.")

    client = _s3_client()
    key = key or _latest_key(client, bucket, prefix)
    with tempfile.TemporaryDirectory(prefix="gestion-personnel-restore-") as tmp:
        dump_path = Path(tmp) / "postgresql.dump"
        client.download_file(bucket, key, str(dump_path))
        head = client.head_object(Bucket=bucket, Key=key)
        expected_sha = (head.get("Metadata") or {}).get("sha256")
        digest = _sha256(dump_path)
        if not expected_sha or digest != expected_sha:
            raise RuntimeError("Checksum SHA-256 invalide : restauration annulée.")
        logger.warning("début restauration PostgreSQL", extra={"backup_key": key})
        restore_env = _postgres_env(target_url)
        subprocess.run(
            [
                pg_restore,
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
                "--dbname",
                restore_env["PGDATABASE"],
                str(dump_path),
            ],
            env=restore_env,
            check=True,
            timeout=int(os.environ.get("RESTORE_TIMEOUT_SECONDS", "7200")),
        )
    logger.warning("restauration PostgreSQL terminée", extra={"backup_key": key})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", help="Clé S3; la plus récente est utilisée par défaut.")
    parser.add_argument("--confirm", required=True, help="Confirmation destructive explicite.")
    args = parser.parse_args()
    restore(args.key, args.confirm)


if __name__ == "__main__":
    main()
