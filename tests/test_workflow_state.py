import json
import unittest
from datetime import timedelta

from app.db.models import WorkflowDraft, WorkflowEvent
from app.services.workflow_contracts import build_listing_draft_payload, extract_listing_draft_payload
from app.utils.time import utcnow_naive
from test_support import in_memory_repo


class WorkflowStateRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = in_memory_repo()
        self.db, self.repo = self._ctx.__enter__()

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def test_save_and_load_workflow_draft_upserts_and_increments_autosave_count(self) -> None:
        created = self.repo.save_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="default",
            draft_payload={"listing_wizard_title": "Draft One"},
            actor="admin",
        )
        self.assertIsInstance(created, WorkflowDraft)
        self.assertEqual(created.autosave_count, 1)

        updated = self.repo.save_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="default",
            draft_payload={"listing_wizard_title": "Draft Two"},
            actor="admin",
        )
        self.assertEqual(updated.id, created.id)
        self.assertEqual(updated.autosave_count, 2)
        payload = json.loads(str(updated.draft_json or "{}"))
        self.assertEqual(payload.get("listing_wizard_title"), "Draft Two")

        loaded = self.repo.load_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="default",
            active_only=True,
        )
        self.assertIsNotNone(loaded)
        self.assertEqual(int(loaded.id), int(created.id))

    def test_resume_latest_workflow_draft_sets_resumed_at(self) -> None:
        self.repo.save_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="default",
            draft_payload={"v": 1},
            actor="admin",
        )
        resumed = self.repo.resume_latest_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            active_only=True,
        )
        self.assertIsNotNone(resumed)
        self.assertIsNotNone(resumed.resumed_at)

    def test_clear_workflow_draft_and_append_event(self) -> None:
        draft = self.repo.save_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="default",
            draft_payload={"v": 1},
            actor="admin",
        )
        event = self.repo.append_workflow_event(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="default",
            action="save_draft",
            status="ok",
            payload={"draft_id": int(draft.id)},
            draft_id=int(draft.id),
            actor="admin",
        )
        self.assertIsInstance(event, WorkflowEvent)
        self.assertEqual(int(event.draft_id or 0), int(draft.id))

        cleared = self.repo.clear_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="default",
            actor="admin",
            reason="test",
        )
        self.assertTrue(cleared)

        loaded_inactive = self.repo.load_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="default",
            active_only=False,
        )
        self.assertIsNotNone(loaded_inactive)
        self.assertFalse(bool(loaded_inactive.is_active))
        self.assertEqual(str(loaded_inactive.status), "cleared")

    def test_workflow_draft_validation_and_missing_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "Workflow key is required"):
            self.repo.save_workflow_draft(
                environment="local",
                workflow_key="",
                username="admin",
                scope_key="default",
                draft_payload={"v": 1},
                actor="admin",
            )
        with self.assertRaisesRegex(ValueError, "Username is required"):
            self.repo.save_workflow_draft(
                environment="local",
                workflow_key="listing_wizard",
                username="",
                scope_key="default",
                draft_payload={"v": 1},
                actor="admin",
            )

        self.assertIsNone(
            self.repo.load_workflow_draft(
                environment="local",
                workflow_key="",
                username="admin",
                scope_key="default",
            )
        )
        self.assertIsNone(
            self.repo.load_workflow_draft(
                environment="local",
                workflow_key="listing_wizard",
                username="",
                scope_key="default",
            )
        )
        self.assertFalse(
            self.repo.clear_workflow_draft(
                environment="local",
                workflow_key="listing_wizard",
                username="admin",
                scope_key="does-not-exist",
                actor="admin",
            )
        )

    def test_resume_latest_across_scopes_and_active_only_toggle(self) -> None:
        older = self.repo.save_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="scope-a",
            draft_payload={"title": "A"},
            actor="admin",
        )
        newer = self.repo.save_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="scope-b",
            draft_payload={"title": "B"},
            actor="admin",
        )
        # Make the newer row inactive to verify active_only behavior.
        newer.is_active = False
        newer.status = "cleared"
        self.db.add(newer)
        self.db.commit()

        resumed_any = self.repo.resume_latest_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            active_only=False,
        )
        self.assertIsNotNone(resumed_any)
        self.assertEqual(int(resumed_any.id), int(newer.id))
        self.assertIsNotNone(resumed_any.resumed_at)

        resumed_active = self.repo.resume_latest_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            active_only=True,
        )
        self.assertIsNotNone(resumed_active)
        self.assertEqual(int(resumed_active.id), int(older.id))
        self.assertIsNotNone(resumed_active.resumed_at)

    def test_append_list_workflow_events_filters_and_validation(self) -> None:
        draft = self.repo.save_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="scope-main",
            draft_payload={"v": 1},
            actor="admin",
        )

        e1 = self.repo.append_workflow_event(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="scope-main",
            action="save_draft",
            payload={"k": 1},
            draft_id=int(draft.id),
            actor="admin",
        )
        e2 = self.repo.append_workflow_event(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="scope-secondary",
            action="resume_draft",
            payload={"k": 2},
            draft_id=int(draft.id),
            actor="admin",
        )
        _ = e1, e2

        by_scope = self.repo.list_workflow_events(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="scope-main",
            limit=50,
        )
        self.assertEqual(len(by_scope), 1)
        self.assertEqual(str(by_scope[0].scope_key), "scope-main")

        limited = self.repo.list_workflow_events(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            limit=1,
        )
        self.assertEqual(len(limited), 1)

        with self.assertRaisesRegex(ValueError, "Workflow key is required"):
            self.repo.append_workflow_event(
                environment="local",
                workflow_key="",
                username="admin",
                action="save_draft",
            )
        with self.assertRaisesRegex(ValueError, "Username is required"):
            self.repo.append_workflow_event(
                environment="local",
                workflow_key="listing_wizard",
                username="",
                action="save_draft",
            )
        with self.assertRaisesRegex(ValueError, "action is required"):
            self.repo.append_workflow_event(
                environment="local",
                workflow_key="listing_wizard",
                username="admin",
                action="",
            )

    def test_list_and_cleanup_workflow_state(self) -> None:
        active = self.repo.save_workflow_draft(
            environment="local",
            workflow_key="inventory_intake_wizard",
            username="admin",
            scope_key="default",
            draft_payload={"step": 1},
            actor="admin",
        )
        stale = self.repo.save_workflow_draft(
            environment="local",
            workflow_key="inventory_intake_wizard",
            username="admin",
            scope_key="stale",
            draft_payload={"step": "old"},
            actor="admin",
        )
        stale.is_active = False
        stale.status = "cleared"
        stale.updated_at = utcnow_naive() - timedelta(days=120)
        self.db.add(stale)
        self.db.commit()

        self.repo.append_workflow_event(
            environment="local",
            workflow_key="inventory_intake_wizard",
            username="admin",
            scope_key="stale",
            action="save_draft",
            status="ok",
            payload={"draft_id": int(stale.id)},
            draft_id=int(stale.id),
            actor="admin",
        )

        listed = self.repo.list_workflow_drafts(
            environment="local",
            workflow_key="inventory_intake_wizard",
            username="admin",
            active_only=False,
            limit=50,
        )
        self.assertGreaterEqual(len(listed), 2)

        cleanup = self.repo.cleanup_workflow_state(
            environment="local",
            draft_retention_days=30,
            event_retention_days=1,
            actor="admin",
        )
        self.assertGreaterEqual(int(cleanup.get("deleted_stale_drafts", 0)), 1)

        active_row = self.repo.load_workflow_draft(
            environment="local",
            workflow_key="inventory_intake_wizard",
            username="admin",
            scope_key="default",
            active_only=True,
        )
        self.assertIsNotNone(active_row)
        self.assertEqual(int(active_row.id), int(active.id))

    def test_cleanup_removes_expired_active_drafts_and_old_events_only(self) -> None:
        keep = self.repo.save_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="keep",
            draft_payload={"step": "keep"},
            actor="admin",
        )
        expired = self.repo.save_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="expired",
            draft_payload={"step": "expired"},
            actor="admin",
            expires_at=utcnow_naive() - timedelta(days=2),
        )
        expired.updated_at = utcnow_naive() - timedelta(days=2)
        self.db.add(expired)
        self.db.commit()

        old_event = self.repo.append_workflow_event(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="expired",
            action="autosave",
            status="ok",
            payload={"expired": True},
            draft_id=int(expired.id),
            actor="admin",
        )
        old_event.created_at = utcnow_naive() - timedelta(days=10)
        self.db.add(old_event)
        old_event_id = int(old_event.id)

        fresh_event = self.repo.append_workflow_event(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="keep",
            action="autosave",
            status="ok",
            payload={"keep": True},
            draft_id=int(keep.id),
            actor="admin",
        )
        fresh_event.created_at = utcnow_naive()
        self.db.add(fresh_event)
        fresh_event_id = int(fresh_event.id)
        self.db.commit()

        cleanup = self.repo.cleanup_workflow_state(
            environment="local",
            draft_retention_days=1,
            event_retention_days=1,
            actor="admin",
        )
        self.assertGreaterEqual(int(cleanup.get("deleted_stale_drafts", 0)), 1)
        self.assertGreaterEqual(int(cleanup.get("deleted_events_for_stale_drafts", 0)), 1)

        expired_row = self.repo.load_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="expired",
            active_only=False,
        )
        self.assertIsNone(expired_row)

        keep_row = self.repo.load_workflow_draft(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            scope_key="keep",
            active_only=True,
        )
        self.assertIsNotNone(keep_row)
        self.assertEqual(int(keep_row.id), int(keep.id))

        events = self.repo.list_workflow_events(
            environment="local",
            workflow_key="listing_wizard",
            username="admin",
            limit=50,
        )
        event_ids = {int(row.id) for row in events}
        self.assertIn(fresh_event_id, event_ids)
        self.assertNotIn(old_event_id, event_ids)

    def test_listings_publish_contract_roundtrip_save_resume_clear(self) -> None:
        listing_state = {
            "ebay_pub_title": "Contract Listing Title",
            "ebay_pub_category_id": "16679",
            "ebay_pub_fixed_price": 49.99,
            "ebay_pub_post_mode": "Create Offer Draft Only",
            "ebay_pub_dependency_preflight_result": {
                "checked_at": "2026-04-13T23:40:00",
                "blockers": ["Missing return policy"],
                "warnings": ["Quantity > 1 for auction"],
                "checks": [{"name": "category_id", "ok": True}],
            },
        }
        payload = build_listing_draft_payload(
            state=listing_state,
            context={"selected_listing_id": 321, "listing_signature": "sig-321"},
            signature="sig-321",
        )

        row = self.repo.save_workflow_draft(
            environment="local",
            workflow_key="listings_ebay_publish",
            username="admin",
            scope_key="listing:321",
            draft_payload=payload,
            actor="admin",
        )
        self.assertIsNotNone(row)
        self.assertEqual(int(row.autosave_count or 0), 1)

        loaded = self.repo.load_workflow_draft(
            environment="local",
            workflow_key="listings_ebay_publish",
            username="admin",
            scope_key="listing:321",
            active_only=True,
        )
        self.assertIsNotNone(loaded)
        loaded_payload = json.loads(str(loaded.draft_json or "{}"))
        parsed_loaded = extract_listing_draft_payload(
            loaded_payload,
            state_keys=[
                "ebay_pub_title",
                "ebay_pub_category_id",
                "ebay_pub_fixed_price",
                "ebay_pub_post_mode",
                "ebay_pub_dependency_preflight_result",
            ],
            context_keys=["selected_listing_id", "listing_signature"],
        )
        self.assertEqual(parsed_loaded.get("signature"), "sig-321")
        loaded_state = parsed_loaded.get("state") or {}
        self.assertEqual(loaded_state.get("ebay_pub_category_id"), "16679")
        self.assertEqual(loaded_state.get("ebay_pub_fixed_price"), 49.99)
        self.assertEqual(
            (loaded_state.get("ebay_pub_dependency_preflight_result") or {}).get("blockers"),
            ["Missing return policy"],
        )

        resumed = self.repo.resume_latest_workflow_draft(
            environment="local",
            workflow_key="listings_ebay_publish",
            username="admin",
            active_only=True,
        )
        self.assertIsNotNone(resumed)
        self.assertEqual(int(resumed.id), int(row.id))
        self.assertIsNotNone(resumed.resumed_at)

        cleared = self.repo.clear_workflow_draft(
            environment="local",
            workflow_key="listings_ebay_publish",
            username="admin",
            scope_key="listing:321",
            actor="admin",
            reason="integration-test",
        )
        self.assertTrue(cleared)
        inactive = self.repo.load_workflow_draft(
            environment="local",
            workflow_key="listings_ebay_publish",
            username="admin",
            scope_key="listing:321",
            active_only=False,
        )
        self.assertIsNotNone(inactive)
        self.assertEqual(str(inactive.status), "cleared")
        self.assertFalse(bool(inactive.is_active))


if __name__ == "__main__":
    unittest.main()
