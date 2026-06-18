import importlib.util
import ast
import inspect
import sys
import types
import unittest
from pathlib import Path


def _bootstrap_view_imports() -> None:
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

    root = Path(__file__).resolve().parents[1]
    views_dir = root / "app" / "components" / "views"
    pkg_name = "app.components.views"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(views_dir)]
        sys.modules[pkg_name] = pkg


def _load_view(name: str):
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"app.components.views.{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    sys.modules[f"app.components.views.{name}"] = module
    return module


class ViewImportCoverageTests(unittest.TestCase):
    def test_import_core_view_modules(self) -> None:
        _bootstrap_view_imports()
        ordered = [
            "shared",
            "workspace_shell",
            "entity_ops",
            "ebay_context",
            "search_edit",
            "system_health",
            "business_chat_room",
            "customers",
            "ai_chat",
            "ai_accountant",
            "lots",
            "ebay",
            "ebay_ops",
            "orders",
            "sales",
            "sync",
            "shipping",
            "operations_home",
            "products",
            "listings",
            "documents",
            "coin_intake_wizard",
            "inventory_intake_wizard",
            "tools",
            "ebay_workspace",
            "tax_support",
            "reports",
            "taxes",
            "quickbooks",
        ]
        loaded = {name: _load_view(name) for name in ordered}

        expected_render_attrs = {
            "search_edit": "render_search_edit",
            "system_health": "render_system_health",
            "business_chat_room": "render_business_chat_room",
            "customers": "render_customers",
            "ai_chat": "render_ai_chat",
            "ai_accountant": "render_ai_accountant",
            "lots": "render_lots",
            "ebay": "render_ebay",
            "ebay_ops": "render_ebay_ops",
            "orders": "render_orders",
            "sales": "render_sales",
            "sync": "render_sync",
            "shipping": "render_shipping",
            "operations_home": "render_operations_home",
            "products": "render_products",
            "listings": "render_listings",
            "documents": "render_documents",
            "coin_intake_wizard": "render_coin_intake_wizard",
            "inventory_intake_wizard": "render_inventory_intake_wizard",
            "tools": "render_tools",
            "ebay_workspace": "render_ebay_workspace",
            "taxes": "render_taxes",
            "quickbooks": "render_quickbooks",
        }
        for module_name, attr in expected_render_attrs.items():
            self.assertTrue(
                hasattr(loaded[module_name], attr),
                f"{module_name} missing expected callable {attr}",
            )

        answer_rows = loaded["business_chat_room"].build_business_room_operator_answer_rows(
            {
                "draft_contract": {
                    "operator_answers": [
                        {"field": "quantity", "value": "20", "source": "slack", "actor": "keith"},
                    ],
                },
                "operator_answers": [
                    {"field": "quantity", "answer": "20", "source": "slack", "actor": "keith"},
                    {"field": "condition_id", "answer": "3000", "source": "business_chat_room", "actor": "keith"},
                ],
            }
        )
        self.assertEqual(
            answer_rows,
            [
                {"field": "quantity", "answer": "20", "source": "slack", "actor": "keith"},
                {"field": "condition_id", "answer": "3000", "source": "business_chat_room", "actor": "keith"},
            ],
        )
        review_field_rows = loaded["business_chat_room"]._business_room_review_field_rows(
            {
                "fields": [
                    {"key": "product_id", "value": 198, "confidence": 0.55, "source": "business_room_prompt_hint"},
                    {"key": "", "value": "ignored"},
                    {"not_key": "ignored"},
                ],
            }
        )
        self.assertEqual(
            review_field_rows,
            [
                {
                    "field": "product_id",
                    "value": 198,
                    "confidence": 0.55,
                    "source": "business_room_prompt_hint",
                },
            ],
        )
        _, _, agent_instruction = loaded["business_chat_room"]._build_agent_instruction(
            agent_key="business_monitor_agent",
            user_message="Atlas, check repeat buyer follow-up.",
            snapshot={
                "customer_rollup": {
                    "available": True,
                    "customer_count": 2,
                    "repeat_buyer_count": 1,
                    "customers_with_internal_notes": 1,
                    "dormant_90d_count": 1,
                    "top_repeat_buyers": [
                        {
                            "identity": "repeatbuyer",
                            "order_count": 3,
                            "total_spend": 123.45,
                            "has_internal_notes": True,
                            "days_since_last_order": 12,
                        }
                    ],
                    "top_dormant_customers": [
                        {
                            "identity": "dormantbuyer",
                            "order_count": 1,
                            "total_spend": 25.0,
                            "has_internal_notes": False,
                            "days_since_last_order": 120,
                        }
                    ],
                },
                "recent_messages": [],
            },
            attachments=[],
        )
        self.assertIn("Customer context:", agent_instruction)
        self.assertIn("Repeat buyers: 1", agent_instruction)
        self.assertIn("repeatbuyer", agent_instruction)
        self.assertIn("dormantbuyer", agent_instruction)
        self.assertIn("notes yes", agent_instruction)
        self.assertIn("Internal customer note bodies are private", agent_instruction)
        self.assertNotIn("Prefers combined shipping", agent_instruction)
        customer_context_rows = loaded["business_chat_room"]._business_room_customer_context_rows(
            {
                "available": True,
                "top_repeat_buyers": [
                    {
                        "customer_id": 7,
                        "identity": "repeatbuyer",
                        "order_count": 3,
                        "total_spend": 123.45,
                        "has_internal_notes": True,
                        "days_since_last_order": 12,
                    }
                ],
                "top_dormant_customers": [
                    {
                        "customer_id": 9,
                        "identity": "dormantbuyer",
                        "order_count": 1,
                        "total_spend": 25.0,
                        "has_internal_notes": False,
                        "days_since_last_order": 120,
                    }
                ],
            }
        )
        self.assertEqual(
            customer_context_rows,
            [
                {
                    "kind": "repeat_buyer",
                    "customer_id": 7,
                    "identity": "repeatbuyer",
                    "orders": 3,
                    "lifetime_spend": 123.45,
                    "has_internal_notes": True,
                    "days_since_last_order": 12,
                },
                {
                    "kind": "dormant",
                    "customer_id": 9,
                    "identity": "dormantbuyer",
                    "orders": 1,
                    "lifetime_spend": 25.0,
                    "has_internal_notes": False,
                    "days_since_last_order": 120,
                },
            ],
        )
        self.assertNotIn("Prefers combined shipping", str(customer_context_rows))
        customer_context_prompts = loaded["business_chat_room"]._business_room_customer_context_prompts(
            {
                "available": True,
                "repeat_buyer_count": 1,
                "dormant_90d_count": 1,
                "customers_with_internal_notes": 1,
            }
        )
        self.assertIn(
            "Atlas, review repeat-buyer and dormant-customer context and recommend customer follow-up priorities.",
            customer_context_prompts,
        )
        self.assertIn(
            "Goldie, review customer/repeat-buyer context for accounting or tax-sensitive follow-up risks, using note-presence only.",
            customer_context_prompts,
        )
        prompt_action_rows = loaded["business_chat_room"]._business_room_prompt_action_rows(
            [
                customer_context_prompts[0],
                customer_context_prompts[0],
                "Murdock, draft listing copy with eBay-safe formatting and buyer-focused details.",
            ],
            limit=2,
        )
        self.assertEqual(len(prompt_action_rows), 2)
        self.assertEqual(prompt_action_rows[0]["prompt"], customer_context_prompts[0])
        self.assertLessEqual(len(prompt_action_rows[0]["label"]), 80)
        prompt_action_signature = inspect.signature(
            loaded["business_chat_room"]._render_business_room_prompt_actions
        )
        self.assertIn("pending_prompt_meta_key", prompt_action_signature.parameters)
        self.assertIn("source_label", prompt_action_signature.parameters)
        read_only_status = loaded["business_chat_room"]._business_room_prepared_prompt_status(
            "Atlas, summarize repeat buyer follow-up priorities."
        )
        self.assertFalse(read_only_status["write_intent"])
        self.assertEqual(read_only_status["status"], "read_only")
        self.assertIn("read-only", read_only_status["message"])
        write_status = loaded["business_chat_room"]._business_room_prepared_prompt_status(
            "Murdock, create a listing draft for product 198."
        )
        self.assertTrue(write_status["write_intent"])
        self.assertEqual(write_status["status"], "approval_required")
        self.assertIn("human-approval queue", write_status["message"])
        self.assertEqual(
            loaded["business_chat_room"]._business_room_prepared_status_caption(read_only_status),
            "Prepared status: `read_only`",
        )
        write_status_caption = loaded["business_chat_room"]._business_room_prepared_status_caption(write_status)
        self.assertIn("Prepared status: `approval_required`", write_status_caption)
        self.assertIn("route `", write_status_caption)
        self.assertEqual(
            loaded["business_chat_room"]._business_room_prepared_source_caption(
                {"source_label": "Room Standup", "prompt_label": "Atlas, triage failed room actions."}
            ),
            "Prepared from: `Room Standup` | `Atlas, triage failed room actions.`",
        )
        self.assertEqual(
            loaded["business_chat_room"]._business_room_prepared_source_caption(
                {"source_label": "Agent Focus"}
            ),
            "Prepared from: `Agent Focus`",
        )
        pending_upload_rows = loaded["business_chat_room"]._business_room_pending_upload_rows(
            [
                types.SimpleNamespace(name="coin.jpg", type="image/jpeg", size=1234),
                types.SimpleNamespace(name="receipt.pdf", type="application/pdf", size=2048),
            ]
        )
        self.assertEqual(
            pending_upload_rows,
            [
                {
                    "filename": "coin.jpg",
                    "kind": "image",
                    "content_type": "image/jpeg",
                    "size_bytes": 1234,
                },
                {
                    "filename": "receipt.pdf",
                    "kind": "pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 2048,
                },
            ],
        )
        attachment_rows = loaded["business_chat_room"].build_business_room_attachment_evidence_rows(
            {
                "attachments": [
                    {
                        "filename": "coin.jpg",
                        "kind": "image",
                        "content_type": "image/jpeg",
                        "size_bytes": 1234,
                        "stored_ref": {"entity_type": "media_asset", "entity_id": 42},
                    },
                    {
                        "filename": "invoice.pdf",
                        "kind": "pdf",
                        "content_type": "application/pdf",
                        "size_bytes": 2048,
                        "error": "Media storage is not configured.",
                    },
                ],
            }
        )
        self.assertEqual(
            attachment_rows,
            [
                {
                    "filename": "coin.jpg",
                    "kind": "image",
                    "content_type": "image/jpeg",
                    "size_bytes": 1234,
                    "stored_as": "media_asset",
                    "stored_id": 42,
                    "error": "",
                },
                {
                    "filename": "invoice.pdf",
                    "kind": "pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 2048,
                    "stored_as": "",
                    "stored_id": 0,
                    "error": "Media storage is not configured.",
                },
            ],
        )

    def test_render_help_panel_calls_include_roadmap_phase(self) -> None:
        root = Path(__file__).resolve().parents[1]
        missing: list[str] = []
        for path in sorted((root / "app" / "components" / "views").glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func_name = getattr(node.func, "id", "")
                if func_name != "render_help_panel":
                    continue
                keyword_names = {kw.arg for kw in node.keywords if kw.arg}
                if "roadmap_phase" not in keyword_names and len(node.args) < 4:
                    missing.append(f"{path.relative_to(root)}:{node.lineno}")

        self.assertEqual(missing, [])

    def test_business_room_prepared_prompt_source_is_persisted(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "app/components/views/business_chat_room.py").read_text()
        self.assertIn("business_chat_pending_prompt_meta", source)
        self.assertIn("prepared_prompt_source", source)
        self.assertIn("prepared_prompt_status", source)
        self.assertIn("def _business_room_prepared_source_caption", source)
        self.assertIn("def _business_room_prepared_status_caption", source)
        self.assertIn("def _render_business_room_attachment_evidence", source)
        self.assertGreaterEqual(source.count("_render_business_room_attachment_evidence("), 3)

    def test_wizard_handoff_answer_suggestions_not_limited_to_missing_questions(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in [
            "app/components/views/inventory_intake_wizard.py",
            "app/components/views/listing_wizard.py",
        ]:
            source = (root / relative).read_text()
            missing_idx = source.index("if missing_questions:")
            suggestion_idx = source.index("answer_suggestions = build_business_room_answer_command_suggestions", missing_idx)
            payload_idx = source.index("selected_payload = selected.get", suggestion_idx)
            segment = source[missing_idx:suggestion_idx]
            self.assertLess(suggestion_idx, payload_idx, relative)
            self.assertNotIn(
                "answer_suggestions = build_business_room_answer_command_suggestions",
                segment,
                relative,
            )

    def test_business_room_draft_card_answer_suggestions_not_limited_to_missing_questions(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "app/components/views/business_chat_room.py").read_text()
        function_idx = source.index("def _render_draft_contract_card")
        missing_idx = source.index("if missing_questions:", function_idx)
        suggestion_idx = source.index("answer_suggestions = build_business_room_answer_command_suggestions", missing_idx)
        operator_idx = source.index("operator_answers = [", suggestion_idx)
        segment = source[missing_idx:suggestion_idx]
        self.assertLess(suggestion_idx, operator_idx)
        self.assertNotIn("answer_suggestions = build_business_room_answer_command_suggestions", segment)

    def test_handoff_answer_suggestion_views_request_full_review_set(self) -> None:
        root = Path(__file__).resolve().parents[1]
        expectations = {
            "app/components/views/business_chat_room.py": 2,
            "app/components/views/inventory_intake_wizard.py": 1,
            "app/components/views/listing_wizard.py": 1,
        }
        for relative, minimum_count in expectations.items():
            source = (root / relative).read_text()
            self.assertGreaterEqual(source.count("max_suggestions=8"), minimum_count, relative)


if __name__ == "__main__":
    unittest.main()
