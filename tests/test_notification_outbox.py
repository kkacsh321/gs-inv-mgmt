from __future__ import annotations

from datetime import datetime, timedelta
import unittest

from tests.test_support import in_memory_repo


class NotificationOutboxRepositoryTests(unittest.TestCase):
    def test_enqueue_notification_outbox_persists_and_audits(self) -> None:
        with in_memory_repo() as (_, repo):
            row = repo.enqueue_notification_outbox(
                environment="local",
                channel="slack",
                event_type="order_imported",
                entity_type="order",
                entity_id="ORDER-1",
                payload_json='{"ok":true}',
                requested_by="ops",
                dedupe_key="order:ORDER-1:order_imported",
                actor="ops",
            )

            self.assertGreater(row.id, 0)
            self.assertEqual(row.status, "queued")
            self.assertEqual(row.channel, "slack")
            self.assertEqual(row.event_type, "order_imported")
            self.assertEqual(row.entity_type, "order")
            self.assertEqual(row.entity_id, "ORDER-1")

            audits = repo.list_audit_logs_for_entity(
                entity_type="notification_outbox",
                entity_id=row.id,
                limit=20,
            )
            self.assertTrue(audits)
            self.assertEqual(audits[0].action, "create")

    def test_list_notification_outbox_filters_by_status(self) -> None:
        with in_memory_repo() as (_, repo):
            queued = repo.enqueue_notification_outbox(
                environment="local",
                channel="slack",
                event_type="daily_report",
                payload_json="{}",
                requested_by="system",
                actor="system",
            )
            repo.enqueue_notification_outbox(
                environment="local",
                channel="slack",
                event_type="backup_result",
                payload_json="{}",
                requested_by="system",
                actor="system",
            )
            repo.update_notification_outbox(queued.id, {"status": "sent"}, actor="system")

            pending = repo.list_notification_outbox(environment="local", statuses={"queued"})
            self.assertTrue(pending)
            self.assertTrue(all(row.status == "queued" for row in pending))

    def test_list_notification_outbox_filters_by_due_before(self) -> None:
        with in_memory_repo() as (_, repo):
            now = datetime(2026, 4, 12, 10, 0, 0)
            due_row = repo.enqueue_notification_outbox(
                environment="local",
                channel="slack",
                event_type="due",
                payload_json="{}",
                requested_by="system",
                next_attempt_at=now,
                actor="system",
            )
            repo.enqueue_notification_outbox(
                environment="local",
                channel="slack",
                event_type="future",
                payload_json="{}",
                requested_by="system",
                next_attempt_at=now + timedelta(hours=2),
                actor="system",
            )
            due = repo.list_notification_outbox(
                environment="local",
                statuses={"queued"},
                due_before=now + timedelta(minutes=10),
            )
            self.assertEqual([int(r.id) for r in due], [int(due_row.id)])

    def test_enqueue_notification_outbox_dedupe_key_reuses_existing_open_or_sent_row(self) -> None:
        with in_memory_repo() as (_, repo):
            row1 = repo.enqueue_notification_outbox(
                environment="local",
                channel="slack",
                event_type="order_imported",
                payload_json='{"v":1}',
                requested_by="system",
                dedupe_key="order:23-1:imported",
                actor="system",
            )
            row2 = repo.enqueue_notification_outbox(
                environment="local",
                channel="slack",
                event_type="order_imported",
                payload_json='{"v":2}',
                requested_by="system",
                dedupe_key="order:23-1:imported",
                actor="system",
            )
            self.assertEqual(int(row1.id), int(row2.id))
            rows = repo.list_notification_outbox(environment="local", statuses={"queued", "sent", "retrying", "processing"})
            self.assertEqual(len(rows), 1)

    def test_update_notification_outbox_updates_row_and_audits(self) -> None:
        with in_memory_repo() as (_, repo):
            row = repo.enqueue_notification_outbox(
                environment="local",
                channel="slack",
                event_type="order_imported",
                payload_json="{}",
                requested_by="system",
                actor="system",
            )
            when = datetime(2026, 4, 12, 10, 0, 0)
            next_when = when + timedelta(minutes=10)
            updated = repo.update_notification_outbox(
                row.id,
                {
                    "status": "failed",
                    "attempt_count": 1,
                    "last_attempt_at": when,
                    "next_attempt_at": next_when,
                    "last_error": "temporary failure",
                },
                actor="worker",
            )

            self.assertEqual(updated.status, "failed")
            self.assertEqual(int(updated.attempt_count), 1)
            self.assertEqual(updated.last_error, "temporary failure")
            self.assertEqual(updated.updated_by, "worker")

            audits = repo.list_audit_logs_for_entity(
                entity_type="notification_outbox",
                entity_id=row.id,
                limit=20,
            )
            self.assertTrue(any(a.action == "update" for a in audits))

    def test_cleanup_notification_outbox_deletes_old_sent_and_failed_rows(self) -> None:
        with in_memory_repo() as (_, repo):
            sent_row = repo.enqueue_notification_outbox(
                environment="local",
                channel="slack",
                event_type="old_sent",
                payload_json="{}",
                requested_by="system",
                actor="system",
            )
            failed_row = repo.enqueue_notification_outbox(
                environment="local",
                channel="slack",
                event_type="old_failed",
                payload_json="{}",
                requested_by="system",
                actor="system",
            )
            keep_row = repo.enqueue_notification_outbox(
                environment="local",
                channel="slack",
                event_type="keep_queued",
                payload_json="{}",
                requested_by="system",
                actor="system",
            )

            old_stamp = datetime(2026, 1, 1, 0, 0, 0)
            repo.update_notification_outbox(sent_row.id, {"status": "sent", "created_at": old_stamp}, actor="system")
            repo.update_notification_outbox(failed_row.id, {"status": "failed", "created_at": old_stamp}, actor="system")
            repo.update_notification_outbox(keep_row.id, {"status": "queued"}, actor="system")

            result = repo.cleanup_notification_outbox(
                environment="local",
                retain_sent_days=1,
                retain_failed_days=1,
                actor="system",
            )
            self.assertEqual(result["deleted_sent"], 1)
            self.assertEqual(result["deleted_failed"], 1)
            remaining = repo.list_notification_outbox(environment="local", statuses={"queued", "sent", "failed"}, limit=20)
            remaining_ids = {int(r.id) for r in remaining}
            self.assertIn(int(keep_row.id), remaining_ids)
            self.assertNotIn(int(sent_row.id), remaining_ids)
            self.assertNotIn(int(failed_row.id), remaining_ids)
