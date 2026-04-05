import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from app.services import media_storage
except ModuleNotFoundError as exc:
    if exc.name not in {"boto3", "botocore", "botocore.config", "botocore.exceptions"}:
        raise
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.session = types.SimpleNamespace(Session=lambda *args, **kwargs: None)
    sys.modules.setdefault("boto3", fake_boto3)

    fake_botocore = types.ModuleType("botocore")
    fake_botocore_config = types.ModuleType("botocore.config")
    fake_botocore_ex = types.ModuleType("botocore.exceptions")

    class _FakeConfig:
        def __init__(self, *args, **kwargs):
            pass

    class _FakeBotoCoreError(Exception):
        pass

    class _FakeClientError(Exception):
        pass

    fake_botocore_config.Config = _FakeConfig
    fake_botocore_ex.BotoCoreError = _FakeBotoCoreError
    fake_botocore_ex.ClientError = _FakeClientError
    sys.modules.setdefault("botocore", fake_botocore)
    sys.modules.setdefault("botocore.config", fake_botocore_config)
    sys.modules.setdefault("botocore.exceptions", fake_botocore_ex)
    media_storage = importlib.import_module("app.services.media_storage")


def _fake_settings(**overrides):
    values = {
        "storage_provider": "s3",
        "s3_bucket": "media-bucket",
        "aws_access_key_id": "",
        "aws_secret_access_key": "",
        "aws_region": "us-east-1",
        "s3_endpoint_url": "",
        "s3_public_base_url": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class MediaStorageTests(unittest.TestCase):
    def test_init_disabled_when_not_configured(self) -> None:
        with patch("app.services.media_storage.settings", _fake_settings(storage_provider="local", s3_bucket="")):
            svc = media_storage.MediaStorageService()
            self.assertFalse(svc.enabled)
            self.assertIsNone(svc.client)

    @patch("app.services.media_storage.boto3.session.Session")
    def test_init_enabled_and_ensure_bucket_paths(self, mock_session: Mock) -> None:
        client = Mock()
        mock_session.return_value.client.return_value = client
        with patch("app.services.media_storage.settings", _fake_settings(aws_region="us-east-1")):
            svc = media_storage.MediaStorageService()
            self.assertTrue(svc.enabled)
            self.assertIsNotNone(svc.client)

            svc.ensure_bucket()
            client.head_bucket.assert_called_once()
            client.create_bucket.assert_not_called()

        with patch("app.services.media_storage.settings", _fake_settings(aws_region="us-west-2")):
            svc = media_storage.MediaStorageService()
            with patch.object(media_storage, "ClientError", Exception):
                client.head_bucket.side_effect = Exception("missing")
                svc.ensure_bucket()
            client.create_bucket.assert_called()
            _args, kwargs = client.create_bucket.call_args
            self.assertEqual(kwargs["Bucket"], "media-bucket")
            self.assertEqual(kwargs["CreateBucketConfiguration"]["LocationConstraint"], "us-west-2")

    @patch("app.services.media_storage.boto3.session.Session")
    def test_upload_file_urls_content_type_and_errors(self, mock_session: Mock) -> None:
        client = Mock()
        mock_session.return_value.client.return_value = client
        with patch("app.services.media_storage.uuid.uuid4", return_value="uuid-1"), patch(
            "app.services.media_storage.settings",
            _fake_settings(s3_public_base_url="https://cdn.example.com"),
        ):
            svc = media_storage.MediaStorageService()
            out = svc.upload_file("coin.jpg", b"abc")
            self.assertEqual(out.bucket, "media-bucket")
            self.assertEqual(out.key, "media/uuid-1-coin.jpg")
            self.assertEqual(out.content_type, "image/jpeg")
            self.assertEqual(out.url, "https://cdn.example.com/media/uuid-1-coin.jpg")
            self.assertEqual(out.size_bytes, 3)

        with patch("app.services.media_storage.uuid.uuid4", return_value="uuid-2"), patch(
            "app.services.media_storage.settings",
            _fake_settings(s3_public_base_url="", s3_endpoint_url="http://minio:9000"),
        ):
            svc = media_storage.MediaStorageService()
            out = svc.upload_file("clip.bin", b"x", content_type="video/mp4")
            self.assertEqual(out.url, "http://minio:9000/media-bucket/media/uuid-2-clip.bin")
            self.assertEqual(out.content_type, "video/mp4")

        with patch("app.services.media_storage.uuid.uuid4", return_value="uuid-3"), patch(
            "app.services.media_storage.settings",
            _fake_settings(s3_public_base_url="", s3_endpoint_url="", aws_region="us-west-1"),
        ):
            svc = media_storage.MediaStorageService()
            out = svc.upload_file("file.unknownext", b"z")
            self.assertIn("https://media-bucket.s3.us-west-1.amazonaws.com/media/uuid-3-file.unknownext", out.url)

        with patch("app.services.media_storage.settings", _fake_settings(storage_provider="local", s3_bucket="")):
            svc = media_storage.MediaStorageService()
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                svc.upload_file("x.jpg", b"abc")

        with patch("app.services.media_storage.settings", _fake_settings()), patch.object(
            media_storage, "BotoCoreError", Exception
        ):
            svc = media_storage.MediaStorageService()
            svc.client.put_object.side_effect = Exception("boom")
            with self.assertRaisesRegex(RuntimeError, "S3 upload failed"):
                svc.upload_file("x.jpg", b"abc")

    @patch("app.services.media_storage.boto3.session.Session")
    def test_get_object_bytes(self, mock_session: Mock) -> None:
        client = Mock()
        mock_session.return_value.client.return_value = client
        with patch("app.services.media_storage.settings", _fake_settings()):
            svc = media_storage.MediaStorageService()
            body_mock = Mock()
            body_mock.read.return_value = b"payload"
            client.get_object.return_value = {"Body": body_mock, "ContentType": "image/png"}
            data, ctype = svc.get_object_bytes("b", "k")
            self.assertEqual(data, b"payload")
            self.assertEqual(ctype, "image/png")

            client.get_object.return_value = {"Body": body_mock}
            data2, ctype2 = svc.get_object_bytes("b", "k2")
            self.assertEqual(data2, b"payload")
            self.assertEqual(ctype2, "application/octet-stream")

        with patch("app.services.media_storage.settings", _fake_settings(storage_provider="local", s3_bucket="")):
            svc = media_storage.MediaStorageService()
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                svc.get_object_bytes("b", "k")

        with patch("app.services.media_storage.settings", _fake_settings()), patch.object(
            media_storage, "BotoCoreError", Exception
        ):
            svc = media_storage.MediaStorageService()
            svc.client.get_object.side_effect = Exception("fetch fail")
            with self.assertRaisesRegex(RuntimeError, "S3 fetch failed"):
                svc.get_object_bytes("b", "k")

    @patch("app.services.media_storage.boto3.session.Session")
    def test_delete_object_paths(self, mock_session: Mock) -> None:
        client = Mock()
        mock_session.return_value.client.return_value = client

        with patch("app.services.media_storage.settings", _fake_settings()):
            svc = media_storage.MediaStorageService()
            svc.delete_object("bucket-a", "key-a")
            client.delete_object.assert_called_once_with(Bucket="bucket-a", Key="key-a")

            with self.assertRaisesRegex(RuntimeError, "bucket/key are required"):
                svc.delete_object("", "key-a")
            with self.assertRaisesRegex(RuntimeError, "bucket/key are required"):
                svc.delete_object("bucket-a", "")

        with patch("app.services.media_storage.settings", _fake_settings(storage_provider="local", s3_bucket="")):
            svc = media_storage.MediaStorageService()
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                svc.delete_object("bucket-a", "key-a")

        with patch("app.services.media_storage.settings", _fake_settings()), patch.object(
            media_storage, "BotoCoreError", Exception
        ):
            svc = media_storage.MediaStorageService()
            svc.client.delete_object.side_effect = Exception("delete fail")
            with self.assertRaisesRegex(RuntimeError, "S3 delete failed"):
                svc.delete_object("bucket-a", "key-a")


if __name__ == "__main__":
    unittest.main()
