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
        self.assertTrue(all("value_type" in row for row in defaults))


if __name__ == "__main__":
    unittest.main()
