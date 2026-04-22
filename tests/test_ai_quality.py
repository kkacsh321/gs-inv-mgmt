import unittest
from types import SimpleNamespace

from app.services.ai_quality import (
    AIQualityPolicy,
    find_forbidden_terms,
    is_placeholder_text,
    is_weak_intake_text,
    is_weak_listing_details,
    is_weak_listing_title,
    load_ai_quality_policy,
)


class AIQualityTests(unittest.TestCase):
    def test_placeholder_detection(self) -> None:
        self.assertTrue(is_placeholder_text("eBay"))
        self.assertTrue(is_placeholder_text("N/A"))
        self.assertFalse(is_placeholder_text("Vintage copper round collectible"))

    def test_listing_title_quality(self) -> None:
        self.assertTrue(is_weak_listing_title(""))
        self.assertTrue(is_weak_listing_title("eBay"))
        self.assertTrue(is_weak_listing_title("Coin"))
        self.assertFalse(is_weak_listing_title("Vintage Statue of Liberty 1 oz Copper Round"))
        self.assertTrue(
            is_weak_listing_title(
                "Guaranteed Profit Copper Round",
                policy=AIQualityPolicy(),
            )
        )

    def test_listing_details_quality(self) -> None:
        self.assertTrue(is_weak_listing_details("eBay"))
        self.assertTrue(is_weak_listing_details("- bullet one\n- bullet two\n- bullet three"))
        strong = (
            "This listing includes a detailed collector-focused description with condition notes, "
            "specifications, handling context, and buyer guidance. The item shown is the exact item "
            "you will receive, with clear photos and practical shipping expectations for confidence."
        )
        self.assertFalse(is_weak_listing_details(strong))
        self.assertTrue(
            is_weak_listing_details(
                "Risk-free guaranteed return from this coin investment advice listing with many words that still violates policy restrictions for apply.",
                policy=AIQualityPolicy(details_min_words=5, details_min_chars=20),
            )
        )

    def test_intake_text_quality(self) -> None:
        self.assertTrue(is_weak_intake_text("short note"))
        self.assertTrue(is_weak_intake_text("unknown"))
        self.assertFalse(
            is_weak_intake_text(
                "Likely copper round with Statue of Liberty motif and visible light wear on obverse."
            )
        )

    def test_find_forbidden_terms(self) -> None:
        policy = AIQualityPolicy(forbidden_terms=("risk-free", "financial advice"))
        matches = find_forbidden_terms("This is risk-free and includes financial advice.", policy=policy)
        self.assertEqual(matches, ["risk-free", "financial advice"])

    def test_load_policy_from_runtime_settings(self) -> None:
        rows = {
            "ai_quality_title_min_words": SimpleNamespace(value="4", value_type="int"),
            "ai_quality_title_min_chars": SimpleNamespace(value="20", value_type="int"),
            "ai_quality_listing_details_min_words": SimpleNamespace(value="40", value_type="int"),
            "ai_quality_listing_details_min_chars": SimpleNamespace(value="260", value_type="int"),
            "ai_quality_intake_min_words": SimpleNamespace(value="12", value_type="int"),
            "ai_quality_intake_min_chars": SimpleNamespace(value="80", value_type="int"),
            "ai_quality_forbidden_terms_csv": SimpleNamespace(
                value="guaranteed return, risky promise\nfinancial advice",
                value_type="str",
            ),
        }

        class RepoStub:
            def get_runtime_setting(self, environment, key, active_only=True):
                return rows.get(key)

        policy = load_ai_quality_policy(RepoStub())
        self.assertEqual(policy.title_min_words, 4)
        self.assertEqual(policy.title_min_chars, 20)
        self.assertEqual(policy.details_min_words, 40)
        self.assertEqual(policy.details_min_chars, 260)
        self.assertEqual(policy.intake_min_words, 12)
        self.assertEqual(policy.intake_min_chars, 80)
        self.assertEqual(
            policy.forbidden_terms,
            ("guaranteed return", "risky promise", "financial advice"),
        )


if __name__ == "__main__":
    unittest.main()
