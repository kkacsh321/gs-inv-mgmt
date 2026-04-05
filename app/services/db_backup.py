from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings


@dataclass(frozen=True)
class BackupResult:
    file_path: Path
    file_name: str
    size_bytes: int


def _db_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PGHOST"] = settings.db_host
    env["PGPORT"] = str(settings.db_port)
    env["PGUSER"] = settings.db_user
    env["PGDATABASE"] = settings.db_name
    env["PGPASSWORD"] = settings.db_password
    return env


def pg_tools_status() -> dict[str, bool]:
    return {
        "pg_dump": bool(shutil.which("pg_dump")),
        "psql": bool(shutil.which("psql")),
    }


def create_backup_dump(*, include_drop_statements: bool = True) -> BackupResult:
    if not shutil.which("pg_dump"):
        raise RuntimeError("`pg_dump` is not installed in this runtime.")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    file_name = f"{settings.app_env}-{settings.db_name}-{ts}.sql"
    temp_dir = Path(tempfile.mkdtemp(prefix="gs-db-backup-"))
    file_path = temp_dir / file_name

    cmd = [
        "pg_dump",
        "--format=plain",
        "--no-owner",
        "--no-privileges",
        "--encoding=UTF8",
        "-f",
        str(file_path),
        settings.db_name,
    ]
    if include_drop_statements:
        cmd.insert(1, "--clean")
        cmd.insert(2, "--if-exists")

    result = subprocess.run(cmd, capture_output=True, text=True, env=_db_env(), check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "pg_dump failed.")

    size_bytes = file_path.stat().st_size
    return BackupResult(file_path=file_path, file_name=file_name, size_bytes=size_bytes)


def restore_dump_file(file_path: Path) -> None:
    if settings.app_env.lower() == "prod":
        raise RuntimeError("Restore is blocked in APP_ENV=prod.")
    if not shutil.which("psql"):
        raise RuntimeError("`psql` is not installed in this runtime.")
    if not file_path.exists():
        raise RuntimeError(f"Dump file not found: {file_path}")

    cmd = [
        "psql",
        "--set",
        "ON_ERROR_STOP=1",
        "-f",
        str(file_path),
        settings.db_name,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=_db_env(), check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "psql restore failed.")


def s3_backup_enabled() -> bool:
    return settings.storage_provider == "s3" and bool(settings.s3_bucket)


def _s3_client():
    if not s3_backup_enabled():
        raise RuntimeError("S3 backup is not configured.")
    session = boto3.session.Session(
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
        region_name=settings.aws_region,
    )
    kwargs = {
        "service_name": "s3",
        "config": Config(signature_version="s3v4"),
    }
    if settings.s3_endpoint_url:
        kwargs["endpoint_url"] = settings.s3_endpoint_url
    return session.client(**kwargs)


def backup_s3_key(file_name: str) -> str:
    return f"db-backups/{settings.app_env}/{file_name}"


def upload_backup_to_s3(file_path: Path, key: str | None = None) -> str:
    if not file_path.exists():
        raise RuntimeError(f"Dump file not found: {file_path}")
    client = _s3_client()
    resolved_key = key or backup_s3_key(file_path.name)
    try:
        client.upload_file(str(file_path), settings.s3_bucket, resolved_key)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"S3 upload failed: {exc}") from exc
    return resolved_key


def list_backups_in_s3(prefix: str | None = None) -> list[dict[str, str]]:
    client = _s3_client()
    resolved_prefix = prefix or f"db-backups/{settings.app_env}/"
    try:
        resp = client.list_objects_v2(Bucket=settings.s3_bucket, Prefix=resolved_prefix)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"S3 list failed: {exc}") from exc

    rows: list[dict[str, str]] = []
    for obj in resp.get("Contents", []):
        rows.append(
            {
                "key": str(obj.get("Key", "")),
                "size_bytes": str(int(obj.get("Size", 0))),
                "last_modified": str(obj.get("LastModified", "")),
            }
        )
    rows.sort(key=lambda r: r["key"], reverse=True)
    return rows


def download_backup_from_s3(key: str) -> Path:
    client = _s3_client()
    temp_dir = Path(tempfile.mkdtemp(prefix="gs-db-restore-"))
    target = temp_dir / Path(key).name
    try:
        client.download_file(settings.s3_bucket, key, str(target))
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"S3 download failed: {exc}") from exc
    return target
