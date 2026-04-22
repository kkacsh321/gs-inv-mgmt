import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


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
    if "app.components.views.ebay" not in sys.modules:
        fake_ebay_view = types.ModuleType("app.components.views.ebay")
        fake_ebay_view.render_ebay_connection_status_card = lambda *args, **kwargs: None
        sys.modules["app.components.views.ebay"] = fake_ebay_view
    if "app.components.views.listing_wizard" not in sys.modules:
        fake_listing_wizard_view = types.ModuleType("app.components.views.listing_wizard")
        fake_listing_wizard_view.DEFAULT_LISTING_WIZARD_AI_INSTRUCTION_TEMPLATE = ""
        fake_listing_wizard_view.DEFAULT_LISTING_WIZARD_AI_SEED_PROMPT = ""
        fake_listing_wizard_view.DEFAULT_LISTING_WIZARD_AI_SYSTEM_MESSAGE = ""
        sys.modules["app.components.views.listing_wizard"] = fake_listing_wizard_view
    if "app.db.seed" not in sys.modules:
        fake_seed = types.ModuleType("app.db.seed")
        fake_seed.seed_dev_data = lambda *args, **kwargs: {}
        sys.modules["app.db.seed"] = fake_seed
    if "alembic" not in sys.modules:
        fake_alembic = types.ModuleType("alembic")
        fake_alembic.command = SimpleNamespace(
            upgrade=lambda *_args, **_kwargs: None,
            downgrade=lambda *_args, **_kwargs: None,
            current=lambda *_args, **_kwargs: None,
            history=lambda *_args, **_kwargs: None,
            revision=lambda *_args, **_kwargs: None,
        )
        sys.modules["alembic"] = fake_alembic
    if "alembic.config" not in sys.modules:
        fake_alembic_config = types.ModuleType("alembic.config")
        fake_alembic_config.Config = lambda *args, **kwargs: None
        sys.modules["alembic.config"] = fake_alembic_config
    if "alembic.script" not in sys.modules:
        fake_alembic_script = types.ModuleType("alembic.script")

        class _FakeScriptDirectory:
            @staticmethod
            def from_config(_cfg):
                return SimpleNamespace(
                    get_current_head=lambda: "",
                    walk_revisions=lambda base="base", head="heads": [],
                )

        fake_alembic_script.ScriptDirectory = _FakeScriptDirectory
        sys.modules["alembic.script"] = fake_alembic_script
    root = Path(__file__).resolve().parents[1]
    for name in ("shared", "entity_ops", "workspace_shell", "system_health", "tools"):
        full = f"app.components.views.{name}"
        if full in sys.modules:
            continue
        path = root / "app" / "components" / "views" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(full, path)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        sys.modules[full] = mod


