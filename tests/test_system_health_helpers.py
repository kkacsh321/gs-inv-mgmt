import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import mock_open, patch


def _bootstrap_views_package() -> None:
    if "boto3" not in sys.modules:
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.session = types.SimpleNamespace(Session=lambda *args, **kwargs: None)
        sys.modules["boto3"] = fake_boto3
    if "botocore" not in sys.modules:
        sys.modules["botocore"] = types.ModuleType("botocore")
    if "botocore.config" not in sys.modules:
        fake_botocore_config = types.ModuleType("botocore.config")
        fake_botocore_config.Config = lambda *args, **kwargs: None
        sys.modules["botocore.config"] = fake_botocore_config
    if "botocore.exceptions" not in sys.modules:
        fake_botocore_exceptions = types.ModuleType("botocore.exceptions")
        fake_botocore_exceptions.BotoCoreError = Exception
        fake_botocore_exceptions.ClientError = Exception
        sys.modules["botocore.exceptions"] = fake_botocore_exceptions
    if "app.components.views" not in sys.modules:
        pkg = types.ModuleType("app.components.views")
        pkg.__path__ = []
        sys.modules["app.components.views"] = pkg

    root = Path(__file__).resolve().parents[1]
    shared_name = "app.components.views.shared"
    if shared_name not in sys.modules:
        shared_path = root / "app" / "components" / "views" / "shared.py"
        shared_spec = importlib.util.spec_from_file_location(shared_name, shared_path)
        shared_mod = importlib.util.module_from_spec(shared_spec)
        assert shared_spec and shared_spec.loader
        shared_spec.loader.exec_module(shared_mod)
        sys.modules[shared_name] = shared_mod


