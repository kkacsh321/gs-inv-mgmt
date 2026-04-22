from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.services import notification_outbox


class _FakeRepo:
    def __init__(self) -> None:
        self.rows: dict[int, SimpleNamespace] = {}
        self._next_id = 1

    def seed(
        self,
        *,
        status: str = "queued",
        payload: dict | None = None,
        next_attempt_at: datetime | None = None,
        max_attempts: int = 3,
    ) -> int:
        row_id = self._next_id
        self._next_id += 1
        self.rows[row_id] = SimpleNamespace(
            id=row_id,
            environment="local",
            channel="slack",
            event_type="test_event",
            payload_json=json.dumps(payload or {"text": "hello", "channel": "#ops"}),
            status=status,
            attempt_count=0,
            max_attempts=max_attempts,
            next_attempt_at=next_attempt_at or datetime(2026, 4, 12, 10, 0, 0),
            last_attempt_at=None,
            dispatched_at=None,
            locked_by="",
            locked_at=None,
            last_error="",
            updated_by="system",
        )
        return row_id

    def get_notification_outbox(self, outbox_id: int, *, environment: str | None = None):
        row = self.rows.get(int(outbox_id))
        if row is None:
            return None
        if environment and row.environment != environment:
            return None
        return row

    def list_notification_outbox(
        self,
        *,
        environment: str,
        statuses: set[str] | None = None,
        limit: int = 200,
        channel: str | None = None,
        due_before: datetime | None = None,
    ):
        rows = [r for r in self.rows.values() if r.environment == environment]
        if statuses:
            rows = [r for r in rows if str(r.status or "").lower() in {s.lower() for s in statuses}]
        if due_before is not None:
            rows = [r for r in rows if r.next_attempt_at is None or r.next_attempt_at <= due_before]
        rows = sorted(rows, key=lambda r: (r.next_attempt_at, r.id))
        return rows[:limit]

    def update_notification_outbox(self, outbox_id: int, updates: dict, actor: str = "system"):
        row = self.rows[int(outbox_id)]
        for k, v in updates.items():
            setattr(row, k, v)
        row.updated_by = actor
        return row


class NotificationOutboxServiceTests(unittest.TestCase):
    def test_process_outbox_row_success_marks_sent(self) -> None:
        repo = _FakeRepo()
        row_id = repo.seed(status="queued")
        with patch("app.services.notification_outbox.utcnow_naive", return_value=datetime(2026, 4, 12, 10, 10, 0)), patch(
            "app.services.notification_outbox.send_slack_message",
            return_value={"channel": "#ops", "ts": "1.0"},
        ):
            ok, msg = notification_outbox.process_notification_outbox_row(repo, outbox_id=row_id, actor="worker")
        self.assertTrue(ok)
        self.assertIn("Delivered", msg)
        row = repo.rows[row_id]
        self.assertEqual(row.status, "sent")
        self.assertEqual(row.attempt_count, 1)
        self.assertEqual(row.locked_by, "")
        self.assertIsNotNone(row.dispatched_at)

    def test_process_outbox_row_failure_sets_retrying(self) -> None:
        repo = _FakeRepo()
        row_id = repo.seed(status="queued", max_attempts=3)
        with patch("app.services.notification_outbox.utcnow_naive", return_value=datetime(2026, 4, 12, 10, 10, 0)), patch(
            "app.services.notification_outbox.send_slack_message",
            side_effect=RuntimeError("network down"),
        ), patch("app.services.notification_outbox.get_runtime_int", side_effect=[60, 3600]):
            ok, _ = notification_outbox.process_notification_outbox_row(repo, outbox_id=row_id, actor="worker")
        self.assertFalse(ok)
        row = repo.rows[row_id]
        self.assertEqual(row.status, "retrying")
        self.assertEqual(row.attempt_count, 1)
        self.assertIn("network down", row.last_error)
        self.assertGreater(row.next_attempt_at, datetime(2026, 4, 12, 10, 10, 0))

    def test_process_due_notification_outbox_counts_due_rows(self) -> None:
        repo = _FakeRepo()
        due_id = repo.seed(status="queued", next_attempt_at=datetime(2026, 4, 12, 10, 0, 0))
        repo.seed(status="queued", next_attempt_at=datetime(2026, 4, 12, 12, 0, 0))
        with patch("app.services.notification_outbox.utcnow_naive", return_value=datetime(2026, 4, 12, 10, 30, 0)), patch(
            "app.services.notification_outbox.send_slack_message",
            return_value={"channel": "#ops", "ts": "1.0"},
        ):
            result = notification_outbox.process_due_notification_outbox(
                repo,
                environment="local",
                actor="worker",
                limit=20,
            )
        self.assertEqual(result["due"], 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(repo.rows[due_id].status, "sent")