def _load_admin_module():
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / "admin.py"
    spec = importlib.util.spec_from_file_location("test_admin_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


admin = _load_admin_module()


class AdminHelpersTests(unittest.TestCase):
    def test_summarize_ai_quality_metrics(self):
        rows = [
            (
                datetime(2026, 4, 20, 10, 0, 0),
                "qa",
                "listing_wizard_apply",
                '{"workflow":"listing_wizard","acceptance":{"prompt_version_id":"v1"}}',
            ),
            (
                datetime(2026, 4, 20, 10, 5, 0),
                "qa",
                "listing_wizard_outcome",
                '{"workflow":"listing_wizard","acceptance":{"prompt_version_id":"v1"},"outcome":{"accepted_as_is":true,"edited_fields":[]}}',
            ),
            (
                datetime(2026, 4, 21, 9, 0, 0),
                "qa",
                "listing_wizard_outcome",
                '{"workflow":"listing_wizard","acceptance":{"prompt_version_id":"v2"},"outcome":{"accepted_as_is":false,"edited_fields":["title","details"]}}',
            ),
        ]
        out = admin._summarize_ai_quality_metrics(rows, workflow_filter="all")
        self.assertEqual(out["apply_events"], 1)
        self.assertEqual(out["outcome_events"], 2)
        self.assertEqual(out["accepted_as_is_count"], 1)
        self.assertEqual(out["edited_count"], 1)
        self.assertIn("listing_wizard", out["workflow_totals"])
        self.assertIn("v1", out["version_totals"])
        self.assertIn("v2", out["version_totals"])
        self.assertEqual(len(out["daily_rows"]), 2)
        self.assertGreaterEqual(len(out["edited_fields_top_rows"]), 2)

    def test_summarize_ai_quality_metrics_workflow_filter(self):
        rows = [
            (
                datetime(2026, 4, 20, 10, 0, 0),
                "qa",
                "listing_wizard_outcome",
                '{"workflow":"listing_wizard","acceptance":{"prompt_version_id":"v1"},"outcome":{"accepted_as_is":true,"edited_fields":[]}}',
            ),
            (
                datetime(2026, 4, 20, 10, 1, 0),
                "qa",
                "listing_wizard_outcome",
                '{"workflow":"intake","acceptance":{"prompt_version_id":"v-intake"},"outcome":{"accepted_as_is":false,"edited_fields":["title"]}}',
            ),
        ]
        out = admin._summarize_ai_quality_metrics(rows, workflow_filter="intake")
        self.assertEqual(out["outcome_events"], 1)
        self.assertEqual(out["accepted_as_is_count"], 0)
        self.assertEqual(out["edited_count"], 1)
        self.assertEqual(set(out["workflow_totals"].keys()), {"intake"})

    def test_go_live_evidence_pack_includes_lifecycle_retention_signoffs(self):
        admin_source = (Path(__file__).resolve().parents[1] / "app" / "components" / "views" / "admin.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("lifecycle_retention_policy_signoffs.csv", admin_source)
        self.assertIn("Lifecycle Retention Policy Sign-Off Tracker", admin_source)
        self.assertIn("economics_threshold_signoffs.csv", admin_source)
        self.assertIn("Economics Threshold Sign-Off Tracker", admin_source)

    def test_audit_changes_parsing(self):
        row_ok = SimpleNamespace(changes_json='{"a":1}')
        row_bad = SimpleNamespace(changes_json="{")
        row_list = SimpleNamespace(changes_json='["x"]')
        self.assertEqual(admin._audit_changes(row_ok), {"a": 1})
        self.assertEqual(admin._audit_changes(row_bad), {})
        self.assertEqual(admin._audit_changes(row_list), {})

    def test_basic_helper_values(self):
        self.assertIn("Append seed data", admin._seed_mode_label("append_only"))
        self.assertIn("Wipe seed tables", admin._seed_mode_label("wipe_seed_tables_then_seed"))
        self.assertIn("Wipe operational", admin._seed_mode_label("unknown"))
        self.assertEqual(admin._mask_secret(""), "(not set)")
        self.assertTrue(admin._mask_secret("abcdef").endswith("cdef"))
        self.assertEqual(admin._health_label_and_emoji(0.98), ("healthy", "green"))
        self.assertEqual(admin._health_label_and_emoji(0.9), ("warning", "orange"))
        self.assertEqual(admin._health_label_and_emoji(0.1), ("critical", "red"))
        self.assertGreater(len(admin._all_permission_options()), 5)
        self.assertGreater(len(admin._workspace_parity_specs()), 3)

    def test_get_current_db_revision(self):
        repo_ok = SimpleNamespace(
            db=SimpleNamespace(
                execute=lambda _sql: SimpleNamespace(scalar_one=lambda: "abc123")
            )
        )
        self.assertEqual(admin._get_current_db_revision(repo_ok), "abc123")

        class _BadDb:
            def execute(self, _sql):
                raise RuntimeError("no table")

        repo_bad = SimpleNamespace(db=_BadDb())
        self.assertIn("unknown", admin._get_current_db_revision(repo_bad))

    def test_migration_history_rows(self):
        rev1 = SimpleNamespace(revision="r2", down_revision="r1", doc="head rev")
        rev2 = SimpleNamespace(revision="r1", down_revision=("r0",), doc="base rev")
        fake_script = SimpleNamespace(
            get_current_head=lambda: "r2",
            walk_revisions=lambda base="base", head="heads": [rev1, rev2],
        )
        with patch.object(admin, "ScriptDirectory") as script_dir:
            script_dir.from_config.return_value = fake_script
            rows = admin._migration_history_rows()
        self.assertEqual(rows[0]["revision"], "r2")
        self.assertEqual(rows[0]["is_head"], "yes")
        self.assertIn("r0", rows[1]["down_revision"])

    def test_normalize_comp_dealer_domains(self):
        raw = "https://www.APMEX.com/path, jmBullion.com,invalid, www.sdbullion.com"
        csv_out, domains = admin._normalize_comp_dealer_domains_csv(raw)
        self.assertIn("apmex.com", domains)
        self.assertIn("jmbullion.com", domains)
        self.assertIn("sdbullion.com", domains)
        self.assertNotIn("invalid", domains)
        self.assertEqual(csv_out, ",".join(domains))

    def test_ebay_finding_recommended_runtime_settings(self):
        rows = admin._ebay_finding_recommended_runtime_settings()
        self.assertEqual(len(rows), 5)
        by_key = {key: (value, value_type, description) for key, value, value_type, description in rows}
        self.assertEqual(by_key["comp_ebay_max_calls_per_run"][0], "3")
        self.assertEqual(by_key["comp_ebay_max_calls_per_10m"][0], "12")
        self.assertEqual(by_key["ebay_finding_rate_limit_cooldown_seconds"][0], "600")
        self.assertEqual(by_key["ebay_finding_rate_limit_severe_cooldown_seconds"][0], "3600")
        self.assertEqual(by_key["ebay_finding_rate_limit_probe_interval_seconds"][0], "120")
        self.assertTrue(all(parts[1] == "int" for parts in by_key.values()))

    def test_runtime_seed_defaults_include_oauth_refresh_failure_controls(self):
        rows = admin._runtime_setting_seed_defaults()
        keys = {str(row.get("key") or "") for row in rows}
        self.assertIn("ebay_user_token_auto_refresh_failure_cooldown_minutes", keys)
        self.assertIn("slack_notify_ebay_oauth_refresh_failures", keys)

    def test_ebay_token_auto_refresh_diagnostics(self):
        now = datetime(2026, 4, 18, 12, 0, 0)
        runtime_int_values = {
            "ebay_user_token_auto_refresh_interval_hours": 12,
            "ebay_user_token_auto_refresh_min_ttl_minutes": 45,
            "ebay_user_token_auto_refresh_failure_cooldown_minutes": 30,
        }
        runtime_str_values = {
            "ebay_user_access_token_refreshed_at": "2026-04-18T10:00:00",
            "ebay_user_access_token_expires_at": "2026-04-18T12:20:00",
            "ebay_user_access_token_refresh_failed_at": "2026-04-18T11:50:00",
            "ebay_user_access_token_refresh_last_error": "boom",
        }
        with patch.object(admin, "utcnow_naive", return_value=now), patch.object(
            admin,
            "get_runtime_int",
            side_effect=lambda _repo, key, default=0: int(runtime_int_values.get(key, default)),
        ), patch.object(
            admin,
            "get_runtime_str",
            side_effect=lambda _repo, key, default="": str(runtime_str_values.get(key, default)),
        ):
            payload = admin._ebay_token_auto_refresh_diagnostics(SimpleNamespace())
        self.assertEqual(payload["interval_hours"], 12)
        self.assertEqual(payload["min_ttl_minutes"], 45)
        self.assertEqual(payload["failure_cooldown_minutes"], 30)
        self.assertEqual(payload["expires_in_minutes"], 20)
        self.assertEqual(payload["next_refresh_due_at"], "2026-04-18T22:00:00")
        self.assertEqual(payload["failure_cooldown_until"], "2026-04-18T12:20:00")
        self.assertTrue(payload["failure_cooldown_active"])
        self.assertEqual(payload["last_error"], "boom")

    def test_clear_ebay_token_refresh_failure_state(self):
        calls = []

        class _Repo:
            def upsert_runtime_setting(self, **kwargs):
                calls.append(kwargs)

        with patch.object(admin, "settings", SimpleNamespace(app_env="prod")):
            admin._clear_ebay_token_refresh_failure_state(_Repo(), actor="qa")
        self.assertEqual(len(calls), 2)
        by_key = {str(row.get("key") or ""): row for row in calls}
        self.assertIn("ebay_user_access_token_refresh_failed_at", by_key)
        self.assertIn("ebay_user_access_token_refresh_last_error", by_key)
        self.assertEqual(by_key["ebay_user_access_token_refresh_failed_at"]["value"], "")
        self.assertEqual(by_key["ebay_user_access_token_refresh_last_error"]["value"], "")
        self.assertEqual(by_key["ebay_user_access_token_refresh_failed_at"]["environment"], "prod")
        self.assertEqual(by_key["ebay_user_access_token_refresh_last_error"]["actor"], "qa")

    def test_build_env_coverage_rows_statuses(self):
        env_values = {"A": "", "B": "x", "C": "custom"}
        defaults = {"A": "1", "B": "x", "D": "d"}
        with patch.object(admin, "is_editable_env_key", side_effect=lambda k: k != "D"), patch.object(
            admin, "mask_env_value", side_effect=lambda _k, v: str(v)
        ):
            rows = admin._build_env_coverage_rows(env_values, defaults)
        by_key = {r["key"]: r for r in rows}
        self.assertEqual(by_key["A"]["status"], "empty")
        self.assertEqual(by_key["B"]["status"], "default")
        self.assertEqual(by_key["C"]["status"], "set")
        self.assertEqual(by_key["D"]["status"], "missing")
        self.assertFalse(by_key["D"]["editable"])

    def test_slack_ops_queue_snapshot_metrics(self):
        now = datetime(2026, 4, 20, 12, 0, 0)
        rows = [
            SimpleNamespace(
                id=1,
                action="command_ingest",
                status="blocked",
                retry_count=0,
                max_retries=2,
                next_attempt_at=None,
                requested_by="ops1",
                created_at=now,
                last_error="Awaiting approval",
                payload_json='{"command":{"intent":"operations"},"approval":{"required":true,"status":"pending","requested_at":"2026-04-20T10:00:00","requested_by":"ops1"}}',
            ),
            SimpleNamespace(
                id=2,
                action="command_ingest",
                status="success",
                retry_count=1,
                max_retries=2,
                next_attempt_at=None,
                requested_by="ops2",
                created_at=now,
                last_error="",
                payload_json='{"command":{"intent":"comp"},"approval":{"required":false,"status":"not_required"}}',
            ),
            SimpleNamespace(
                id=3,
                action="command_ingest",
                status="failed",
                retry_count=2,
                max_retries=2,
                next_attempt_at=None,
                requested_by="ops3",
                created_at=now,
                last_error="boom",
                payload_json='{"command":{"intent":"intake"}}',
            ),
        ]
        out = admin._slack_ops_queue_snapshot(rows, now=now)
        self.assertEqual(out["total_count"], 3)
        self.assertEqual(out["blocked_count"], 1)
        self.assertEqual(out["success_count"], 1)
        self.assertEqual(out["failed_count"], 1)
        self.assertEqual(out["pending_approval_count"], 1)
        self.assertGreater(out["pending_approval_avg_hours"], 1.9)
        self.assertGreater(out["pending_approval_max_hours"], 1.9)
        by_id = {row["id"]: row for row in out["rows"]}
        self.assertEqual(by_id[1]["intent"], "operations")
        self.assertEqual(by_id[1]["approval_status"], "pending")
        self.assertEqual(by_id[2]["intent"], "comp")

    def test_apply_required_and_all_env_defaults(self):
        calls = []
        with patch.object(admin, "upsert_env_key", side_effect=lambda p, k, v: calls.append((p, k, v))):
            updated_required = admin._apply_required_env_defaults(
                env_path=".env",
                required_keys={"A", "B", "Z"},
                env_values={"A": "", "B": "ok"},
                recommended_defaults={"A": "1", "B": "2", "C": "3"},
            )
            updated_all = admin._apply_all_env_defaults(
                env_path=".env",
                env_values={"A": "", "B": "ok"},
                recommended_defaults={"A": "1", "B": "2", "C": "3"},
            )
        self.assertEqual(updated_required, 1)
        self.assertEqual(updated_all, 2)
        self.assertTrue(any(c[1] == "A" for c in calls))
        self.assertTrue(any(c[1] == "C" for c in calls))

    def test_apply_runtime_defaults_and_coverage_rows(self):
        upserts = []

        class _Repo:
            def upsert_runtime_setting(self, **kwargs):
                upserts.append(kwargs)

        defaults = [
            {"key": "A", "value": "1", "value_type": "str", "description": "a"},
            {"key": "B", "value": "2", "value_type": "int", "description": "b"},
            {"key": "C", "value": "3", "value_type": "bool", "description": "c"},
        ]
        runtime_rows = [
            SimpleNamespace(key="B", value="9", value_type="int", description="override", is_active=False),
            SimpleNamespace(key="X", value="x", value_type="str", description="custom", is_active=True, updated_by="u", updated_at=datetime(2026, 4, 2, 10, 0, 0)),
        ]
        repo = _Repo()
        req_updated = admin._apply_required_runtime_defaults(
            repo=repo,
            actor="qa",
            required_keys={"A", "B"},
            runtime_rows=runtime_rows,
            seed_defaults=defaults,
        )
        all_updated = admin._apply_all_runtime_defaults(
            repo=repo,
            actor="qa",
            runtime_rows=runtime_rows,
            seed_defaults=defaults,
        )
        self.assertGreaterEqual(req_updated, 2)
        self.assertGreaterEqual(all_updated, 2)
        self.assertTrue(any(u.get("key") == "A" for u in upserts))
        self.assertTrue(any(u.get("key") == "B" for u in upserts))
        rows = admin._build_runtime_coverage_rows(
            runtime_rows=[
                SimpleNamespace(
                    key="A",
                    value="1",
                    value_type="str",
                    description="a",
                    is_active=True,
                    updated_by="qa",
                    updated_at=datetime(2026, 4, 2, 11, 0, 0),
                ),
                SimpleNamespace(
                    key="B",
                    value="9",
                    value_type="int",
                    description="b",
                    is_active=True,
                    updated_by="qa",
                    updated_at=datetime(2026, 4, 2, 11, 0, 0),
                ),
                SimpleNamespace(
                    key="Z",
                    value="custom",
                    value_type="str",
                    description="z",
                    is_active=True,
                    updated_by="qa",
                    updated_at=datetime(2026, 4, 2, 11, 0, 0),
                ),
            ],
            seed_defaults=defaults,
        )
        by_key = {r["key"]: r for r in rows}
        self.assertEqual(by_key["A"]["status"], "default")
        self.assertEqual(by_key["B"]["status"], "overridden")
        self.assertEqual(by_key["C"]["status"], "missing")
        self.assertEqual(by_key["Z"]["status"], "custom_untracked")

    def test_seed_missing_runtime_defaults(self):
        upserts = []

        class _Repo:
            def __init__(self):
                self.existing = {"A": object()}

            def get_runtime_setting(self, environment, key, active_only=False):
                return self.existing.get(key)

            def upsert_runtime_setting(self, **kwargs):
                upserts.append(kwargs)

        defaults = [
            {"key": "A", "value": "1", "value_type": "str", "description": "a"},
            {"key": "B", "value": "2", "value_type": "str", "description": "b"},
        ]
        seeded = admin._seed_missing_runtime_defaults(_Repo(), actor="qa", seed_defaults=defaults)
        self.assertEqual(seeded, 1)
        self.assertEqual(upserts[0]["key"], "B")

    def test_runtime_setting_seed_defaults_shape(self):
        defaults = admin._runtime_setting_seed_defaults()
        self.assertTrue(defaults)
        keys = {row.get("key") for row in defaults}
        self.assertIn("app_build_version", keys)
        self.assertIn("app_build_sha", keys)
        self.assertIn("ebay_finding_rate_limit_probe_interval_seconds", keys)
        self.assertTrue(all("value_type" in row for row in defaults))


if __name__ == "__main__":
    unittest.main()