def _load_system_health_module():
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / "system_health.py"
    spec = importlib.util.spec_from_file_location("test_system_health_module", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


system_health = _load_system_health_module()


class SystemHealthHelpersTests(unittest.TestCase):
    def test_rollup_explain_failures_filters_and_normalizes(self):
        rows = [
            {
                "rollup_name": "dashboard_live_metrics",
                "error": "",
                "elapsed_ms": 10.5,
                "sample_limit": 2000,
            },
            {
                "rollup_name": "report_orders_rows",
                "error": "probe failed",
                "elapsed_ms": 7.2,
                "sample_limit": 1500,
            },
        ]
        failures = system_health._rollup_explain_failures(rows)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["rollup_name"], "report_orders_rows")
        self.assertEqual(failures[0]["error"], "probe failed")
        self.assertEqual(failures[0]["sample_limit"], 1500)

    def test_rollup_explain_failures_handles_none(self):
        self.assertEqual(system_health._rollup_explain_failures(None), [])

    def test_rollup_explain_skips_filters_and_normalizes(self):
        rows = [
            {
                "rollup_name": "slack_ops_events_24h",
                "skipped": True,
                "skip_reason": "table audit_logs not present",
                "sample_limit": 2000,
            },
            {
                "rollup_name": "dashboard_live_metrics",
                "skipped": False,
                "skip_reason": "",
                "sample_limit": 2000,
            },
        ]
        skips = system_health._rollup_explain_skips(rows)
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["rollup_name"], "slack_ops_events_24h")
        self.assertEqual(skips[0]["skip_reason"], "table audit_logs not present")
        self.assertEqual(skips[0]["sample_limit"], 2000)

    def test_slack_ops_health_snapshot_metrics(self):
        now = datetime(2026, 4, 20, 12, 0, 0)
        rows = [
            types.SimpleNamespace(
                status="queued",
                next_attempt_at=now,
                payload_json='{"approval":{"required":false}}',
            ),
            types.SimpleNamespace(
                status="blocked",
                next_attempt_at=None,
                payload_json='{"approval":{"required":true,"status":"pending","requested_at":"2026-04-20T10:00:00"}}',
            ),
            types.SimpleNamespace(
                status="failed",
                next_attempt_at=None,
                payload_json='{}',
            ),
            types.SimpleNamespace(
                status="success",
                next_attempt_at=None,
                payload_json='{}',
            ),
        ]
        out = system_health._slack_ops_health_snapshot(rows, now=now)
        self.assertEqual(out["total_count"], 4)
        self.assertEqual(out["queued_count"], 1)
        self.assertEqual(out["blocked_count"], 1)
        self.assertEqual(out["failed_count"], 1)
        self.assertEqual(out["success_count"], 1)
        self.assertEqual(out["pending_approval_count"], 1)
        self.assertGreater(out["pending_approval_avg_hours"], 1.9)
        self.assertGreater(out["pending_approval_max_hours"], 1.9)

    def test_normalize_page_baseline_rows_adds_budget_and_over_budget(self):
        with patch.object(system_health, "_probe_budget_ms", return_value=100.0):
            normalized = system_health._normalize_page_baseline_rows(
                repo=types.SimpleNamespace(),
                rows=[
                    {"probe_name": "dashboard_metrics", "elapsed_ms": 120.0},
                    {"probe_name": "list_products", "elapsed_ms": 80.0},
                ],
            )
        self.assertEqual(len(normalized), 2)
        self.assertEqual(normalized[0]["budget_ms"], 100.0)
        self.assertTrue(normalized[0]["over_budget"])
        self.assertFalse(normalized[1]["over_budget"])

    def test_page_baseline_summary_handles_empty_and_non_empty(self):
        empty = system_health._page_baseline_summary([])
        self.assertEqual(empty["total_count"], 0)
        self.assertEqual(empty["over_budget_count"], 0)
        self.assertEqual(empty["worst_elapsed_ms"], 0.0)

        summary = system_health._page_baseline_summary(
            [
                {"elapsed_ms": 120.0, "over_budget": True},
                {"elapsed_ms": 80.0, "over_budget": False},
            ]
        )
        self.assertEqual(summary["total_count"], 2)
        self.assertEqual(summary["over_budget_count"], 1)
        self.assertEqual(summary["worst_elapsed_ms"], 120.0)

    def test_format_helpers_and_status_row(self):
        self.assertEqual(system_health._fmt_gb_from_kb(None), "n/a")
        self.assertIn("GB", system_health._fmt_gb_from_kb(1024 * 1024))
        row = system_health._status_row("DB", "ok", "healthy")
        self.assertEqual(row["component"], "DB")
        self.assertEqual(row["status"], "ok")

    def test_service_critical_signals_promotes_error_rows(self):
        signals = system_health._service_critical_signals(
            [
                {"component": "AI Accountant LLM Route", "status": "error"},
                {"component": "Sync Runner", "status": "warn"},
                {"component": "Database", "status": "ok"},
                {"component": "AI Accountant LLM Route", "status": "error"},
            ]
        )

        self.assertEqual(signals, ["service_ai_accountant_llm_route"])

    def test_ai_accountant_latest_review_health_row_completed(self):
        class Result:
            def all(self):
                return [
                    (
                        "2026-05-11T12:00:00",
                        (
                            '{"after":{"integration":"ai_accountant","action":"monitor","status":"success",'
                            '"details":{"actionable_count":2,"review_enabled":true,'
                            '"review_status":"completed","review_hash":"aaaaaaaaaaaaaaaa",'
                            '"review_runtime_route":"localai/Qwen (chat, db, ready)"}}}'
                        ),
                    )
                ]

        class DB:
            def execute(self, *_args, **_kwargs):
                return Result()

        row = system_health._ai_accountant_latest_review_health_row(types.SimpleNamespace(db=DB()))

        self.assertEqual(row["component"], "AI Accountant Review Evidence")
        self.assertEqual(row["status"], "ok")
        self.assertIn("review_status=completed", row["details"])
        self.assertIn("hash=aaaaaaaaaaaa", row["details"])
        self.assertIn("localai/Qwen", row["details"])

    def test_ai_accountant_latest_review_health_row_flags_unavailable(self):
        class Result:
            def all(self):
                return [
                    (
                        "2026-05-11T12:00:00",
                        (
                            '{"after":{"integration":"ai_accountant","action":"monitor","status":"success",'
                            '"details":{"actionable_count":4,"review_enabled":true,'
                            '"review_status":"unavailable","review_error":"localai 500",'
                            '"review_runtime_route":"localai/Qwen (chat, db, error)"}}}'
                        ),
                    )
                ]

        class DB:
            def execute(self, *_args, **_kwargs):
                return Result()

        row = system_health._ai_accountant_latest_review_health_row(types.SimpleNamespace(db=DB()))

        self.assertEqual(row["status"], "warn")
        self.assertIn("review_status=unavailable", row["details"])
        self.assertIn("localai 500", row["details"])

    def test_system_health_critical_slack_policy_respects_route(self):
        values = {
            "slack_notify_system_health_critical": True,
            "notification_route_system_health_critical": "disabled",
        }
        slack_cfg = types.SimpleNamespace(enabled=True, bot_token="xoxb-test", default_channel="#ops")

        with patch.object(system_health, "get_runtime_bool", side_effect=lambda _r, key, default=False: bool(values.get(key, default))), patch.object(
            system_health, "get_runtime_str", side_effect=lambda _r, key, default="": str(values.get(key, default) or "")
        ), patch.object(system_health, "resolve_slack_notify_config", return_value=slack_cfg), patch.object(
            system_health, "resolve_slack_channel", return_value="#ops"
        ):
            disabled = system_health._system_health_critical_slack_policy(types.SimpleNamespace())

        self.assertFalse(disabled["route_allows_slack"])
        self.assertFalse(disabled["slack_allowed"])
        self.assertTrue(disabled["delivery_ready"])

        values["notification_route_system_health_critical"] = "both"
        with patch.object(system_health, "get_runtime_bool", side_effect=lambda _r, key, default=False: bool(values.get(key, default))), patch.object(
            system_health, "get_runtime_str", side_effect=lambda _r, key, default="": str(values.get(key, default) or "")
        ), patch.object(system_health, "resolve_slack_notify_config", return_value=slack_cfg), patch.object(
            system_health, "resolve_slack_channel", return_value="#ops"
        ):
            both = system_health._system_health_critical_slack_policy(types.SimpleNamespace())

        self.assertTrue(both["route_allows_slack"])
        self.assertTrue(both["slack_allowed"])
        self.assertTrue(both["delivery_ready"])

    def test_system_health_critical_alert_policy_row(self):
        values = {
            "health_auto_alert_critical_enabled": True,
            "health_auto_alert_cooldown_minutes": 60,
            "slack_notify_system_health_critical": True,
            "notification_route_system_health_critical": "slack",
        }
        slack_cfg = types.SimpleNamespace(enabled=True, bot_token="xoxb-test", default_channel="#ops")

        def runtime_bool(_repo, key, default=False):
            return bool(values.get(key, default))

        def runtime_int(_repo, key, default=0):
            return int(values.get(key, default))

        def runtime_str(_repo, key, default=""):
            return str(values.get(key, default) or "")

        with patch.object(system_health, "get_runtime_bool", side_effect=runtime_bool), patch.object(
            system_health, "get_runtime_int", side_effect=runtime_int
        ), patch.object(system_health, "get_runtime_str", side_effect=runtime_str), patch.object(
            system_health, "resolve_slack_notify_config", return_value=slack_cfg
        ), patch.object(system_health, "resolve_slack_channel", return_value="#ops"):
            row = system_health._system_health_critical_alert_policy_row(types.SimpleNamespace())

        self.assertEqual(row["component"], "System Health Critical Alerts")
        self.assertEqual(row["status"], "ok")
        self.assertIn("slack_allowed=True", row["details"])
        self.assertIn("delivery_ready=True", row["details"])

        values["notification_route_system_health_critical"] = "email"
        with patch.object(system_health, "get_runtime_bool", side_effect=runtime_bool), patch.object(
            system_health, "get_runtime_int", side_effect=runtime_int
        ), patch.object(system_health, "get_runtime_str", side_effect=runtime_str), patch.object(
            system_health, "resolve_slack_notify_config", return_value=slack_cfg
        ), patch.object(system_health, "resolve_slack_channel", return_value="#ops"):
            route_blocked = system_health._system_health_critical_alert_policy_row(types.SimpleNamespace())

        self.assertEqual(route_blocked["status"], "warn")
        self.assertIn("route=email", route_blocked["details"])
        self.assertIn("slack_allowed=False", route_blocked["details"])

        values["notification_route_system_health_critical"] = "slack"
        missing_token_cfg = types.SimpleNamespace(enabled=True, bot_token="", default_channel="#ops")
        with patch.object(system_health, "get_runtime_bool", side_effect=runtime_bool), patch.object(
            system_health, "get_runtime_int", side_effect=runtime_int
        ), patch.object(system_health, "get_runtime_str", side_effect=runtime_str), patch.object(
            system_health, "resolve_slack_notify_config", return_value=missing_token_cfg
        ), patch.object(system_health, "resolve_slack_channel", return_value="#ops"):
            delivery_blocked = system_health._system_health_critical_alert_policy_row(types.SimpleNamespace())

        self.assertEqual(delivery_blocked["status"], "warn")
        self.assertIn("token_present=False", delivery_blocked["details"])
        self.assertIn("delivery_ready=False", delivery_blocked["details"])

    def test_system_health_critical_slack_delivery_rows_filter_event_type(self):
        class Repo:
            def list_integration_queue_jobs(self, *, environment, integration, statuses, limit):
                self.args = {
                    "environment": environment,
                    "integration": integration,
                    "statuses": statuses,
                    "limit": limit,
                }
                return [
                    types.SimpleNamespace(
                        id=1,
                        status="queued",
                        payload_json='{"event_type":"system_health_critical","channel":"#ops"}',
                        attempt_count=1,
                        max_attempts=5,
                        next_attempt_at="2026-05-10T12:00:00",
                        requested_by="system",
                        last_error="",
                    ),
                    types.SimpleNamespace(
                        id=2,
                        status="failed",
                        payload_json='{"event_type":"other","channel":"#ops"}',
                        attempt_count=5,
                        max_attempts=5,
                        next_attempt_at="",
                        requested_by="system",
                        last_error="boom",
                    ),
                    types.SimpleNamespace(
                        id=3,
                        status="failed",
                        payload_json='{"event_type":"system_health_critical","channel":"#ops"}',
                        attempt_count=5,
                        max_attempts=5,
                        next_attempt_at="",
                        requested_by="system",
                        last_error="token missing",
                    ),
                ]

        repo = Repo()
        with patch.object(system_health, "settings", types.SimpleNamespace(app_env="local")):
            rows = system_health._system_health_critical_slack_delivery_rows(repo, limit=10)
        summary = system_health._system_health_critical_slack_delivery_summary(rows)

        self.assertEqual(repo.args["integration"], "slack")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], 1)
        self.assertEqual(rows[1]["last_error"], "token missing")
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["failed"], 1)

    def test_process_due_system_health_critical_slack_jobs_only_processes_due_matching_jobs(self):
        now = datetime(2026, 5, 11, 12, 0, 0)
        processed: list[int] = []

        class DB:
            def __init__(self):
                self.rows = {
                    1: types.SimpleNamespace(id=1, status="success"),
                    3: types.SimpleNamespace(id=3, status="queued"),
                }

            def get(self, _model, row_id):
                return self.rows.get(int(row_id))

        class Repo:
            def __init__(self):
                self.db = DB()

            def list_integration_queue_jobs(self, *, environment, integration, statuses, limit):
                return [
                    types.SimpleNamespace(
                        id=1,
                        next_attempt_at=now,
                        payload_json='{"event_type":"system_health_critical"}',
                    ),
                    types.SimpleNamespace(
                        id=2,
                        next_attempt_at=now,
                        payload_json='{"event_type":"other"}',
                    ),
                    types.SimpleNamespace(
                        id=3,
                        next_attempt_at=now.replace(hour=13),
                        payload_json='{"event_type":"system_health_critical"}',
                    ),
                ]

        def fake_process(_repo, *, job_id, actor):
            processed.append(int(job_id))
            return True, "sent"

        with patch.object(system_health, "settings", types.SimpleNamespace(app_env="local")), patch.object(
            system_health, "datetime"
        ) as dt_mock, patch.object(system_health, "process_integration_queue_job", side_effect=fake_process):
            dt_mock.now.return_value = now
            dt_mock.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            result = system_health._process_due_system_health_critical_slack_jobs(
                Repo(),
                actor="admin",
                limit=10,
            )

        self.assertEqual(processed, [1])
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["skipped"], 2)

    def test_ai_accountant_monitor_health_row_flags_interval_overdue(self):
        values = {
            "ai_accountant_monitor_enabled": True,
            "ai_accountant_monitor_slack_enabled": True,
            "ai_accountant_monitor_schedule_mode": "interval",
            "notification_route_ai_accountant_monitor": "slack",
            "ai_accountant_monitor_last_attempt_at": "2026-05-08T00:00:00",
            "ai_accountant_monitor_last_success_at": "2026-05-08T00:00:00",
        }

        with patch.object(system_health, "get_runtime_bool", side_effect=lambda _r, key, default=False: bool(values.get(key, default))), patch.object(
            system_health, "get_runtime_str", side_effect=lambda _r, key, default="": str(values.get(key, default) or "")
        ), patch.object(system_health, "get_runtime_int", return_value=6):
            row = system_health._ai_accountant_monitor_health_row(
                types.SimpleNamespace(),
                now=datetime(2026, 5, 8, 7, 0, 0),
            )

        self.assertEqual(row["component"], "AI Accountant Monitor")
        self.assertEqual(row["status"], "warn")
        self.assertIn("due=overdue", row["details"])
        self.assertIn("next_due=2026-05-08T06:00:00", row["details"])

    def test_ai_accountant_monitor_health_row_flags_disabled_route(self):
        values = {
            "ai_accountant_monitor_enabled": True,
            "ai_accountant_monitor_slack_enabled": True,
            "ai_accountant_monitor_schedule_mode": "interval",
            "notification_route_ai_accountant_monitor": "disabled",
            "ai_accountant_monitor_last_attempt_at": "2026-05-08T06:30:00",
            "ai_accountant_monitor_last_success_at": "2026-05-08T06:30:00",
        }

        with patch.object(system_health, "get_runtime_bool", side_effect=lambda _r, key, default=False: bool(values.get(key, default))), patch.object(
            system_health, "get_runtime_str", side_effect=lambda _r, key, default="": str(values.get(key, default) or "")
        ), patch.object(system_health, "get_runtime_int", return_value=6):
            row = system_health._ai_accountant_monitor_health_row(
                types.SimpleNamespace(),
                now=datetime(2026, 5, 8, 7, 0, 0),
            )

        self.assertEqual(row["status"], "warn")
        self.assertIn("due=route_disabled", row["details"])
        self.assertIn("route=disabled", row["details"])

    def test_ai_accountant_monitor_health_row_daily_attempted_today(self):
        values = {
            "ai_accountant_monitor_enabled": True,
            "ai_accountant_monitor_slack_enabled": False,
            "ai_accountant_monitor_schedule_mode": "daily",
            "notification_route_ai_accountant_monitor": "slack",
            "ai_accountant_monitor_local_time": "08:30",
            "ai_accountant_monitor_last_attempt_local_date": "2026-05-08",
            "ai_accountant_monitor_last_success_local_date": "2026-05-08",
        }

        with patch.object(system_health, "get_runtime_bool", side_effect=lambda _r, key, default=False: bool(values.get(key, default))), patch.object(
            system_health, "get_runtime_str", side_effect=lambda _r, key, default="": str(values.get(key, default) or "")
        ), patch.object(system_health, "get_runtime_int", return_value=6):
            row = system_health._ai_accountant_monitor_health_row(
                types.SimpleNamespace(),
                now=datetime(2026, 5, 8, 9, 0, 0),
            )

        self.assertEqual(row["status"], "ok")
        self.assertIn("mode=daily", row["details"])
        self.assertIn("due=attempted_today", row["details"])

    def test_ai_accountant_runtime_route_health_row_summarizes_route(self):
        rows = [
            {
                "order": 1,
                "workflow": "accounting",
                "status": "ready",
                "source": "db",
                "provider": "localai",
                "model": "Qwen",
                "endpoint_type": "chat",
                "enabled": True,
                "api_key": "present",
                "profile_selector": "Accounting",
                "error": "",
            }
        ]

        with patch.object(system_health, "describe_llm_runtime_chain", return_value=rows):
            row = system_health._ai_accountant_runtime_route_health_row(types.SimpleNamespace())

        self.assertEqual(row["component"], "AI Accountant LLM Route")
        self.assertEqual(row["status"], "ok")
        self.assertIn("workflow=accounting", row["details"])
        self.assertIn("localai/Qwen", row["details"])
        self.assertIn("selector=Accounting", row["details"])

    def test_ai_accountant_runtime_route_health_row_flags_error(self):
        rows = [
            {
                "order": 1,
                "workflow": "accounting",
                "status": "error",
                "source": "",
                "provider": "",
                "model": "",
                "endpoint_type": "",
                "enabled": False,
                "api_key": "",
                "profile_selector": "(default chain)",
                "error": "db down",
            }
        ]

        with patch.object(system_health, "describe_llm_runtime_chain", return_value=rows):
            row = system_health._ai_accountant_runtime_route_health_row(types.SimpleNamespace())

        self.assertEqual(row["status"], "error")
        self.assertIn("db down", row["details"])

    def test_read_proc_meminfo(self):
        content = "MemTotal:       1000000 kB\nMemAvailable:   400000 kB\n"
        with patch("builtins.open", mock_open(read_data=content)):
            total, avail = system_health._read_proc_meminfo()
        self.assertEqual(total, 1000000)
        self.assertEqual(avail, 400000)

        with patch("builtins.open", side_effect=OSError("no proc")):
            total, avail = system_health._read_proc_meminfo()
        self.assertIsNone(total)
        self.assertIsNone(avail)

    def test_read_proc_rss_kb(self):
        content = "Name:\tpython\nVmRSS:\t   12345 kB\n"
        with patch("builtins.open", mock_open(read_data=content)):
            self.assertEqual(system_health._read_proc_rss_kb(), 12345)

        with patch("builtins.open", side_effect=OSError("no proc")):
            self.assertIsNone(system_health._read_proc_rss_kb())

    def test_render_system_health_permission_denied(self):
        class _FakeSt:
            def __init__(self):
                self.session_state = {}

            def subheader(self, *_a, **_k):
                return None

            def stop(self):
                raise RuntimeError("st.stop")

        fake_st = _FakeSt()
        with patch.object(system_health, "st", fake_st), patch.object(
            system_health, "current_user", return_value=types.SimpleNamespace(username="u", role="viewer")
        ), patch.object(
            system_health, "render_help_panel"
        ), patch.object(
            system_health, "ensure_permission", return_value=False
        ):
            with self.assertRaisesRegex(RuntimeError, "st.stop"):
                system_health.render_system_health(types.SimpleNamespace())

    def test_render_system_health_refresh_rerun(self):
        class _FakeSt:
            def subheader(self, *_a, **_k):
                return None

            def caption(self, *_a, **_k):
                return None

            def button(self, label, key=None):
                return str(label) == "Refresh Health Snapshot"

            def rerun(self):
                raise RuntimeError("st.rerun")

        fake_st = _FakeSt()
        with patch.object(system_health, "st", fake_st), patch.object(
            system_health, "current_user", return_value=types.SimpleNamespace(username="admin", role="admin")
        ), patch.object(
            system_health, "render_help_panel"
        ), patch.object(
            system_health, "ensure_permission", return_value=True
        ):
            with self.assertRaisesRegex(RuntimeError, "st.rerun"):
                system_health.render_system_health(types.SimpleNamespace())

    def test_render_system_health_full_smoke(self):
        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def metric(self, *_a, **_k):
                return None

        class _FakeSt:
            def subheader(self, *_a, **_k):
                return None

            def stop(self):
                raise RuntimeError("st.stop")

            def caption(self, *_a, **_k):
                return None

            def button(self, *_a, **_k):
                return False

            def rerun(self):
                return None

            def markdown(self, *_a, **_k):
                return None

            def columns(self, n):
                count = len(n) if isinstance(n, (list, tuple)) else int(n)
                return [_Ctx() for _ in range(count)]

            def dataframe(self, *_a, **_k):
                return None

            def link_button(self, *_a, **_k):
                return None

            def form(self, *_a, **_k):
                return _Ctx()

            def form_submit_button(self, *_a, **_k):
                return False

            def selectbox(self, _label, options, index=0, **_k):
                opts = list(options)
                if not opts:
                    return None
                idx = int(index) if isinstance(index, int) else 0
                if idx < 0 or idx >= len(opts):
                    idx = 0
                return opts[idx]

            def date_input(self, _label, value=None, **_k):
                return value

            def text_input(self, _label, value="", **_k):
                return value

            def text_area(self, _label, value="", **_k):
                return value

            def checkbox(self, _label, value=False, **_k):
                return bool(value)

            def number_input(self, _label, value=0.0, **_k):
                return value

            def metric(self, *_a, **_k):
                return None

            def download_button(self, *_a, **_k):
                return None

            def json(self, *_a, **_k):
                return None

            def success(self, *_a, **_k):
                return None

            def error(self, *_a, **_k):
                return None

            def warning(self, *_a, **_k):
                return None

        class _ExecResult:
            def __init__(self, *, rows=None, scalar=None, first_row=None):
                self._rows = rows or []
                self._scalar = scalar
                self._first = first_row

            def all(self):
                return list(self._rows)

            def scalar_one(self):
                return self._scalar

            def first(self):
                return self._first

        class _DB:
            def execute(self, query, params=None):
                q = str(query)
                if "SELECT version_num FROM alembic_version" in q:
                    return _ExecResult(first_row=("abc123",))
                if "SELECT COUNT(*) FROM sync_errors WHERE resolved_at IS NULL" in q:
                    return _ExecResult(scalar=0)
                return _ExecResult(rows=[])

        class _Repo:
            def __init__(self):
                self.db = _DB()

            def list_runtime_settings(self, environment=None, active_only=False):
                return []

            def list_ai_provider_configs(self, environment=None, active_only=False):
                return []

            def list_sync_runs(self, limit=1000):
                return []

            def list_notification_outbox(self, *, environment, statuses=None, limit=200, channel=None):
                return []

            def list_integration_queue_jobs(self, *, environment, integration=None, statuses=None, limit=200):
                return []

        fake_st = _FakeSt()
        fake_user = types.SimpleNamespace(username="admin", role="admin")
        fake_slack_cfg = types.SimpleNamespace(enabled=False, bot_token="", default_channel="")
        fake_storage = types.SimpleNamespace(enabled=False, client=None, bucket="")
        fake_ebay = types.SimpleNamespace(is_configured=lambda: False, environment="sandbox")
        fake_spot = types.SimpleNamespace(provider="none", is_configured=lambda: False)
        with patch.object(system_health, "st", fake_st), patch.object(
            system_health, "current_user", return_value=fake_user
        ), patch.object(
            system_health, "ensure_permission", return_value=True
        ), patch.object(
            system_health, "render_help_panel"
        ), patch.object(
            system_health, "required_env_keys", return_value=set()
        ), patch.object(
            system_health, "required_runtime_keys", return_value=set()
        ), patch.object(
            system_health, "read_env_file", return_value={}
        ), patch.object(
            system_health, "resolve_slack_notify_config", return_value=fake_slack_cfg
        ), patch.object(
            system_health, "MediaStorageService", return_value=fake_storage
        ), patch.object(
            system_health, "EbayClient", return_value=fake_ebay
        ), patch.object(
            system_health, "SpotPriceService", return_value=fake_spot
        ), patch.object(
            system_health, "get_runtime_bool", side_effect=lambda _r, _k, default=False: bool(default)
        ), patch.object(
            system_health, "get_runtime_int", side_effect=lambda _r, _k, default=0: int(default)
        ), patch.object(
            system_health, "get_runtime_str", side_effect=lambda _r, _k, default="": str(default)
        ):
            system_health.render_system_health(_Repo())


if __name__ == "__main__":
    unittest.main()
