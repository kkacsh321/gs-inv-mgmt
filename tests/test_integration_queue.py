import base64
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.services import integration_queue
from app.utils.time import utcnow_naive


class _FakeDB:
    def __init__(self, rows: dict[int, object] | None = None) -> None:
        self.rows = rows or {}

    def get(self, _model, row_id: int):
        return self.rows.get(int(row_id))


class _FakeRepo:
    def __init__(self) -> None:
        self.db = _FakeDB()
        self.updated_sales: list[tuple[int, dict, str]] = []
        self.updated_jobs: list[tuple[int, dict, str]] = []
        self.logged_events: list[dict] = []
        self.queue_rows: list[object] = []

    def update_sale(self, sale_id: int, updates: dict, *, actor: str):
        self.updated_sales.append((sale_id, updates, actor))

    def update_integration_queue_job(self, job_id: int, updates: dict, *, actor: str):
        self.updated_jobs.append((job_id, updates, actor))
        row = self.db.get(None, int(job_id))
        if row is not None:
            for key, value in updates.items():
                setattr(row, key, value)

    def log_integration_event(self, **kwargs):
        self.logged_events.append(kwargs)

    def list_integration_queue_jobs(self, **_kwargs):
        return list(self.queue_rows)


class IntegrationQueueTests(unittest.TestCase):
    def test_calc_backoff_seconds_google_and_shipping(self) -> None:
        with patch("app.services.integration_queue.get_runtime_int", side_effect=[120, 1000]):
            self.assertEqual(integration_queue._calc_backoff_seconds(object(), 1, integration="google"), 240)
        with patch("app.services.integration_queue.get_runtime_int", side_effect=[60, 3600]):
            self.assertEqual(integration_queue._calc_backoff_seconds(object(), 2, integration="shipping"), 240)
        with patch("app.services.integration_queue.get_runtime_int", side_effect=[30, 300]):
            self.assertEqual(integration_queue._calc_backoff_seconds(object(), 3, integration="slack"), 240)

    def test_capture_queue_execute_exception_tolerates_log_errors(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(id=1, integration="google", action="gmail")
        with patch.object(repo, "log_integration_event", side_effect=RuntimeError("log-failed")):
            message = integration_queue._capture_queue_execute_exception(
                repo,
                actor="qa",
                job=job,
                exc=RuntimeError("boom"),
            )
        self.assertIn("boom", message)

    def test_emit_terminal_queue_failure_alert_guardrails(self) -> None:
        repo = _FakeRepo()
        job_google = SimpleNamespace(id=10, integration="google", action="gmail_send_document_email", max_retries=3)
        job_other = SimpleNamespace(id=11, integration="shipping", action="purchase_label", max_retries=3)

        # slack disabled -> no dispatch
        with patch("app.services.integration_queue.resolve_slack_notify_config", return_value=SimpleNamespace(enabled=False)):
            integration_queue._emit_terminal_queue_failure_alert(
                repo, actor="qa", job=job_google, retry_count=4, error_text="x"
            )

        # google integration with both toggles false -> no dispatch
        with patch("app.services.integration_queue.resolve_slack_notify_config", return_value=SimpleNamespace(enabled=True)), patch(
            "app.services.integration_queue.get_runtime_bool", side_effect=[False, False]
        ), patch("app.services.integration_queue.dispatch_slack_alert") as dispatch:
            integration_queue._emit_terminal_queue_failure_alert(
                repo, actor="qa", job=job_google, retry_count=4, error_text="x"
            )
        dispatch.assert_not_called()

        # non-google with general toggle false -> no dispatch
        with patch("app.services.integration_queue.resolve_slack_notify_config", return_value=SimpleNamespace(enabled=True)), patch(
            "app.services.integration_queue.get_runtime_bool", return_value=False
        ), patch("app.services.integration_queue.dispatch_slack_alert") as dispatch2:
            integration_queue._emit_terminal_queue_failure_alert(
                repo, actor="qa", job=job_other, retry_count=4, error_text="x"
            )
        dispatch2.assert_not_called()

        # enabled path -> dispatch called
        with patch("app.services.integration_queue.resolve_slack_notify_config", return_value=SimpleNamespace(enabled=True)), patch(
            "app.services.integration_queue.get_runtime_bool", side_effect=[True, True]
        ), patch("app.services.integration_queue.build_slack_alert_text", return_value="alert"), patch(
            "app.services.integration_queue.dispatch_slack_alert"
        ) as dispatch3:
            integration_queue._emit_terminal_queue_failure_alert(
                repo, actor="qa", job=job_google, retry_count=4, error_text="x"
            )
        dispatch3.assert_called_once()

    def test_execute_integration_queue_job_rejects_unsupported(self) -> None:
        job = SimpleNamespace(integration="other", action="noop", payload_json="{}")
        ok, message = integration_queue.execute_integration_queue_job(_FakeRepo(), job, actor="qa")
        self.assertFalse(ok)
        self.assertIn("Unsupported integration", message)

    def test_execute_integration_queue_job_bad_payload_json(self) -> None:
        job = SimpleNamespace(integration="slack", action="post_message", payload_json="{bad-json")
        with patch("app.services.integration_queue.send_slack_message") as send_slack:
            ok, message = integration_queue.execute_integration_queue_job(_FakeRepo(), job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("Slack post completed", message)
        send_slack.assert_called_once()

    def test_execute_integration_queue_job_slack_post(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            integration="slack",
            action="post_message",
            payload_json=json.dumps({"text": "hello", "channel": "#ops"}),
        )
        with patch("app.services.integration_queue.send_slack_message") as send_slack:
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("Slack post completed", message)
        send_slack.assert_called_once()

    def test_execute_integration_queue_job_slack_unsupported_action(self) -> None:
        job = SimpleNamespace(integration="slack", action="other", payload_json="{}")
        ok, message = integration_queue.execute_integration_queue_job(_FakeRepo(), job, actor="qa")
        self.assertFalse(ok)
        self.assertIn("Unsupported slack action", message)

    def test_execute_integration_queue_job_shipping_dry_run(self) -> None:
        repo = _FakeRepo()
        sale = SimpleNamespace(id=10, tracking_status="")
        repo.db.rows[10] = sale
        job = SimpleNamespace(
            id=1,
            integration="shipping",
            action="purchase_label",
            payload_json=json.dumps({"sale_id": 10, "dry_run": True}),
        )
        with patch("app.services.integration_queue.get_runtime_bool", return_value=True):
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("dry-run", message)
        self.assertEqual(repo.updated_sales, [])

    def test_execute_integration_queue_job_shipping_validation_branches(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(id=1, integration="shipping", action="other", payload_json="{}")
        ok, msg = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertFalse(ok)
        self.assertIn("Unsupported shipping action", msg)

        job2 = SimpleNamespace(id=2, integration="shipping", action="purchase_label", payload_json="{}")
        with patch("app.services.integration_queue.get_runtime_bool", return_value=False):
            ok, msg = integration_queue.execute_integration_queue_job(repo, job2, actor="qa")
        self.assertFalse(ok)
        self.assertIn("Shipping queue is disabled", msg)

        # queue enabled, purchase disabled
        with patch("app.services.integration_queue.get_runtime_bool", side_effect=[True, False]):
            ok, msg = integration_queue.execute_integration_queue_job(repo, job2, actor="qa")
        self.assertFalse(ok)
        self.assertIn("purchase is disabled", msg)

        # invalid sale payload
        with patch("app.services.integration_queue.get_runtime_bool", side_effect=[True, True]):
            ok, msg = integration_queue.execute_integration_queue_job(
                repo,
                SimpleNamespace(id=3, integration="shipping", action="purchase_label", payload_json='{"sale_id":"x"}'),
                actor="qa",
            )
        self.assertFalse(ok)
        self.assertIn("Missing/invalid `sale_id`", msg)

        # missing sale row
        with patch("app.services.integration_queue.get_runtime_bool", side_effect=[True, True]):
            ok, msg = integration_queue.execute_integration_queue_job(
                repo,
                SimpleNamespace(id=4, integration="shipping", action="purchase_label", payload_json='{"sale_id":999}'),
                actor="qa",
            )
        self.assertFalse(ok)
        self.assertIn("not found", msg)

    def test_execute_integration_queue_job_shipping_scaffold_updates_sale(self) -> None:
        repo = _FakeRepo()
        sale = SimpleNamespace(id=11, tracking_status="")
        repo.db.rows[11] = sale
        payload = {
            "sale_id": 11,
            "shipping_provider": "usps",
            "tracking_number": "TRACK123",
            "shipping_service": "Ground",
            "shipping_package_type": "Box",
        }
        job = SimpleNamespace(
            id=2,
            integration="shipping",
            action="purchase_label",
            payload_json=json.dumps(payload),
        )
        with patch("app.services.integration_queue.get_runtime_bool") as runtime_bool:
            runtime_bool.side_effect = lambda *_args, **_kwargs: False if _args[1] == "shipping_label_live_provider_calls_enabled" else True
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("scaffold", message)
        self.assertEqual(len(repo.updated_sales), 1)
        _, updates, _ = repo.updated_sales[0]
        self.assertEqual(updates["tracking_number"], "TRACK123")
        self.assertEqual(updates["shipping_provider"], "usps")
        self.assertEqual(updates["tracking_status"], "label_created")

    def test_execute_integration_queue_job_shipping_live_provider_path(self) -> None:
        repo = _FakeRepo()
        sale = SimpleNamespace(id=20, tracking_status="")
        repo.db.rows[20] = sale
        job = SimpleNamespace(
            id=20,
            integration="shipping",
            action="purchase_label",
            payload_json=json.dumps({"sale_id": 20, "shipping_provider": "usps"}),
        )

        def _runtime_bool(_repo, key, default=True):
            if key == "shipping_queue_enabled":
                return True
            if key == "shipping_label_purchase_enabled":
                return True
            if key == "shipping_label_provider_usps_enabled":
                return True
            if key == "shipping_label_live_provider_calls_enabled":
                return True
            return default

        provider_result = SimpleNamespace(
            label_id="LBL-1",
            label_url="https://x/label.pdf",
            label_cost=4.5,
            label_currency="USD",
            tracking_number="TRACKX",
        )
        with patch("app.services.integration_queue.get_runtime_bool", side_effect=_runtime_bool), patch(
            "app.services.integration_queue.purchase_shipping_label", return_value=provider_result
        ):
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("completed", message)
        _, updates, _ = repo.updated_sales[0]
        self.assertEqual(updates["shipping_label_id"], "LBL-1")
        self.assertEqual(updates["tracking_number"], "TRACKX")

    def test_execute_integration_queue_job_shipping_provider_disabled(self) -> None:
        repo = _FakeRepo()
        repo.db.rows[21] = SimpleNamespace(id=21, tracking_status="")
        job = SimpleNamespace(
            id=21,
            integration="shipping",
            action="purchase_label",
            payload_json=json.dumps({"sale_id": 21, "shipping_provider": "usps"}),
        )

        def _runtime_bool(_repo, key, default=True):
            if key in {"shipping_queue_enabled", "shipping_label_purchase_enabled"}:
                return True
            if key == "shipping_label_provider_usps_enabled":
                return False
            return default

        with patch("app.services.integration_queue.get_runtime_bool", side_effect=_runtime_bool):
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertFalse(ok)
        self.assertIn("provider `usps` is disabled", message)

    def test_execute_integration_queue_job_google_drive_missing_payload(self) -> None:
        job = SimpleNamespace(integration="google", action="drive_upload_artifact", payload_json="{}")
        with patch("app.services.integration_queue.resolve_google_workspace_config", return_value=SimpleNamespace()):
            ok, message = integration_queue.execute_integration_queue_job(_FakeRepo(), job, actor="qa")
        self.assertFalse(ok)
        self.assertIn("Missing `file_b64` payload", message)

    def test_execute_integration_queue_job_google_drive_upload(self) -> None:
        file_bytes = b"hello"
        job = SimpleNamespace(
            integration="google",
            action="drive_upload_artifact",
            payload_json=json.dumps(
                {
                    "file_b64": base64.b64encode(file_bytes).decode("utf-8"),
                    "file_name": "x.txt",
                    "mime_type": "text/plain",
                    "folder_id": "abc",
                }
            ),
        )
        with patch("app.services.integration_queue.resolve_google_workspace_config", return_value=SimpleNamespace()), patch(
            "app.services.integration_queue.upload_drive_file"
        ) as upload:
            ok, message = integration_queue.execute_integration_queue_job(_FakeRepo(), job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("Drive upload completed", message)
        upload.assert_called_once()

    def test_execute_integration_queue_job_google_routes_gmail_and_calendar(self) -> None:
        repo = _FakeRepo()
        gmail_job = SimpleNamespace(
            integration="google",
            action="gmail_send_document_email",
            payload_json=json.dumps({"to_email": "x@y.com", "subject": "s", "body_html": "<p>x</p>"}),
        )
        cal_job = SimpleNamespace(
            integration="google",
            action="calendar_create_event",
            payload_json=json.dumps({"summary": "s", "start_iso": "2026-01-01T00:00:00", "end_iso": "2026-01-01T01:00:00"}),
        )
        with patch("app.services.integration_queue.resolve_google_workspace_config", return_value=SimpleNamespace(default_timezone="UTC", default_calendar_id="primary")), patch(
            "app.services.integration_queue.send_gmail_message"
        ) as send_gmail, patch("app.services.integration_queue.create_calendar_event") as create_event:
            ok1, _ = integration_queue.execute_integration_queue_job(repo, gmail_job, actor="qa")
            ok2, _ = integration_queue.execute_integration_queue_job(repo, cal_job, actor="qa")
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        send_gmail.assert_called_once()
        create_event.assert_called_once()

    def test_execute_integration_queue_job_google_unsupported_action(self) -> None:
        job = SimpleNamespace(integration="google", action="other", payload_json="{}")
        with patch("app.services.integration_queue.resolve_google_workspace_config", return_value=SimpleNamespace()):
            ok, message = integration_queue.execute_integration_queue_job(_FakeRepo(), job, actor="qa")
        self.assertFalse(ok)
        self.assertIn("Unsupported integration action", message)

    def test_process_integration_queue_job_success_path(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(id=30, integration="google", action="gmail_send_document_email", retry_count=0, max_retries=3)
        repo.db.rows[30] = job
        with patch("app.services.integration_queue.execute_integration_queue_job", return_value=(True, "ok")):
            ok, _ = integration_queue.process_integration_queue_job(repo, job_id=30, actor="qa")
        self.assertTrue(ok)
        self.assertTrue(any(u[1].get("status") == "success" for u in repo.updated_jobs))
        self.assertTrue(any(e.get("status") == "success" for e in repo.logged_events))

    def test_process_integration_queue_job_exception_captured(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(id=33, integration="google", action="gmail_send_document_email", retry_count=0, max_retries=1)
        repo.db.rows[33] = job
        with patch("app.services.integration_queue.execute_integration_queue_job", side_effect=RuntimeError("boom")):
            ok, message = integration_queue.process_integration_queue_job(repo, job_id=33, actor="qa")
        self.assertFalse(ok)
        self.assertIn("boom", message)

    def test_process_integration_queue_job_failure_requeues_when_retry_left(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(id=31, integration="google", action="gmail_send_document_email", retry_count=0, max_retries=2)
        repo.db.rows[31] = job
        with patch("app.services.integration_queue.execute_integration_queue_job", return_value=(False, "bad")):
            ok, _ = integration_queue.process_integration_queue_job(repo, job_id=31, actor="qa")
        self.assertFalse(ok)
        queued_updates = [u for u in repo.updated_jobs if u[1].get("status") == "queued"]
        self.assertEqual(len(queued_updates), 1)
        self.assertIn("next_attempt_at", queued_updates[0][1])

    def test_process_integration_queue_job_failure_terminal(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(id=32, integration="google", action="gmail_send_document_email", retry_count=2, max_retries=2)
        repo.db.rows[32] = job
        with patch("app.services.integration_queue.execute_integration_queue_job", return_value=(False, "bad")), patch(
            "app.services.integration_queue._emit_terminal_queue_failure_alert"
        ) as emit_alert:
            ok, _ = integration_queue.process_integration_queue_job(repo, job_id=32, actor="qa")
        self.assertFalse(ok)
        self.assertTrue(any(u[1].get("status") == "failed" for u in repo.updated_jobs))
        emit_alert.assert_called_once()

    def test_process_integration_queue_job_not_found_raises(self) -> None:
        with self.assertRaises(ValueError):
            integration_queue.process_integration_queue_job(_FakeRepo(), job_id=999, actor="qa")

    def test_process_due_integration_queue_jobs_handles_blocked_and_processed(self) -> None:
        repo = _FakeRepo()
        now = utcnow_naive()
        row1 = SimpleNamespace(id=41, next_attempt_at=now - timedelta(minutes=1))
        row2 = SimpleNamespace(id=42, next_attempt_at=now - timedelta(minutes=1))
        job1 = SimpleNamespace(id=41, integration="google", action="gmail_send_document_email")
        job2 = SimpleNamespace(id=42, integration="google", action="gmail_send_document_email")
        repo.queue_rows = [row1, row2]
        repo.db.rows[41] = job1
        repo.db.rows[42] = job2

        def fake_eval(_repo, job, actor, trigger_status):
            if job.id == 41:
                return {"matched_rule_ids": [1], "applied_rule_ids": [], "approval_gated_rule_ids": [1], "blocked": True, "blocked_reason": "needs approval"}
            return {"matched_rule_ids": [2], "applied_rule_ids": [2], "approval_gated_rule_ids": [], "blocked": False}

        with patch("app.services.integration_queue.evaluate_and_apply_rules_for_job", side_effect=fake_eval), patch(
            "app.services.integration_queue.process_integration_queue_job", return_value=(True, "ok")
        ):
            summary = integration_queue.process_due_integration_queue_jobs(
                repo,
                integration="google",
                actor="qa",
                limit=10,
            )
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["success"], 1)
        self.assertEqual(summary["rules_matched"], 2)

    def test_process_due_integration_queue_jobs_counts_queued_and_failed(self) -> None:
        repo = _FakeRepo()
        now = utcnow_naive()
        row1 = SimpleNamespace(id=51, next_attempt_at=now - timedelta(minutes=1))
        row2 = SimpleNamespace(id=52, next_attempt_at=now - timedelta(minutes=1))
        job1 = SimpleNamespace(id=51, integration="google", action="gmail_send_document_email", status="queued")
        job2 = SimpleNamespace(id=52, integration="google", action="gmail_send_document_email", status="failed")
        repo.queue_rows = [row1, row2]
        repo.db.rows[51] = job1
        repo.db.rows[52] = job2
        with patch("app.services.integration_queue.evaluate_and_apply_rules_for_job", return_value={"matched_rule_ids": [], "applied_rule_ids": [], "approval_gated_rule_ids": [], "blocked": False}), patch(
            "app.services.integration_queue.process_integration_queue_job", return_value=(False, "bad")
        ):
            summary = integration_queue.process_due_integration_queue_jobs(
                repo,
                integration="google",
                actor="qa",
                limit=10,
            )
        # first refreshed row currently queued => queued bucket, second => failed bucket
        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["failed"], 1)

    def test_process_due_google_queue_jobs_wrapper(self) -> None:
        with patch("app.services.integration_queue.process_due_integration_queue_jobs", return_value={"processed": 1}) as proc:
            out = integration_queue.process_due_google_queue_jobs(_FakeRepo(), actor="qa", limit=5)
        self.assertEqual(out["processed"], 1)
        proc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
