import mimetypes
import uuid
from dataclasses import dataclass

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings


@dataclass
class UploadResult:
    bucket: str
    key: str
    url: str
    content_type: str
    size_bytes: int


class MediaStorageService:
    def __init__(self) -> None:
        self.enabled = settings.storage_provider == "s3" and bool(settings.s3_bucket)
        self.bucket = settings.s3_bucket

        if not self.enabled:
            self.client = None
            return

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

        self.client = session.client(**kwargs)

    def ensure_bucket(self) -> None:
        if not self.enabled or self.client is None:
            return

        try:
            self.client.head_bucket(Bucket=self.bucket)
            return
        except ClientError:
            pass

        kwargs = {"Bucket": self.bucket}
        if settings.aws_region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": settings.aws_region}

        self.client.create_bucket(**kwargs)

    def upload_file(self, file_name: str, file_bytes: bytes, content_type: str | None = None) -> UploadResult:
        if not self.enabled or self.client is None:
            raise RuntimeError("S3 media storage is not configured.")

        guessed = content_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        key = f"media/{uuid.uuid4()}-{file_name}"

        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=file_bytes,
                ContentType=guessed,
            )
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(f"S3 upload failed: {exc}") from exc

        if settings.s3_public_base_url:
            url = f"{settings.s3_public_base_url.rstrip('/')}/{key}"
        elif settings.s3_endpoint_url:
            endpoint = settings.s3_endpoint_url.rstrip("/")
            url = f"{endpoint}/{self.bucket}/{key}"
        else:
            url = f"https://{self.bucket}.s3.{settings.aws_region}.amazonaws.com/{key}"

        return UploadResult(
            bucket=self.bucket,
            key=key,
            url=url,
            content_type=guessed,
            size_bytes=len(file_bytes),
        )

    def get_object_bytes(self, bucket: str, key: str) -> tuple[bytes, str]:
        if not self.enabled or self.client is None:
            raise RuntimeError("S3 media storage is not configured.")
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
            body = response["Body"].read()
            content_type = response.get("ContentType") or "application/octet-stream"
            return body, content_type
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(f"S3 fetch failed: {exc}") from exc

    def delete_object(self, bucket: str, key: str) -> None:
        if not self.enabled or self.client is None:
            raise RuntimeError("S3 media storage is not configured.")
        if not bucket or not key:
            raise RuntimeError("S3 delete failed: bucket/key are required.")
        try:
            self.client.delete_object(Bucket=bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(f"S3 delete failed: {exc}") from exc
