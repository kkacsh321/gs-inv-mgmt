import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.services.ebay_health import _parse_iso, summarize_ebay_connection_status


class EbayHealthTests(unittest.TestCase):
    def test_parse_iso_handles_invalid_and_valid_values(self) -> None:
        self.assertIsNone(_parse_iso(""))
        self.assertIsNone(_parse_iso("not-a-date"))
        parsed = _parse_iso("2026-04-13T13:16:09.905Z")
        self.assertEqual(parsed, datetime(2026, 4, 13, 13, 16, 9, 905000))

    def test_summarize_status_with_verify_and_fresh_health_run(self) -> None:
        now = datetime(2026, 4, 13, 12, 0, 0)
        verify_success = SimpleNamespace(
            entity_type="ebay_verify",
            created_at=now - timedelta(minutes=2),
            actor="admin",
            changes={
                "status": "success",
                "resolved_user": "goldenstackers",
                "seller_registered": True,
                "message": "ok",
            },
        )
        verify_error = SimpleNamespace(
            entity_type="ebay_verify",
            created_at=now - timedelta(minutes=5),
            actor="admin",
            changes={"status": "error", "message": "bad token"},
        )
        health_row = SimpleNamespace(
            id=10,
            job_name="ebay_connection_health_check",
            status="success",
            completed_at=now - timedelta(minutes=5),
            notes="healthy",
        )
        repo = SimpleNamespace(
            list_audit_logs=lambda limit=500: [verify_success, verify_error],
            list_sync_runs=lambda provider="ebay", limit=500: [health_row],
        )
        with patch("app.services.ebay_health.utcnow_naive", return_value=now), patch(
            "app.services.ebay_health.get_runtime_str"
        ) as get_runtime_str_mock, patch("app.services.runtime_settings.get_runtime_int", return_value=30):
            # ebay_user_access_token, ebay_user_refresh_token, expires_at
            get_runtime_str_mock.side_effect = [
                "token-value",
                "refresh-value",
                "2026-04-13T13:00:00Z",
            ]
            status = summarize_ebay_connection_status(repo)

        self.assertTrue(status["token_present"])
        self.assertTrue(status["refresh_token_present"])
        self.assertFalse(status["token_expired"])
        self.assertEqual(status["token_expires_in_minutes"], 60)
        self.assertEqual((status["latest_verify_success"] or {}).get("resolved_user"), "goldenstackers")
        self.assertEqual((status["latest_verify_error"] or {}).get("message"), "bad token")
        self.assertEqual(status["latest_health_run_id"], 10)
        self.assertEqual(status["latest_health_status"], "success")
        self.assertFalse(status["health_stale"])

    def test_summarize_status_handles_repo_and_runtime_failures(self) -> None:
        now = datetime(2026, 4, 13, 12, 0, 0)
        stale_health_row = SimpleNamespace(
            id=2,
            job_name="ebay_connection_health_check",
            status="error",
            completed_at=now - timedelta(hours=3),
            notes="stale",
        )

        class Repo:
            def list_audit_logs(self, limit=500):
                raise RuntimeError("db down")

            def list_sync_runs(self, provider="ebay", limit=500):
                return [stale_health_row]

        with patch("app.services.ebay_health.utcnow_naive", return_value=now), patch(
            "app.services.ebay_health.get_runtime_str"
        ) as get_runtime_str_mock, patch("app.services.runtime_settings.get_runtime_int", side_effect=RuntimeError("boom")):
            get_runtime_str_mock.side_effect = [
                "",  # no access token
                "",  # no refresh token
                "not-a-date",  # invalid expiry
            ]
            status = summarize_ebay_connection_status(Repo())

        self.assertFalse(status["token_present"])
        self.assertFalse(status["refresh_token_present"])
        self.assertIsNone(status["token_expires_at"])
        self.assertIsNone(status["token_expires_in_minutes"])
        self.assertFalse(status["token_expired"])
        self.assertIsNone(status["latest_verify_success"])
        self.assertIsNone(status["latest_verify_error"])
        self.assertEqual(status["health_interval_minutes"], 30)
        self.assertTrue(status["health_stale"])


if __name__ == "__main__":
    unittest.main()
