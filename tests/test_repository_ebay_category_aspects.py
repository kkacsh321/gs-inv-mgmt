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


if __name__ == "__main__":
    unittest.main()
