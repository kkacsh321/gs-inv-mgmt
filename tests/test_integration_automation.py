import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from app.services import integration_automation


class _FakeDB:
    def __init__(self, jobs: dict[int, object] | None = None) -> None:
        self.jobs = jobs or {}

    def get(self, _model, job_id: int):
        return self.jobs.get(int(job_id))


class _FakeRepo:
    def __init__(self) -> None:
        self.db = _FakeDB()
        self.rules: list[object] = []
        self.queue_jobs: list[object] = []
        self.updated_jobs: list[tuple[int, dict, str]] = []
        self.events: list[dict] = []
        self.has_approval = False

    def list_integration_automation_rules(self, **_kwargs):
        return list(self.rules)

    def update_integration_queue_job(self, job_id: int, updates: dict, *, actor: str):
        self.updated_jobs.append((job_id, updates, actor))

    def log_integration_event(self, **kwargs):
        self.events.append(kwargs)

    def has_active_integration_automation_approval(self, **_kwargs):
        return bool(self.has_approval)

    def list_integration_queue_jobs(self, **_kwargs):
        return list(self.queue_jobs)


class IntegrationAutomationTests(unittest.TestCase):
    def test_get_path_handles_dict_and_list(self) -> None:
        data = {"a": {"b": [{"c": 7}]}}
        self.assertEqual(integration_automation._get_path(data, "a.b.0.c"), 7)
        self.assertIsNone(integration_automation._get_path(data, "a.b.x.c"))
        self.assertIsNone(integration_automation._get_path({"a": 1}, "a.b"))
        self.assertIsNone(integration_automation._get_path({}, ""))

    def test_to_float_helper(self) -> None:
        self.assertEqual(integration_automation._to_float("1.5"), 1.5)
        self.assertIsNone(integration_automation._to_float("bad"))

    def test_condition_match_operations(self) -> None:
        context = {"payload": {"price": "10.5", "title": "gold coin", "tags": ["a", "b"]}}
        self.assertTrue(integration_automation._condition_match(context, {"field": "payload.title", "op": "contains", "value": "gold"}))
        self.assertTrue(integration_automation._condition_match(context, {"field": "payload.price", "op": "gt", "value": 10}))
        self.assertTrue(integration_automation._condition_match(context, {"field": "payload.price", "op": "gte", "value": 10.5}))
        self.assertTrue(integration_automation._condition_match(context, {"field": "payload.price", "op": "lte", "value": 10.5}))
        self.assertTrue(integration_automation._condition_match(context, {"field": "payload.title", "op": "in", "value": ["gold coin", "silver"]}))
        self.assertFalse(integration_automation._condition_match(context, {"field": "payload.title", "op": "in", "value": "not-a-list"}))
        self.assertFalse(integration_automation._condition_match(context, {"field": "payload.price", "op": "gt", "value": "not-a-number"}))
        self.assertFalse(integration_automation._condition_match(context, {"field": "payload.price", "op": "lt", "value": 1}))
        self.assertTrue(integration_automation._condition_match(context, {"field": "payload.title", "op": "neq", "value": "silver"}))
        self.assertFalse(integration_automation._condition_match(context, {"field": "payload.title", "op": "unknown", "value": "x"}))

    def test_rule_match_all_any_and_invalid_json(self) -> None:
        context = {"job": {"status": "queued", "retry_count": 1}}
        all_json = json.dumps({"all": [{"field": "job.status", "op": "eq", "value": "queued"}]})
        any_json = json.dumps(
            {"any": [{"field": "job.retry_count", "op": "gt", "value": 5}, {"field": "job.status", "op": "eq", "value": "queued"}]}
        )
        self.assertTrue(integration_automation._rule_match(context, all_json))
        self.assertTrue(integration_automation._rule_match(context, any_json))
        self.assertFalse(integration_automation._rule_match(context, "{bad"))
        self.assertFalse(integration_automation._rule_match(context, "[]"))
        self.assertTrue(integration_automation._rule_match(context, "{}"))
        self.assertTrue(integration_automation._rule_match(context, json.dumps({"meta": "no-conditions"})))

    def test_allowed_queue_update_sanitizes_effects(self) -> None:
        updates = integration_automation._allowed_queue_update(
            {
                "status": "running",
                "max_retries": "3",
                "retry_count": "2",
                "next_attempt_in_seconds": "30",
                "last_error": "x" * 2500,
            }
        )
        self.assertEqual(updates["status"], "running")
        self.assertEqual(updates["max_retries"], 3)
        self.assertEqual(updates["retry_count"], 2)
        self.assertIn("next_attempt_at", updates)
        self.assertEqual(len(updates["last_error"]), 2000)
        updates2 = integration_automation._allowed_queue_update({"status": "bad", "max_retries": "x"})
        self.assertEqual(updates2, {})
        updates3 = integration_automation._allowed_queue_update(
            {"max_retries": "bad", "retry_count": object(), "next_attempt_in_seconds": "bad"}
        )
        self.assertEqual(updates3, {})

    def test_evaluate_and_apply_rules_dry_run_no_update(self) -> None:
        repo = _FakeRepo()
        repo.rules = [
            SimpleNamespace(
                id=1,
                trigger_status="queued",
                requires_approval=False,
                conditions_json=json.dumps({"all": [{"field": "job.integration", "op": "eq", "value": "google"}]}),
                effect_json=json.dumps({"type": "queue_update", "set": {"status": "running"}}),
            )
        ]
        job = SimpleNamespace(
            id=77,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="{}",
        )
        with patch("app.services.integration_automation.get_runtime_bool", side_effect=[False, True]):
            result = integration_automation.evaluate_and_apply_rules_for_job(
                repo, job=job, actor="qa", trigger_status="queued"
            )
        self.assertEqual(result["applied_rule_ids"], [1])
        self.assertTrue(result["dry_run"])
        self.assertEqual(repo.updated_jobs, [])
        self.assertEqual(len(repo.events), 1)

    def test_evaluate_and_apply_rules_non_dry_run_updates_queue(self) -> None:
        repo = _FakeRepo()
        repo.rules = [
            SimpleNamespace(
                id=2,
                trigger_status="queued",
                requires_approval=False,
                conditions_json="{}",
                effect_json=json.dumps({"type": "queue_update", "set": {"status": "running", "retry_count": 1}}),
            )
        ]
        job = SimpleNamespace(
            id=78,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="{}",
        )
        with patch("app.services.integration_automation.get_runtime_bool", side_effect=[False, False]):
            result = integration_automation.evaluate_and_apply_rules_for_job(
                repo, job=job, actor="qa", trigger_status="queued"
            )
        self.assertEqual(result["updates"]["status"], "running")
        self.assertEqual(len(repo.updated_jobs), 1)

    def test_evaluate_and_apply_rules_approval_gate(self) -> None:
        repo = _FakeRepo()
        repo.rules = [
            SimpleNamespace(
                id=3,
                trigger_status="queued",
                requires_approval=True,
                conditions_json="{}",
                effect_json=json.dumps({"type": "queue_update", "set": {"status": "running"}}),
            )
        ]
        job = SimpleNamespace(
            id=79,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="{}",
        )
        with patch("app.services.integration_automation.get_runtime_bool", side_effect=[True, True]):
            result = integration_automation.evaluate_and_apply_rules_for_job(
                repo, job=job, actor="qa", trigger_status="queued"
            )
        self.assertEqual(result["approval_gated_rule_ids"], [3])
        self.assertEqual(result["applied_rule_ids"], [])

    def test_evaluate_and_apply_rules_approved_and_block_execute(self) -> None:
        repo = _FakeRepo()
        repo.has_approval = True
        repo.rules = [
            SimpleNamespace(
                id=4,
                trigger_status="queued",
                requires_approval=True,
                conditions_json="{}",
                effect_json=json.dumps({"type": "queue_update", "set": {"status": "running"}}),
            ),
            SimpleNamespace(
                id=5,
                trigger_status="queued",
                requires_approval=False,
                conditions_json="{}",
                effect_json=json.dumps({"type": "block_execute", "reason": "manual hold"}),
            ),
        ]
        job = SimpleNamespace(
            id=80,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="{}",
        )
        with patch("app.services.integration_automation.get_runtime_bool", side_effect=[True, False]):
            result = integration_automation.evaluate_and_apply_rules_for_job(
                repo, job=job, actor="qa", trigger_status="queued"
            )
        self.assertIn(4, result["applied_rule_ids"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["blocked_reason"], "manual hold")
        self.assertEqual(len(repo.updated_jobs), 1)

    def test_evaluate_and_apply_rules_ignores_bad_effect_json_and_trigger_mismatch(self) -> None:
        repo = _FakeRepo()
        repo.rules = [
            SimpleNamespace(
                id=6,
                trigger_status="running",
                requires_approval=False,
                conditions_json="{}",
                effect_json="{bad",
            ),
            SimpleNamespace(
                id=7,
                trigger_status="queued",
                requires_approval=False,
                conditions_json="{bad",
                effect_json=json.dumps({"type": "queue_update", "set": {"status": "running"}}),
            ),
        ]
        job = SimpleNamespace(
            id=81,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="{}",
        )
        with patch("app.services.integration_automation.get_runtime_bool", side_effect=[False, False]):
            result = integration_automation.evaluate_and_apply_rules_for_job(
                repo, job=job, actor="qa", trigger_status="queued"
            )
        self.assertEqual(result["matched_rule_ids"], [])
        self.assertEqual(result["applied_rule_ids"], [])

    def test_evaluate_and_apply_rules_payload_non_dict_and_effect_non_dict(self) -> None:
        repo = _FakeRepo()
        repo.rules = [
            SimpleNamespace(
                id=8,
                trigger_status="queued",
                requires_approval=False,
                conditions_json="{}",
                effect_json="[]",
            ),
        ]
        job = SimpleNamespace(
            id=82,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="[]",
        )
        with patch("app.services.integration_automation.get_runtime_bool", side_effect=[False, False]):
            result = integration_automation.evaluate_and_apply_rules_for_job(repo, job=job, actor="qa", trigger_status="queued")
        self.assertEqual(result["matched_rule_ids"], [8])
        self.assertEqual(result["applied_rule_ids"], [])

    def test_evaluate_and_apply_rules_payload_parse_error_and_effect_parse_error(self) -> None:
        repo = _FakeRepo()
        repo.rules = [
            SimpleNamespace(
                id=18,
                trigger_status="queued",
                requires_approval=False,
                conditions_json="{}",
                effect_json="{bad",
            ),
        ]
        job = SimpleNamespace(
            id=85,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="{bad",
        )
        with patch("app.services.integration_automation.get_runtime_bool", side_effect=[False, False]):
            result = integration_automation.evaluate_and_apply_rules_for_job(repo, job=job, actor="qa", trigger_status="queued")
        self.assertEqual(result["matched_rule_ids"], [18])
        self.assertEqual(result["applied_rule_ids"], [])

    def test_evaluate_and_apply_rules_queue_update_with_no_allowed_updates(self) -> None:
        repo = _FakeRepo()
        repo.rules = [
            SimpleNamespace(
                id=19,
                trigger_status="queued",
                requires_approval=False,
                conditions_json="{}",
                effect_json=json.dumps({"type": "queue_update", "set": {"status": "bad"}}),
            ),
            SimpleNamespace(
                id=20,
                trigger_status="queued",
                requires_approval=False,
                conditions_json="{}",
                effect_json=json.dumps({"type": "queue_update", "set": {"status": "running"}}),
            ),
        ]
        job = SimpleNamespace(
            id=86,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="{}",
        )
        with patch("app.services.integration_automation.get_runtime_bool", side_effect=[False, False]):
            result = integration_automation.evaluate_and_apply_rules_for_job(repo, job=job, actor="qa", trigger_status="queued")
        self.assertEqual(result["matched_rule_ids"], [19, 20])
        self.assertEqual(result["applied_rule_ids"], [20])

    def test_evaluate_and_apply_rules_requires_approval_but_global_disabled(self) -> None:
        repo = _FakeRepo()
        repo.rules = [
            SimpleNamespace(
                id=9,
                trigger_status="queued",
                requires_approval=True,
                conditions_json="{}",
                effect_json=json.dumps({"type": "queue_update", "set": {"status": "running"}}),
            ),
        ]
        job = SimpleNamespace(
            id=83,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="{}",
        )
        with patch("app.services.integration_automation.get_runtime_bool", side_effect=[False, False]):
            result = integration_automation.evaluate_and_apply_rules_for_job(repo, job=job, actor="qa", trigger_status="queued")
        self.assertEqual(result["approval_gated_rule_ids"], [9])
        self.assertEqual(result["applied_rule_ids"], [])

    def test_evaluate_and_apply_rules_logging_failure_is_swallowed(self) -> None:
        class _Repo(_FakeRepo):
            def log_integration_event(self, **kwargs):
                raise RuntimeError("log failed")

        repo = _Repo()
        repo.rules = [
            SimpleNamespace(
                id=10,
                trigger_status="queued",
                requires_approval=False,
                conditions_json="{}",
                effect_json=json.dumps({"type": "queue_update", "set": {"status": "running"}}),
            ),
        ]
        job = SimpleNamespace(
            id=84,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="{}",
        )
        with patch("app.services.integration_automation.get_runtime_bool", side_effect=[False, False]):
            result = integration_automation.evaluate_and_apply_rules_for_job(repo, job=job, actor="qa", trigger_status="queued")
        self.assertIn(10, result["applied_rule_ids"])

    def test_preview_rule_impact(self) -> None:
        repo = _FakeRepo()
        repo.queue_jobs = [
            SimpleNamespace(
                id=1,
                environment="local",
                integration="google",
                action="gmail_send_document_email",
                status="queued",
                retry_count=0,
                max_retries=3,
                requested_by="qa",
                payload_json=json.dumps({"x": 1}),
                next_attempt_at=None,
                updated_at=datetime(2026, 3, 29, 12, 0, 0),
            ),
            SimpleNamespace(
                id=2,
                environment="local",
                integration="google",
                action="gmail_send_document_email",
                status="queued",
                retry_count=0,
                max_retries=3,
                requested_by="qa",
                payload_json="{bad",
                next_attempt_at=None,
                updated_at=datetime(2026, 3, 29, 12, 0, 0),
            ),
        ]
        result = integration_automation.preview_rule_impact(
            repo,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            trigger_status="queued",
            conditions_json="{}",
            scan_limit=100,
            sample_limit=10,
        )
        self.assertEqual(result["candidate_jobs"], 2)
        self.assertEqual(result["matched_jobs"], 2)
        self.assertEqual(result["payload_parse_errors"], 1)
        self.assertGreaterEqual(len(result["samples"]), 1)

    def test_preview_rule_impact_handles_non_dict_payload(self) -> None:
        repo = _FakeRepo()
        repo.queue_jobs = [
            SimpleNamespace(
                id=22,
                environment="local",
                integration="google",
                action="gmail_send_document_email",
                status="queued",
                retry_count=0,
                max_retries=3,
                requested_by="qa",
                payload_json="[]",
                next_attempt_at=None,
                updated_at=None,
            )
        ]
        result = integration_automation.preview_rule_impact(
            repo,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            trigger_status="queued",
            conditions_json="{}",
            scan_limit=100,
            sample_limit=10,
        )
        self.assertEqual(result["candidate_jobs"], 1)
        self.assertEqual(result["matched_jobs"], 1)

    def test_preview_rule_impact_action_filter_and_empty(self) -> None:
        repo = _FakeRepo()
        repo.queue_jobs = [
            SimpleNamespace(
                id=3,
                environment="local",
                integration="google",
                action="calendar_create_event",
                status="queued",
                retry_count=0,
                max_retries=3,
                requested_by="qa",
                payload_json="{}",
                next_attempt_at=None,
                updated_at=None,
            )
        ]
        result = integration_automation.preview_rule_impact(
            repo,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            trigger_status="queued",
            conditions_json="{}",
            scan_limit=100,
            sample_limit=10,
        )
        self.assertEqual(result["candidate_jobs"], 0)
        self.assertEqual(result["matched_jobs"], 0)
        self.assertEqual(result["match_rate"], 0.0)

    def test_preview_rule_impact_no_action_filter_and_non_match_loop_path(self) -> None:
        repo = _FakeRepo()
        repo.queue_jobs = [
            SimpleNamespace(
                id=30,
                environment="local",
                integration="google",
                action="calendar_create_event",
                status="queued",
                retry_count=0,
                max_retries=3,
                requested_by="qa",
                payload_json="{}",
                next_attempt_at=None,
                updated_at=None,
            )
        ]
        result = integration_automation.preview_rule_impact(
            repo,
            environment="local",
            integration="google",
            action="",
            trigger_status="queued",
            conditions_json=json.dumps({"all": [{"field": "payload.nope", "op": "eq", "value": "x"}]}),
            scan_limit=100,
            sample_limit=10,
        )
        self.assertEqual(result["candidate_jobs"], 1)
        self.assertEqual(result["matched_jobs"], 0)

    def test_simulate_rule_evaluation_for_job(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=10,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="{}",
        )
        repo.db.jobs[10] = job
        repo.rules = [
            SimpleNamespace(
                id=1,
                name="Block Rule",
                is_active=True,
                trigger_status="queued",
                requires_approval=False,
                conditions_json="{}",
                effect_json=json.dumps({"type": "block_execute"}),
            ),
            SimpleNamespace(
                id=2,
                name="Approval Rule",
                is_active=True,
                trigger_status="queued",
                requires_approval=True,
                conditions_json="{}",
                effect_json=json.dumps({"type": "queue_update", "set": {"status": "running"}}),
            ),
        ]
        with patch("app.services.integration_automation.get_runtime_bool", return_value=True):
            result = integration_automation.simulate_rule_evaluation_for_job(
                repo,
                environment="local",
                job_id=10,
                trigger_status="queued",
            )
        self.assertEqual(result["rules_considered"], 2)
        self.assertEqual(result["matched_rules"], 2)
        self.assertEqual(result["approval_gated_rules"], 1)

    def test_simulate_rule_evaluation_invalid_payload_and_effect_json_shapes(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=12,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="[]",
        )
        repo.db.jobs[12] = job
        repo.rules = [
            SimpleNamespace(
                id=12,
                name="Bad Effect Json",
                is_active=True,
                trigger_status="queued",
                requires_approval=False,
                conditions_json="{}",
                effect_json="{bad",
            ),
            SimpleNamespace(
                id=13,
                name="List Effect Json",
                is_active=True,
                trigger_status="queued",
                requires_approval=False,
                conditions_json="{}",
                effect_json="[]",
            ),
            SimpleNamespace(
                id=14,
                name="Approval Disabled Branch",
                is_active=True,
                trigger_status="queued",
                requires_approval=True,
                conditions_json="{}",
                effect_json=json.dumps({"type": "queue_update", "set": {"status": "running"}}),
            ),
        ]
        with patch("app.services.integration_automation.get_runtime_bool", return_value=False):
            result = integration_automation.simulate_rule_evaluation_for_job(
                repo,
                environment="local",
                job_id=12,
                trigger_status="queued",
                include_inactive=False,
            )
        self.assertEqual(result["rules_considered"], 3)
        self.assertEqual(result["approval_gated_rules"], 1)

    def test_simulate_rule_evaluation_payload_parse_error(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=31,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="{bad",
        )
        repo.db.jobs[31] = job
        repo.rules = [
            SimpleNamespace(
                id=31,
                name="Any",
                is_active=True,
                trigger_status="queued",
                requires_approval=False,
                conditions_json="{}",
                effect_json=json.dumps({"type": "queue_update", "set": {"status": "running"}}),
            )
        ]
        with patch("app.services.integration_automation.get_runtime_bool", return_value=False):
            result = integration_automation.simulate_rule_evaluation_for_job(
                repo,
                environment="local",
                job_id=31,
                trigger_status="queued",
            )
        self.assertEqual(result["matched_rules"], 1)

    def test_simulate_rule_evaluation_for_job_not_found_and_include_inactive(self) -> None:
        repo = _FakeRepo()
        with self.assertRaises(ValueError):
            integration_automation.simulate_rule_evaluation_for_job(
                repo,
                environment="local",
                job_id=999,
                trigger_status="queued",
            )

        job = SimpleNamespace(
            id=11,
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            status="queued",
            retry_count=0,
            max_retries=3,
            requested_by="qa",
            payload_json="{}",
        )
        repo.db.jobs[11] = job
        repo.rules = [
            SimpleNamespace(
                id=9,
                name="Inactive Rule",
                is_active=False,
                trigger_status="queued",
                requires_approval=False,
                conditions_json="{}",
                effect_json=json.dumps({"type": "queue_update", "set": {"status": "running"}}),
            )
        ]
        with patch("app.services.integration_automation.get_runtime_bool", return_value=False):
            result = integration_automation.simulate_rule_evaluation_for_job(
                repo,
                environment="local",
                job_id=11,
                trigger_status="queued",
                include_inactive=True,
            )
        self.assertEqual(result["rules_considered"], 1)


if __name__ == "__main__":
    unittest.main()
