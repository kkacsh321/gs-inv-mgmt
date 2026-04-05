import unittest

from app.services.ai_text import (
    coin_grader_structured_to_text,
    extract_json_object,
    normalize_ai_text,
    parse_coin_grader_structured,
)


class AITextTests(unittest.TestCase):
    def test_extract_json_object_variants(self) -> None:
        self.assertEqual(extract_json_object(""), {})
        self.assertEqual(extract_json_object('{"a":1}'), {"a": 1})
        self.assertEqual(extract_json_object("[1,2,3]"), {})
        self.assertEqual(extract_json_object('prefix {"a":"x"} suffix'), {"a": "x"})
        self.assertEqual(extract_json_object("prefix {not json} suffix"), {})

    def test_normalize_ai_text_prefers_summary_keys(self) -> None:
        payload = '{"notes":"from-notes","summary":"from-summary","description":"from-description"}'
        self.assertEqual(normalize_ai_text(payload), "from-notes")

        payload2 = '{"summary":"from-summary","description":"from-description"}'
        self.assertEqual(normalize_ai_text(payload2), "from-summary")

        payload3 = '{"description":"from-description"}'
        self.assertEqual(normalize_ai_text(payload3), "from-description")

    def test_normalize_ai_text_structured_fallback_and_keywords(self) -> None:
        payload = (
            '{"coin_name":"Morgan Dollar","possible_country_or_mint":"US","year_or_period":"1881",'
            '"denomination":"$1","metal":"silver","confidence":"high",'
            '"search_keywords":["morgan","1881","silver","","pcgs"]}'
        )
        text = normalize_ai_text(payload)
        self.assertIn("Coin: Morgan Dollar", text)
        self.assertIn("Country/Mint: US", text)
        self.assertIn("Confidence: high", text)
        self.assertIn("Keywords: morgan, 1881, silver, pcgs", text)

    def test_normalize_ai_text_passthrough_when_not_json(self) -> None:
        self.assertEqual(normalize_ai_text("plain text"), "plain text")
        self.assertEqual(normalize_ai_text(""), "")

    def test_parse_coin_grader_structured(self) -> None:
        payload = """
        {
          "estimated_grade_range":"MS63-MS64",
          "confidence_0_100":82,
          "key_observations":["strong luster","minor marks",""],
          "red_flags":["light cleaning"],
          "estimated_as_is_value_usd":120,
          "estimated_post_grade_value_usd":180,
          "estimated_grading_total_cost_usd":45,
          "estimated_net_upside_usd":15,
          "submit_for_professional_grading":"yes",
          "recommendation_rationale":"possible upside",
          "suggested_grade_service_priority":["PCGS","NGC",""],
          "notes":"watch for hairlines"
        }
        """
        out = parse_coin_grader_structured(payload)
        self.assertEqual(out["estimated_grade_range"], "MS63-MS64")
        self.assertEqual(out["confidence_0_100"], 82)
        self.assertEqual(out["key_observations"], ["strong luster", "minor marks"])
        self.assertEqual(out["red_flags"], ["light cleaning"])
        self.assertEqual(out["submit_for_professional_grading"], "YES")
        self.assertEqual(out["suggested_grade_service_priority"], ["PCGS", "NGC"])
        self.assertEqual(out["notes"], "watch for hairlines")

        out2 = parse_coin_grader_structured('{"submit_for_professional_grading":"maybe"}')
        self.assertEqual(out2["submit_for_professional_grading"], "")

    def test_coin_grader_structured_to_text(self) -> None:
        self.assertEqual(coin_grader_structured_to_text({}), "")
        payload = {
            "estimated_grade_range": "AU58",
            "confidence_0_100": 70,
            "submit_for_professional_grading": "CONDITIONAL",
            "recommendation_rationale": "borderline economics",
            "estimated_as_is_value_usd": 90,
            "estimated_post_grade_value_usd": 120,
            "estimated_grading_total_cost_usd": 40,
            "estimated_net_upside_usd": -10,
            "key_observations": ["wear on high points"],
            "red_flags": ["cleaning risk"],
            "suggested_grade_service_priority": ["NGC", "PCGS"],
            "notes": "sell raw if no premium.",
        }
        text = coin_grader_structured_to_text(payload)
        self.assertIn("Estimated Grade Range: AU58", text)
        self.assertIn("Confidence: 70", text)
        self.assertIn("Submit For Professional Grading: CONDITIONAL", text)
        self.assertIn("Estimated Net Upside USD: -10", text)
        self.assertIn("Suggested Service Priority: NGC, PCGS", text)


if __name__ == "__main__":
    unittest.main()

