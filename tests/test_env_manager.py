import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import env_manager


class EnvManagerTests(unittest.TestCase):
    def test_parse_and_read_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text(
                "# comment\nA=1\n\nB = two words\nINVALID_LINE\nC=3=extra\n",
                encoding="utf-8",
            )
            lines = env_manager._parse_env_lines(p)
            self.assertTrue(lines)
            values = env_manager.read_env_file(str(p))
            self.assertEqual(values["A"], "1")
            self.assertEqual(values["B"], "two words")
            self.assertEqual(values["C"], "3=extra")
            self.assertEqual(env_manager.read_env_file(str(Path(tmp) / "missing.env")), {})

    def test_mask_env_value(self) -> None:
        self.assertEqual(env_manager.mask_env_value("OPENAI_API_KEY", ""), "")
        self.assertEqual(env_manager.mask_env_value("OPENAI_API_KEY", "abc"), "***")
        self.assertEqual(env_manager.mask_env_value("OPENAI_API_KEY", "abcdefghi"), "*****fghi")
        self.assertEqual(env_manager.mask_env_value("APP_NAME", "gold"), "gold")

    def test_is_editable_env_key(self) -> None:
        self.assertTrue(env_manager.is_editable_env_key("APP_ENV"))
        self.assertTrue(env_manager.is_editable_env_key("EBAY_CLIENT_ID"))
        self.assertFalse(env_manager.is_editable_env_key("POSTGRES_HOST"))
        self.assertFalse(env_manager.is_editable_env_key("RANDOM_KEY"))

    def test_upsert_and_ensure_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text("# keep\nAPP_ENV=local\n\n", encoding="utf-8")

            env_manager.upsert_env_key(str(p), "APP_ENV", "dev")
            text = p.read_text(encoding="utf-8")
            self.assertIn("APP_ENV=dev", text)
            self.assertIn("# keep", text)

            env_manager.upsert_env_key(str(p), "APP_NAME", "GS Inventory")
            text2 = p.read_text(encoding="utf-8")
            self.assertIn("APP_NAME=GS Inventory", text2)
            self.assertTrue(text2.endswith("\n"))

            added = env_manager.ensure_env_defaults(
                str(p),
                {"APP_NAME": "Already", "SYNC_ENABLED": "true", "OPENAI_API_KEY": ""},
            )
            self.assertEqual(added, ["SYNC_ENABLED", "OPENAI_API_KEY"])
            values = env_manager.read_env_file(str(p))
            self.assertEqual(values["SYNC_ENABLED"], "true")
            self.assertIn("OPENAI_API_KEY", values)

    def test_uses_env_file(self) -> None:
        self.assertTrue(env_manager.uses_env_file("local"))
        self.assertFalse(env_manager.uses_env_file("dev"))
        with patch.dict("os.environ", {"APP_ENV": "production"}, clear=False):
            self.assertFalse(env_manager.uses_env_file())

    def test_read_process_env_values(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "APP_ENV": "dev",
                "APP_NAME": "GS",
                "POSTGRES_HOST": "db",
                "RANDOM_KEY": "x",
            },
            clear=True,
        ):
            tracked_only = env_manager.read_process_env_values(tracked_keys={"APP_ENV"})
            self.assertEqual(tracked_only["APP_ENV"], "dev")
            self.assertNotIn("RANDOM_KEY", tracked_only)

            with_editable = env_manager.read_process_env_values(tracked_keys={"APP_ENV"}, include_untracked_editable=True)
            self.assertIn("APP_NAME", with_editable)
            self.assertIn("POSTGRES_HOST", with_editable)
            self.assertNotIn("RANDOM_KEY", with_editable)


if __name__ == "__main__":
    unittest.main()
