from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.utils.time import utcnow_naive

from app.services import slack_ops_bot
from app.services.integration_queue import process_integration_queue_job


class _FakeRepo:
    def __init__(self) -> None:
        self.audit_events: list[dict] = []
        self.integration_events: list[dict] = []
        self.queue_jobs: list[SimpleNamespace] = []
        self._job_id = 0
        self.db = SimpleNamespace(get=self._db_get)

    def record_audit_event(self, **kwargs):
        self.audit_events.append(dict(kwargs))

    def log_integration_event(self, **kwargs):
        self.integration_events.append(dict(kwargs))

    def create_integration_queue_job(self, **kwargs):
        self._job_id += 1
        row = SimpleNamespace(
            id=self._job_id,
            environment=str(kwargs.get("environment") or "").strip().lower(),
            integration=str(kwargs.get("integration") or "").strip().lower(),
            action=str(kwargs.get("action") or "").strip().lower(),
            status="queued",
            payload_json=str(kwargs.get("payload_json") or "{}"),
            requested_by=str(kwargs.get("requested_by") or "").strip(),
            created_at=utcnow_naive(),
            retry_count=0,
            max_retries=int(kwargs.get("max_retries") or 0),
            last_error="",
        )
        self.queue_jobs.append(row)
        return row

    def list_integration_queue_jobs(self, **kwargs):
        env = str(kwargs.get("environment") or "").strip().lower()
        integration = str(kwargs.get("integration") or "").strip().lower()
        statuses = {str(v).strip().lower() for v in (kwargs.get("statuses") or set()) if str(v).strip()}
        out = []
        for row in self.queue_jobs:
            if env and row.environment != env:
                continue
            if integration and row.integration != integration:
                continue
            if statuses and row.status not in statuses:
                continue
            out.append(row)
        return out[: int(kwargs.get("limit") or 200)]

    def update_integration_queue_job(self, job_id: int, updates: dict, *, actor: str = "system"):
        row = self._db_get(None, job_id)
        if row is None:
            raise ValueError("missing job")
        for k, v in updates.items():
            setattr(row, k, v)
        return row

    def _db_get(self, _model, row_id: int):
        for row in self.queue_jobs:
            if int(row.id) == int(row_id):
                return row
        return None


