import json
import importlib.util
import sys
import types
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
from unittest.mock import patch


def _load_ai_accountant_module():
    inserted_pkg = False
    inserted_shared = False
    if "app.components.views" not in sys.modules:
        pkg = types.ModuleType("app.components.views")
        pkg.__path__ = []
        sys.modules["app.components.views"] = pkg
        inserted_pkg = True
    shared_name = "app.components.views.shared"
    if shared_name not in sys.modules:
        shared_module = types.ModuleType(shared_name)
        shared_module.render_help_panel = lambda *args, **kwargs: None
        sys.modules[shared_name] = shared_module
        inserted_shared = True
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / "ai_accountant.py"
    spec = importlib.util.spec_from_file_location("test_ai_accountant_module", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    if inserted_shared:
        sys.modules.pop(shared_name, None)
    if inserted_pkg:
        sys.modules.pop("app.components.views", None)
    return module


ai_accountant = _load_ai_accountant_module()

from app.services import ai_accountant_monitor


class AIAccountantHelperTests(unittest.TestCase):
    def test_monitor_rows_include_exception_actions_and_dashboard_profit_basis(self):
        rows = ai_accountant.build_ai_accountant_monitor_rows(
            [
                {
                    "severity": "P0",
                    "exception_type": "missing_cost_basis",
                    "entity_type": "sale",
                    "entity_id": 10,
                    "sku": "SKU-1",
                    "reference": "ORDER-1",
                    "amount": 0,
                    "details": "No cost basis.",
                    "occurred_at": "2026-04-01T00:00:00",
                },
                {
                    "severity": "P2",
                    "exception_type": "lot_equal_fallback_review_needed",
                    "entity_type": "purchase_lot",
                    "entity_id": 5,
                    "reference": "LOT-1",
                    "details": "Equal fallback.",
                },
                {
                    "severity": "P1",
                    "exception_type": "active_bundle_listing_stock_shortage",
                    "entity_type": "listing",
                    "entity_id": 22,
                    "reference": "EBAY-BUNDLE-SHORT",
                    "details": "Active bundle listing exceeds component stock.",
                },
                {
                    "severity": "P1",
                    "exception_type": "active_bundle_component_overcommitted",
                    "entity_type": "product",
                    "entity_id": 23,
                    "sku": "SKU-BUNDLE",
                    "details": "Active bundle listings collectively overcommit component stock.",
                },
            ],
            dashboard_metrics={
                "sales_30d_profit_basis_status": "review_needed",
                "sales_30d_cogs_review_count": 2,
                "sales_30d_profit_before_returns": 67.5,
                "sales_30d_est_profit": -12.5,
                "sales_30d_bundle_sale_count": 1,
                "sales_30d_bundle_inventory_units_sold": 10,
                "returns_30d_count": 2,
                "returns_30d_refund_total": 125.0,
                "returns_30d_cogs_reversal": 45.0,
                "returns_30d_profit_impact": -80.0,
            },
        )

        self.assertEqual([row["severity"] for row in rows[:2]], ["P0", "P1"])
        by_type = {row["task_type"]: row for row in rows}
        self.assertIn("missing_cost_basis", by_type)
        self.assertIn("dashboard_profit_basis_review", by_type)
        self.assertIn("Add product landed cost", by_type["missing_cost_basis"]["recommended_action"])
        self.assertIn("Reduce/end", by_type["active_bundle_listing_stock_shortage"]["recommended_action"])
        self.assertIn("restock", by_type["active_bundle_listing_stock_shortage"]["recommended_action"])
        self.assertIn("overlapping active bundle listings", by_type["active_bundle_component_overcommitted"]["recommended_action"])
        self.assertIn("expected lot quantity", by_type["dashboard_profit_basis_review"]["recommended_action"])
        self.assertIn("Bundle accounting detected 1 sale", by_type["dashboard_profit_basis_review"]["details"])
        self.assertIn("Profit before returns $67.50", by_type["dashboard_profit_basis_review"]["details"])
        self.assertIn(
            "estimated profit after returns $-12.50",
            by_type["dashboard_profit_basis_review"]["details"],
        )
        self.assertIn("component quantities", by_type["dashboard_profit_basis_review"]["recommended_action"])
        self.assertIn("dashboard_return_profit_impact_review", by_type)
        self.assertEqual(by_type["dashboard_return_profit_impact_review"]["severity"], "P2")
        self.assertAlmostEqual(by_type["dashboard_return_profit_impact_review"]["amount"], -80.0)
        self.assertIn("refund total $125.00", by_type["dashboard_return_profit_impact_review"]["details"])
        self.assertIn("returned listing/inventory units", by_type["dashboard_return_profit_impact_review"]["recommended_action"])

    def test_build_ai_accountant_message_summarizes_severity_mix(self):
        message = ai_accountant.build_ai_accountant_message(
            [
                {"severity": "P0", "task_type": "missing_cost_basis", "entity_type": "sale", "entity_id": 1},
                {"severity": "P1", "task_type": "missing_fee_evidence", "entity_type": "sale", "entity_id": 2},
                {"severity": "P2", "task_type": "fee_source_fallback", "entity_type": "sale", "entity_id": 3},
            ],
            period_label="2026-04-01 to 2026-04-30",
        )

        self.assertIn("AI Accountant monitor for 2026-04-01 to 2026-04-30", message)
        self.assertIn("P0=1, P1=1, P2=1", message)
        self.assertIn("missing_cost_basis", message)
        self.assertIn("Question status: unanswered=3.", message)
        self.assertIn("Questions to answer in Ask or Slack", message)
        self.assertIn("accountant answer missing_cost_basis sale#1", message)

    def test_build_ai_accountant_message_marks_recently_answered_questions(self):
        message = ai_accountant_monitor.build_ai_accountant_message(
            [
                {
                    "severity": "P0",
                    "task_type": "missing_cost_basis",
                    "entity_type": "sale",
                    "entity_id": 1,
                }
            ],
            period_label="2026-05-01 to 2026-05-31",
            answer_rows=[
                {
                    "task_type": "missing_cost_basis",
                    "reference": "sale#1",
                    "actor": "ops1",
                    "answer_preview": "Use lot 8 landed cost.",
                }
            ],
        )

        self.assertNotIn("Questions to answer in Ask or Slack", message)
        self.assertIn("Question status: answered=1.", message)
        self.assertIn("Recently answered AI Accountant questions", message)
        self.assertIn("Use lot 8 landed cost", message)

    def test_build_ai_accountant_message_reasks_when_answer_needs_more_info(self):
        message = ai_accountant_monitor.build_ai_accountant_message(
            [
                {
                    "severity": "P0",
                    "task_type": "missing_cost_basis",
                    "entity_type": "sale",
                    "entity_id": 1,
                }
            ],
            period_label="2026-05-01 to 2026-05-31",
            answer_rows=[
                {
                    "task_type": "missing_cost_basis",
                    "reference": "sale#1",
                    "actor": "ops1",
                    "answer_preview": "Maybe lot 8.",
                    "followup_status": "needs_more_info",
                }
            ],
        )

        self.assertIn("Questions to answer in Ask or Slack", message)
        self.assertIn("Question status: needs_more_info=1.", message)
        self.assertNotIn("Recently answered AI Accountant questions", message)

    def test_build_ai_accountant_question_rows_turns_monitor_items_into_operator_questions(self):
        rows = ai_accountant_monitor.build_ai_accountant_question_rows(
            [
                {
                    "severity": "P0",
                    "task_type": "missing_product_link",
                    "entity_type": "sale",
                    "entity_id": 3,
                    "reference": "ORDER-3",
                },
                {
                    "severity": "P1",
                    "task_type": "lot_equal_fallback_review_needed",
                    "entity_type": "purchase_lot",
                    "entity_id": 39,
                    "reference": "LOT-39",
                },
            ]
        )

        self.assertEqual(rows[0]["question"], "Which product should sale#3 be linked to?")
        self.assertIn("SKU/product ID", rows[0]["suggested_answer_format"])
        self.assertEqual(rows[0]["reply_prompt"], "accountant answer missing_product_link sale#3: ")
        self.assertIn("How should the lot cost be allocated", rows[1]["question"])

    def test_build_ai_accountant_question_rows_handles_answer_followup(self):
        rows = ai_accountant_monitor.build_ai_accountant_question_rows(
            [
                {
                    "severity": "P1",
                    "task_type": "ai_accountant_answer_followup",
                    "entity_type": "sale",
                    "entity_id": 3,
                    "reference": "sale#3",
                    "recommended_action": "Collect additional evidence and record a replacement answer.",
                }
            ]
        )

        self.assertEqual(len(rows), 1)
        self.assertIn("replacement answer or evidence", rows[0]["question"])
        self.assertIn("prior AI Accountant answer", rows[0]["why_needed"])
        self.assertEqual(rows[0]["reply_prompt"], "accountant answer ai_accountant_answer_followup sale#3: ")

    def test_parse_ai_accountant_answer_prompt_extracts_target_and_answer(self):
        parsed = ai_accountant_monitor.parse_ai_accountant_answer_prompt(
            "accountant answer missing_cost_basis sale#3: Use lot 39 assignment at $12.50 landed."
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["task_type"], "missing_cost_basis")
        self.assertEqual(parsed["reference"], "sale#3")
        self.assertEqual(parsed["entity_type"], "sale")
        self.assertEqual(parsed["entity_id"], 3)
        self.assertIn("$12.50", parsed["answer_text"])
        self.assertEqual(len(parsed["answer_hash_sha256"]), 64)

    def test_record_ai_accountant_answer_persists_read_only_audit_event(self):
        class Repo:
            def __init__(self):
                self.calls = []

            def record_audit_event(self, **kwargs):
                self.calls.append(kwargs)

        repo = Repo()
        parsed = ai_accountant_monitor.record_ai_accountant_answer(
            repo,
            actor="ops1",
            prompt="accountant answer lot_underallocated purchase_lot#39: Expected total quantity is 25.",
            source="slack",
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(len(repo.calls), 1)
        self.assertEqual(repo.calls[0]["entity_type"], "ai_accountant_answer")
        self.assertEqual(repo.calls[0]["entity_id"], 39)
        self.assertEqual(repo.calls[0]["changes"]["source"], "slack")
        self.assertTrue(repo.calls[0]["changes"]["read_only"])

    def test_record_ai_accountant_answer_followup_persists_status(self):
        class Repo:
            def __init__(self):
                self.calls = []

            def record_audit_event(self, **kwargs):
                self.calls.append(kwargs)

        repo = Repo()
        payload = ai_accountant_monitor.record_ai_accountant_answer_followup(
            repo,
            actor="ops1",
            answer_hash_sha256="a" * 64,
            outcome="applied",
            notes="Updated the lot assignment cost.",
        )

        self.assertEqual(payload["outcome"], "applied")
        self.assertTrue(payload["correction_applied_through_normal_workflow"])
        self.assertEqual(len(repo.calls), 1)
        self.assertEqual(repo.calls[0]["entity_type"], "ai_accountant_answer_followup")
        self.assertEqual(repo.calls[0]["changes"]["answer_hash_sha256"], "a" * 64)

    def test_ai_accountant_answer_followup_row_parses_audit_payload(self):
        row = SimpleNamespace(
            created_at="2026-05-18T12:00:00",
            actor="ops1",
            changes={
                "answer_hash_sha256": "a" * 64,
                "outcome": "needs_more_info",
                "notes": "Need receipt evidence.",
            },
        )

        parsed = ai_accountant_monitor.ai_accountant_answer_followup_row(row)

        self.assertEqual(parsed["outcome"], "needs_more_info")
        self.assertEqual(parsed["answer_hash_sha256"], "a" * 64)
        self.assertEqual(parsed["notes"], "Need receipt evidence.")

    def test_annotate_ai_accountant_question_rows_marks_latest_answer(self):
        questions = ai_accountant_monitor.build_ai_accountant_question_rows(
            [
                {
                    "severity": "P0",
                    "task_type": "missing_cost_basis",
                    "entity_type": "sale",
                    "entity_id": 3,
                }
            ]
        )
        annotated = ai_accountant_monitor.annotate_ai_accountant_question_rows(
            questions,
            [
                {
                    "task_type": "missing_cost_basis",
                    "reference": "sale#3",
                    "actor": "ops1",
                    "recorded_at": "2026-05-18T12:00:00",
                    "answer_preview": "Use lot 39 landed cost.",
                    "answer_hash_sha256": "a" * 64,
                }
            ],
        )

        self.assertEqual(annotated[0]["answer_status"], "answered")
        self.assertEqual(annotated[0]["latest_answer_actor"], "ops1")
        self.assertIn("lot 39", annotated[0]["latest_answer_preview"])

    def test_annotate_ai_accountant_question_rows_keeps_needs_more_info_open(self):
        questions = ai_accountant_monitor.build_ai_accountant_question_rows(
            [
                {
                    "severity": "P0",
                    "task_type": "missing_cost_basis",
                    "entity_type": "sale",
                    "entity_id": 3,
                }
            ]
        )
        annotated = ai_accountant_monitor.annotate_ai_accountant_question_rows(
            questions,
            [
                {
                    "task_type": "missing_cost_basis",
                    "reference": "sale#3",
                    "actor": "ops1",
                    "answer_preview": "Maybe lot 39.",
                    "answer_hash_sha256": "a" * 64,
                    "followup_status": "needs_more_info",
                }
            ],
        )

        self.assertEqual(annotated[0]["answer_status"], "needs_more_info")
        self.assertEqual(annotated[0]["latest_answer_followup_status"], "needs_more_info")

    def test_audit_row_to_message_includes_monitor_threshold_metadata(self):
        row = SimpleNamespace(
            created_at="2026-05-08T12:00:00",
            actor="runner",
            changes={
                "message": "Monitor run",
                "period": "2026-04-08 to 2026-05-08",
                "item_count": 2,
                "min_severity": "P1",
                "requested_min_severity": "URGENT",
                "min_severity_fallback_applied": True,
                "slack_outbox_id": 99,
                "automated_review": {
                    "enabled": True,
                    "answer_hash_sha256": "a" * 64,
                    "prompt_hash_sha256": "p" * 64,
                    "data_scope_hash_sha256": "d" * 64,
                    "compact_retry": True,
                    "monitor_rows": 5,
                    "exception_rows": 4,
                    "sale_fifo_cogs_evidence_rows": 3,
                    "monitor_rows_omitted": 7,
                    "exception_rows_omitted": 6,
                    "sale_fifo_cogs_evidence_rows_omitted": 2,
                    "runtime_chain_brief": "localai/Qwen (chat, db, ready)",
                    "error": "",
                },
            },
        )

        parsed = ai_accountant._audit_row_to_message(row)

        self.assertEqual(parsed["min_severity"], "P1")
        self.assertEqual(parsed["requested_min_severity"], "URGENT")
        self.assertTrue(parsed["min_severity_fallback_applied"])
        self.assertEqual(parsed["slack_outbox_id"], 99)
        self.assertTrue(parsed["review_enabled"])
        self.assertEqual(parsed["review_status"], "completed")
        self.assertTrue(parsed["review_compact_retry"])
        self.assertEqual(parsed["review_monitor_rows"], 5)
        self.assertEqual(parsed["review_exception_rows"], 4)
        self.assertEqual(parsed["review_fifo_evidence_rows"], 3)
        self.assertEqual(parsed["review_rows_omitted"], 15)
        self.assertEqual(parsed["review_hash"], "a" * 12)
        self.assertEqual(parsed["review_prompt_hash"], "p" * 12)
        self.assertEqual(parsed["review_data_scope_hash"], "d" * 12)
        self.assertEqual(parsed["review_error"], "")
        self.assertIn("localai/Qwen", parsed["review_runtime_route"])

    def test_audit_row_to_message_surfaces_automated_review_error(self):
        row = SimpleNamespace(
            created_at="2026-05-08T12:00:00",
            actor="runner",
            changes={
                "message": "Monitor run",
                "period": "2026-04-08 to 2026-05-08",
                "automated_review": {
                    "enabled": True,
                    "answer_hash_sha256": "",
                    "compact_retry": True,
                    "error": "All AI runtime fallback attempts failed.",
                },
            },
        )

        parsed = ai_accountant._audit_row_to_message(row)

        self.assertTrue(parsed["review_enabled"])
        self.assertEqual(parsed["review_status"], "unavailable")
        self.assertTrue(parsed["review_compact_retry"])
        self.assertIn("All AI runtime", parsed["review_error"])

    def test_summarize_message_thresholds_flags_severity_fallback(self):
        summary = ai_accountant.summarize_ai_accountant_message_thresholds(
            [
                {
                    "created_at": "2026-05-08T12:00:00",
                    "requested_min_severity": "urgent",
                    "min_severity": "P1",
                    "min_severity_fallback_applied": True,
                },
                {
                    "created_at": "2026-05-08T11:00:00",
                    "requested_min_severity": "P0",
                    "min_severity": "P0",
                    "min_severity_fallback_applied": False,
                },
            ]
        )

        self.assertEqual(summary["fallback_count"], 1)
        self.assertEqual(summary["latest_requested_min_severity"], "URGENT")
        self.assertEqual(summary["latest_effective_min_severity"], "P1")
        self.assertIn("ai_accountant_monitor_min_severity", summary["warning"])

    def test_summarize_message_thresholds_is_quiet_without_fallback(self):
        summary = ai_accountant.summarize_ai_accountant_message_thresholds(
            [{"requested_min_severity": "P1", "min_severity": "P1"}]
        )

        self.assertEqual(summary["fallback_count"], 0)
        self.assertEqual(summary["warning"], "")

    def test_monitor_rows_include_ai_review_followup_for_rejected_latest_outcome(self):
        rows = ai_accountant_monitor.build_ai_accountant_monitor_rows(
            [],
            review_outcome_rows=[
                {
                    "outcome": "accepted",
                    "actor": "admin",
                    "recorded_at": "2026-05-05T12:00:00",
                    "answer_hash_sha256": "a" * 64,
                },
                {
                    "outcome": "rejected",
                    "actor": "ops",
                    "recorded_at": "2026-05-06T12:00:00",
                    "answer_hash_sha256": "b" * 64,
                },
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_type"], "ai_accountant_review_followup")
        self.assertEqual(rows[0]["severity"], "P0")
        self.assertEqual(rows[0]["source"], "ai_accountant_review_outcomes")
        self.assertIn("record an accepted outcome", rows[0]["recommended_action"])

    def test_service_monitor_rows_include_dashboard_return_profit_impact(self):
        rows = ai_accountant_monitor.build_ai_accountant_monitor_rows(
            [],
            dashboard_metrics={
                "returns_30d_count": 1,
                "returns_30d_refund_total": 100.0,
                "returns_30d_cogs_reversal": 25.0,
                "returns_30d_profit_impact": -75.0,
            },
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["task_type"], "dashboard_return_profit_impact_review")
        self.assertEqual(row["severity"], "P2")
        self.assertEqual(row["source"], "dashboard_live_metrics")
        self.assertAlmostEqual(row["amount"], -75.0)
        self.assertIn("COGS reversal $25.00", row["details"])
        self.assertIn("restock status", row["recommended_action"])

    def test_monitor_rows_ignore_ai_review_followup_when_latest_is_accepted(self):
        rows = ai_accountant_monitor.build_ai_accountant_monitor_rows(
            [],
            review_outcome_rows=[
                {
                    "outcome": "edited",
                    "actor": "ops",
                    "recorded_at": "2026-05-05T12:00:00",
                    "answer_hash_sha256": "a" * 64,
                },
                {
                    "outcome": "accepted",
                    "actor": "admin",
                    "recorded_at": "2026-05-06T12:00:00",
                    "answer_hash_sha256": "b" * 64,
                },
            ],
        )

        self.assertEqual(rows, [])

    def test_monitor_rows_include_answer_followup_when_more_info_needed(self):
        rows = ai_accountant_monitor.build_ai_accountant_monitor_rows(
            [],
            answer_rows=[
                {
                    "task_type": "missing_cost_basis",
                    "reference": "sale#3",
                    "entity_type": "sale",
                    "entity_id": 3,
                    "actor": "ops1",
                    "answer_hash_sha256": "a" * 64,
                    "followup_status": "needs_more_info",
                    "followup_at": "2026-05-18T12:00:00",
                }
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_type"], "ai_accountant_answer_followup")
        self.assertEqual(rows[0]["severity"], "P1")
        self.assertEqual(rows[0]["source"], "ai_accountant_answers")
        self.assertIn("replacement answer", rows[0]["recommended_action"])

    def test_monitor_rows_ignore_applied_answer_followup(self):
        rows = ai_accountant_monitor.build_ai_accountant_monitor_rows(
            [],
            answer_rows=[
                {
                    "task_type": "missing_cost_basis",
                    "reference": "sale#3",
                    "followup_status": "applied",
                }
            ],
        )

        self.assertEqual(rows, [])

    def test_monitor_rows_ignore_answer_followup_with_replacement_answer(self):
        rows = ai_accountant_monitor.build_ai_accountant_monitor_rows(
            [],
            answer_rows=[
                {
                    "task_type": "missing_cost_basis",
                    "reference": "sale#3",
                    "followup_status": "needs_more_info",
                    "answer_hash_sha256": "a" * 64,
                },
                {
                    "task_type": "ai_accountant_answer_followup",
                    "reference": "sale#3",
                    "followup_status": "unreviewed",
                    "answer_hash_sha256": "b" * 64,
                },
            ],
        )

        self.assertEqual(rows, [])

    def test_action_summary_groups_by_task_and_prioritizes_severity(self):
        rows = [
            {
                "severity": "P2",
                "task_type": "missing_fee_evidence",
                "entity_type": "sale",
                "entity_id": 1,
                "reference": "ORDER-1",
                "recommended_action": "Import/link marketplace fee evidence.",
            },
            {
                "severity": "P0",
                "task_type": "missing_cost_basis",
                "entity_type": "sale",
                "entity_id": 2,
                "sku": "SKU-2",
                "recommended_action": "Add product landed cost.",
            },
            {
                "severity": "P1",
                "task_type": "missing_cost_basis",
                "entity_type": "sale",
                "entity_id": 3,
                "sku": "SKU-3",
                "recommended_action": "Add product landed cost.",
            },
        ]

        summary = ai_accountant.build_ai_accountant_action_summary(rows)

        self.assertEqual(len(summary), 2)
        self.assertEqual(summary[0]["task_type"], "missing_cost_basis")
        self.assertEqual(summary[0]["item_count"], 2)
        self.assertEqual(summary[0]["P0"], 1)
        self.assertEqual(summary[0]["P1"], 1)
        self.assertIn("sale 2", summary[0]["sample_reference"])
        self.assertEqual(summary[1]["task_type"], "missing_fee_evidence")

    def test_packet_review_action_rows_flag_unverified_packet(self):
        self.assertEqual(ai_accountant.build_ai_accountant_packet_review_action_rows({}), [])

        rows = ai_accountant.build_ai_accountant_packet_review_action_rows(
            {
                "packet_needs_review": True,
                "packet_status": "review_needed",
                "packet_manifest_status": "review_needed",
                "packet_integrity_error_count": 2,
                "answer_hash_sha256": "a" * 64,
            }
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["severity"], "P0")
        self.assertEqual(rows[0]["task_type"], "evidence_packet_integrity_review")
        self.assertIn("errors=2", rows[0]["reference"])
        self.assertEqual(rows[0]["entity_id"], "a" * 12)
        packet = ai_accountant.build_ai_accountant_evidence_zip(
            period_label="2026-05-01 to 2026-05-31",
            action_summary=ai_accountant.build_ai_accountant_action_summary(rows),
            monitor_rows=[],
            exception_rows=[],
            messages=[],
            review_outcomes=[],
            answer_rows=[],
            answer_followup_rows=[],
            dashboard_metrics={},
            sale_fifo_cogs_evidence_rows=[],
        )
        packet_summary = ai_accountant.read_ai_accountant_evidence_zip_summary(
            packet,
            period_label="2026-05-01 to 2026-05-31",
        )
        self.assertEqual(packet_summary["action_summary_task_counts"]["evidence_packet_integrity_review"], 1)

    def test_summarize_monitor_run_result_survives_rerun(self):
        empty = ai_accountant.summarize_monitor_run_result(None)
        self.assertFalse(empty["has_result"])
        self.assertEqual(empty["severity"], "info")

        ok = ai_accountant.summarize_monitor_run_result(
            {
                "item_count": 3,
                "actionable_count": 2,
                "audit_id": 10,
                "slack_outbox_id": 11,
                "review_enabled": True,
                "review_hash": "a" * 64,
            }
        )
        self.assertTrue(ok["has_result"])
        self.assertEqual(ok["severity"], "success")
        self.assertIn("items=3", ok["details"])
        self.assertIn("review_hash=aaaaaaaaaaaa", ok["details"])

        warning = ai_accountant.summarize_monitor_run_result(
            {
                "item_count": 1,
                "actionable_count": 1,
                "audit_id": 12,
                "review_enabled": True,
                "review_error": "llm down",
            }
        )
        self.assertEqual(warning["severity"], "warning")
        self.assertIn("llm down", warning["status"])

    def test_build_ai_accountant_runtime_summary_resolves_monitor_settings(self):
        values = {
            "ai_accountant_monitor_enabled": True,
            "ai_accountant_monitor_schedule_mode": "interval",
            "ai_accountant_monitor_interval_hours": 4,
            "ai_accountant_monitor_lookback_days": 45,
            "ai_accountant_monitor_timezone": "America/Denver",
            "ai_accountant_monitor_local_time": "09:15",
            "ai_accountant_monitor_slack_enabled": True,
            "ai_accountant_monitor_channel": "#accounting",
            "notification_route_ai_accountant_monitor": "both",
            "ai_accountant_monitor_llm_review_enabled": True,
            "ai_accountant_monitor_min_severity": "P0",
            "ai_accountant_chat_ai_enabled": True,
            "ai_accountant_web_research_enabled": True,
            "ai_accountant_web_research_limit": 7,
            "ai_accountant_web_research_timeout_seconds": 12,
            "slack_notifications_enabled": True,
            "slack_bot_token": "xoxb-test",
            "slack_default_channel": "#ops",
        }

        class Repo:
            def get_runtime_setting(self, *, environment, key, active_only=True):
                value = values.get(key)
                if value is None:
                    return None
                if isinstance(value, bool):
                    return SimpleNamespace(value="true" if value else "false", value_type="bool")
                if isinstance(value, int):
                    return SimpleNamespace(value=str(value), value_type="int")
                return SimpleNamespace(value=str(value), value_type="str")

        rows = ai_accountant.build_ai_accountant_runtime_summary(Repo())
        by_setting = {row["setting"]: row for row in rows}

        self.assertEqual(by_setting["Scheduled Monitor"]["status"], "enabled")
        self.assertEqual(by_setting["Scheduled Monitor"]["value"], "every 4h")
        self.assertEqual(by_setting["Scheduled Monitor"]["schedule_mode"], "interval")
        self.assertEqual(by_setting["Scheduled Monitor"]["configured_interval_hours"], 4)
        self.assertEqual(by_setting["Scheduled Monitor"]["configured_lookback_days"], 45)
        self.assertEqual(by_setting["Scheduled Monitor"]["configured_timezone"], "America/Denver")
        self.assertEqual(by_setting["Scheduled Monitor"]["configured_local_time"], "09:15")
        self.assertEqual(by_setting["Slack Alerts"]["value"], "#accounting")
        self.assertEqual(by_setting["Slack Alerts"]["configured_route"], "both")
        self.assertEqual(by_setting["Slack Delivery"]["status"], "enabled")
        self.assertTrue(by_setting["Slack Delivery"]["configured_token_present"])
        self.assertTrue(by_setting["Slack Delivery"]["configured_default_channel_present"])
        self.assertTrue(by_setting["Slack Delivery"]["configured_monitor_channel_present"])
        self.assertEqual(by_setting["Automated LLM Review"]["status"], "enabled")
        self.assertEqual(by_setting["Automated LLM Review"]["value"], "P0")
        self.assertEqual(by_setting["Automated LLM Review"]["configured_min_severity"], "P0")
        self.assertEqual(by_setting["External Web Research"]["value"], "limit 7; timeout 12s")
        self.assertEqual(by_setting["External Web Research"]["configured_limit"], 7)
        self.assertEqual(by_setting["External Web Research"]["configured_timeout_seconds"], 12)

    def test_build_ai_accountant_runtime_summary_defaults_automation_on(self):
        class Repo:
            def get_runtime_setting(self, *, environment, key, active_only=True):
                return None

        rows = ai_accountant.build_ai_accountant_runtime_summary(Repo())
        by_setting = {row["setting"]: row for row in rows}

        self.assertEqual(by_setting["Scheduled Monitor"]["status"], "enabled")
        self.assertEqual(by_setting["Slack Alerts"]["status"], "enabled")
        self.assertEqual(by_setting["Automated LLM Review"]["status"], "enabled")
        self.assertEqual(by_setting["Interactive Chat"]["status"], "enabled")
        self.assertEqual(by_setting["External Web Research"]["status"], "enabled")
        self.assertEqual(by_setting["Slack Alerts"]["configured_route"], "slack")
        self.assertEqual(by_setting["Slack Delivery"]["status"], "enabled")
        self.assertFalse(by_setting["Slack Delivery"]["configured_token_present"])
        self.assertFalse(by_setting["Slack Delivery"]["configured_default_channel_present"])

    def test_build_ai_accountant_runtime_chain_rows_sanitizes_config(self):
        class Repo:
            def get_runtime_setting(self, *, environment, key, active_only=True):
                if key == "ai_workflow_profile_accounting":
                    return SimpleNamespace(value="Accounting Profile", value_type="str")
                return None

        cfg = SimpleNamespace(
            source="db",
            enabled=True,
            provider="localai",
            model="Qwen",
            endpoint_type="chat",
            base_url="https://localai.example/v1",
            api_key="secret",
            max_output_tokens=16000,
            timeout_seconds=60,
        )

        with patch.object(ai_accountant, "describe_llm_runtime_chain") as describe_mock:
            describe_mock.return_value = [
                {
                    "order": 1,
                    "workflow": "accounting",
                    "status": "ready",
                    "source": cfg.source,
                    "provider": cfg.provider,
                    "model": cfg.model,
                    "endpoint_type": cfg.endpoint_type,
                    "base_url": cfg.base_url,
                    "enabled": cfg.enabled,
                    "api_key": "present",
                    "max_output_tokens": cfg.max_output_tokens,
                    "timeout_seconds": cfg.timeout_seconds,
                    "profile_selector": "Accounting Profile",
                    "error": "",
                }
            ]
            rows = ai_accountant.build_ai_accountant_runtime_chain_rows(Repo())

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "ready")
        self.assertEqual(rows[0]["provider"], "localai")
        self.assertEqual(rows[0]["api_key"], "present")
        self.assertNotIn("secret", str(rows[0]))
        self.assertEqual(rows[0]["profile_selector"], "Accounting Profile")

    def test_run_ai_accountant_runtime_smoke_test_reports_success_and_failure(self):
        success_result = SimpleNamespace(
            text='{"status":"ok"}',
            citation={
                "provider": "localai",
                "text_model": "Qwen",
                "endpoint_type": "chat",
                "source": "db",
                "fallback_attempts": 1,
                "fallback_errors": ["first failed"],
            },
        )

        class Repo:
            pass

        with patch.object(ai_accountant, "execute_comp_summary", return_value=success_result) as execute_mock:
            ok = ai_accountant.run_ai_accountant_runtime_smoke_test(Repo())

        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["provider"], "localai")
        self.assertEqual(ok["fallback_attempts"], 1)
        self.assertIn("first failed", ok["fallback_errors"])
        self.assertEqual(execute_mock.call_args.kwargs["workflow"], "accounting")

        class DB:
            def __init__(self):
                self.rollback_count = 0

            def rollback(self):
                self.rollback_count += 1

        class FailingRepo:
            def __init__(self):
                self.db = DB()

        failing_repo = FailingRepo()
        with patch.object(ai_accountant, "execute_comp_summary", side_effect=RuntimeError("llm down")):
            failed = ai_accountant.run_ai_accountant_runtime_smoke_test(failing_repo)

        self.assertEqual(failed["status"], "failed")
        self.assertIn("llm down", failed["error"])
        self.assertEqual(failing_repo.db.rollback_count, 1)

    def test_build_ai_accountant_setup_checks_flags_disabled_automation(self):
        checks = ai_accountant.build_ai_accountant_setup_checks(
            [
                {"setting": "Scheduled Monitor", "status": "disabled"},
                {"setting": "Slack Alerts", "status": "disabled"},
                {"setting": "Automated LLM Review", "status": "disabled"},
                {"setting": "Interactive Chat", "status": "enabled"},
                {"setting": "External Web Research", "status": "disabled"},
            ]
        )
        by_check = {row["check"]: row for row in checks}

        self.assertEqual(by_check["scheduled_monitor_enabled"]["status"], "warn")
        self.assertEqual(by_check["slack_alerts_enabled"]["status"], "warn")
        self.assertEqual(by_check["automated_llm_review_enabled"]["status"], "warn")
        self.assertEqual(by_check["interactive_chat_enabled"]["status"], "pass")
        self.assertEqual(by_check["external_web_research_review"]["status"], "warn")

        ready = ai_accountant.build_ai_accountant_setup_checks(
            [
                {"setting": "Scheduled Monitor", "status": "enabled"},
                {"setting": "Slack Alerts", "status": "enabled"},
                {
                    "setting": "Slack Delivery",
                    "status": "enabled",
                    "configured_token_present": True,
                    "configured_default_channel_present": True,
                },
                {"setting": "Automated LLM Review", "status": "enabled"},
                {"setting": "Interactive Chat", "status": "enabled"},
                {"setting": "External Web Research", "status": "enabled"},
            ]
        )
        self.assertTrue(all(row["status"] == "pass" for row in ready))

        missing_token = ai_accountant.build_ai_accountant_setup_checks(
            [
                {"setting": "Scheduled Monitor", "status": "enabled"},
                {"setting": "Slack Alerts", "status": "enabled"},
                {
                    "setting": "Slack Delivery",
                    "status": "enabled",
                    "configured_token_present": False,
                    "configured_default_channel_present": True,
                },
                {"setting": "Automated LLM Review", "status": "enabled"},
                {"setting": "Interactive Chat", "status": "enabled"},
                {"setting": "External Web Research", "status": "enabled"},
            ]
        )
        missing_token_by_check = {row["check"]: row for row in missing_token}
        self.assertEqual(missing_token_by_check["slack_delivery_configured"]["status"], "warn")
        self.assertIn("slack_bot_token", missing_token_by_check["slack_delivery_configured"]["details"])

        missing_channel = ai_accountant.build_ai_accountant_setup_checks(
            [
                {"setting": "Scheduled Monitor", "status": "enabled"},
                {"setting": "Slack Alerts", "status": "enabled"},
                {
                    "setting": "Slack Delivery",
                    "status": "enabled",
                    "configured_token_present": True,
                    "configured_default_channel_present": False,
                    "configured_monitor_channel_present": False,
                },
                {"setting": "Automated LLM Review", "status": "enabled"},
                {"setting": "Interactive Chat", "status": "enabled"},
                {"setting": "External Web Research", "status": "enabled"},
            ]
        )
        missing_channel_by_check = {row["check"]: row for row in missing_channel}
        self.assertEqual(missing_channel_by_check["slack_delivery_configured"]["status"], "warn")
        self.assertIn("slack_default_channel", missing_channel_by_check["slack_delivery_configured"]["details"])

        bad_limit = ai_accountant.build_ai_accountant_setup_checks(
            [
                {"setting": "Scheduled Monitor", "status": "enabled"},
                {"setting": "Slack Alerts", "status": "enabled"},
                {"setting": "Automated LLM Review", "status": "enabled"},
                {"setting": "Interactive Chat", "status": "enabled"},
                {
                    "setting": "External Web Research",
                    "status": "enabled",
                    "configured_limit": 0,
                    "configured_timeout_seconds": 10,
                },
            ]
        )
        bad_limit_by_check = {row["check"]: row for row in bad_limit}
        self.assertEqual(bad_limit_by_check["external_web_research_review"]["status"], "warn")
        self.assertIn("ai_accountant_web_research_limit", bad_limit_by_check["external_web_research_review"]["details"])

        disabled_route = ai_accountant.build_ai_accountant_setup_checks(
            [
                {"setting": "Scheduled Monitor", "status": "enabled"},
                {"setting": "Slack Alerts", "status": "enabled", "configured_route": "disabled"},
                {"setting": "Automated LLM Review", "status": "enabled"},
                {"setting": "Interactive Chat", "status": "enabled"},
                {"setting": "External Web Research", "status": "enabled"},
            ]
        )
        disabled_route_by_check = {row["check"]: row for row in disabled_route}
        self.assertEqual(disabled_route_by_check["slack_alerts_enabled"]["status"], "warn")
        self.assertIn(
            "notification_route_ai_accountant_monitor",
            disabled_route_by_check["slack_alerts_enabled"]["details"],
        )

        bad_timeout = ai_accountant.build_ai_accountant_setup_checks(
            [
                {"setting": "Scheduled Monitor", "status": "enabled"},
                {"setting": "Slack Alerts", "status": "enabled"},
                {"setting": "Automated LLM Review", "status": "enabled"},
                {"setting": "Interactive Chat", "status": "enabled"},
                {
                    "setting": "External Web Research",
                    "status": "enabled",
                    "configured_limit": 5,
                    "configured_timeout_seconds": 1,
                },
            ]
        )
        bad_timeout_by_check = {row["check"]: row for row in bad_timeout}
        self.assertEqual(bad_timeout_by_check["external_web_research_review"]["status"], "warn")
        self.assertIn(
            "ai_accountant_web_research_timeout_seconds",
            bad_timeout_by_check["external_web_research_review"]["details"],
        )

        bad_interval = ai_accountant.build_ai_accountant_setup_checks(
            [
                {
                    "setting": "Scheduled Monitor",
                    "status": "enabled",
                    "schedule_mode": "interval",
                    "configured_interval_hours": 0,
                },
                {"setting": "Slack Alerts", "status": "enabled"},
                {"setting": "Automated LLM Review", "status": "enabled"},
                {"setting": "Interactive Chat", "status": "enabled"},
                {"setting": "External Web Research", "status": "enabled"},
            ]
        )
        bad_interval_by_check = {row["check"]: row for row in bad_interval}
        self.assertEqual(bad_interval_by_check["scheduled_monitor_enabled"]["status"], "warn")
        self.assertIn(
            "ai_accountant_monitor_interval_hours",
            bad_interval_by_check["scheduled_monitor_enabled"]["details"],
        )

        bad_mode = ai_accountant.build_ai_accountant_setup_checks(
            [
                {
                    "setting": "Scheduled Monitor",
                    "status": "enabled",
                    "schedule_mode": "weekly",
                    "configured_interval_hours": 6,
                },
                {"setting": "Slack Alerts", "status": "enabled"},
                {"setting": "Automated LLM Review", "status": "enabled"},
                {"setting": "Interactive Chat", "status": "enabled"},
                {"setting": "External Web Research", "status": "enabled"},
            ]
        )
        bad_mode_by_check = {row["check"]: row for row in bad_mode}
        self.assertEqual(bad_mode_by_check["scheduled_monitor_enabled"]["status"], "warn")
        self.assertIn(
            "ai_accountant_monitor_schedule_mode",
            bad_mode_by_check["scheduled_monitor_enabled"]["details"],
        )

        daily_mode = ai_accountant.build_ai_accountant_setup_checks(
            [
                {
                    "setting": "Scheduled Monitor",
                    "status": "enabled",
                    "schedule_mode": "daily",
                    "configured_interval_hours": 0,
                },
                {"setting": "Slack Alerts", "status": "enabled"},
                {"setting": "Automated LLM Review", "status": "enabled"},
                {"setting": "Interactive Chat", "status": "enabled"},
                {"setting": "External Web Research", "status": "enabled"},
            ]
        )
        daily_mode_by_check = {row["check"]: row for row in daily_mode}
        self.assertEqual(daily_mode_by_check["scheduled_monitor_enabled"]["status"], "pass")

        bad_min_severity = ai_accountant.build_ai_accountant_setup_checks(
            [
                {"setting": "Scheduled Monitor", "status": "enabled"},
                {"setting": "Slack Alerts", "status": "enabled"},
                {
                    "setting": "Automated LLM Review",
                    "status": "enabled",
                    "configured_min_severity": "urgent",
                },
                {"setting": "Interactive Chat", "status": "enabled"},
                {"setting": "External Web Research", "status": "enabled"},
            ]
        )
        bad_min_severity_by_check = {row["check"]: row for row in bad_min_severity}
        self.assertEqual(bad_min_severity_by_check["automated_llm_review_enabled"]["status"], "warn")
        self.assertIn(
            "ai_accountant_monitor_min_severity",
            bad_min_severity_by_check["automated_llm_review_enabled"]["details"],
        )

        bad_lookback = ai_accountant.build_ai_accountant_setup_checks(
            [
                {
                    "setting": "Scheduled Monitor",
                    "status": "enabled",
                    "schedule_mode": "interval",
                    "configured_interval_hours": 6,
                    "configured_lookback_days": 0,
                },
                {"setting": "Slack Alerts", "status": "enabled"},
                {"setting": "Automated LLM Review", "status": "enabled"},
                {"setting": "Interactive Chat", "status": "enabled"},
                {"setting": "External Web Research", "status": "enabled"},
            ]
        )
        bad_lookback_by_check = {row["check"]: row for row in bad_lookback}
        self.assertEqual(bad_lookback_by_check["scheduled_monitor_enabled"]["status"], "warn")
        self.assertIn(
            "ai_accountant_monitor_lookback_days",
            bad_lookback_by_check["scheduled_monitor_enabled"]["details"],
        )

        bad_daily_time = ai_accountant.build_ai_accountant_setup_checks(
            [
                {
                    "setting": "Scheduled Monitor",
                    "status": "enabled",
                    "schedule_mode": "daily",
                    "configured_interval_hours": 0,
                    "configured_lookback_days": 30,
                    "configured_local_time": "25:99",
                },
                {"setting": "Slack Alerts", "status": "enabled"},
                {"setting": "Automated LLM Review", "status": "enabled"},
                {"setting": "Interactive Chat", "status": "enabled"},
                {"setting": "External Web Research", "status": "enabled"},
            ]
        )
        bad_daily_time_by_check = {row["check"]: row for row in bad_daily_time}
        self.assertEqual(bad_daily_time_by_check["scheduled_monitor_enabled"]["status"], "warn")
        self.assertIn(
            "ai_accountant_monitor_local_time",
            bad_daily_time_by_check["scheduled_monitor_enabled"]["details"],
        )

        bad_timezone = ai_accountant.build_ai_accountant_setup_checks(
            [
                {
                    "setting": "Scheduled Monitor",
                    "status": "enabled",
                    "schedule_mode": "daily",
                    "configured_interval_hours": 0,
                    "configured_lookback_days": 30,
                    "configured_timezone": "Mars/Base",
                    "configured_local_time": "08:30",
                },
                {"setting": "Slack Alerts", "status": "enabled"},
                {"setting": "Automated LLM Review", "status": "enabled"},
                {"setting": "Interactive Chat", "status": "enabled"},
                {"setting": "External Web Research", "status": "enabled"},
            ]
        )
        bad_timezone_by_check = {row["check"]: row for row in bad_timezone}
        self.assertEqual(bad_timezone_by_check["scheduled_monitor_enabled"]["status"], "warn")
        self.assertIn(
            "ai_accountant_monitor_timezone",
            bad_timezone_by_check["scheduled_monitor_enabled"]["details"],
        )

    def test_apply_ai_accountant_recommended_runtime_settings_upserts_core_defaults(self):
        calls = []

        class Repo:
            def upsert_runtime_setting(self, **kwargs):
                calls.append(kwargs)

        applied = ai_accountant.apply_ai_accountant_recommended_runtime_settings(
            Repo(),
            actor="admin",
        )
        by_key = {row["key"]: row for row in calls}

        self.assertEqual(len(applied), len(calls))
        self.assertEqual(by_key["ai_accountant_monitor_enabled"]["value"], "true")
        self.assertEqual(by_key["ai_accountant_monitor_enabled"]["value_type"], "bool")
        self.assertEqual(by_key["ai_accountant_monitor_schedule_mode"]["value"], "interval")
        self.assertEqual(by_key["ai_accountant_monitor_interval_hours"]["value"], "6")
        self.assertEqual(by_key["ai_accountant_monitor_timezone"]["value"], "America/Denver")
        self.assertEqual(by_key["ai_accountant_monitor_local_time"]["value"], "08:30")
        self.assertEqual(by_key["ai_accountant_monitor_lookback_days"]["value"], "30")
        self.assertEqual(by_key["ai_accountant_monitor_lookback_days"]["value_type"], "int")
        self.assertEqual(by_key["ai_accountant_monitor_min_severity"]["value"], "P1")
        self.assertEqual(by_key["ai_accountant_monitor_slack_enabled"]["value"], "true")
        self.assertEqual(by_key["ai_accountant_monitor_llm_review_enabled"]["value"], "true")
        self.assertEqual(by_key["ai_accountant_monitor_review_max_rows"]["value"], "25")
        self.assertEqual(by_key["ai_accountant_monitor_review_max_rows"]["value_type"], "int")
        self.assertEqual(by_key["ai_accountant_monitor_review_max_exception_rows"]["value"], "25")
        self.assertEqual(by_key["ai_accountant_monitor_review_max_exception_rows"]["value_type"], "int")
        self.assertEqual(by_key["ai_accountant_chat_ai_enabled"]["value"], "true")
        self.assertEqual(by_key["ai_accountant_web_research_enabled"]["value"], "true")
        self.assertEqual(by_key["ai_accountant_web_research_enabled"]["value_type"], "bool")
        self.assertEqual(by_key["ai_accountant_web_research_limit"]["value"], "5")
        self.assertEqual(by_key["ai_accountant_web_research_limit"]["value_type"], "int")
        self.assertEqual(by_key["ai_accountant_web_research_timeout_seconds"]["value"], "10")
        self.assertEqual(by_key["ai_accountant_web_research_timeout_seconds"]["value_type"], "int")
        self.assertTrue(all(row["actor"] == "admin" for row in calls))
        self.assertTrue(all(row["is_active"] for row in calls))

    def test_evidence_zip_contains_expected_artifacts_and_manifest(self):
        summary = ai_accountant.build_ai_accountant_evidence_summary(
            period_label="2026-04-01 to 2026-04-30",
            action_summary=[{"task_type": "missing_cost_basis", "item_count": 1}],
            monitor_rows=[{"severity": "P0", "task_type": "missing_cost_basis"}],
            exception_rows=[{"exception_type": "missing_cost_basis"}],
            messages=[{"actor": "admin", "message": "Review"}],
            review_outcomes=[{"outcome": "accepted", "actor": "admin"}],
            answer_rows=[
                {"task_type": "missing_cost_basis", "reference": "sale#3", "followup_status": "applied"},
                {"task_type": "missing_product_link", "reference": "sale#4", "followup_status": "needs_more_info"},
            ],
            answer_followup_rows=[{"outcome": "applied", "answer_hash_sha256": "a" * 64}],
            review_hash_index=[{"source": "ai_accountant_review_outcomes"}],
            dashboard_metrics={"sales_30d_profit_basis_status": "review_needed"},
            sale_fifo_cogs_evidence_rows=[{"sale_id": 7, "total_cost": 12.5}],
            artifact_hashes={"artifact.csv": "a" * 64},
        )
        self.assertEqual(summary["row_counts"]["sale_fifo_cogs_evidence"], 1)
        self.assertEqual(summary["row_counts"]["accounting_exception_rows"], 1)
        self.assertEqual(summary["row_counts"]["answers"], 2)
        self.assertEqual(summary["row_counts"]["answer_followups"], 1)
        self.assertEqual(summary["row_counts"]["review_hash_index"], 1)
        self.assertEqual(summary["answer_followup_status_counts"], {"applied": 1, "needs_more_info": 1})
        self.assertEqual(summary["action_summary_task_counts"]["missing_cost_basis"], 1)
        self.assertEqual(summary["dashboard_profit_basis_status"], "review_needed")
        self.assertEqual(summary["packet_schema_version"], "ai_accountant_evidence_packet_v1")
        self.assertEqual(summary["artifact_count"], 1)
        self.assertEqual(summary["artifact_names"], ["artifact.csv"])
        self.assertEqual(len(summary["evidence_hash_sha256"]), 64)
        hash_index = ai_accountant.build_ai_accountant_review_hash_index(
            messages=[
                {
                    "created_at": "2026-04-30T12:00:00",
                    "actor": "runner",
                    "review_status": "completed",
                    "review_hash": "a" * 12,
                    "review_prompt_hash": "p" * 12,
                    "review_data_scope_hash": "d" * 12,
                    "evidence_packet_integrity_status": "verified",
                    "evidence_packet_integrity_error_count": 0,
                    "evidence_packet_manifest_status": "verified",
                    "evidence_packet_manifest_rows": 9,
                    "evidence_packet_manifest_expected_rows": 9,
                    "evidence_packet_action_summary_task_counts": '{"missing_cost_basis": 1}',
                }
            ],
            review_outcomes=[
                {
                    "recorded_at": "2026-04-30T12:05:00",
                    "actor": "admin",
                    "outcome": "accepted",
                    "answer_hash_sha256": "a" * 64,
                    "evidence_packet_integrity_status": "review_needed",
                    "evidence_packet_integrity_error_count": 2,
                    "evidence_packet_manifest_status": "review_needed",
                    "evidence_packet_manifest_rows": 8,
                    "evidence_packet_manifest_expected_rows": 9,
                    "evidence_packet_action_summary_task_counts": '{"evidence_packet_integrity_review": 1}',
                }
            ],
        )
        self.assertEqual([row["source"] for row in hash_index], ["ai_accountant_messages", "ai_accountant_review_outcomes"])
        self.assertEqual(hash_index[0]["prompt_hash_sha256"], "p" * 12)
        self.assertEqual(hash_index[0]["evidence_packet_integrity_status"], "verified")
        self.assertEqual(hash_index[0]["evidence_packet_integrity_error_count"], 0)
        self.assertEqual(hash_index[0]["evidence_packet_manifest_status"], "verified")
        self.assertIn("missing_cost_basis", hash_index[0]["evidence_packet_action_summary_task_counts"])
        self.assertEqual(hash_index[1]["evidence_packet_integrity_status"], "review_needed")
        self.assertEqual(hash_index[1]["evidence_packet_integrity_error_count"], 2)
        self.assertEqual(hash_index[1]["evidence_packet_manifest_rows"], 8)
        self.assertEqual(hash_index[1]["evidence_packet_manifest_expected_rows"], 9)
        self.assertIn(
            "evidence_packet_integrity_review",
            hash_index[1]["evidence_packet_action_summary_task_counts"],
        )

        packet = ai_accountant.build_ai_accountant_evidence_zip(
            period_label="2026-04-01 to 2026-04-30",
            action_summary=[{"task_type": "missing_cost_basis", "item_count": 1}],
            monitor_rows=[{"severity": "P0", "task_type": "missing_cost_basis"}],
            exception_rows=[{"exception_type": "missing_cost_basis", "entity_type": "sale"}],
            messages=[{"actor": "admin", "message": "Review"}],
            review_outcomes=[{"outcome": "accepted", "actor": "admin"}],
            answer_rows=[
                {
                    "task_type": "missing_cost_basis",
                    "reference": "sale#3",
                    "actor": "ops1",
                    "answer_preview": "Use lot 39 landed cost.",
                    "followup_status": "applied",
                }
            ],
            answer_followup_rows=[
                {
                    "answer_hash_sha256": "a" * 64,
                    "outcome": "applied",
                    "actor": "ops1",
                    "notes": "Updated assignment cost.",
                }
            ],
            dashboard_metrics={"sales_30d_profit_basis_status": "review_needed"},
            sale_fifo_cogs_evidence_rows=[
                {
                    "sale_id": 7,
                    "lot_id": 3,
                    "assignment_id": 2,
                    "quantity": 1,
                    "unit_cost": 12.5,
                    "total_cost": 12.5,
                    "cost_source": "assignment_unit_landed_cost",
                }
            ],
        )

        with zipfile.ZipFile(BytesIO(packet), "r") as archive:
            names = set(archive.namelist())
            prefix = "2026-04-01_to_2026-04-30"
            self.assertIn(f"{prefix}/ai_accountant_action_summary.csv", names)
            self.assertIn(f"{prefix}/ai_accountant_monitor_rows.csv", names)
            self.assertIn(f"{prefix}/accounting_exception_queue.csv", names)
            self.assertIn(f"{prefix}/ai_accountant_messages.csv", names)
            self.assertIn(f"{prefix}/ai_accountant_review_outcomes.csv", names)
            self.assertIn(f"{prefix}/ai_accountant_answers.csv", names)
            self.assertIn(f"{prefix}/ai_accountant_answer_followups.csv", names)
            self.assertIn(f"{prefix}/ai_accountant_review_hash_index.csv", names)
            self.assertIn(f"{prefix}/sale_fifo_cogs_evidence.csv", names)
            self.assertIn(f"{prefix}/dashboard_profit_basis.json", names)
            self.assertIn(f"{prefix}/evidence_summary.json", names)
            self.assertIn(f"{prefix}/manifest.csv", names)
            manifest = archive.read(f"{prefix}/manifest.csv").decode("utf-8")
            self.assertIn("sha256", manifest)
            self.assertIn("ai_accountant_monitor_rows.csv", manifest)
            self.assertIn("accounting_exception_queue.csv", manifest)
            self.assertIn("ai_accountant_answers.csv", manifest)
            self.assertIn("ai_accountant_answer_followups.csv", manifest)
            self.assertIn("ai_accountant_review_hash_index.csv", manifest)
            self.assertIn("sale_fifo_cogs_evidence.csv", manifest)
            self.assertIn("evidence_summary.json", manifest)
            fifo_evidence = archive.read(f"{prefix}/sale_fifo_cogs_evidence.csv").decode("utf-8")
            self.assertIn("assignment_unit_landed_cost", fifo_evidence)
            evidence_summary = json.loads(archive.read(f"{prefix}/evidence_summary.json"))
            self.assertEqual(evidence_summary["row_counts"]["sale_fifo_cogs_evidence"], 1)
            self.assertEqual(evidence_summary["row_counts"]["accounting_exception_rows"], 1)
            self.assertEqual(evidence_summary["row_counts"]["answers"], 1)
            self.assertEqual(evidence_summary["row_counts"]["answer_followups"], 1)
            self.assertEqual(evidence_summary["row_counts"]["review_hash_index"], 2)
            self.assertEqual(evidence_summary["answer_followup_status_counts"], {"applied": 1})
            self.assertEqual(evidence_summary["action_summary_task_counts"]["missing_cost_basis"], 1)
            self.assertEqual(evidence_summary["dashboard_profit_basis_status"], "review_needed")
            self.assertEqual(evidence_summary["packet_schema_version"], "ai_accountant_evidence_packet_v1")
            self.assertEqual(evidence_summary["artifact_count"], 10)
            self.assertEqual(len(evidence_summary["evidence_hash_sha256"]), 64)
            self.assertIn(
                f"{prefix}/accounting_exception_queue.csv",
                evidence_summary["artifact_names"],
            )
            self.assertIn(
                f"{prefix}/ai_accountant_review_hash_index.csv",
                evidence_summary["artifact_names"],
            )
            self.assertIn(
                f"{prefix}/ai_accountant_answers.csv",
                evidence_summary["artifact_names"],
            )
            self.assertIn(
                f"{prefix}/ai_accountant_answer_followups.csv",
                evidence_summary["artifact_names"],
            )
            self.assertIn(
                f"{prefix}/sale_fifo_cogs_evidence.csv",
                evidence_summary["artifact_hashes_sha256"],
            )
            parsed_summary = ai_accountant.read_ai_accountant_evidence_zip_summary(
                packet,
                period_label="2026-04-01 to 2026-04-30",
            )
            self.assertEqual(parsed_summary["evidence_hash_sha256"], evidence_summary["evidence_hash_sha256"])
            self.assertEqual(parsed_summary["packet_integrity_status"], "verified")
            self.assertEqual(parsed_summary["packet_integrity_errors"], [])
            self.assertEqual(parsed_summary["packet_integrity_error_count"], 0)
            self.assertEqual(parsed_summary["packet_verified_artifact_count"], 10)
            self.assertEqual(parsed_summary["packet_zip_artifact_count"], 10)
            self.assertEqual(parsed_summary["packet_manifest_status"], "verified")
            self.assertEqual(parsed_summary["packet_manifest_row_count"], 11)
            self.assertEqual(parsed_summary["packet_manifest_expected_row_count"], 11)
            dashboard_payload = json.loads(archive.read(f"{prefix}/dashboard_profit_basis.json"))
            self.assertEqual(dashboard_payload["sales_30d_profit_basis_status"], "review_needed")

        tampered_buffer = BytesIO()
        with zipfile.ZipFile(BytesIO(packet), "r") as source:
            with zipfile.ZipFile(tampered_buffer, "w", compression=zipfile.ZIP_DEFLATED) as target:
                for name in source.namelist():
                    content = source.read(name)
                    if name.endswith("/sale_fifo_cogs_evidence.csv"):
                        content = b"tampered\n"
                    target.writestr(name, content)
        tampered_summary = ai_accountant.read_ai_accountant_evidence_zip_summary(
            tampered_buffer.getvalue(),
            period_label="2026-04-01 to 2026-04-30",
        )
        self.assertEqual(tampered_summary["packet_integrity_status"], "review_needed")
        self.assertGreater(tampered_summary["packet_integrity_error_count"], 0)
        self.assertTrue(
            any("sale_fifo_cogs_evidence.csv" in error for error in tampered_summary["packet_integrity_errors"])
        )
        self.assertEqual(tampered_summary["packet_manifest_status"], "review_needed")
        self.assertTrue(
            any("manifest_hash_mismatch" in error for error in tampered_summary["packet_integrity_errors"])
        )

    def test_deterministic_review_fallback_returns_structured_json(self):
        raw = ai_accountant.build_deterministic_ai_accountant_review(
            monitor_rows=[
                {
                    "severity": "P1",
                    "task_type": "dashboard_profit_basis_review",
                    "recommended_action": "Review sold COGS source mix.",
                }
            ],
            action_summary=[
                {
                    "task_type": "dashboard_profit_basis_review",
                    "item_count": 1,
                    "P0": 0,
                    "P1": 1,
                    "P2": 0,
                    "recommended_action": "Review sold COGS source mix.",
                }
            ],
            dashboard_metrics={
                "sales_30d_profit_basis_status": "review_needed",
                "sales_30d_cogs_review_count": 1,
                "sales_30d_est_profit": -9.0,
                "sales_30d_net_after_returns": 16.0,
                "returns_30d_count": 1,
                "returns_30d_refund_total": 100.0,
                "returns_30d_cogs_reversal": 25.0,
                "returns_30d_profit_impact": -75.0,
            },
            llm_error="All AI runtime fallback attempts failed.",
        )
        payload = json.loads(raw)

        self.assertIn("close_status", payload)
        self.assertIn("deterministic evidence fallback", payload["close_status"])
        self.assertIn("recommended_human_actions", payload)
        self.assertIn("Review sold COGS source mix.", payload["recommended_human_actions"])
        self.assertIn("unsupported_tax_or_legal_items", payload)
        joined_notes = "\n".join(payload["profit_basis_notes"])
        self.assertIn("estimated profit after returns: $-9.00", joined_notes)
        self.assertIn("profit impact $-75.00", joined_notes)

    def test_execute_ai_accountant_workspace_review_uses_accounting_workflow_and_limits(self):
        calls = []

        class Repo:
            pass

        def fake_execute(*_args, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text='{"close_status":"ok"}', citation={"provider": "test"})

        with patch.object(ai_accountant, "get_runtime_int", return_value=7), patch.object(
            ai_accountant,
            "build_sale_fifo_cogs_evidence_rows",
            return_value=[{"sale_id": 1, "total_cost": 12.5, "cost_source": "assignment_unit_landed_cost"}],
        ) as mock_fifo, patch.object(ai_accountant, "execute_comp_summary", side_effect=fake_execute):
            result = ai_accountant.execute_ai_accountant_workspace_review(
                Repo(),
                prompt="Review",
                system_message="System",
                instruction="Instruction",
                period_label="2026-05",
                start_date="2026-05-01",
                end_date="2026-05-09",
                monitor_rows=[{"severity": "P1", "idx": idx} for idx in range(20)],
                exception_rows=[{"exception_type": "missing_cost_basis", "idx": idx} for idx in range(20)],
                dashboard_metrics={},
            )

        self.assertEqual(result["text"], '{"close_status":"ok"}')
        self.assertFalse(result["compact_retry"])
        self.assertEqual(calls[0]["workflow"], "accounting")
        self.assertLessEqual(len(calls[0]["spot_context"]["monitor_rows"]), 7)
        self.assertLessEqual(len(calls[0]["spot_context"]["accounting_exception_rows"]), 7)
        self.assertEqual(len(calls[0]["spot_context"]["sale_fifo_cogs_evidence_rows"]), 1)
        self.assertEqual(calls[0]["spot_context"]["sale_fifo_cogs_evidence_summary"]["row_count"], 1)
        self.assertTrue(mock_fifo.called)

    def test_execute_ai_accountant_workspace_review_retries_compact_context(self):
        calls = []

        class DB:
            def __init__(self):
                self.rollback_count = 0

            def rollback(self):
                self.rollback_count += 1

        db = DB()

        class Repo:
            def __init__(self):
                self.db = db

        def fake_execute(*_args, **kwargs):
            calls.append(kwargs)
            if len(kwargs["spot_context"]["monitor_rows"]) > 5:
                raise RuntimeError("payload too large")
            return SimpleNamespace(text='{"close_status":"compact ok"}', citation={"provider": "test"})

        with patch.object(ai_accountant, "get_runtime_int", return_value=25), patch.object(
            ai_accountant,
            "build_sale_fifo_cogs_evidence_rows",
            return_value=[{"sale_id": idx, "total_cost": 1.0} for idx in range(12)],
        ), patch.object(ai_accountant, "execute_comp_summary", side_effect=fake_execute):
            result = ai_accountant.execute_ai_accountant_workspace_review(
                Repo(),
                prompt="Review",
                system_message="System",
                instruction="Instruction",
                period_label="2026-05",
                start_date="2026-05-01",
                end_date="2026-05-09",
                monitor_rows=[{"severity": "P1", "idx": idx} for idx in range(20)],
                exception_rows=[{"exception_type": "missing_cost_basis", "idx": idx} for idx in range(20)],
                dashboard_metrics={},
            )

        self.assertEqual(len(calls), 2)
        self.assertTrue(result["compact_retry"])
        self.assertIn("default_context", result["error"])
        self.assertEqual(len(calls[1]["spot_context"]["monitor_rows"]), 5)
        self.assertEqual(len(calls[1]["spot_context"]["sale_fifo_cogs_evidence_rows"]), 5)
        self.assertGreater(db.rollback_count, 0)

    def test_ai_accountant_page_imports_scheduled_monitor_runner(self):
        self.assertIs(ai_accountant.run_ai_accountant_monitor, ai_accountant_monitor.run_ai_accountant_monitor)

    def test_enqueue_ai_accountant_slack_message_uses_notification_outbox(self):
        calls = []

        class Repo:
            def enqueue_notification_outbox(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(id=42)

        outbox = ai_accountant.enqueue_ai_accountant_slack_message(
            Repo(),
            actor="admin",
            message="Review accounting items",
            period_label="2026-04",
            channel="#accounting",
        )

        self.assertEqual(outbox.id, 42)
        self.assertEqual(calls[0]["channel"], "slack")
        self.assertEqual(calls[0]["event_type"], "ai_accountant_monitor")
        self.assertEqual(calls[0]["requested_by"], "admin")
        payload = json.loads(calls[0]["payload_json"])
        self.assertEqual(payload["text"], "Review accounting items")
        self.assertEqual(payload["channel"], "#accounting")
        self.assertTrue(str(calls[0]["dedupe_key"]).startswith("ai_accountant_monitor:"))

    def test_build_ai_accountant_outbox_delivery_rows_filters_and_parses_monitor_rows(self):
        now = datetime(2026, 5, 9, 12, 0, 0)

        class Repo:
            def list_notification_outbox(self, **kwargs):
                self.kwargs = kwargs
                return [
                    SimpleNamespace(
                        id=1,
                        event_type="daily_report",
                        status="sent",
                        attempt_count=1,
                        max_attempts=6,
                        next_attempt_at=now,
                        last_attempt_at=now,
                        dispatched_at=now,
                        payload_json='{"text":"ignore"}',
                        last_error="",
                    ),
                    SimpleNamespace(
                        id=2,
                        event_type="ai_accountant_monitor",
                        status="retrying",
                        attempt_count=2,
                        max_attempts=6,
                        next_attempt_at=now,
                        last_attempt_at=now,
                        dispatched_at=None,
                        payload_json='{"text":"Review missing cost basis", "channel":"#accounting"}',
                        last_error="Slack bot token is not configured (`slack_bot_token`).",
                    ),
                ]

        repo = Repo()
        rows = ai_accountant.build_ai_accountant_outbox_delivery_rows(repo, limit=5)

        self.assertEqual(repo.kwargs["channel"], "slack")
        self.assertEqual(rows, [
            {
                "id": 2,
                "status": "retrying",
                "attempt_count": 2,
                "max_attempts": 6,
                "next_attempt_at": now,
                "last_attempt_at": now,
                "dispatched_at": None,
                "target_channel": "#accounting",
                "last_error": "Slack bot token is not configured (`slack_bot_token`).",
                "text_preview": "Review missing cost basis",
            }
        ])

    def test_build_ai_accountant_outbox_delivery_rows_uses_default_channel_label(self):
        class Repo:
            def list_notification_outbox(self, **kwargs):
                return [
                    SimpleNamespace(
                        id=3,
                        event_type="ai_accountant_monitor",
                        status="queued",
                        attempt_count=0,
                        max_attempts=6,
                        next_attempt_at=None,
                        last_attempt_at=None,
                        dispatched_at=None,
                        payload_json="{bad json",
                        last_error="",
                    ),
                ]

        rows = ai_accountant.build_ai_accountant_outbox_delivery_rows(Repo())

        self.assertEqual(rows[0]["target_channel"], "(default)")
        self.assertEqual(rows[0]["text_preview"], "")

    def test_summarize_ai_accountant_outbox_delivery_rows_counts_status_and_due_rows(self):
        past = datetime(2000, 1, 1, 0, 0, 0)
        rows = [
            {"status": "queued", "next_attempt_at": past},
            {"status": "retrying", "next_attempt_at": "2000-01-01T00:00:00", "last_error": "missing token"},
            {"status": "retrying", "next_attempt_at": "2999-01-01T00:00:00"},
            {"status": "failed", "next_attempt_at": None, "last_error": "terminal"},
            {"status": "sent", "next_attempt_at": None},
        ]

        summary = ai_accountant.summarize_ai_accountant_outbox_delivery_rows(rows)

        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["retrying"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["blocked"], 3)
        self.assertGreaterEqual(summary["due"], 2)
        self.assertEqual(summary["latest_error"], "missing token")

    def test_process_due_ai_accountant_outbox_rows_processes_only_monitor_rows(self):
        processed = []

        class Repo:
            def list_notification_outbox(self, **kwargs):
                self.kwargs = kwargs
                return [
                    SimpleNamespace(id=1, event_type="daily_report"),
                    SimpleNamespace(id=2, event_type="ai_accountant_monitor"),
                    SimpleNamespace(id=3, event_type="ai_accountant_monitor"),
                ]

        def fake_process(_repo, *, outbox_id, actor):
            processed.append((outbox_id, actor))
            return (outbox_id == 2, "Delivered" if outbox_id == 2 else "Slack bot token is not configured.")

        repo = Repo()
        with patch.object(ai_accountant, "process_notification_outbox_row", side_effect=fake_process):
            result = ai_accountant.process_due_ai_accountant_outbox_rows(repo, actor="admin", limit=10)

        self.assertEqual(repo.kwargs["channel"], "slack")
        self.assertEqual(repo.kwargs["statuses"], {"queued", "retrying"})
        self.assertEqual(processed, [(2, "admin"), (3, "admin")])
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertIn("Slack bot token", result["messages"][1])

    def test_record_ai_accountant_message_persists_audit_payload(self):
        calls = []

        class Repo:
            def record_audit_event(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(id=7)

        row = ai_accountant.record_ai_accountant_message(
            Repo(),
            actor="admin",
            message="Clean up P0 items",
            period_label="2026-04",
            rows=[{"severity": "P0"}, {"severity": "P2"}],
            slack_outbox_id=42,
        )

        self.assertEqual(row.id, 7)
        self.assertEqual(calls[0]["entity_type"], "ai_accountant_message")
        self.assertEqual(calls[0]["action"], "create")
        self.assertEqual(calls[0]["changes"]["item_count"], 2)
        self.assertEqual(calls[0]["changes"]["severity_counts"], {"P0": 1, "P1": 0, "P2": 1})
        self.assertEqual(calls[0]["changes"]["slack_outbox_id"], 42)

    def test_review_context_and_metadata_include_hashes_and_guardrails(self):
        context = ai_accountant_monitor.build_ai_accountant_review_context(
            period_label="2026-04-01 to 2026-04-30",
            start_date="2026-04-01",
            end_date="2026-04-30",
            monitor_rows=[
                {"severity": "P0", "task_type": "missing_cost_basis"},
                {"severity": "P2", "task_type": "fee_source_fallback"},
            ],
            exception_rows=[{"exception_type": "missing_cost_basis"}],
            dashboard_metrics={
                "sales_30d_est_profit": -12.5,
                "sales_30d_profit_before_returns": 62.5,
                "sales_30d_profit_basis_status": "review_needed",
                "sales_30d_cogs_review_count": 1,
                "returns_30d_count": 1,
                "returns_30d_refund_total": 100.0,
                "returns_30d_cogs_reversal": 25.0,
                "returns_30d_profit_impact": -75.0,
                "sales_30d_net_after_returns": -9.0,
            },
            sale_fifo_cogs_evidence_rows=[
                {
                    "sale_id": 10,
                    "lot_id": 3,
                    "assignment_id": 8,
                    "quantity": 2,
                    "unit_cost": 12.5,
                    "total_cost": 25.0,
                    "cost_source": "assignment_allocated_landed_cost",
                }
            ],
        )
        metadata = ai_accountant_monitor.build_ai_accountant_review_metadata(
            surface="ai_accountant_workspace",
            prompt="Review",
            system_message="System",
            instruction="Instruction",
            context=context,
            citation={"provider": "test"},
        )

        self.assertEqual(context["monitor_summary"]["severity_counts"], {"P0": 1, "P1": 0, "P2": 1})
        self.assertTrue(context["guardrails"]["read_only"])
        self.assertEqual(context["dashboard_profit_basis"]["sales_30d_profit_basis_status"], "review_needed")
        self.assertEqual(context["dashboard_profit_basis"]["sales_30d_profit_before_returns"], 62.5)
        self.assertEqual(context["dashboard_profit_basis"]["returns_30d_count"], 1)
        self.assertEqual(context["dashboard_profit_basis"]["returns_30d_profit_impact"], -75.0)
        self.assertEqual(context["dashboard_profit_basis"]["sales_30d_net_after_returns"], -9.0)
        self.assertEqual(context["sale_fifo_cogs_evidence_summary"]["row_count"], 1)
        self.assertEqual(context["sale_fifo_cogs_evidence_summary"]["distinct_sale_count"], 1)
        self.assertEqual(context["sale_fifo_cogs_evidence_summary"]["total_cost"], 25.0)
        self.assertEqual(context["sale_fifo_cogs_evidence_rows"][0]["cost_source"], "assignment_allocated_landed_cost")
        self.assertEqual(len(metadata["prompt_hash_sha256"]), 64)
        self.assertEqual(len(metadata["data_scope_hash_sha256"]), 64)
        self.assertEqual(metadata["data_scope"]["row_counts"]["monitor_rows"], 2)
        self.assertEqual(metadata["data_scope"]["row_counts"]["sale_fifo_cogs_evidence_rows"], 1)
        self.assertEqual(metadata["data_scope"]["row_counts"]["monitor_rows_omitted"], 0)
        self.assertEqual(metadata["ai_citation"], {"provider": "test"})

    def test_sale_fifo_cogs_evidence_rows_trace_sales_to_lot_assignments(self):
        class Result:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        class Db:
            def __init__(self):
                self.calls = 0

            def execute(self, _statement):
                self.calls += 1
                if self.calls == 1:
                    return Result(
                        [
                            SimpleNamespace(
                                id=5,
                                acquisition_cost=None,
                                acquisition_tax_paid=None,
                                acquisition_shipping_paid=None,
                                acquisition_handling_paid=None,
                                product_cost=10,
                            )
                        ]
                    )
                return Result(
                    [
                        SimpleNamespace(
                            sale_id=12,
                            sold_at=datetime(2026, 4, 10, 12, 0, 0),
                            marketplace="ebay",
                            external_order_id="ORDER-12",
                            quantity_sold=2,
                            sku="SKU-5",
                            product_title="Product 5",
                        )
                    ]
                )

        class Repo:
            db = Db()

            @staticmethod
            def _product_default_landed_unit_cost(row):
                return float(row.product_cost or 0)

            def report_sale_unit_cost_maps(self, **kwargs):
                self.cost_map_kwargs = kwargs
                return {
                    "fifo_cogs_evidence_by_sale": {
                        12: [
                            {
                                "product_id": 5,
                                "lot_id": 7,
                                "assignment_id": 9,
                                "quantity": 2,
                                "unit_cost": 10.5,
                                "total_cost": 21.0,
                                "cost_source": "assignment_unit_landed_cost",
                            }
                        ]
                    }
                }

        repo = Repo()
        rows = ai_accountant_monitor.build_sale_fifo_cogs_evidence_rows(
            repo,
            start_dt=datetime(2026, 4, 1),
            end_dt=datetime(2026, 4, 30),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sale_id"], 12)
        self.assertEqual(rows[0]["lot_id"], 7)
        self.assertEqual(rows[0]["assignment_id"], 9)
        self.assertEqual(rows[0]["quantity"], 2)
        self.assertEqual(rows[0]["total_cost"], 21.0)
        self.assertEqual(rows[0]["cost_source"], "assignment_unit_landed_cost")
        self.assertEqual(repo.cost_map_kwargs["default_unit_cost_by_product"], {5: 10.0})

    def test_review_context_includes_bundle_dashboard_profit_basis_evidence(self):
        context = ai_accountant_monitor.build_ai_accountant_review_context(
            period_label="2026-04-01 to 2026-04-30",
            start_date="2026-04-01",
            end_date="2026-04-30",
            monitor_rows=[],
            exception_rows=[],
            dashboard_metrics={
                "sales_30d_est_profit": 25.0,
                "sales_30d_profit_before_returns": 25.0,
                "sales_30d_est_cogs": 250.0,
                "sales_30d_profit_basis_status": "ok",
                "sales_30d_cogs_review_count": 0,
                "sales_30d_bundle_sale_count": 1,
                "sales_30d_bundle_inventory_units_sold": 10,
            },
        )

        dashboard = context["dashboard_profit_basis"]
        self.assertEqual(dashboard["sales_30d_bundle_sale_count"], 1)
        self.assertEqual(dashboard["sales_30d_bundle_inventory_units_sold"], 10)

    def test_review_context_limits_rows_and_reports_omitted_counts(self):
        context = ai_accountant_monitor.build_ai_accountant_review_context(
            period_label="2026-04",
            start_date="2026-04-01",
            end_date="2026-04-30",
            monitor_rows=[{"severity": "P1", "idx": idx} for idx in range(8)],
            exception_rows=[{"exception_type": "missing_cost_basis", "idx": idx} for idx in range(7)],
            dashboard_metrics={},
            max_monitor_rows=3,
            max_exception_rows=2,
        )

        self.assertEqual(len(context["monitor_rows"]), 3)
        self.assertEqual(context["monitor_rows_omitted"], 5)
        self.assertEqual(len(context["accounting_exception_rows"]), 2)
        self.assertEqual(context["accounting_exception_rows_omitted"], 5)

    def test_record_ai_accountant_review_outcome_logs_chat_audit(self):
        calls = []

        class Repo:
            def log_ai_chat_interaction(self, **kwargs):
                calls.append(kwargs)

        ai_accountant_monitor.record_ai_accountant_review_outcome(
            Repo(),
            actor="admin",
            outcome="edited",
            answer_text='{"recommended_human_actions":["review"]}',
            review_metadata={"surface": "ai_accountant_workspace"},
        )

        self.assertEqual(calls[0]["intent"], "ai_accountant_review_outcome")
        self.assertEqual(calls[0]["metadata"]["event_type"], "ai_accountant_review_outcome")
        self.assertEqual(calls[0]["metadata"]["outcome"], "edited")
        self.assertEqual(calls[0]["metadata"]["surface"], "ai_accountant_workspace")
        self.assertEqual(len(calls[0]["metadata"]["answer_hash_sha256"]), 64)

    def test_ai_accountant_review_outcome_row_extracts_feedback_audit(self):
        row = SimpleNamespace(
            created_at=datetime(2026, 5, 6, 12, 0, 0),
            actor="admin",
            changes={
                "after": {
                    "intent": "ai_accountant_review_outcome",
                    "answer_preview": '{"recommended_human_actions":["review"]}',
                    "metadata": {
                        "event_type": "ai_accountant_review_outcome",
                        "review_type": "ai_accountant_review",
                        "outcome": "accepted",
                        "surface": "ai_accountant_workspace",
                        "prompt_hash_sha256": "p" * 64,
                        "data_scope_hash_sha256": "d" * 64,
                        "answer_hash_sha256": "a" * 64,
                        "evidence_packet_hash_sha256": "e" * 64,
                        "evidence_packet_row_counts": {
                            "monitor_rows": 4,
                            "accounting_exception_rows": 6,
                            "sale_fifo_cogs_evidence": 5,
                        },
                        "evidence_packet_integrity_status": "verified",
                        "evidence_packet_integrity_errors": [],
                        "evidence_packet_integrity_error_count": 0,
                        "evidence_packet_manifest_status": "verified",
                        "evidence_packet_manifest_row_count": 9,
                        "evidence_packet_manifest_expected_row_count": 9,
                        "evidence_packet_action_summary_task_counts": {"missing_cost_basis": 1},
                        "data_scope": {
                            "row_counts": {
                                "monitor_rows": 3,
                                "accounting_exception_rows": 2,
                                "sale_fifo_cogs_evidence_rows": 1,
                            }
                        },
                    },
                }
            },
        )

        parsed = ai_accountant._ai_accountant_review_outcome_row(row)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["outcome"], "accepted")
        self.assertEqual(parsed["monitor_rows"], 3)
        self.assertEqual(parsed["exception_rows"], 2)
        self.assertEqual(parsed["fifo_evidence_rows"], 1)
        self.assertEqual(parsed["evidence_packet_hash_sha256"], "e" * 64)
        self.assertEqual(parsed["evidence_packet_monitor_rows"], 4)
        self.assertEqual(parsed["evidence_packet_exception_rows"], 6)
        self.assertEqual(parsed["evidence_packet_fifo_rows"], 5)
        self.assertEqual(parsed["evidence_packet_integrity_status"], "verified")
        self.assertEqual(parsed["evidence_packet_integrity_error_count"], 0)
        self.assertEqual(parsed["evidence_packet_manifest_status"], "verified")
        self.assertEqual(parsed["evidence_packet_manifest_rows"], 9)
        self.assertEqual(parsed["evidence_packet_manifest_expected_rows"], 9)
        self.assertIn("missing_cost_basis", parsed["evidence_packet_action_summary_task_counts"])
        self.assertEqual(parsed["answer_hash_sha256"], "a" * 64)

        service_parsed = ai_accountant_monitor.ai_accountant_review_outcome_row(row)
        self.assertIsNotNone(service_parsed)
        self.assertEqual(service_parsed["outcome"], "accepted")
        self.assertEqual(service_parsed["monitor_rows"], 3)
        self.assertEqual(service_parsed["fifo_evidence_rows"], 1)
        self.assertEqual(service_parsed["evidence_packet_hash_sha256"], "e" * 64)
        self.assertEqual(service_parsed["evidence_packet_exception_rows"], 6)
        self.assertEqual(service_parsed["evidence_packet_integrity_status"], "verified")
        self.assertEqual(service_parsed["evidence_packet_integrity_error_count"], 0)
        self.assertEqual(service_parsed["evidence_packet_manifest_status"], "verified")
        self.assertEqual(service_parsed["evidence_packet_manifest_expected_rows"], 9)
        self.assertIn("missing_cost_basis", service_parsed["evidence_packet_action_summary_task_counts"])

    def test_ai_accountant_review_outcome_row_ignores_unrelated_or_malformed_audit(self):
        self.assertIsNone(
            ai_accountant._ai_accountant_review_outcome_row(
                SimpleNamespace(actor="admin", changes={"after": {"metadata": {"event_type": "other"}}})
            )
        )
        self.assertIsNone(
            ai_accountant_monitor.ai_accountant_review_outcome_row(
                SimpleNamespace(actor="admin", changes={"after": {"metadata": {"event_type": "other"}}})
            )
        )
        self.assertIsNone(
            ai_accountant._ai_accountant_review_outcome_row(
                SimpleNamespace(actor="admin", changes_json="not-json")
            )
        )

    def test_summarize_ai_accountant_review_outcomes_flags_latest_followup(self):
        self.assertEqual(
            ai_accountant.summarize_ai_accountant_review_outcomes([])["latest_outcome"],
            "none",
        )

        accepted = ai_accountant.summarize_ai_accountant_review_outcomes(
            [{"outcome": "accepted", "actor": "admin", "recorded_at": "2026-05-06T12:00:00"}]
        )
        self.assertFalse(accepted["needs_followup"])
        self.assertFalse(accepted["packet_needs_review"])
        self.assertIn("accepted", accepted["status"])

        accepted_bad_packet = ai_accountant.summarize_ai_accountant_review_outcomes(
            [
                {
                    "outcome": "accepted",
                    "actor": "admin",
                    "evidence_packet_integrity_status": "review_needed",
                    "evidence_packet_manifest_status": "review_needed",
                    "evidence_packet_integrity_error_count": 2,
                    "evidence_packet_manifest_rows": 8,
                    "evidence_packet_manifest_expected_rows": 9,
                }
            ]
        )
        self.assertFalse(accepted_bad_packet["needs_followup"])
        self.assertTrue(accepted_bad_packet["packet_needs_review"])
        self.assertEqual(accepted_bad_packet["packet_integrity_error_count"], 2)

        edited = ai_accountant.summarize_ai_accountant_review_outcomes(
            [
                {"outcome": "edited", "actor": "ops"},
                {"outcome": "accepted", "actor": "admin"},
            ]
        )
        self.assertTrue(edited["needs_followup"])
        self.assertEqual(edited["latest_outcome"], "edited")

        rejected = ai_accountant.summarize_ai_accountant_review_outcomes(
            [{"outcome": "rejected", "actor": "ops"}]
        )
        self.assertTrue(rejected["needs_followup"])
        self.assertIn("rejected", rejected["status"])

    def test_scheduled_monitor_records_and_queues_actionable_findings(self):
        calls = {"audit": [], "outbox": []}

        class Repo:
            def report_accounting_exception_rows(self, *, start_dt, end_dt):
                self.window = (start_dt, end_dt)
                return [
                    {
                        "severity": "P1",
                        "exception_type": "missing_fee_evidence",
                        "entity_type": "sale",
                        "entity_id": 9,
                    },
                    {
                        "severity": "P2",
                        "exception_type": "fee_source_fallback",
                        "entity_type": "sale",
                        "entity_id": 10,
                    },
                ]

            def dashboard_live_metrics(self, *, now, include_fee_type_breakdown=False):
                return {"sales_30d_profit_basis_status": "ok"}

            def enqueue_notification_outbox(self, **kwargs):
                calls["outbox"].append(kwargs)
                return SimpleNamespace(id=88)

            def record_audit_event(self, **kwargs):
                calls["audit"].append(kwargs)
                return SimpleNamespace(id=77)

        result = ai_accountant_monitor.run_ai_accountant_monitor(
            Repo(),
            actor="runner",
            now=ai_accountant.datetime(2026, 4, 30, 12, 0, 0),
            lookback_days=30,
            min_severity="P1",
            slack_enabled=True,
            slack_channel="#accounting",
        )

        self.assertEqual(result["item_count"], 2)
        self.assertEqual(result["actionable_count"], 1)
        self.assertEqual(result["audit_id"], 77)
        self.assertEqual(result["slack_outbox_id"], 88)
        self.assertEqual(calls["audit"][0]["changes"]["item_count"], 1)
        self.assertEqual(calls["audit"][0]["changes"]["min_severity"], "P1")
        self.assertEqual(calls["audit"][0]["changes"]["requested_min_severity"], "P1")
        self.assertFalse(calls["audit"][0]["changes"]["min_severity_fallback_applied"])
        payload = json.loads(calls["outbox"][0]["payload_json"])
        self.assertEqual(payload["channel"], "#accounting")
        self.assertIn("missing_fee_evidence", payload["text"])

    def test_scheduled_monitor_reports_effective_min_severity_for_invalid_input(self):
        calls = {"audit": []}

        class Repo:
            def report_accounting_exception_rows(self, *, start_dt, end_dt):
                return [
                    {
                        "severity": "P1",
                        "exception_type": "missing_cost_basis",
                        "entity_type": "sale",
                        "entity_id": 1,
                    },
                    {
                        "severity": "P2",
                        "exception_type": "fee_source_fallback",
                        "entity_type": "sale",
                        "entity_id": 2,
                    },
                ]

            def dashboard_live_metrics(self, *, now, include_fee_type_breakdown=False):
                return {"sales_30d_profit_basis_status": "ok"}

            def record_audit_event(self, **kwargs):
                calls["audit"].append(kwargs)
                return SimpleNamespace(id=79)

        result = ai_accountant_monitor.run_ai_accountant_monitor(
            Repo(),
            actor="runner",
            now=ai_accountant.datetime(2026, 5, 8, 12, 0, 0),
            lookback_days=30,
            min_severity="urgent",
        )

        self.assertEqual(result["requested_min_severity"], "URGENT")
        self.assertEqual(result["min_severity"], "P1")
        self.assertEqual(result["item_count"], 2)
        self.assertEqual(result["actionable_count"], 1)
        self.assertEqual(len(calls["audit"]), 1)
        self.assertEqual(calls["audit"][0]["changes"]["min_severity"], "P1")
        self.assertEqual(calls["audit"][0]["changes"]["requested_min_severity"], "URGENT")
        self.assertTrue(calls["audit"][0]["changes"]["min_severity_fallback_applied"])

    def test_scheduled_monitor_records_ai_review_followup_findings(self):
        calls = {"audit": []}
        original_list_outcomes = ai_accountant_monitor.list_ai_accountant_review_outcomes
        ai_accountant_monitor.list_ai_accountant_review_outcomes = lambda repo: [
            {
                "outcome": "edited",
                "actor": "ops",
                "recorded_at": "2026-05-06T12:00:00",
                "answer_hash_sha256": "c" * 64,
            }
        ]

        class Repo:
            def report_accounting_exception_rows(self, *, start_dt, end_dt):
                return []

            def dashboard_live_metrics(self, *, now, include_fee_type_breakdown=False):
                return {"sales_30d_profit_basis_status": "ok"}

            def record_audit_event(self, **kwargs):
                calls["audit"].append(kwargs)
                return SimpleNamespace(id=91)

        try:
            result = ai_accountant_monitor.run_ai_accountant_monitor(
                Repo(),
                actor="runner",
                now=ai_accountant.datetime(2026, 5, 6, 12, 0, 0),
                lookback_days=30,
                min_severity="P1",
            )
        finally:
            ai_accountant_monitor.list_ai_accountant_review_outcomes = original_list_outcomes

        self.assertEqual(result["item_count"], 1)
        self.assertEqual(result["actionable_count"], 1)
        self.assertEqual(calls["audit"][0]["changes"]["sample_items"][0]["task_type"], "ai_accountant_review_followup")

    def test_scheduled_monitor_runs_optional_automated_llm_review(self):
        calls = {"audit": [], "chat": []}

        class Repo:
            def report_accounting_exception_rows(self, *, start_dt, end_dt):
                return [
                    {
                        "severity": "P1",
                        "exception_type": "missing_cost_basis",
                        "entity_type": "sale",
                        "entity_id": 12,
                    }
                ]

            def dashboard_live_metrics(self, *, now, include_fee_type_breakdown=False):
                return {"sales_30d_profit_basis_status": "ok"}

            def record_audit_event(self, **kwargs):
                calls["audit"].append(kwargs)
                return SimpleNamespace(id=92)

            def log_ai_chat_interaction(self, **kwargs):
                calls["chat"].append(kwargs)

        def runtime_bool(_repo, key, default):
            if key == "ai_accountant_monitor_llm_review_enabled":
                return True
            return default

        def runtime_int(_repo, key, default):
            if key == "ai_accountant_monitor_review_max_rows":
                return 9
            if key == "ai_accountant_monitor_review_max_exception_rows":
                return 8
            return default

        fake_result = SimpleNamespace(
            text="Watch status: review missing cost basis before close.",
            citation={"provider": "test"},
        )
        with patch("app.services.ai_accountant_monitor.get_runtime_bool", side_effect=runtime_bool), patch(
            "app.services.ai_accountant_monitor.get_runtime_int", side_effect=runtime_int
        ), patch(
            "app.services.ai_orchestration.execute_comp_summary",
            return_value=fake_result,
        ) as mock_exec:
            result = ai_accountant_monitor.run_ai_accountant_monitor(
                Repo(),
                actor="runner",
                now=ai_accountant.datetime(2026, 5, 6, 12, 0, 0),
                lookback_days=30,
                min_severity="P1",
            )

        self.assertTrue(result["review_enabled"])
        self.assertEqual(len(result["review_hash"]), 64)
        self.assertIn("AI Accountant automated review", result["message"])
        self.assertEqual(calls["chat"][0]["intent"], "ai_accountant_scheduled_monitor_review")
        self.assertEqual(calls["chat"][0]["metadata"]["event_type"], "ai_accountant_automated_review")
        self.assertEqual(calls["audit"][0]["changes"]["automated_review"]["answer_hash_sha256"], result["review_hash"])
        self.assertEqual(
            calls["audit"][0]["changes"]["automated_review"]["prompt_hash_sha256"],
            calls["chat"][0]["metadata"]["prompt_hash_sha256"],
        )
        self.assertEqual(
            calls["audit"][0]["changes"]["automated_review"]["data_scope_hash_sha256"],
            calls["chat"][0]["metadata"]["data_scope_hash_sha256"],
        )
        _args, kwargs = mock_exec.call_args
        self.assertEqual(kwargs["workflow"], "accounting")
        self.assertLessEqual(len(kwargs["spot_context"]["monitor_rows"]), 9)
        self.assertLessEqual(len(kwargs["spot_context"]["accounting_exception_rows"]), 8)

    def test_scheduled_monitor_llm_review_failure_is_nonblocking(self):
        calls = {"audit": []}

        class DB:
            def __init__(self):
                self.rolled_back = False

            def rollback(self):
                self.rolled_back = True

        db = DB()

        class Repo:
            def __init__(self):
                self.db = db

            def report_accounting_exception_rows(self, *, start_dt, end_dt):
                return [
                    {
                        "severity": "P1",
                        "exception_type": "missing_cost_basis",
                        "entity_type": "sale",
                        "entity_id": 12,
                    }
                ]

            def dashboard_live_metrics(self, *, now, include_fee_type_breakdown=False):
                return {"sales_30d_profit_basis_status": "ok"}

            def record_audit_event(self, **kwargs):
                calls["audit"].append(kwargs)
                return SimpleNamespace(id=93)

        def runtime_bool(_repo, key, default):
            if key == "ai_accountant_monitor_llm_review_enabled":
                return True
            return default

        with patch("app.services.ai_accountant_monitor.get_runtime_bool", side_effect=runtime_bool), patch(
            "app.services.ai_accountant_monitor.get_runtime_int", return_value=25
        ), patch(
            "app.services.ai_orchestration.execute_comp_summary",
            side_effect=RuntimeError("llm down"),
        ):
            result = ai_accountant_monitor.run_ai_accountant_monitor(
                Repo(),
                actor="runner",
                now=ai_accountant.datetime(2026, 5, 6, 12, 0, 0),
                lookback_days=30,
                min_severity="P1",
            )

        self.assertTrue(result["review_enabled"])
        self.assertIn("default_context", result["review_error"])
        self.assertIn("compact_context", result["review_error"])
        self.assertIn("llm down", result["review_error"])
        self.assertTrue(result["review_compact_retry"])
        self.assertTrue(result["review_runtime_route"])
        self.assertIn("automated review unavailable", result["message"])
        self.assertIn("Runtime route:", result["message"])
        self.assertTrue(db.rolled_back)
        self.assertIn("compact_context", calls["audit"][0]["changes"]["automated_review"]["error"])
        self.assertTrue(calls["audit"][0]["changes"]["automated_review"]["compact_retry"])
        self.assertTrue(calls["audit"][0]["changes"]["automated_review"]["runtime_chain"])
        self.assertTrue(calls["audit"][0]["changes"]["automated_review"]["runtime_chain_brief"])
        self.assertEqual(calls["audit"][0]["changes"]["automated_review"]["monitor_rows"], 1)

    def test_scheduled_monitor_llm_review_retries_with_smaller_context(self):
        calls = {"audit": [], "chat": []}

        class Repo:
            def report_accounting_exception_rows(self, *, start_dt, end_dt):
                return [
                    {
                        "severity": "P1",
                        "exception_type": "missing_cost_basis",
                        "entity_type": "sale",
                        "entity_id": idx,
                    }
                    for idx in range(12)
                ]

            def dashboard_live_metrics(self, *, now, include_fee_type_breakdown=False):
                return {"sales_30d_profit_basis_status": "review_needed"}

            def record_audit_event(self, **kwargs):
                calls["audit"].append(kwargs)
                return SimpleNamespace(id=94)

            def log_ai_chat_interaction(self, **kwargs):
                calls["chat"].append(kwargs)

        def runtime_bool(_repo, key, default):
            if key == "ai_accountant_monitor_llm_review_enabled":
                return True
            return default

        def fake_execute(*_args, **kwargs):
            if len(kwargs["spot_context"]["monitor_rows"]) > 5:
                raise RuntimeError("payload too large")
            return SimpleNamespace(text="Compact accountant review ok.", citation={"provider": "test"})

        with patch("app.services.ai_accountant_monitor.get_runtime_bool", side_effect=runtime_bool), patch(
            "app.services.ai_accountant_monitor.get_runtime_int", return_value=25
        ), patch(
            "app.services.ai_orchestration.execute_comp_summary",
            side_effect=fake_execute,
        ) as mock_exec:
            result = ai_accountant_monitor.run_ai_accountant_monitor(
                Repo(),
                actor="runner",
                now=ai_accountant.datetime(2026, 5, 9, 12, 0, 0),
                lookback_days=30,
                min_severity="P1",
            )

        self.assertTrue(result["review_enabled"])
        self.assertIn("Compact accountant review ok.", result["message"])
        self.assertTrue(result["review_compact_retry"])
        self.assertTrue(result["review_runtime_route"])
        self.assertEqual(mock_exec.call_count, 2)
        self.assertTrue(calls["chat"][0]["metadata"]["compact_retry"])
        self.assertTrue(calls["chat"][0]["metadata"]["runtime_chain"])
        self.assertTrue(calls["chat"][0]["metadata"]["runtime_chain_brief"])
        self.assertTrue(calls["audit"][0]["changes"]["automated_review"]["compact_retry"])
        self.assertTrue(calls["audit"][0]["changes"]["automated_review"]["runtime_chain"])
        self.assertEqual(calls["audit"][0]["changes"]["automated_review"]["monitor_rows"], 5)
        self.assertGreater(calls["audit"][0]["changes"]["automated_review"]["monitor_rows_omitted"], 0)
        self.assertEqual(calls["chat"][0]["metadata"]["data_scope"]["row_counts"]["monitor_rows"], 5)
        self.assertGreater(calls["chat"][0]["metadata"]["data_scope"]["row_counts"]["monitor_rows_omitted"], 0)


if __name__ == "__main__":
    unittest.main()
