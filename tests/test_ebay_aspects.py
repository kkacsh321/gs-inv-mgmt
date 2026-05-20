import unittest

from app.services.ebay_aspects import (
    _fineness_from_text,
    _first,
    _norm_key,
    _shape_from_title,
    _weight_label_oz,
    aspects_have_approved_grader_evidence,
    is_bullion_like_product,
    merge_ebay_aspects_defaults,
    missing_required_ebay_aspects,
    normalize_ebay_category_aspect_rows,
    title_has_numerical_coin_grade,
)


class EbayAspectsTests(unittest.TestCase):
    def test_helpers_normalize_and_extract_shapes(self) -> None:
        self.assertEqual(_norm_key("  Fine   Silver  "), "fine silver")
        self.assertEqual(_first(["", "  ", "abc", "z"]), "abc")
        self.assertEqual(_first(["", "  "]), "")
        self.assertEqual(_shape_from_title("Vintage Copper Bar"), "Bar")
        self.assertEqual(_shape_from_title("1 oz Silver Round"), "Round")
        self.assertEqual(_shape_from_title("Morgan Coin"), "Coin")
        self.assertEqual(_shape_from_title("unknown token"), "Round")

    def test_helpers_weight_and_fineness_paths(self) -> None:
        self.assertEqual(_weight_label_oz(None), "")
        self.assertEqual(_weight_label_oz("0"), "")
        self.assertEqual(_weight_label_oz("2"), "2 oz")
        self.assertEqual(_weight_label_oz("2.5"), "2.5 oz")
        self.assertEqual(_fineness_from_text("fine .999 silver"), "0.999")
        self.assertEqual(_fineness_from_text("fineness 0.925"), "0.925")
        self.assertEqual(_fineness_from_text("no fineness"), "")

    def test_bullion_like_detection_paths(self) -> None:
        self.assertTrue(
            is_bullion_like_product(
                category="misc",
                metal_type="silver",
                title="Decorative piece",
            )
        )
        self.assertTrue(
            is_bullion_like_product(
                category="coins",
                metal_type="unknown",
                title="Decorative piece",
            )
        )
        self.assertTrue(
            is_bullion_like_product(
                category="misc",
                metal_type="unknown",
                title="1 oz generic round",
            )
        )
        self.assertFalse(
            is_bullion_like_product(
                category="misc",
                metal_type="unknown",
                title="paper invoice",
            )
        )

    def test_merge_defaults_adds_circulated_uncirculated(self) -> None:
        payload, added = merge_ebay_aspects_defaults(
            category="bullion",
            metal_type="copper",
            title="1 oz Copper Round",
            weight_oz=1,
            existing_aspects={},
        )
        self.assertIn("Circulated/Uncirculated", payload)
        self.assertEqual(payload["Circulated/Uncirculated"], ["Uncirculated"])
        self.assertIn("Circulated/Uncirculated", added)

    def test_existing_circulated_uncirculated_is_preserved(self) -> None:
        payload, added = merge_ebay_aspects_defaults(
            category="bullion",
            metal_type="copper",
            title="1 oz Copper Round",
            weight_oz=1,
            existing_aspects={"Circulated/Uncirculated": ["Circulated"]},
        )
        self.assertEqual(payload["Circulated/Uncirculated"], ["Circulated"])
        self.assertNotIn("Circulated/Uncirculated", added)

    def test_merge_defaults_non_bullion_returns_source_unchanged(self) -> None:
        original = {"Custom Field": ["Value"]}
        payload, added = merge_ebay_aspects_defaults(
            category="paper",
            metal_type="plastic",
            title="invoice sheet",
            weight_oz=0,
            existing_aspects=original,
        )
        self.assertEqual(payload, original)
        self.assertEqual(added, [])

    def test_merge_defaults_preserves_existing_keys_case_insensitive(self) -> None:
        payload, added = merge_ebay_aspects_defaults(
            category="bullion",
            metal_type="silver",
            title="1 oz silver bar",
            weight_oz=1,
            existing_aspects={" certification ": ["PCGS"], "unit type": ["gram"]},
        )
        self.assertEqual(payload[" certification "], ["PCGS"])
        self.assertEqual(payload["unit type"], ["gram"])
        self.assertNotIn("Certification", added)
        self.assertNotIn("Unit Type", added)

    def test_merge_defaults_infers_approved_grader_and_grade_from_title(self) -> None:
        payload, added = merge_ebay_aspects_defaults(
            category="coins",
            metal_type="silver",
            title="Bradford exchange 2021 Silver Eagle PCGS MS69 First Year Type 2 in box",
            weight_oz=1,
            existing_aspects={},
        )

        self.assertEqual(payload["Certification"], ["PCGS"])
        self.assertEqual(payload["Grade"], ["MS 69"])
        self.assertEqual(payload["Professional Grader"], ["Professional Coin Grading Service (PCGS)"])
        self.assertIn("Grade", added)
        self.assertIn("Professional Grader", added)

    def test_grader_policy_helpers_detect_grade_and_evidence(self) -> None:
        self.assertTrue(title_has_numerical_coin_grade("2021 Silver Eagle PCGS MS69"))
        self.assertTrue(title_has_numerical_coin_grade("Proof coin PF-70"))
        self.assertFalse(title_has_numerical_coin_grade("2021 Silver Eagle Type 2"))
        self.assertTrue(aspects_have_approved_grader_evidence({"Certification": ["PCGS"]}))
        self.assertTrue(
            aspects_have_approved_grader_evidence(
                {"Professional Grader": ["Professional Coin Grading Service (PCGS)"]}
            )
        )
        self.assertFalse(aspects_have_approved_grader_evidence({"Certification": ["Uncertified"]}))

    def test_merge_defaults_repairs_uncertified_when_title_has_approved_grader(self) -> None:
        payload, added = merge_ebay_aspects_defaults(
            category="coins",
            metal_type="silver",
            title="2021 Silver Eagle PCGS MS69 Type 2",
            weight_oz=1,
            existing_aspects={"Certification": ["Uncertified"]},
        )

        self.assertEqual(payload["Certification"], ["PCGS"])
        self.assertEqual(payload["Grade"], ["MS 69"])
        self.assertIn("Certification", added)

    def test_merge_defaults_preserves_existing_certified_grader(self) -> None:
        payload, added = merge_ebay_aspects_defaults(
            category="coins",
            metal_type="silver",
            title="2021 Silver Eagle PCGS MS69 Type 2",
            weight_oz=1,
            existing_aspects={"Certification": ["NGC"], "Grade": ["MS 68"]},
        )

        self.assertEqual(payload["Certification"], ["NGC"])
        self.assertEqual(payload["Grade"], ["MS 68"])
        self.assertNotIn("Certification", added)
        self.assertNotIn("Grade", added)

    def test_merge_defaults_fineness_default_and_weight_branch(self) -> None:
        payload, added = merge_ebay_aspects_defaults(
            category="bullion",
            metal_type="copper",
            title="collectible copper piece",
            weight_oz="not-a-number",
            existing_aspects={},
        )
        self.assertEqual(payload["Fineness"], ["0.999"])
        self.assertNotIn("Precious Metal Content per Unit", payload)
        self.assertIn("Fineness", added)

    def test_normalize_category_aspect_rows_extracts_constraints_and_values(self) -> None:
        rows = normalize_ebay_category_aspect_rows(
            [
                {
                    "localizedAspectName": "Brand",
                    "aspectConstraint": {
                        "aspectRequired": True,
                        "aspectUsage": "RECOMMENDED",
                        "aspectMode": "FREE_TEXT",
                        "itemToAspectCardinality": "SINGLE",
                    },
                    "aspectValues": [{"localizedValue": "US Mint"}, {"localizedValue": "US Mint"}],
                },
                {"localizedAspectName": ""},
                "bad",
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Brand")
        self.assertTrue(rows[0]["required"])
        self.assertEqual(rows[0]["usage"], "RECOMMENDED")
        self.assertEqual(rows[0]["values"], ["US Mint"])

    def test_missing_required_ebay_aspects_uses_filled_existing_values(self) -> None:
        rows = normalize_ebay_category_aspect_rows(
            [
                {
                    "localizedAspectName": "Brand",
                    "aspectConstraint": {"aspectRequired": "true"},
                    "aspectValues": [],
                },
                {
                    "localizedAspectName": "Color",
                    "aspectConstraint": {"aspectRequired": True},
                    "aspectValues": [{"localizedValue": "Red"}],
                },
                {
                    "localizedAspectName": "Size",
                    "aspectConstraint": {"aspectRequired": False},
                    "aspectValues": [],
                },
            ]
        )
        missing = missing_required_ebay_aspects(rows, {" brand ": ["US Mint"], "Color": []})
        self.assertEqual([row["name"] for row in missing], ["Color"])


if __name__ == "__main__":
    unittest.main()
