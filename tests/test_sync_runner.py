import unittest
from datetime import datetime, timedelta
import sys
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

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

    def record_audit_event(self, **kwargs):
        self.audit_events.append(kwargs)


class SyncRunnerTests(unittest.TestCase):
    def test_run_once_calls_both_jobs(self) -> None:
        with patch("app.services.sync_runner._run_ebay_orders_pull_import") as ebay_job, patch(
            "app.services.sync_runner._run_governance_snapshot_schedule"
        ) as gov_job:
            sync_runner.run_once()
        ebay_job.assert_called_once()
        gov_job.assert_called_once()

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


if __name__ == "__main__":
    unittest.main()
