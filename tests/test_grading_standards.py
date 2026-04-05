import unittest
from unittest.mock import Mock, patch

from app.services import grading_standards as gs


class GradingStandardsTests(unittest.TestCase):
    def test_strip_html_removes_script_style_and_tags(self) -> None:
        raw = "<html><style>.x{}</style><script>alert(1)</script><body><p>Hello&nbsp;World</p></body></html>"
        cleaned = gs._strip_html(raw)
        self.assertEqual(cleaned, "Hello World")

    def test_split_sentences_dedupes_and_filters_short(self) -> None:
        text = (
            "Short. "
            "This is a sufficiently long sentence for extraction purposes. "
            "This is a sufficiently long sentence for extraction purposes. "
            "Another long sentence exists for matching too."
        )
        rows = gs._split_sentences(text)
        self.assertEqual(
            rows,
            [
                "This is a sufficiently long sentence for extraction purposes.",
                "Another long sentence exists for matching too.",
            ],
        )

    def test_find_sentences_matches_patterns_with_limit(self) -> None:
        text = (
            "The grading process uses a scale of 1 to 70 for many coins. "
            "Luster and strike are key technical factors in assessment. "
            "This sentence should not match patterns at all."
        )
        rows = gs._find_sentences(text, patterns=[r"\b1 to 70\b", r"\bluster\b"], limit=1)
        self.assertEqual(len(rows), 1)
        self.assertIn("1 to 70", rows[0])

    def test_compact_short_and_long(self) -> None:
        self.assertEqual(gs._compact("abc", limit=10), "abc")
        self.assertEqual(gs._compact("abcdefghij", limit=7), "abcd...")

    def test_contains_any_case_insensitive(self) -> None:
        self.assertTrue(gs._contains_any("Questionable TONING appears", ["toning"]))
        self.assertFalse(gs._contains_any("No markers", ["toning", "altered"]))

    @patch("app.services.grading_standards.requests.get")
    def test_fetch_page_text_success_and_error(self, mock_get: Mock) -> None:
        ok_resp = Mock()
        ok_resp.text = "<p>Hello</p>"
        ok_resp.raise_for_status.return_value = None
        mock_get.return_value = ok_resp

        ok, text = gs._fetch_page_text("https://example.com")
        self.assertTrue(ok)
        self.assertEqual(text, "Hello")

        mock_get.side_effect = RuntimeError("boom")
        ok, text = gs._fetch_page_text("https://example.com")
        self.assertFalse(ok)
        self.assertEqual(text, "")

    @patch("app.services.grading_standards._fetch_page_text")
    def test_fetch_standards_snapshot_detects_indicators_and_extracts(self, mock_fetch: Mock) -> None:
        gs.clear_standards_snapshot_cache()
        payloads = {
            gs.PCGS_GRADES_URL: "MS-63 coins consider strike and luster with surface marks in eye appeal balancing.",
            gs.NGC_GRADING_PROCESS_URL: "NGC uses a scale of 1 to 70 in the grading process with luster and strike factors.",
            gs.NGC_DETAILS_URL: "Details grading applies when improperly cleaned or damaged with corrosion concerns.",
            gs.ANACS_FAQ_URL: "ANACS can grade problem coins and assign detail grade outcomes.",
            gs.ICG_GUARANTEE_URL: "ICG may flag questionable toning and altered surfaces.",
        }

        def _side_effect(url: str, timeout_seconds: int = 8):
            return True, payloads[url]

        mock_fetch.side_effect = _side_effect
        snapshot = gs.fetch_standards_snapshot()
        indicators = snapshot["indicators"]
        self.assertTrue(indicators["sheldon_scale_detected"])
        self.assertTrue(indicators["details_grading_detected"])
        self.assertTrue(indicators["anacs_problem_coin_detected"])
        self.assertTrue(indicators["icg_problem_language_detected"])
        self.assertIn("pcgs", snapshot["extracted"])
        self.assertIn("ngc_details", snapshot["extracted"])

    @patch("app.services.grading_standards.fetch_standards_snapshot")
    def test_build_coin_grading_rules_context_fallback_when_no_snippets(self, mock_snapshot: Mock) -> None:
        mock_snapshot.return_value = {
            "checked_at_utc": "2026-04-02T00:00:00+00:00",
            "indicators": {},
            "extracted": {"pcgs": [], "ngc_process": [], "ngc_details": [], "anacs": [], "icg": []},
            "sources": {
                "pcgs_grades": {"url": gs.PCGS_GRADES_URL},
                "ngc_process": {"url": gs.NGC_GRADING_PROCESS_URL},
                "ngc_details": {"url": gs.NGC_DETAILS_URL},
                "anacs_faq": {"url": gs.ANACS_FAQ_URL},
                "icg_guarantee": {"url": gs.ICG_GUARANTEE_URL},
            },
        }
        result = gs.build_coin_grading_rules_context_from_web()
        self.assertIn("Coin grading baseline", result)
        self.assertIn("Fallback note", result)

    @patch("app.services.grading_standards.fetch_standards_snapshot")
    def test_build_coin_grading_rules_context_with_snippets(self, mock_snapshot: Mock) -> None:
        mock_snapshot.return_value = {
            "checked_at_utc": "2026-04-02T00:00:00+00:00",
            "indicators": {
                "sheldon_scale_detected": True,
                "details_grading_detected": True,
                "anacs_problem_coin_detected": False,
                "icg_problem_language_detected": False,
            },
            "extracted": {
                "pcgs": ["PCGS line about luster and strike in grading."],
                "ngc_process": ["NGC process mentions scale 1 to 70 for grading."],
                "ngc_details": ["NGC details line for cleaned/damaged coins."],
                "anacs": [],
                "icg": [],
            },
            "sources": {
                "pcgs_grades": {"url": gs.PCGS_GRADES_URL},
                "ngc_process": {"url": gs.NGC_GRADING_PROCESS_URL},
                "ngc_details": {"url": gs.NGC_DETAILS_URL},
                "anacs_faq": {"url": gs.ANACS_FAQ_URL},
                "icg_guarantee": {"url": gs.ICG_GUARANTEE_URL},
            },
        }
        result = gs.build_coin_grading_rules_context_from_web()
        self.assertIn("Web snapshot", result)
        self.assertIn("Service digests", result)
        self.assertIn("PCGS", result)

    @patch("app.services.grading_standards.fetch_standards_snapshot")
    def test_build_comp_rules_context_with_and_without_signals(self, mock_snapshot: Mock) -> None:
        mock_snapshot.return_value = {
            "checked_at_utc": "2026-04-02T00:00:00+00:00",
            "indicators": {
                "sheldon_scale_detected": True,
                "details_grading_detected": True,
                "anacs_problem_coin_detected": True,
                "icg_problem_language_detected": True,
            },
            "extracted": {
                "pcgs": ["PCGS comp example sentence."],
                "ngc_details": ["NGC details comp sentence."],
                "anacs": ["ANACS problem-coin comp sentence."],
                "icg": ["ICG altered surface caution sentence."],
            },
            "sources": {
                "pcgs_grades": {"url": gs.PCGS_GRADES_URL},
                "ngc_details": {"url": gs.NGC_DETAILS_URL},
                "anacs_faq": {"url": gs.ANACS_FAQ_URL},
                "icg_guarantee": {"url": gs.ICG_GUARANTEE_URL},
            },
        }
        result = gs.build_comp_rules_context_from_web()
        self.assertIn("Comp baseline", result)
        self.assertIn("Web snapshot", result)
        self.assertIn("PCGS", result)

        mock_snapshot.return_value = {
            "checked_at_utc": "2026-04-02T00:00:00+00:00",
            "indicators": {},
            "extracted": {"pcgs": [], "ngc_details": [], "anacs": [], "icg": []},
            "sources": {
                "pcgs_grades": {"url": gs.PCGS_GRADES_URL},
                "ngc_details": {"url": gs.NGC_DETAILS_URL},
                "anacs_faq": {"url": gs.ANACS_FAQ_URL},
                "icg_guarantee": {"url": gs.ICG_GUARANTEE_URL},
            },
        }
        fallback = gs.build_comp_rules_context_from_web()
        self.assertIn("Fallback note", fallback)


if __name__ == "__main__":
    unittest.main()

