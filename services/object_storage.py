"""Stockage hybride PostgreSQL / S3 pour les fichiers privés.

Les petits fichiers restent en ``BYTEA`` pour préserver la compatibilité. Quand
le stockage objet est activé, les nouveaux fichiers dépassant le seuil configuré
sont écrits dans un bucket S3 compatible (AWS S3, Cloudflare R2, Backblaze B2,
MinIO...). Les routes Flask continuent d'effectuer le contrôle d'accès avant de
lire l'objet : aucun bucket ni URL publique n'est nécessaire.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
import mimetypes
import os
from pathlib import PurePosixPath
import re
from typing import Iterator
from uuid import uuid4

from flask import Response, send_file, stream_with_context
import io


logger = logging.getLogger("gestion_personnel.storage")


class ObjectStorageError(RuntimeError):
    """Erreur de configuration ou d'accès au stockage objet."""


def _bool_env(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _safe_component(value: str, fallback: str = "fichier") -> str:
    """Nettoie une composante de clé sans réintroduire de chemin utilisateur."""
    value = PurePosixPath(value or fallback).name
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return (value or fallback)[:180]


@dataclass(frozen=True)
class StoredFile:
    """Valeurs à enregistrer dans la ligne PostgreSQL."""

    content: bytes | None
    storage_key: str | None
    storage_etag: str | None
    storage_sha256: str

    @property
    def external(self) -> bool:
        return self.storage_key is not None


class HybridObjectStorage:
    """Façade S3 avec repli contrôlé sur PostgreSQL."""

    def __init__(self) -> None:
        self.enabled = _bool_env("OBJECT_STORAGE_ENABLED", False)
        self.required = _bool_env("OBJECT_STORAGE_REQUIRED", False)
        self.bucket = os.environ.get("S3_BUCKET", "").strip()
        self.prefix = os.environ.get("S3_PREFIX", "gestion-personnel").strip(" /")
        self.endpoint_url = os.environ.get("S3_ENDPOINT_URL") or None
        self.region = os.environ.get("S3_REGION") or "us-east-1"
        self.access_key = os.environ.get("S3_ACCESS_KEY_ID") or None
        self.secret_key = os.environ.get("S3_SECRET_ACCESS_KEY") or None
        self.addressing_style = os.environ.get("S3_ADDRESSING_STYLE", "auto")
        self.sse = os.environ.get("S3_SERVER_SIDE_ENCRYPTION", "AES256").strip()
        self.kms_key_id = os.environ.get("S3_KMS_KEY_ID", "").strip()
        try:
            self.threshold_bytes = max(
                0, int(os.environ.get("OBJECT_STORAGE_THRESHOLD_BYTES", 1_048_576))
            )
        except ValueError as exc:
            raise ObjectStorageError(
                "OBJECT_STORAGE_THRESHOLD_BYTES doit être un entier."
            ) from exc
        self._client_instance = None

        if self.enabled and not self.bucket:
            raise ObjectStorageError(
                "S3_BUCKET est obligatoire lorsque OBJECT_STORAGE_ENABLED=true."
            )
        if self.required and not self.enabled:
            raise ObjectStorageError(
                "OBJECT_STORAGE_REQUIRED=true exige OBJECT_STORAGE_ENABLED=true."
            )

    @property
    def backend_name(self) -> str:
        return "s3" if self.enabled else "postgresql"

    def should_externalize(self, size: int, force: bool = False) -> bool:
        return self.enabled and (force or size >= self.threshold_bytes)

    def _client(self):
        if not self.enabled:
            raise ObjectStorageError("Le stockage objet n'est pas activé.")
        if self._client_instance is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:  # pragma: no cover - dépendance de production
                raise ObjectStorageError(
                    "boto3 est requis pour utiliser le stockage S3."
                ) from exc

            kwargs = {
                "service_name": "s3",
                "region_name": self.region,
                "endpoint_url": self.endpoint_url,
                "config": Config(
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"max_attempts": 3, "mode": "standard"},
                    s3={"addressing_style": self.addressing_style},
                ),
            }
            # Laisser la chaîne d'identifiants boto3 (IAM/instance profile)
            # fonctionner lorsque les clés explicites ne sont pas renseignées.
            if self.access_key:
                kwargs["aws_access_key_id"] = self.access_key
            if self.secret_key:
                kwargs["aws_secret_access_key"] = self.secret_key
            self._client_instance = boto3.client(**kwargs)
        return self._client_instance

    def _put_options(self, sha256: str, content_type: str | None = None) -> dict:
        options: dict = {
            "Metadata": {"sha256": sha256},
            "ContentType": content_type or "application/octet-stream",
        }
        if self.sse:
            options["ServerSideEncryption"] = self.sse
            if self.sse == "aws:kms" and self.kms_key_id:
                options["SSEKMSKeyId"] = self.kms_key_id
        return options

    def _make_key(self, category: str, filename: str) -> str:
        now = datetime.now(timezone.utc)
        parts = [
            self.prefix,
            _safe_component(category, "fichiers"),
            now.strftime("%Y"),
            now.strftime("%m"),
            uuid4().hex,
            _safe_component(filename),
        ]
        return "/".join(part for part in parts if part)

    def store(
        self,
        category: str,
        content: bytes,
        filename: str,
        content_type: str | None = None,
        *,
        force_external: bool = False,
    ) -> StoredFile:
        """Stocke un fichier selon le seuil et retourne les colonnes hybrides.

        Si S3 est optionnel et momentanément indisponible, le binaire est gardé
        en PostgreSQL. Avec ``OBJECT_STORAGE_REQUIRED=true``, l'écriture échoue
        explicitement afin d'éviter de gonfler silencieusement la base.
        """
        raw = bytes(content)
        digest = hashlib.sha256(raw).hexdigest()
        if not self.should_externalize(len(raw), force_external):
            return StoredFile(raw, None, None, digest)

        key = self._make_key(category, filename)
        guessed_type = content_type or mimetypes.guess_type(filename)[0]
        try:
            response = self._client().put_object(
                Bucket=self.bucket,
                Key=key,
                Body=raw,
                **self._put_options(digest, guessed_type),
            )
            # Un HEAD vérifie l'existence et le checksum applicatif écrit dans
            # les métadonnées, sans rendre l'objet public.
            head = self._client().head_object(Bucket=self.bucket, Key=key)
            remote_digest = (head.get("Metadata") or {}).get("sha256")
            if remote_digest != digest:
                try:
                    self._client().delete_object(Bucket=self.bucket, Key=key)
                finally:
                    raise ObjectStorageError(
                        "La vérification SHA-256 du fichier S3 a échoué."
                    )
            etag = (response.get("ETag") or head.get("ETag") or "").strip('"') or None
            return StoredFile(None, key, etag, digest)
        except ObjectStorageError:
            raise
        except Exception as exc:
            if self.required:
                raise ObjectStorageError(
                    "Le stockage objet est indisponible; le fichier n'a pas été enregistré."
                ) from exc
            logger.exception(
                "Échec S3 pour %s; repli temporaire sur PostgreSQL", category
            )
            return StoredFile(raw, None, None, digest)

    def delete(self, key: str | None) -> bool:
        if not key:
            return False
        try:
            self._client().delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            logger.exception("Impossible de supprimer l'objet privé %s", key)
            return False

    def get_object(self, key: str):
        try:
            return self._client().get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise ObjectStorageError("Le fichier demandé est indisponible.") from exc

    def healthcheck(self) -> None:
        self._client().head_bucket(Bucket=self.bucket)

    def download_response(
        self,
        *,
        content: bytes | memoryview | None,
        storage_key: str | None,
        filename: str,
        content_type: str | None = None,
    ) -> Response:
        """Construit une réponse privée, depuis BYTEA ou en streaming depuis S3."""
        safe_name = _safe_component(filename, "fichier")
        mimetype = content_type or mimetypes.guess_type(safe_name)[0]
        if storage_key:
            obj = self.get_object(storage_key)
            body = obj["Body"]

            def chunks() -> Iterator[bytes]:
                try:
                    while True:
                        chunk = body.read(128 * 1024)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    body.close()

            response = Response(
                stream_with_context(chunks()),
                mimetype=obj.get("ContentType") or mimetype or "application/octet-stream",
                direct_passthrough=True,
            )
            if obj.get("ContentLength") is not None:
                response.content_length = int(obj["ContentLength"])
            response.headers.set("Content-Disposition", "attachment", filename=safe_name)
        elif content is not None:
            response = send_file(
                io.BytesIO(bytes(content)),
                as_attachment=True,
                download_name=safe_name,
                mimetype=mimetype,
            )
        else:
            raise ObjectStorageError("Aucun contenu n'est associé à ce fichier.")

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "private, no-store"
        return response


# Une instance sans connexion réseau à l'import. Le client boto3 est paresseux.
object_storage = HybridObjectStorage()
