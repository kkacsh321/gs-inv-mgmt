import tempfile
import unittest
import sys
import types
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from app.services import db_backup
except ModuleNotFoundError as exc:
    if exc.name not in {"boto3", "botocore", "botocore.config", "botocore.exceptions"}:
        raise
    fake_boto3 = types.ModuleType("boto3")
    fake_session_ns = types.SimpleNamespace(Session=lambda *args, **kwargs: None)
    fake_boto3.session = fake_session_ns
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
    db_backup = importlib.import_module("app.services.db_backup")


def _fake_settings(**overrides):
    values = {
        "app_env": "local",
        "db_host": "db",
        "db_port": 5432,
        "db_user": "user",
        "db_name": "goldenstackers",
        "db_password": "pw",
        "storage_provider": "s3",
        "s3_bucket": "bucket",
        "aws_access_key_id": "",
        "aws_secret_access_key": "",
        "aws_region": "us-east-1",
        "s3_endpoint_url": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class DbBackupTests(unittest.TestCase):
    def test_db_env(self) -> None:
        with patch("app.services.db_backup.settings", _fake_settings(db_user="u", db_name="n", db_password="p")):
            env = db_backup._db_env()
            self.assertEqual(env["PGHOST"], "db")
            self.assertEqual(env["PGPORT"], "5432")
            self.assertEqual(env["PGUSER"], "u")
            self.assertEqual(env["PGDATABASE"], "n")
            self.assertEqual(env["PGPASSWORD"], "p")

    @patch("app.services.db_backup.shutil.which")
    def test_pg_tools_status(self, mock_which: Mock) -> None:
        mock_which.side_effect = lambda tool: "/usr/bin/x" if tool == "pg_dump" else None
        status = db_backup.pg_tools_status()
        self.assertTrue(status["pg_dump"])
        self.assertFalse(status["psql"])

    @patch("app.services.db_backup.shutil.which", return_value=None)
    def test_create_backup_dump_requires_pg_dump(self, _mock_which: Mock) -> None:
        with self.assertRaisesRegex(RuntimeError, "pg_dump"):
            db_backup.create_backup_dump()

    @patch("app.services.db_backup.subprocess.run")
    @patch("app.services.db_backup.shutil.which", return_value="/usr/bin/pg_dump")
    def test_create_backup_dump_success_and_failure(
        self,
        _mock_which: Mock,
        mock_run: Mock,
    ) -> None:
        with patch("app.services.db_backup.settings", _fake_settings()):
            def _ok_run(cmd, capture_output, text, env, check):
                dump_path = Path(cmd[cmd.index("-f") + 1])
                dump_path.write_text("dump", encoding="utf-8")
                return SimpleNamespace(returncode=0, stderr="")

            mock_run.side_effect = _ok_run
            out = db_backup.create_backup_dump(include_drop_statements=False)
            self.assertTrue(out.file_path.exists())
            self.assertFalse("--clean" in mock_run.call_args[0][0])

            mock_run.side_effect = lambda *a, **k: SimpleNamespace(returncode=1, stderr="bad dump")
            with self.assertRaisesRegex(RuntimeError, "bad dump"):
                db_backup.create_backup_dump()

    def test_restore_dump_file_guards(self) -> None:
        with patch("app.services.db_backup.settings", _fake_settings(app_env="prod")):
            with self.assertRaisesRegex(RuntimeError, "blocked in APP_ENV=prod"):
                db_backup.restore_dump_file(Path("/tmp/nope.sql"))

        with patch("app.services.db_backup.settings", _fake_settings(app_env="local")), patch(
            "app.services.db_backup.shutil.which", return_value=None
        ):
            with self.assertRaisesRegex(RuntimeError, "`psql`"):
                db_backup.restore_dump_file(Path("/tmp/nope.sql"))

        with patch("app.services.db_backup.settings", _fake_settings(app_env="local")), patch(
            "app.services.db_backup.shutil.which", return_value="/usr/bin/psql"
        ):
            with self.assertRaisesRegex(RuntimeError, "Dump file not found"):
                db_backup.restore_dump_file(Path("/tmp/nope.sql"))

    @patch("app.services.db_backup.subprocess.run")
    @patch("app.services.db_backup.shutil.which", return_value="/usr/bin/psql")
    def test_restore_dump_file_success_and_failure(
        self,
        _mock_which: Mock,
        mock_run: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "dump.sql"
            dump.write_text("select 1;", encoding="utf-8")
            with patch("app.services.db_backup.settings", _fake_settings(app_env="local", db_name="goldenstackers")):
                mock_run.return_value = SimpleNamespace(returncode=0, stderr="")
                db_backup.restore_dump_file(dump)
                self.assertTrue(mock_run.called)

                mock_run.return_value = SimpleNamespace(returncode=1, stderr="restore failed")
                with self.assertRaisesRegex(RuntimeError, "restore failed"):
                    db_backup.restore_dump_file(dump)

    def test_s3_config_and_key_helpers(self) -> None:
        with patch("app.services.db_backup.settings", _fake_settings(storage_provider="s3", s3_bucket="bucket")):
            self.assertTrue(db_backup.s3_backup_enabled())
        with patch("app.services.db_backup.settings", _fake_settings(storage_provider="local", s3_bucket="bucket")):
            self.assertFalse(db_backup.s3_backup_enabled())

        with patch("app.services.db_backup.settings", _fake_settings(app_env="dev")):
            self.assertEqual(db_backup.backup_s3_key("a.sql"), "db-backups/dev/a.sql")

    def test_s3_client_requires_config(self) -> None:
        with patch("app.services.db_backup.settings", _fake_settings(storage_provider="local", s3_bucket="")):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                db_backup._s3_client()

    @patch("app.services.db_backup.boto3.session.Session")
    def test_s3_client_builds_session_with_and_without_endpoint(self, mock_session_cls: Mock) -> None:
        fake_client = object()
        fake_session = Mock()
        fake_session.client.return_value = fake_client
        mock_session_cls.return_value = fake_session

        with patch(
            "app.services.db_backup.settings",
            _fake_settings(
                storage_provider="s3",
                s3_bucket="bucket",
                aws_access_key_id="ak",
                aws_secret_access_key="sk",
                aws_region="us-west-2",
                s3_endpoint_url="https://minio.local",
            ),
        ):
            out = db_backup._s3_client()
            self.assertIs(out, fake_client)
            mock_session_cls.assert_called_once_with(
                aws_access_key_id="ak",
                aws_secret_access_key="sk",
                region_name="us-west-2",
            )
            _, kwargs = fake_session.client.call_args
            self.assertEqual(kwargs["service_name"], "s3")
            self.assertEqual(kwargs["endpoint_url"], "https://minio.local")

        mock_session_cls.reset_mock()
        fake_session.client.reset_mock()
        with patch(
            "app.services.db_backup.settings",
            _fake_settings(
                storage_provider="s3",
                s3_bucket="bucket",
                aws_access_key_id="",
                aws_secret_access_key="",
                aws_region="us-east-1",
                s3_endpoint_url="",
            ),
        ):
            db_backup._s3_client()
            mock_session_cls.assert_called_once_with(
                aws_access_key_id=None,
                aws_secret_access_key=None,
                region_name="us-east-1",
            )
            _, kwargs = fake_session.client.call_args
            self.assertNotIn("endpoint_url", kwargs)

    @patch("app.services.db_backup._s3_client")
    def test_upload_list_download_wrappers(self, mock_s3_client: Mock) -> None:
        client = Mock()
        mock_s3_client.return_value = client
        with patch("app.services.db_backup.settings", _fake_settings(s3_bucket="bucket", app_env="local")):
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "dump.sql"
                p.write_text("x", encoding="utf-8")
                key = db_backup.upload_backup_to_s3(p)
                self.assertEqual(key, "db-backups/local/dump.sql")

                with self.assertRaisesRegex(RuntimeError, "Dump file not found"):
                    db_backup.upload_backup_to_s3(Path(tmp) / "missing.sql")

                with patch.object(db_backup, "BotoCoreError", Exception):
                    client.upload_file.side_effect = Exception("upload boom")
                    with self.assertRaisesRegex(RuntimeError, "S3 upload failed"):
                        db_backup.upload_backup_to_s3(p, key="x")

                client.upload_file.side_effect = None
                client.list_objects_v2.return_value = {
                    "Contents": [
                        {"Key": "db-backups/local/b.sql", "Size": 2, "LastModified": "2026-03-01"},
                        {"Key": "db-backups/local/a.sql", "Size": 1, "LastModified": "2026-03-02"},
                    ]
                }
                rows = db_backup.list_backups_in_s3()
                self.assertEqual(rows[0]["key"], "db-backups/local/b.sql")
                with patch.object(db_backup, "BotoCoreError", Exception):
                    client.list_objects_v2.side_effect = Exception("list boom")
                    with self.assertRaisesRegex(RuntimeError, "S3 list failed"):
                        db_backup.list_backups_in_s3()
                client.list_objects_v2.side_effect = None

                out = db_backup.download_backup_from_s3("db-backups/local/a.sql")
                self.assertEqual(out.name, "a.sql")
                with patch.object(db_backup, "BotoCoreError", Exception):
                    client.download_file.side_effect = Exception("download boom")
                    with self.assertRaisesRegex(RuntimeError, "S3 download failed"):
                        db_backup.download_backup_from_s3("db-backups/local/a.sql")


if __name__ == "__main__":
    unittest.main()