class SlackOpsBotTests(unittest.TestCase):
    def test_build_envelope_is_deterministic(self) -> None:
        payload = {
            "environment": "prod",
            "team_id": "T1",
            "channel_id": "C1",
            "channel_name": "ops",
            "message_ts": "123.456",
            "thread_ts": "123.456",
            "user_id": "U1",
            "user_name": "keith",
            "app_username": "keith",
            "app_role": "ops",
            "text": "  intake   coin  image   ",
            "files": [{"id": "F1", "name": "coin.jpg", "mimetype": "image/jpeg", "url_private": "x"}],
        }
        e1 = slack_ops_bot.build_slack_command_envelope(payload, default_env="dev")
        e2 = slack_ops_bot.build_slack_command_envelope(payload, default_env="dev")
        self.assertEqual(e1.idempotency_key, e2.idempotency_key)
        self.assertEqual(e1.intent, "intake")
        self.assertEqual(e1.args, ["coin", "image"])
        self.assertEqual(e1.environment, "prod")

    def test_route_rejects_unsupported_intent(self) -> None:
        repo = _FakeRepo()
        env = slack_ops_bot.build_slack_command_envelope(
            {
                "user_name": "viewer1",
                "app_role": "viewer",
                "text": "foobar thing",
                "message_ts": "1.2",
            },
            default_env="prod",
        )
        out = slack_ops_bot.route_slack_command_request(repo, envelope=env, actor="slack-bot")
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["reason"], "unsupported_intent")
        self.assertTrue(repo.audit_events)
        self.assertEqual(repo.audit_events[-1]["action"], "rejected")

    def test_route_denies_role_intent_mismatch(self) -> None:
        repo = _FakeRepo()
        env = slack_ops_bot.build_slack_command_envelope(
            {
                "user_name": "viewer1",
                "app_role": "viewer",
                "text": "operations run_sync",
                "message_ts": "1.3",
            },
            default_env="prod",
        )
        out = slack_ops_bot.route_slack_command_request(repo, envelope=env, actor="slack-bot")
        self.assertEqual(out["status"], "denied")
        self.assertEqual(out["reason"], "role_not_allowed")
        self.assertEqual(out["role"], "viewer")
        self.assertTrue(repo.audit_events)
        self.assertEqual(repo.audit_events[-1]["action"], "denied")

    def test_route_accepts_allowed_intent_and_logs(self) -> None:
        repo = _FakeRepo()
        env = slack_ops_bot.build_slack_command_envelope(
            {
                "team_id": "T1",
                "channel_id": "COPS",
                "thread_ts": "2.1",
                "user_name": "ops1",
                "app_role": "ops",
                "text": "operations queue_status",
                "message_ts": "2.1",
            },
            default_env="prod",
        )
        out = slack_ops_bot.route_slack_command_request(repo, envelope=env, actor="slack-bot")
        self.assertEqual(out["status"], "accepted")
        self.assertEqual(out["intent"], "operations")
        self.assertTrue(out["write_intent"])
        self.assertTrue(repo.audit_events)
        self.assertEqual(repo.audit_events[-1]["action"], "accepted")
        self.assertTrue(repo.integration_events)
        self.assertEqual(repo.integration_events[-1]["integration"], "slack_ops")
        self.assertEqual(repo.integration_events[-1]["action"], "command_routed")

    def test_ingest_queues_accepted_slack_command(self) -> None:
        repo = _FakeRepo()
        result = slack_ops_bot.ingest_slack_command_request(
            repo,
            payload={
                "environment": "prod",
                "team_id": "T1",
                "channel_id": "C1",
                "channel_name": "ops",
                "thread_ts": "101.1",
                "message_ts": "101.1",
                "user_id": "U1",
                "user_name": "ops-user",
                "app_username": "ops-user",
                "app_role": "ops",
                "text": "comp 1909 vdb cent",
            },
            actor="slack-bot",
            default_env="local",
        )
        self.assertEqual(result["status"], "queued")
        self.assertTrue(result["queued"])
        self.assertEqual(result["intent"], "comp")
        self.assertEqual(result["queue_job_id"], 1)
        self.assertEqual(len(repo.queue_jobs), 1)
        payload = json.loads(repo.queue_jobs[0].payload_json)
        self.assertEqual(payload["request_context"]["channel_id"], "C1")
        self.assertEqual(payload["request_context"]["thread_ts"], "101.1")
        self.assertEqual(payload["request_context"]["slack_user_id"], "U1")
        self.assertEqual(payload["request_context"]["app_role"], "ops")
        self.assertEqual(payload["command"]["intent"], "comp")
        self.assertEqual(payload["idempotency_key"], result["idempotency_key"])

    def test_ingest_dedupes_by_idempotency_key(self) -> None:
        repo = _FakeRepo()
        payload = {
            "environment": "prod",
            "team_id": "T1",
            "channel_id": "C1",
            "thread_ts": "200.1",
            "message_ts": "200.1",
            "user_id": "U1",
            "user_name": "ops-user",
            "app_username": "ops-user",
            "app_role": "ops",
            "text": "status queue",
        }
        first = slack_ops_bot.ingest_slack_command_request(repo, payload=payload, actor="slack-bot", default_env="local")
        second = slack_ops_bot.ingest_slack_command_request(repo, payload=payload, actor="slack-bot", default_env="local")
        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "duplicate")
        self.assertFalse(second["queued"])
        self.assertEqual(second["queue_job_id"], first["queue_job_id"])
        self.assertEqual(len(repo.queue_jobs), 1)

    def test_ingest_dedupes_against_success_jobs(self) -> None:
        repo = _FakeRepo()
        payload = {
            "environment": "prod",
            "team_id": "T1",
            "channel_id": "C1",
            "thread_ts": "200.2",
            "message_ts": "200.2",
            "user_id": "U1",
            "user_name": "ops-user",
            "app_username": "ops-user",
            "app_role": "ops",
            "text": "status queue",
        }
        first = slack_ops_bot.ingest_slack_command_request(repo, payload=payload, actor="slack-bot", default_env="local")
        self.assertEqual(first["status"], "queued")
        repo.queue_jobs[0].status = "success"
        second = slack_ops_bot.ingest_slack_command_request(repo, payload=payload, actor="slack-bot", default_env="local")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["queue_job_id"], first["queue_job_id"])
        self.assertEqual(len(repo.queue_jobs), 1)

    def test_ingest_returns_denied_without_queueing(self) -> None:
        repo = _FakeRepo()
        out = slack_ops_bot.ingest_slack_command_request(
            repo,
            payload={
                "environment": "prod",
                "message_ts": "1.1",
                "app_role": "viewer",
                "user_name": "viewer1",
                "text": "operations run_sync",
            },
            actor="slack-bot",
            default_env="local",
        )
        self.assertEqual(out["status"], "denied")
        self.assertFalse(out["queued"])
        self.assertEqual(len(repo.queue_jobs), 0)

    def test_ingest_write_intent_is_pending_approval_when_enabled(self) -> None:
        repo = _FakeRepo()
        with patch("app.services.slack_ops_bot.get_runtime_bool", return_value=True):
            out = slack_ops_bot.ingest_slack_command_request(
                repo,
                payload={
                    "environment": "prod",
                    "team_id": "T1",
                    "channel_id": "C1",
                    "thread_ts": "300.1",
                    "message_ts": "300.1",
                    "user_name": "ops-user",
                    "app_username": "ops-user",
                    "app_role": "ops",
                    "text": "operations run_sync",
                },
                actor="slack-bot",
            )
        self.assertEqual(out["status"], "pending_approval")
        self.assertTrue(out["approval_required"])
        self.assertFalse(out["queued"])
        self.assertEqual(len(repo.queue_jobs), 1)
        self.assertEqual(repo.queue_jobs[0].status, "blocked")
        payload = json.loads(repo.queue_jobs[0].payload_json)
        self.assertEqual(payload["approval"]["status"], "pending")

    def test_approve_slack_ops_queue_job_updates_to_queued(self) -> None:
        repo = _FakeRepo()
        with patch("app.services.slack_ops_bot.get_runtime_bool", return_value=True):
            created = slack_ops_bot.ingest_slack_command_request(
                repo,
                payload={
                    "environment": "prod",
                    "team_id": "T1",
                    "channel_id": "C1",
                    "thread_ts": "301.1",
                    "message_ts": "301.1",
                    "user_name": "ops-user",
                    "app_username": "ops-user",
                    "app_role": "ops",
                    "text": "operations run_sync",
                },
                actor="slack-bot",
            )
        self.assertEqual(created["status"], "pending_approval")
        result = slack_ops_bot.approve_slack_ops_queue_job(
            repo,
            queue_job_id=int(created["queue_job_id"]),
            approver_username="admin1",
            approver_role="admin",
            actor="admin-ui",
        )
        self.assertEqual(result["status"], "approved")
        row = repo._db_get(None, int(created["queue_job_id"]))
        self.assertEqual(row.status, "queued")
        payload = json.loads(row.payload_json)
        self.assertEqual(payload["approval"]["status"], "approved")
        self.assertEqual(payload["approval"]["approved_by"], "admin1")

    def test_approval_gated_command_executes_after_approval(self) -> None:
        repo = _FakeRepo()
        with patch("app.services.slack_ops_bot.get_runtime_bool", return_value=True):
            created = slack_ops_bot.ingest_slack_command_request(
                repo,
                payload={
                    "environment": "prod",
                    "team_id": "T1",
                    "channel_id": "C1",
                    "thread_ts": "302.1",
                    "message_ts": "302.1",
                    "user_name": "ops-user",
                    "app_username": "ops-user",
                    "app_role": "ops",
                    "text": "operations run_sync",
                },
                actor="slack-bot",
            )
        self.assertEqual(created["status"], "pending_approval")
        job_id = int(created["queue_job_id"])
        approved = slack_ops_bot.approve_slack_ops_queue_job(
            repo,
            queue_job_id=job_id,
            approver_username="admin1",
            approver_role="admin",
            actor="admin-ui",
        )
        self.assertEqual(approved["status"], "approved")
        ok, _message = process_integration_queue_job(repo, job_id=job_id, actor="queue-runner")
        self.assertTrue(ok)
        row = repo._db_get(None, job_id)
        self.assertEqual(row.status, "success")
        self.assertEqual(str(getattr(row, "last_error", "") or "").strip(), "")

    def test_ingest_rejected_when_slack_ops_disabled(self) -> None:
        repo = _FakeRepo()
        with patch("app.services.slack_ops_bot.get_runtime_bool", side_effect=lambda _r, key, default=True: False if key == "slack_ops_enabled" else default):
            out = slack_ops_bot.ingest_slack_command_request(
                repo,
                payload={"environment": "prod", "message_ts": "400.1", "app_role": "ops", "text": "comp silver bar"},
                actor="slack-bot",
            )
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["reason"], "slack_ops_disabled")
        self.assertEqual(len(repo.queue_jobs), 0)

    def test_ingest_rejected_when_channel_not_allowlisted(self) -> None:
        repo = _FakeRepo()
        with patch("app.services.slack_ops_bot.get_runtime_bool", return_value=True), patch(
            "app.services.slack_ops_bot.get_runtime_str",
            side_effect=lambda _r, key, default="": "allowed-channel" if key == "slack_ops_allowed_channels" else "",
        ), patch("app.services.slack_ops_bot.get_runtime_int", return_value=1000):
            out = slack_ops_bot.ingest_slack_command_request(
                repo,
                payload={
                    "environment": "prod",
                    "message_ts": "401.1",
                    "channel_id": "COPS",
                    "channel_name": "ops",
                    "app_role": "ops",
                    "text": "comp silver bar",
                },
                actor="slack-bot",
            )
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["reason"], "channel_not_allowed")
        self.assertEqual(len(repo.queue_jobs), 0)

    def test_ingest_rejected_when_rate_limited(self) -> None:
        repo = _FakeRepo()
        for i in range(3):
            repo.create_integration_queue_job(
                environment="prod",
                integration="slack_ops",
                action="command_ingest",
                payload_json=json.dumps({"idempotency_key": f"k{i}"}),
                requested_by="ops",
                max_retries=1,
                actor="seed",
            )
        with patch("app.services.slack_ops_bot.get_runtime_bool", return_value=True), patch(
            "app.services.slack_ops_bot.get_runtime_str", return_value=""
        ), patch(
            "app.services.slack_ops_bot.get_runtime_int",
            side_effect=lambda _r, key, default=0: 60 if key == "slack_ops_rate_limit_window_minutes" else (3 if key == "slack_ops_rate_limit_max_requests" else default),
        ):
            out = slack_ops_bot.ingest_slack_command_request(
                repo,
                payload={"environment": "prod", "message_ts": "402.1", "app_role": "ops", "text": "comp silver bar"},
                actor="slack-bot",
            )
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["reason"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
