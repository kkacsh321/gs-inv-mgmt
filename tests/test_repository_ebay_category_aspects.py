import unittest

from test_support import in_memory_repo


class EbayCategoryAspectCacheRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_context = in_memory_repo()
        self.db, self.repo = self._repo_context.__enter__()

    def tearDown(self) -> None:
        self._repo_context.__exit__(None, None, None)

    def test_cache_and_get_ebay_category_aspects(self) -> None:
        saved = self.repo.cache_ebay_category_aspects(
            environment="LOCAL",
            marketplace_id="ebay_us",
            category_id="111",
            aspects=[
                {"name": "Brand", "required": True, "values": ["US Mint"]},
                {"name": "Color", "required": False, "values": ["Red"]},
            ],
            actor="tester",
        )
        self.assertTrue(saved)

        cached = self.repo.get_cached_ebay_category_aspects(
            environment="local",
            marketplace_id="EBAY_US",
            category_id="111",
        )

        self.assertIsNotNone(cached)
        self.assertEqual(cached["source"], "db_cache")
        self.assertEqual(cached["required_count"], 1)
        self.assertEqual(cached["total_count"], 2)
        self.assertEqual(cached["aspects"][0]["name"], "Brand")

    def test_cache_ebay_category_aspects_upserts_same_category(self) -> None:
        self.repo.cache_ebay_category_aspects(
            environment="local",
            marketplace_id="EBAY_US",
            category_id="111",
            aspects=[{"name": "Brand", "required": True, "values": []}],
            actor="tester",
        )
        self.repo.cache_ebay_category_aspects(
            environment="local",
            marketplace_id="EBAY_US",
            category_id="111",
            aspects=[{"name": "Material", "required": True, "values": ["Silver"]}],
            actor="tester",
        )

        cached = self.repo.get_cached_ebay_category_aspects(
            environment="local",
            marketplace_id="EBAY_US",
            category_id="111",
        )

        self.assertIsNotNone(cached)
        self.assertEqual(cached["required_count"], 1)
        self.assertEqual(cached["total_count"], 1)
        self.assertEqual(cached["aspects"][0]["name"], "Material")
        self.assertEqual(cached["hit_count"], 2)

    def test_get_cached_ebay_category_aspects_missing_returns_none(self) -> None:
        self.assertIsNone(
            self.repo.get_cached_ebay_category_aspects(
                environment="local",
                marketplace_id="EBAY_US",
                category_id="",
            )
        )
        self.assertIsNone(
            self.repo.get_cached_ebay_category_aspects(
                environment="local",
                marketplace_id="EBAY_US",
                category_id="999",
            )
        )

    def test_ebay_store_category_upsert_list_and_update(self) -> None:
        first = self.repo.upsert_ebay_store_category(
            environment="LOCAL",
            marketplace_id="ebay_us",
            category_path="Coins//Bullion/Copper/Extra",
            external_category_id="123",
            sort_order=10,
            is_active=True,
            source="manual",
            notes="Copper rounds",
            actor="tester",
        )

        self.assertEqual(first.environment, "local")
        self.assertEqual(first.marketplace_id, "EBAY_US")
        self.assertEqual(first.category_path, "/Coins/Bullion/Copper")
        self.assertEqual(first.category_name, "Copper")
        self.assertEqual(first.parent_path, "/Coins/Bullion")
        self.assertEqual(first.external_category_id, "123")

        second = self.repo.upsert_ebay_store_category(
            environment="local",
            marketplace_id="EBAY_US",
            category_path=r"\Coins\Bullion\Copper",
            external_category_id="456",
            sort_order=3,
            is_active=True,
            source="manual",
            notes="Updated",
            actor="tester",
        )

        self.assertEqual(second.id, first.id)
        self.assertEqual(second.external_category_id, "456")
        self.assertEqual(second.sort_order, 3)
        self.assertEqual(second.notes, "Updated")

        self.repo.upsert_ebay_store_category(
            environment="local",
            marketplace_id="EBAY_US",
            category_path="/Coins/Silver",
            sort_order=1,
            actor="tester",
        )
        self.repo.upsert_ebay_store_category(
            environment="local",
            marketplace_id="EBAY_US",
            category_path="/Hidden",
            sort_order=0,
            is_active=False,
            actor="tester",
        )

        active_rows = self.repo.list_ebay_store_categories(environment="local", marketplace_id="EBAY_US")
        self.assertEqual([row.category_path for row in active_rows], ["/Coins/Silver", "/Coins/Bullion/Copper"])

        all_rows = self.repo.list_ebay_store_categories(
            environment="local",
            marketplace_id="EBAY_US",
            active_only=False,
        )
        self.assertIn("/Hidden", [row.category_path for row in all_rows])

        updated = self.repo.update_ebay_store_category(
            first.id,
            {"category_path": "/Coins/Copper", "is_active": False},
            actor="tester",
        )
        self.assertEqual(updated.category_path, "/Coins/Copper")
        self.assertEqual(updated.category_name, "Copper")
        self.assertEqual(updated.parent_path, "/Coins")
        self.assertFalse(updated.is_active)

    def test_ebay_store_category_sync_reconcile_preview_and_deactivate(self) -> None:
        synced_keep = self.repo.upsert_ebay_store_category(
            environment="local",
            marketplace_id="EBAY_US",
            category_path="/Coins/Bullion",
            external_category_id="101",
            source="ebay_get_store",
            actor="tester",
            mark_synced=True,
        )
        synced_missing = self.repo.upsert_ebay_store_category(
            environment="local",
            marketplace_id="EBAY_US",
            category_path="/Coins/Old",
            external_category_id="102",
            source="ebay_get_store",
            actor="tester",
            mark_synced=True,
        )
        manual_missing = self.repo.upsert_ebay_store_category(
            environment="local",
            marketplace_id="EBAY_US",
            category_path="/Manual/Seasonal",
            source="manual",
            actor="tester",
        )

        preview = self.repo.reconcile_ebay_store_category_sync(
            environment="local",
            marketplace_id="EBAY_US",
            synced_category_paths=["/Coins/Bullion"],
            deactivate_missing=False,
            actor="tester",
        )

        self.assertEqual(preview["synced_count"], 1)
        self.assertEqual(preview["missing_count"], 1)
        self.assertEqual(preview["deactivated_count"], 0)
        self.assertEqual(preview["missing"][0]["category_path"], "/Coins/Old")
        self.db.refresh(synced_missing)
        self.assertTrue(synced_missing.is_active)

        applied = self.repo.reconcile_ebay_store_category_sync(
            environment="local",
            marketplace_id="EBAY_US",
            synced_category_paths=["/Coins/Bullion"],
            deactivate_missing=True,
            actor="tester",
            sync_message="Unit test sync removed this category.",
        )

        self.assertEqual(applied["missing_count"], 1)
        self.assertEqual(applied["deactivated_count"], 1)
        self.db.refresh(synced_keep)
        self.db.refresh(synced_missing)
        self.db.refresh(manual_missing)
        self.assertTrue(synced_keep.is_active)
        self.assertFalse(synced_missing.is_active)
        self.assertEqual(synced_missing.last_sync_status, "missing_from_ebay")
        self.assertIn("Unit test", synced_missing.last_sync_message)
        self.assertTrue(manual_missing.is_active)


if __name__ == "__main__":
    unittest.main()
