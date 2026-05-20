import unittest
from datetime import datetime, timedelta
import sys
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

_stub_session_module = ModuleType("app.db.session")
_stub_session_module.SessionLocal = lambda: None
sys.modules.setdefault("app.db.session", _stub_session_module)

from app.services import sync_runner


class _FakeSession:
    def close(self):
        return None


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeQuery:
    def __init__(self, count_value=0):
        self._count_value = count_value

    def filter(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def count(self):
        return self._count_value


class _FakeDB:
    def __init__(self, scalars_rows=None, query_count=0):
        self._scalars_rows = scalars_rows or []
        self._query_count = query_count

    def scalars(self, _query):
        return _FakeScalars(self._scalars_rows)

    def query(self, _model):
        return _FakeQuery(count_value=self._query_count)


class _FakeRepo:
    def __init__(self, db):
        self.db = db
        self.audit_events = []
        self.integration_events = []
        self.runtime_updates = []
        self._fee_reconciliation_rows = []

    def record_audit_event(self, **kwargs):
        self.audit_events.append(kwargs)

    def log_integration_event(self, **kwargs):
        self.integration_events.append(kwargs)

    def upsert_runtime_setting(self, **kwargs):
        self.runtime_updates.append(kwargs)

    def dashboard_metrics(self):
        return {
            "product_count": 2,
            "listing_count": 3,
            "sale_count": 1,
            "inventory_cost": 100.0,
            "gross_sales": 50.0,
            "net_sales": 45.0,
        }

    def list_sales(self):
        return []

    def list_products(self):
        return []

    def list_listings(self):
        return []

    def list_orders(self):
        return []

    def report_ebay_fee_reconciliation_rows(self, *, start_dt, end_dt):
        _ = (start_dt, end_dt)
        return list(self._fee_reconciliation_rows)

    def report_returns_rows(self, *, start_dt, end_dt):
        _ = (start_dt, end_dt)
        return []


class SyncRunnerTests(unittest.TestCase):
    def test_normalized_fee_source_coverage_health(self) -> None:
        rows = [
            {
                "sold_at": "2026-04-01T10:00:00",
                "actual_fee_source": "normalized_order_finance_entries_marketplace_fee_sum",
            },
            {
                "sold_at": "2026-04-03T10:00:00",
                "actual_fee_source": "sale_fees_field",
            },
            {
                "sold_at": "2026-04-10T10:00:00",
                "actual_fee_source": "sale_fees_field",
            },
        ]
        result = sync_runner._normalized_fee_source_coverage_health(
            rows,
            threshold_percent=70.0,
            min_consecutive_weeks=2,
        )
        self.assertTrue(result["triggered"])
        self.assertEqual(result["consecutive_below"], 2)
        self.assertGreaterEqual(len(result["weekly_rows"]), 2)

    def test_schedule_helper_parsers(self) -> None:
        self.assertEqual(sync_runner._parse_schedule_local_time("09:45", fallback_hour=2, fallback_minute=0), (9, 45))
        self.assertEqual(sync_runner._parse_schedule_local_time("bad", fallback_hour=2, fallback_minute=0), (2, 0))
        self.assertEqual(sync_runner._parse_schedule_local_time("99:88", fallback_hour=2, fallback_minute=0), (23, 59))

        self.assertEqual(sync_runner._parse_daily_cron_hhmm_utc("15 6 * * *", default_hour=16, default_minute=0), (6, 15))
        self.assertEqual(sync_runner._parse_daily_cron_hhmm_utc("* * * * *", default_hour=16, default_minute=0), (16, 0))
        self.assertEqual(sync_runner._parse_daily_cron_hhmm_utc("bad expr", default_hour=16, default_minute=0), (16, 0))

    def test_interval_job_due_uses_last_attempt_timestamp(self) -> None:
        repo = _FakeRepo(_FakeDB())
        now = datetime(2026, 5, 6, 12, 0, tzinfo=ZoneInfo("UTC"))

        def runtime_str(_repo, key, default):
            if key == "ai_accountant_monitor_last_attempt_at":
                return "2026-05-06T07:00:00+00:00"
            return default

        with patch("app.services.sync_runner._resolve_schedule_timezone", return_value=ZoneInfo("UTC")), patch(
            "app.services.sync_runner.datetime"
        ) as dt_mod, patch("app.services.sync_runner.get_runtime_int", return_value=6), patch(
            "app.services.sync_runner.get_runtime_str", side_effect=runtime_str
        ):
            dt_mod.now.return_value = now
            dt_mod.fromisoformat.side_effect = datetime.fromisoformat
            due, _local_now, _local_date = sync_runner._is_interval_job_due(
                repo,
                key_prefix="ai_accountant_monitor",
                timezone_key="ai_accountant_monitor_timezone",
                interval_hours_key="ai_accountant_monitor_interval_hours",
                default_timezone="UTC",
                default_interval_hours=6,
            )
        self.assertFalse(due)

        def stale_runtime_str(_repo, key, default):
            if key == "ai_accountant_monitor_last_attempt_at":
                return "2026-05-06T05:00:00+00:00"
            return default

        with patch("app.services.sync_runner._resolve_schedule_timezone", return_value=ZoneInfo("UTC")), patch(
            "app.services.sync_runner.datetime"
        ) as dt_mod, patch("app.services.sync_runner.get_runtime_int", return_value=6), patch(
            "app.services.sync_runner.get_runtime_str", side_effect=stale_runtime_str
        ):
            dt_mod.now.return_value = now
            dt_mod.fromisoformat.side_effect = datetime.fromisoformat
            due, _local_now, _local_date = sync_runner._is_interval_job_due(
                repo,
                key_prefix="ai_accountant_monitor",
                timezone_key="ai_accountant_monitor_timezone",
                interval_hours_key="ai_accountant_monitor_interval_hours",
                default_timezone="UTC",
                default_interval_hours=6,
            )
        self.assertTrue(due)

    def test_resolve_timezone_and_notification_route_helpers(self) -> None:
        repo = _FakeRepo(_FakeDB())
        with patch("app.services.sync_runner.get_runtime_str", return_value="America/Denver"):
            tz = sync_runner._resolve_schedule_timezone(repo, key="tz_key", default_tz="UTC")
        self.assertEqual(getattr(tz, "key", ""), "America/Denver")

        with patch("app.services.sync_runner.get_runtime_str", return_value="not/a-zone"):
            tz = sync_runner._resolve_schedule_timezone(repo, key="tz_key", default_tz="UTC")
        self.assertEqual(getattr(tz, "key", ""), "UTC")

        for route in ["disabled", "off", "none", "email"]:
            with patch("app.services.sync_runner.get_runtime_str", return_value=route):
                self.assertFalse(
                    sync_runner._notification_route_allows_slack(repo, route_key="route_key", default_route="slack")
                )
        for route in ["slack", "both", "all", ""]:
            with patch("app.services.sync_runner.get_runtime_str", return_value=route):
                self.assertTrue(
                    sync_runner._notification_route_allows_slack(repo, route_key="route_key", default_route="slack")
                )

    def test_is_daily_job_due_paths(self) -> None:
        repo = _FakeRepo(_FakeDB())
        fake_now = datetime(2026, 4, 13, 8, 30, 0)
        denver = SimpleNamespace()  # placeholder
        with patch("app.services.sync_runner._resolve_schedule_timezone") as tz_resolve, patch(
            "app.services.sync_runner.datetime"
        ) as dt_mod, patch("app.services.sync_runner.get_runtime_str", return_value=""):
            tz_resolve.return_value = ZoneInfo("UTC")
            dt_mod.now.return_value = fake_now.replace(tzinfo=ZoneInfo("UTC"))
            due, _local_now, local_date = sync_runner._is_daily_job_due(
                repo,
                key_prefix="job_x",
                timezone_key="tz",
                local_time_key="local_time",
                default_timezone="UTC",
                default_local_time="08:00",
            )
        self.assertTrue(due)
        self.assertEqual(local_date, "2026-04-13")

        # Already attempted today -> not due.
        with patch("app.services.sync_runner._resolve_schedule_timezone") as tz_resolve, patch(
            "app.services.sync_runner.datetime"
        ) as dt_mod, patch("app.services.sync_runner.get_runtime_str", side_effect=["08:00", "2026-04-13"]):
            tz_resolve.return_value = ZoneInfo("UTC")
            dt_mod.now.return_value = fake_now.replace(tzinfo=ZoneInfo("UTC"))
            due, _local_now, _local_date = sync_runner._is_daily_job_due(
                repo,
                key_prefix="job_x",
                timezone_key="tz",
                local_time_key="local_time",
                default_timezone="UTC",
                default_local_time="08:00",
            )
        self.assertFalse(due)

    def test_run_once_calls_both_jobs(self) -> None:
        with patch("app.services.sync_runner._run_ebay_token_auto_refresh") as ebay_token_refresh_job, patch(
            "app.services.sync_runner._run_ebay_connection_health_check"
        ) as ebay_health_job, patch(
            "app.services.sync_runner._run_ebay_orders_pull_import"
        ) as ebay_job, patch(
            "app.services.sync_runner._run_governance_snapshot_schedule"
        ) as gov_job, patch(
            "app.services.sync_runner._run_scheduled_db_backup"
        ) as backup_job, patch(
            "app.services.sync_runner._run_daily_slack_report"
        ) as report_job, patch(
            "app.services.sync_runner._run_ai_accountant_monitor_schedule"
        ) as ai_accountant_job, patch(
            "app.services.sync_runner._run_notification_outbox"
        ) as outbox_job:
            with patch("app.services.sync_runner._run_notification_outbox_cleanup") as outbox_cleanup_job, patch(
                "app.services.sync_runner._run_lifecycle_archive_cleanup"
            ) as lifecycle_cleanup_job:
                sync_runner.run_once()
        ebay_token_refresh_job.assert_called_once()
        ebay_health_job.assert_called_once()
        ebay_job.assert_called_once()
        gov_job.assert_called_once()
        backup_job.assert_called_once()
        report_job.assert_called_once()
        ai_accountant_job.assert_called_once()
        outbox_job.assert_called_once()
        outbox_cleanup_job.assert_called_once()
        lifecycle_cleanup_job.assert_called_once()

    def test_run_notification_outbox_disabled(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.get_runtime_bool", return_value=False), patch(
            "app.services.sync_runner.process_due_notification_outbox"
        ) as proc:
            sync_runner._run_notification_outbox()
        proc.assert_not_called()

    def test_run_ebay_token_auto_refresh_logs_success_event(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.maybe_auto_refresh_ebay_user_token",
            return_value={"status": "refreshed", "reason": "near_expiry", "expires_at": "2026-04-18T14:00:00"},
        ):
            sync_runner._run_ebay_token_auto_refresh()
        self.assertTrue(repo.integration_events)
        evt = repo.integration_events[-1]
        self.assertEqual(evt.get("integration"), "ebay_oauth")
        self.assertEqual(evt.get("action"), "auto_refresh")
        self.assertEqual(evt.get("status"), "success")

    def test_run_ebay_token_auto_refresh_logs_error_event(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.maybe_auto_refresh_ebay_user_token",
            return_value={"status": "failed", "reason": "refresh_failed", "error": "boom"},
        ), patch(
            "app.services.sync_runner.get_runtime_bool", return_value=True
        ), patch("app.services.sync_runner.build_slack_alert_text", return_value="alert"), patch(
            "app.services.sync_runner.dispatch_slack_alert"
        ) as dispatch:
            sync_runner._run_ebay_token_auto_refresh()
        self.assertTrue(repo.integration_events)
        evt = repo.integration_events[-1]
        self.assertEqual(evt.get("integration"), "ebay_oauth")
        self.assertEqual(evt.get("action"), "auto_refresh")
        self.assertEqual(evt.get("status"), "error")
        dispatch.assert_called_once()

    def test_run_ebay_token_auto_refresh_failed_alert_can_be_disabled(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.maybe_auto_refresh_ebay_user_token",
            return_value={"status": "failed", "reason": "refresh_failed", "error": "boom"},
        ), patch(
            "app.services.sync_runner.get_runtime_bool", return_value=False
        ), patch("app.services.sync_runner.dispatch_slack_alert") as dispatch:
            sync_runner._run_ebay_token_auto_refresh()
        dispatch.assert_not_called()

    def test_run_ebay_token_auto_refresh_transient_network_logs_warning_without_alert(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.maybe_auto_refresh_ebay_user_token",
            return_value={
                "status": "failed",
                "reason": "transient_network_unavailable",
                "error": "NameResolutionError: failed to resolve api.ebay.com",
            },
        ), patch("app.services.sync_runner.dispatch_slack_alert") as dispatch, patch(
            "app.services.sync_runner._log"
        ) as log:
            sync_runner._run_ebay_token_auto_refresh()
        self.assertTrue(repo.integration_events)
        evt = repo.integration_events[-1]
        self.assertEqual(evt.get("integration"), "ebay_oauth")
        self.assertEqual(evt.get("action"), "auto_refresh")
        self.assertEqual(evt.get("status"), "warning")
        dispatch.assert_not_called()
        self.assertTrue(any("transient network" in str(c.args[0]).lower() for c in log.call_args_list))

    def test_run_notification_outbox_success(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.get_runtime_bool", return_value=True
        ), patch("app.services.sync_runner.get_runtime_int", return_value=50), patch(
            "app.services.sync_runner.process_due_notification_outbox",
            return_value={"due": 2, "sent": 2, "failed": 0},
        ) as proc:
            sync_runner._run_notification_outbox()
        proc.assert_called_once()
        self.assertTrue(repo.integration_events)
        self.assertEqual(repo.integration_events[-1]["integration"], "notification_outbox")

    def test_run_notification_outbox_defaults_enabled_when_setting_missing(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")

        def runtime_bool(_repo, key, default=False):
            self.assertEqual(key, "notification_outbox_runner_enabled")
            self.assertTrue(default)
            return default

        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.get_runtime_bool", side_effect=runtime_bool
        ), patch("app.services.sync_runner.get_runtime_int", return_value=50), patch(
            "app.services.sync_runner.process_due_notification_outbox",
            return_value={"due": 0, "sent": 0, "failed": 0},
        ) as proc:
            sync_runner._run_notification_outbox()

        proc.assert_called_once()

    def test_run_notification_outbox_cleanup_disabled(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.get_runtime_bool", return_value=False), patch(
            "app.services.sync_runner.cleanup_notification_outbox_retention"
        ) as cleanup:
            sync_runner._run_notification_outbox_cleanup()
        cleanup.assert_not_called()

    def test_run_notification_outbox_cleanup_success(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.get_runtime_bool", return_value=True
        ), patch(
            "app.services.sync_runner._is_daily_job_due",
            return_value=(True, datetime(2026, 4, 12, 8, 0, 0), datetime(2026, 4, 12).date()),
        ), patch(
            "app.services.sync_runner.cleanup_notification_outbox_retention",
            return_value={"deleted_total": 3, "deleted_sent": 2, "deleted_failed": 1},
        ) as cleanup, patch("app.services.sync_runner._mark_daily_job_attempt") as mark:
            sync_runner._run_notification_outbox_cleanup()
        cleanup.assert_called_once()
        mark.assert_called_once()
        self.assertTrue(repo.integration_events)
        self.assertEqual(repo.integration_events[-1]["action"], "cleanup")

    def test_run_lifecycle_archive_cleanup_disabled(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.get_runtime_bool", return_value=False), patch(
            "app.services.sync_runner.cleanup_lifecycle_retention"
        ) as cleanup:
            sync_runner._run_lifecycle_archive_cleanup()
        cleanup.assert_not_called()

    def test_run_lifecycle_archive_cleanup_success(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.get_runtime_bool", return_value=True
        ), patch(
            "app.services.sync_runner._is_daily_job_due",
            return_value=(True, datetime(2026, 4, 12, 8, 0, 0), datetime(2026, 4, 12).date()),
        ), patch(
            "app.services.sync_runner.cleanup_lifecycle_retention",
            return_value={
                "retain_days_media": 180,
                "retain_days_listing": 365,
                "retain_days_lot": 365,
                "retain_days_product": 365,
                "deleted_archived_media": 4,
                "deleted_archived_listings": 2,
                "deleted_archived_lots": 1,
                "deleted_archived_products": 3,
                "skipped_listings_with_dependencies": 1,
                "skipped_lots_with_dependencies": 0,
                "skipped_products_with_dependencies": 2,
            },
        ) as cleanup, patch("app.services.sync_runner._mark_daily_job_attempt") as mark:
            sync_runner._run_lifecycle_archive_cleanup()
        cleanup.assert_called_once()
        mark.assert_called_once()
        self.assertTrue(repo.integration_events)
        self.assertEqual(repo.integration_events[-1]["integration"], "lifecycle_retention")

    def test_run_ebay_orders_pull_import_disabled(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.is_sync_job_enabled", return_value=False), patch(
            "app.services.sync_runner._log"
        ) as log:
            sync_runner._run_ebay_orders_pull_import()
        self.assertTrue(any("disabled" in str(c.args[0]) for c in log.call_args_list))

    def test_run_ebay_orders_pull_import_missing_token(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(
            ebay_user_access_token="",
            sync_runner_actor="runner",
            sync_job_ebay_orders_pull_import_limit=25,
            sync_job_ebay_orders_pull_import_offset=0,
        )
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.is_sync_job_enabled", return_value=True
        ), patch("app.services.sync_runner.get_runtime_str", return_value=""), patch(
            "app.services.sync_runner._log"
        ) as log:
            sync_runner._run_ebay_orders_pull_import()
        self.assertTrue(any("missing eBay access token" in str(c.args[0]) for c in log.call_args_list))

    def test_run_ebay_orders_pull_import_success(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(
            ebay_user_access_token="tok",
            sync_runner_actor="runner",
            sync_job_ebay_orders_pull_import_limit=25,
            sync_job_ebay_orders_pull_import_offset=0,
        )
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.is_sync_job_enabled", return_value=True
        ), patch("app.services.sync_runner.get_runtime_str", return_value="tok"), patch(
            "app.services.sync_runner.get_runtime_int", side_effect=[50, 0]
        ), patch(
            "app.services.sync_runner.execute_sync_job",
            return_value={"run_id": 1, "status": "success", "processed": 2, "created": 1, "updated": 1, "failed": 0},
        ) as exec_job:
            sync_runner._run_ebay_orders_pull_import()
        exec_job.assert_called_once()

    def test_run_ebay_orders_pull_import_exception_is_logged(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(
            ebay_user_access_token="tok",
            sync_runner_actor="runner",
            sync_job_ebay_orders_pull_import_limit=25,
            sync_job_ebay_orders_pull_import_offset=0,
        )
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.is_sync_job_enabled", return_value=True
        ), patch("app.services.sync_runner.get_runtime_str", return_value="tok"), patch(
            "app.services.sync_runner.execute_sync_job", side_effect=RuntimeError("boom")
        ), patch("app.services.sync_runner._log") as log:
            sync_runner._run_ebay_orders_pull_import()
        self.assertTrue(any("failed" in str(c.args[0]).lower() for c in log.call_args_list))

    def test_run_ebay_connection_health_check_disabled_not_due_and_success(self) -> None:
        db = _FakeSession()
        now = datetime(2026, 4, 13, 12, 0, 0)

        class RepoWithRuns(_FakeRepo):
            def __init__(self, rows):
                super().__init__(_FakeDB())
                self._rows = rows

            def list_sync_runs(self, provider="ebay", limit=500):
                return list(self._rows)

        disabled_repo = RepoWithRuns([])
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=disabled_repo
        ), patch("app.services.sync_runner.is_sync_job_enabled", return_value=False), patch(
            "app.services.sync_runner._log"
        ) as log:
            sync_runner._run_ebay_connection_health_check()
        self.assertTrue(any("disabled" in str(c.args[0]).lower() for c in log.call_args_list))

        not_due_repo = RepoWithRuns([SimpleNamespace(started_at=now - timedelta(minutes=5), completed_at=now - timedelta(minutes=5), job_name="ebay_connection_health_check")])
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=not_due_repo
        ), patch("app.services.sync_runner.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_runner.get_runtime_int", return_value=30
        ), patch("app.services.sync_runner.utcnow_naive", return_value=now), patch(
            "app.services.sync_runner._log"
        ) as log:
            sync_runner._run_ebay_connection_health_check()
        self.assertTrue(any("not due" in str(c.args[0]).lower() for c in log.call_args_list))

        success_repo = RepoWithRuns([])
        settings = SimpleNamespace(
            ebay_user_access_token="tok",
            sync_runner_actor="runner",
            sync_job_ebay_connection_health_check_interval_minutes=30,
        )
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=success_repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.is_sync_job_enabled", return_value=True
        ), patch("app.services.sync_runner.get_runtime_int", return_value=30), patch(
            "app.services.sync_runner.get_runtime_str", return_value="tok"
        ), patch(
            "app.services.sync_runner.execute_sync_job",
            return_value={"run_id": 2, "status": "success", "processed": 1, "failed": 0},
        ) as exec_job:
            sync_runner._run_ebay_connection_health_check()
        exec_job.assert_called_once()

    def test_run_ebay_connection_health_check_exception_is_logged(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_runner.get_runtime_int", side_effect=RuntimeError("bad")
        ), patch("app.services.sync_runner._log") as log:
            sync_runner._run_ebay_connection_health_check()
        self.assertTrue(any("failed" in str(c.args[0]).lower() for c in log.call_args_list))

    def test_run_governance_snapshot_disabled(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.get_runtime_bool", return_value=False), patch(
            "app.services.sync_runner._log"
        ) as log:
            sync_runner._run_governance_snapshot_schedule()
        self.assertTrue(any("disabled" in str(c.args[0]) for c in log.call_args_list))
        self.assertEqual(repo.audit_events, [])

    def test_run_governance_snapshot_not_due(self) -> None:
        now = datetime(2026, 3, 29, 10, 0, 0)
        recent = [
            SimpleNamespace(
                created_at=now - timedelta(hours=1),
                changes={"source": "sync_runner"},
            )
        ]
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB(scalars_rows=recent, query_count=5))

        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.get_runtime_bool", return_value=True), patch(
            "app.services.sync_runner.get_runtime_int", side_effect=[24, 30, 2000]
        ), patch("app.services.sync_runner.utcnow_naive", return_value=now), patch(
            "app.services.sync_runner._log"
        ) as log:
            sync_runner._run_governance_snapshot_schedule()
        self.assertTrue(any("not due" in str(c.args[0]) for c in log.call_args_list))
        self.assertEqual(repo.audit_events, [])

    def test_run_governance_snapshot_records_event_when_due(self) -> None:
        now = datetime(2026, 3, 29, 10, 0, 0)
        recent = [
            SimpleNamespace(
                created_at=now - timedelta(hours=48),
                changes={"source": "sync_runner"},
            )
        ]
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB(scalars_rows=recent, query_count=3))
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")

        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.get_runtime_bool", return_value=True
        ), patch("app.services.sync_runner.get_runtime_int", side_effect=[24, 30, 2000]), patch(
            "app.services.sync_runner.utcnow_naive", return_value=now
        ):
            sync_runner._run_governance_snapshot_schedule()

        self.assertEqual(len(repo.audit_events), 1)
        payload = repo.audit_events[0]["changes"]
        self.assertEqual(payload["source"], "sync_runner")
        self.assertEqual(payload["counts"]["handoff_events"], 3)

    def test_run_governance_snapshot_ignores_non_runner_snapshot_rows(self) -> None:
        now = datetime(2026, 3, 29, 10, 0, 0)
        recent = [
            SimpleNamespace(created_at=now - timedelta(hours=1), changes={"source": "someone_else"}),
            SimpleNamespace(created_at=now - timedelta(hours=2), changes="not-a-dict"),
        ]
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB(scalars_rows=recent, query_count=2))
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.get_runtime_bool", return_value=True
        ), patch("app.services.sync_runner.get_runtime_int", side_effect=[24, 30, 2000]), patch(
            "app.services.sync_runner.utcnow_naive", return_value=now
        ):
            sync_runner._run_governance_snapshot_schedule()
        self.assertEqual(len(repo.audit_events), 1)

    def test_run_governance_snapshot_scheduler_exception_is_logged(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.get_runtime_bool", return_value=True), patch(
            "app.services.sync_runner.get_runtime_int", side_effect=RuntimeError("bad runtime")
        ), patch("app.services.sync_runner._log") as log:
            sync_runner._run_governance_snapshot_schedule()
        self.assertTrue(any("scheduler failed" in str(c.args[0]).lower() for c in log.call_args_list))

    def test_run_forever_disabled_and_run_once_modes(self) -> None:
        with patch("app.services.sync_runner.settings", SimpleNamespace(sync_runner_enabled=False)), patch(
            "app.services.sync_runner._log"
        ) as log:
            sync_runner.run_forever()
        self.assertTrue(any("SYNC_RUNNER_ENABLED=false" in str(c.args[0]) for c in log.call_args_list))

        settings = SimpleNamespace(
            sync_runner_enabled=True,
            sync_runner_interval_seconds=1,
            sync_runner_actor="runner",
            sync_runner_run_once=True,
        )
        with patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.run_once"
        ) as run_once, patch("app.services.sync_runner._log"):
            sync_runner.run_forever()
        run_once.assert_called_once()

    def test_run_forever_sleep_loop_mode(self) -> None:
        settings = SimpleNamespace(
            sync_runner_enabled=True,
            sync_runner_interval_seconds=31,
            sync_runner_actor="runner",
            sync_runner_run_once=False,
        )
        with patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.run_once"
        ) as run_once, patch("app.services.sync_runner.time.sleep", side_effect=RuntimeError("stop")), patch(
            "app.services.sync_runner._log"
        ) as log:
            with self.assertRaises(RuntimeError):
                sync_runner.run_forever()
        self.assertTrue(any("Sleeping 31s" in str(c.args[0]) for c in log.call_args_list))
        run_once.assert_called_once()

    def test_run_scheduled_db_backup_disabled(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.get_runtime_bool", return_value=False), patch(
            "app.services.sync_runner._log"
        ) as log:
            sync_runner._run_scheduled_db_backup()
        self.assertTrue(any("disabled" in str(c.args[0]).lower() for c in log.call_args_list))

    def test_run_scheduled_db_backup_success(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        backup_result = SimpleNamespace(file_name="local-db.sql", size_bytes=1234, file_path=SimpleNamespace())
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.get_runtime_bool", side_effect=[True, True, True, False]
        ), patch("app.services.sync_runner._is_daily_job_due", return_value=(True, datetime(2026, 4, 9, 2, 5), "2026-04-09")), patch(
            "app.services.sync_runner._backup_create_dump", return_value=backup_result
        ) as create_dump, patch(
            "app.services.sync_runner._mark_daily_job_attempt"
        ) as mark_attempt, patch(
            "app.services.sync_runner._log"
        ):
            sync_runner._run_scheduled_db_backup()
        create_dump.assert_called_once()
        mark_attempt.assert_called()
        self.assertTrue(any(e.get("action") == "scheduled_db_backup" for e in repo.integration_events))

    def test_run_daily_slack_report_disabled(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.get_runtime_bool", return_value=False), patch(
            "app.services.sync_runner._log"
        ) as log:
            sync_runner._run_daily_slack_report()
        self.assertTrue(any("disabled" in str(c.args[0]).lower() for c in log.call_args_list))

    def test_run_ai_accountant_monitor_schedule_disabled(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.get_runtime_bool", return_value=False), patch(
            "app.services.sync_runner._log"
        ) as log:
            sync_runner._run_ai_accountant_monitor_schedule()
        self.assertTrue(any("AI Accountant monitor disabled" in str(call.args[0]) for call in log.call_args_list))

    def test_run_ai_accountant_monitor_schedule_success(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        local_now = datetime(2026, 4, 9, 8, 31, tzinfo=ZoneInfo("UTC"))
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.get_runtime_bool", side_effect=[True, True, False]
        ), patch(
            "app.services.sync_runner._is_interval_job_due",
            return_value=(True, local_now, "2026-04-09"),
        ), patch(
            "app.services.sync_runner.get_runtime_int", return_value=30
        ), patch(
            "app.services.sync_runner._app_default_timezone", return_value="UTC"
        ), patch(
            "app.services.sync_runner.get_runtime_str", side_effect=["interval", "P1", "slack", "#accounting"]
        ), patch(
            "app.services.sync_runner.run_ai_accountant_monitor",
            return_value={
                "item_count": 3,
                "actionable_count": 2,
                "audit_id": 11,
                "slack_outbox_id": 12,
                "period_label": "2026-03-10 to 2026-04-09",
                "review_enabled": True,
                "review_hash": "a" * 64,
                "review_error": "",
                "review_compact_retry": False,
                "review_runtime_route": "localai/Qwen (chat, db, ready)",
            },
        ) as monitor, patch("app.services.sync_runner._log"):
            sync_runner._run_ai_accountant_monitor_schedule()

        monitor.assert_called_once()
        self.assertTrue(any(row["key"] == "ai_accountant_monitor_last_success_at" for row in repo.runtime_updates))
        event = next(event for event in repo.integration_events if event.get("integration") == "ai_accountant")
        self.assertEqual(event["details"]["schedule_mode"], "interval")
        self.assertTrue(event["details"]["review_enabled"])
        self.assertEqual(event["details"]["review_status"], "completed")
        self.assertEqual(event["details"]["review_hash"], "a" * 12)
        self.assertEqual(event["details"]["review_runtime_route"], "localai/Qwen (chat, db, ready)")

    def test_run_ai_accountant_monitor_schedule_honors_notification_route(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        local_now = datetime(2026, 4, 9, 8, 31, tzinfo=ZoneInfo("UTC"))
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.get_runtime_bool", side_effect=[True, True, False]
        ), patch(
            "app.services.sync_runner._is_interval_job_due",
            return_value=(True, local_now, "2026-04-09"),
        ), patch(
            "app.services.sync_runner.get_runtime_int", return_value=30
        ), patch(
            "app.services.sync_runner._app_default_timezone", return_value="UTC"
        ), patch(
            "app.services.sync_runner.get_runtime_str", side_effect=["interval", "P1", "disabled", "#accounting"]
        ), patch(
            "app.services.sync_runner.run_ai_accountant_monitor",
            return_value={
                "item_count": 3,
                "actionable_count": 2,
                "audit_id": 11,
                "slack_outbox_id": None,
                "period_label": "2026-03-10 to 2026-04-09",
            },
        ) as monitor, patch("app.services.sync_runner._log"):
            sync_runner._run_ai_accountant_monitor_schedule()

        monitor.assert_called_once()
        self.assertFalse(monitor.call_args.kwargs["slack_enabled"])

    def test_run_ai_accountant_monitor_schedule_interval_failure_marks_interval_attempt(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        local_now = datetime(2026, 4, 9, 8, 31, tzinfo=ZoneInfo("UTC"))
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.get_runtime_bool", side_effect=[True, False, False]
        ), patch(
            "app.services.sync_runner._is_interval_job_due",
            return_value=(True, local_now, "2026-04-09"),
        ) as interval_due, patch(
            "app.services.sync_runner.get_runtime_int", return_value=30
        ), patch(
            "app.services.sync_runner._app_default_timezone", return_value="UTC"
        ), patch(
            "app.services.sync_runner.get_runtime_str", side_effect=["interval", "P1", ""]
        ), patch(
            "app.services.sync_runner.run_ai_accountant_monitor",
            side_effect=RuntimeError("monitor failed"),
        ), patch("app.services.sync_runner._log"):
            sync_runner._run_ai_accountant_monitor_schedule()

        self.assertEqual(interval_due.call_count, 2)
        self.assertTrue(any(row["key"] == "ai_accountant_monitor_last_attempt_at" for row in repo.runtime_updates))
        self.assertFalse(
            any(row["key"] == "ai_accountant_monitor_last_attempt_local_date" for row in repo.runtime_updates)
        )
        event = next(event for event in repo.integration_events if event.get("integration") == "ai_accountant")
        self.assertEqual(event["status"], "error")
        self.assertEqual(event["details"]["schedule_mode"], "interval")

    def test_run_ai_accountant_monitor_schedule_daily_failure_marks_daily_attempt(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        local_now = datetime(2026, 4, 9, 8, 31, tzinfo=ZoneInfo("UTC"))
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.get_runtime_bool", side_effect=[True, False, False]
        ), patch(
            "app.services.sync_runner._is_daily_job_due",
            return_value=(True, local_now, "2026-04-09"),
        ) as daily_due, patch(
            "app.services.sync_runner.get_runtime_int", return_value=30
        ), patch(
            "app.services.sync_runner._app_default_timezone", return_value="UTC"
        ), patch(
            "app.services.sync_runner.get_runtime_str", side_effect=["daily", "P1", ""]
        ), patch(
            "app.services.sync_runner.run_ai_accountant_monitor",
            side_effect=RuntimeError("monitor failed"),
        ), patch("app.services.sync_runner._log"):
            sync_runner._run_ai_accountant_monitor_schedule()

        self.assertEqual(daily_due.call_count, 2)
        self.assertTrue(
            any(row["key"] == "ai_accountant_monitor_last_attempt_local_date" for row in repo.runtime_updates)
        )
        self.assertFalse(any(row["key"] == "ai_accountant_monitor_last_attempt_at" for row in repo.runtime_updates))
        event = next(event for event in repo.integration_events if event.get("integration") == "ai_accountant")
        self.assertEqual(event["status"], "error")
        self.assertEqual(event["details"]["schedule_mode"], "daily")

    def test_run_daily_slack_report_success(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        now = datetime(2026, 4, 9, 14, 0, 0)
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.utcnow_naive", return_value=now
        ), patch(
            "app.services.sync_runner.get_runtime_bool", return_value=True
        ), patch(
            "app.services.sync_runner.get_runtime_int", side_effect=[8, 2]
        ), patch(
            "app.services.sync_runner.get_runtime_float", return_value=80.0
        ), patch(
            "app.services.sync_runner._is_daily_job_due", return_value=(True, datetime(2026, 4, 9, 8, 1), "2026-04-09")
        ), patch(
            "app.services.sync_runner.get_runtime_str", return_value=""
        ), patch(
            "app.services.sync_runner.build_slack_alert_text", return_value="daily report"
        ), patch(
            "app.services.sync_runner.dispatch_slack_alert", return_value={"status": "sent", "channel": "#ops"}
        ) as dispatch, patch(
            "app.services.sync_runner._mark_daily_job_attempt"
        ) as mark_attempt, patch(
            "app.services.sync_runner._log"
        ):
            sync_runner._run_daily_slack_report()
        dispatch.assert_called_once()
        mark_attempt.assert_called()
        self.assertTrue(any(e.get("action") == "daily_report" for e in repo.integration_events))

    def test_run_daily_slack_report_uses_actual_economics_net(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        now = datetime(2026, 4, 9, 14, 0, 0)
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        sale = SimpleNamespace(
            id=11,
            product_id=1,
            sold_at=now,
            quantity_sold=1,
            sold_price=100.0,
            fees=10.0,
            shipping_cost=5.0,
            shipping_label_cost=9.0,
        )
        repo.list_sales = lambda: [sale]
        repo.list_products = lambda: [
            SimpleNamespace(
                id=1,
                acquisition_cost=None,
                acquisition_tax_paid=None,
                acquisition_shipping_paid=None,
                acquisition_handling_paid=None,
                product_cost=20.0,
                current_quantity=1,
            )
        ]
        repo.report_sale_unit_cost_maps = lambda end_dt, default_unit_cost_by_product: {
            "fifo_unit_cost_by_sale": {11: 12.5},
            "fifo_unit_cost_source_by_sale": {11: "lot_expected_quantity_fallback"},
        }
        repo.report_sales_actual_econ_rows = lambda start_dt, end_dt: [
            {
                "sale_id": 11,
                "sold_price": 100.0,
                "allocated_fee_actual": 7.5,
                "allocated_shipping_charged": 5.0,
                "allocated_shipping_actual": 4.25,
                "net_before_cogs_actual": 93.25,
            }
        ]
        repo.report_returns_rows = lambda start_dt, end_dt: [
            {
                "return_id": 9,
                "sale_id": 11,
                "product_id": 1,
                "quantity": 1,
                "refund_amount": 30.0,
                "refund_fees": 2.0,
                "refund_shipping": 3.0,
            }
        ]
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.utcnow_naive", return_value=now
        ), patch(
            "app.services.sync_runner.get_runtime_bool", return_value=True
        ), patch(
            "app.services.sync_runner.get_runtime_int", side_effect=[8, 2]
        ), patch(
            "app.services.sync_runner.get_runtime_float", return_value=80.0
        ), patch(
            "app.services.sync_runner._is_daily_job_due",
            return_value=(True, datetime(2026, 4, 9, 8, 1), "2026-04-09"),
        ), patch(
            "app.services.sync_runner.get_runtime_str", return_value=""
        ), patch(
            "app.services.sync_runner.build_slack_alert_text", return_value="daily report"
        ) as build_text, patch(
            "app.services.sync_runner.dispatch_slack_alert", return_value={"status": "sent", "channel": "#ops"}
        ), patch(
            "app.services.sync_runner._mark_daily_job_attempt"
        ), patch(
            "app.services.sync_runner._log"
        ):
            sync_runner._run_daily_slack_report()

        context = build_text.call_args.kwargs["context"]
        self.assertEqual(context["gross_24h"], "100.00")
        self.assertEqual(context["net_24h"], "93.25")
        self.assertEqual(context["cogs_24h"], "12.50")
        self.assertEqual(context["profit_before_returns_24h"], "80.75")
        self.assertEqual(context["returns_24h_count"], 1)
        self.assertEqual(context["returns_refund_24h"], "35.00")
        self.assertEqual(context["returns_cogs_reversal_24h"], "12.50")
        self.assertEqual(context["returns_profit_impact_24h"], "-22.50")
        self.assertEqual(context["net_after_returns_24h"], "58.25")
        self.assertEqual(context["estimated_profit_24h"], "58.25")
        self.assertIn("lot_expected_quantity_fallback", context["cogs_source_mix"])
        event = next(e for e in repo.integration_events if e.get("action") == "daily_report")
        self.assertAlmostEqual(event["details"]["net_24h"], 93.25)
        self.assertAlmostEqual(event["details"]["cogs_24h"], 12.5)
        self.assertAlmostEqual(event["details"]["profit_before_returns_24h"], 80.75)
        self.assertEqual(event["details"]["returns_24h_count"], 1)
        self.assertAlmostEqual(event["details"]["returns_refund_24h"], 35.0)
        self.assertAlmostEqual(event["details"]["returns_cogs_reversal_24h"], 12.5)
        self.assertAlmostEqual(event["details"]["returns_profit_impact_24h"], -22.5)
        self.assertAlmostEqual(event["details"]["estimated_profit_24h"], 58.25)
        self.assertEqual(
            event["details"]["cogs_source_totals"],
            {"lot_expected_quantity_fallback": 12.5},
        )

    def test_run_daily_slack_report_logs_fee_coverage_alert(self) -> None:
        db = _FakeSession()
        repo = _FakeRepo(_FakeDB())
        repo._fee_reconciliation_rows = [
            {"sold_at": "2026-04-02T08:00:00", "actual_fee_source": "sale_fees_field"},
            {"sold_at": "2026-04-09T08:00:00", "actual_fee_source": "sale_fees_field"},
        ]
        now = datetime(2026, 4, 9, 14, 0, 0)
        settings = SimpleNamespace(sync_runner_actor="runner", app_env="local")
        with patch("app.services.sync_runner.SessionLocal", return_value=db), patch(
            "app.services.sync_runner.InventoryRepository", return_value=repo
        ), patch("app.services.sync_runner.settings", settings), patch(
            "app.services.sync_runner.utcnow_naive", return_value=now
        ), patch(
            "app.services.sync_runner.get_runtime_bool", return_value=True
        ), patch(
            "app.services.sync_runner.get_runtime_int", side_effect=[8, 2]
        ), patch(
            "app.services.sync_runner.get_runtime_float", return_value=90.0
        ), patch(
            "app.services.sync_runner._is_daily_job_due", return_value=(True, datetime(2026, 4, 9, 8, 1), "2026-04-09")
        ), patch(
            "app.services.sync_runner.get_runtime_str", return_value=""
        ), patch(
            "app.services.sync_runner.build_slack_alert_text", return_value="daily report"
        ), patch(
            "app.services.sync_runner.dispatch_slack_alert", return_value={"status": "sent", "channel": "#ops"}
        ), patch(
            "app.services.sync_runner._mark_daily_job_attempt"
        ), patch(
            "app.services.sync_runner._log"
        ):
            sync_runner._run_daily_slack_report()
        event = repo.integration_events[-1]
        self.assertTrue(bool(event["details"].get("normalized_fee_coverage_triggered")))


if __name__ == "__main__":
    unittest.main()
