import unittest

from app.services.workflow_contracts import (
    LISTING_DRAFT_CONTRACT_TYPE,
    build_listing_draft_payload,
    extract_listing_draft_payload,
)


class WorkflowContractsTests(unittest.TestCase):
    def test_extract_returns_empty_defaults_for_non_mapping_payload(self):
        parsed = extract_listing_draft_payload(None)
        self.assertEqual(parsed.get("is_contract"), False)
        self.assertEqual(parsed.get("contract_version"), 0)
        self.assertEqual(parsed.get("signature"), "")
        self.assertEqual(parsed.get("state"), {})
        self.assertEqual(parsed.get("context"), {})

    def test_extract_handles_non_integer_contract_version_and_unfiltered_payload(self):
        payload = {
            "contract": {"type": LISTING_DRAFT_CONTRACT_TYPE, "version": "v-next"},
            "signature": "",
            "listing_signature": "fallback-sig",
            "state": {"alpha": 1, "beta": 2},
            "context": {"one": "x", "two": "y"},
        }
        parsed = extract_listing_draft_payload(payload)
        self.assertEqual(parsed.get("is_contract"), True)
        self.assertEqual(parsed.get("contract_version"), 0)
        self.assertEqual(parsed.get("signature"), "fallback-sig")
        self.assertEqual(parsed.get("state"), {"alpha": 1, "beta": 2})
        self.assertEqual(parsed.get("context"), {"one": "x", "two": "y"})

    def test_build_and_extract_contract_payload(self):
        payload = build_listing_draft_payload(
            state={"a": 1, "b": "x"},
            context={"selected_listing_id": 9, "listing_signature": "sig-1"},
            signature="sig-1",
        )
        self.assertEqual(payload.get("contract", {}).get("type"), LISTING_DRAFT_CONTRACT_TYPE)
        parsed = extract_listing_draft_payload(
            payload,
            state_keys=["a"],
            context_keys=["selected_listing_id", "listing_signature"],
        )
        self.assertEqual(parsed.get("is_contract"), True)
        self.assertEqual(parsed.get("signature"), "sig-1")
        self.assertEqual(parsed.get("state"), {"a": 1})
        self.assertEqual(
            parsed.get("context"),
            {"selected_listing_id": 9, "listing_signature": "sig-1"},
        )

    def test_extract_legacy_flat_payload_for_listing_wizard(self):
        payload = {
            "listing_wizard_title": "Coin A",
            "listing_wizard_price": 42.0,
            "selected_product_id": 123,
            "selected_template_id": 8,
        }
        parsed = extract_listing_draft_payload(
            payload,
            state_keys=["listing_wizard_title", "listing_wizard_price"],
            context_keys=["selected_product_id", "selected_template_id"],
        )
        self.assertEqual(parsed.get("is_contract"), False)
        self.assertEqual(
            parsed.get("state"),
            {"listing_wizard_title": "Coin A", "listing_wizard_price": 42.0},
        )
        self.assertEqual(
            parsed.get("context"),
            {"selected_product_id": 123, "selected_template_id": 8},
        )

    def test_extract_legacy_nested_state_payload_for_listings(self):
        payload = {
            "selected_listing_id": 77,
            "listing_signature": "legacy-sig",
            "state": {
                "ebay_pub_title": "My listing",
                "ebay_pub_fixed_price": 19.99,
            },
        }
        parsed = extract_listing_draft_payload(
            payload,
            state_keys=["ebay_pub_title", "ebay_pub_fixed_price"],
            context_keys=["selected_listing_id", "listing_signature"],
        )
        self.assertEqual(parsed.get("signature"), "legacy-sig")
        self.assertEqual(
            parsed.get("state"),
            {"ebay_pub_title": "My listing", "ebay_pub_fixed_price": 19.99},
        )
        self.assertEqual(
            parsed.get("context"),
            {"selected_listing_id": 77, "listing_signature": "legacy-sig"},
        )

    def test_contract_preserves_preflight_payload_shapes(self):
        preflight_payload = {
            "checked_at": "2026-04-12T12:00:00",
            "blockers": ["Missing eBay category ID"],
            "warnings": ["Status is not draft"],
            "checks": [{"name": "category_id", "ok": False}],
        }
        payload = build_listing_draft_payload(
            state={
                "ebay_pub_dependency_preflight_result": preflight_payload,
                "listing_wizard_preflight_blocker_count": 1,
                "listing_wizard_preflight_warning_count": 1,
            },
            context={"selected_listing_id": 77},
            signature="sig-preflight",
        )
        parsed = extract_listing_draft_payload(
            payload,
            state_keys=[
                "ebay_pub_dependency_preflight_result",
                "listing_wizard_preflight_blocker_count",
                "listing_wizard_preflight_warning_count",
            ],
            context_keys=["selected_listing_id"],
        )
        self.assertEqual(parsed.get("signature"), "sig-preflight")
        state = parsed.get("state") or {}
        self.assertEqual(
            state.get("ebay_pub_dependency_preflight_result", {}).get("blockers"),
            ["Missing eBay category ID"],
        )
        self.assertEqual(state.get("listing_wizard_preflight_blocker_count"), 1)
        self.assertEqual(state.get("listing_wizard_preflight_warning_count"), 1)

    def test_contract_preserves_listing_wizard_ai_business_state(self):
        ai_suggestions = {
            "suggested_title": "Sample Title",
            "suggested_details": "Detailed marketplace-ready description.",
            "best_offer_enabled": True,
            "best_offer_minimum": 19.99,
        }
        ai_diag = {"provider": "localai", "model": "qwen", "mode": "multimodal"}
        ai_acceptance = {"accepted_fields": ["title", "details"], "actor": "admin"}
        payload = build_listing_draft_payload(
            state={
                "listing_wizard_ai_suggestions": ai_suggestions,
                "listing_wizard_ai_diagnostics": ai_diag,
                "listing_wizard_ai_acceptance": ai_acceptance,
                "listing_wizard_ai_has_run": True,
                "listing_wizard_risk_summary": "Medium risk",
            },
            context={"selected_product_id": 55},
            signature="sig-ai",
        )
        parsed = extract_listing_draft_payload(
            payload,
            state_keys=[
                "listing_wizard_ai_suggestions",
                "listing_wizard_ai_diagnostics",
                "listing_wizard_ai_acceptance",
                "listing_wizard_ai_has_run",
                "listing_wizard_risk_summary",
            ],
            context_keys=["selected_product_id"],
        )
        state = parsed.get("state") or {}
        self.assertEqual(parsed.get("signature"), "sig-ai")
        self.assertEqual((state.get("listing_wizard_ai_suggestions") or {}).get("suggested_title"), "Sample Title")
        self.assertEqual((state.get("listing_wizard_ai_diagnostics") or {}).get("provider"), "localai")
        self.assertEqual((state.get("listing_wizard_ai_acceptance") or {}).get("actor"), "admin")
        self.assertEqual(state.get("listing_wizard_ai_has_run"), True)
        self.assertEqual(state.get("listing_wizard_risk_summary"), "Medium risk")

    def test_contract_preserves_listing_query_seed_state(self):
        payload = build_listing_draft_payload(
            state={
                "listing_wizard_category_query": "1 oz silver bar",
                "listing_wizard_category_query_seed_product_id": 101,
                "listing_wizard_category_suggestions": [{"id": "39481", "title": "Bullion"}],
                "ebay_pub_category_query": "1 oz silver bar",
                "ebay_pub_category_query_seed_product_id": 101,
            },
            context={"selected_listing_id": 44},
            signature="sig-query",
        )
        parsed = extract_listing_draft_payload(
            payload,
            state_keys=[
                "listing_wizard_category_query",
                "listing_wizard_category_query_seed_product_id",
                "listing_wizard_category_suggestions",
                "ebay_pub_category_query",
                "ebay_pub_category_query_seed_product_id",
            ],
            context_keys=["selected_listing_id"],
        )
        state = parsed.get("state") or {}
        self.assertEqual(parsed.get("signature"), "sig-query")
        self.assertEqual(state.get("listing_wizard_category_query"), "1 oz silver bar")
        self.assertEqual(state.get("listing_wizard_category_query_seed_product_id"), 101)
        self.assertEqual((state.get("listing_wizard_category_suggestions") or [{}])[0].get("id"), "39481")
        self.assertEqual(state.get("ebay_pub_category_query_seed_product_id"), 101)


if __name__ == "__main__":
    unittest.main()
