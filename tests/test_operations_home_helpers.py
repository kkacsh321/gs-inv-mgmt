import importlib.util
import json
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

    root = Path(__file__).resolve().parents[1]
    for name in ("shared", "workspace_shell", "entity_ops"):
        full = f"app.components.views.{name}"
        if full in sys.modules:
            continue
        path = root / "app" / "components" / "views" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(full, path)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        sys.modules[full] = mod


def _load_module():
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / "operations_home.py"
    spec = importlib.util.spec_from_file_location("test_operations_home_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


ops = _load_module()


class OperationsHomeHelpersTests(unittest.TestCase):
    def test_basic_helpers(self):
        self.assertEqual(ops._status("  Open  "), "open")
        self.assertEqual(ops._format_dt(None), "")
        self.assertTrue(ops._format_dt(datetime(2026, 4, 2, 12, 0)).startswith("2026-04-02"))
        now = datetime(2026, 4, 2, 12, 0)
        self.assertEqual(ops._age_hours(None, now=now), None)
        self.assertAlmostEqual(ops._age_hours(datetime(2026, 4, 2, 10, 0), now=now), 2.0)
        self.assertIn("critical", ops._sla_label(10, warn=2, critical=8))
        self.assertIn("warn", ops._sla_label(3, warn=2, critical=8))
        self.assertIn("ok", ops._sla_label(1, warn=2, critical=8))

    def test_listing_format_hint(self):
        row = SimpleNamespace(marketplace="ebay", marketplace_details="{}", listing_price=0)
        hint = ops._listing_format_hint(row, default_format_type="FIXED_PRICE", default_auction_duration="DAYS_7")
        self.assertIn("Missing BIN", hint)

        auction_meta = {
            "ebay_publish": {
                "format": "AUCTION",
                "listing_duration": "BAD",
                "auction_start_price": 0,
                "auction_reserve_price": 1,
                "auction_buy_now_price": 1,
            }
        }
        row2 = SimpleNamespace(marketplace="ebay", marketplace_details=json.dumps(auction_meta), listing_price=10)
        hint2 = ops._listing_format_hint(row2, default_format_type="FIXED_PRICE", default_auction_duration="DAYS_7")
        self.assertIn("Auction Missing Start", hint2)

        row3 = SimpleNamespace(marketplace="local", marketplace_details="", listing_price=10)
        self.assertEqual(ops._listing_format_hint(row3, default_format_type="FIXED_PRICE", default_auction_duration="DAYS_7"), "")

    def test_action_rows_for_role(self):
        self.assertGreaterEqual(len(ops._action_rows_for_role("admin")), 5)
        self.assertGreaterEqual(len(ops._action_rows_for_role("ops")), 5)
        self.assertGreaterEqual(len(ops._action_rows_for_role("viewer")), 3)

    def test_photo_comp_created_listing_ids(self):
        rows = [
            SimpleNamespace(entity_type="navigation", action="photo_comp_product_draft_created", changes_json=json.dumps({"draft_listing_ids": [1, "2", "bad"]})),
            SimpleNamespace(entity_type="navigation", action="other", changes_json="{}"),
        ]
        repo = SimpleNamespace(list_audit_logs=lambda limit=5000: rows)
        result = ops._photo_comp_created_listing_ids(repo)
        self.assertEqual(result, {1, 2})

    def test_followup_rows(self):
        now = datetime(2026, 4, 2, 12, 0, 0)
        listings_payload = {
            "workflow": "listings_readiness:blocker",
            "task_key": "L1",
            "title": "[Listings/Readiness] Missing Media",
            "owner": "ops",
            "priority": "high",
            "due_date": "2026-04-03",
            "status": "open",
            "blocker_reason": "missing image",
        }
        gov_payload = {
            "workflow": "governance_snapshot_cadence",
            "task_key": "G1",
            "title": "Governance Snapshot",
            "owner": "admin",
            "priority": "medium",
            "due_date": "2026-04-05",
            "status": "open",
            "note": "weekly",
        }
        rows = [
            SimpleNamespace(entity_type="workspace_followup", action="create", changes_json=json.dumps(listings_payload), changed_at=now, changed_by="ops"),
            SimpleNamespace(entity_type="workspace_followup", action="create", changes_json=json.dumps(gov_payload), changed_at=now, changed_by="admin"),
        ]
        repo = SimpleNamespace(list_audit_logs=lambda limit=2000: rows)
        with patch.object(ops, "utc_today", return_value=datetime(2026, 4, 2).date()):
            list_rows = ops._listings_blocker_followup_rows(repo)
            gov_rows = ops._governance_cadence_followup_rows(repo)
        self.assertEqual(len(list_rows), 1)
        self.assertEqual(list_rows[0]["task_key"], "L1")
        self.assertEqual(len(gov_rows), 1)
        self.assertEqual(gov_rows[0]["task_key"], "G1")


if __name__ == "__main__":
    unittest.main()
