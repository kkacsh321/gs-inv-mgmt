import unittest
from decimal import Decimal
from types import SimpleNamespace

from app.services.security import hash_password, verify_password
from app.services.validation import ValidationError, ValidationService


class _FakeDB:
    def __init__(self, scalar_result=None):
        self.scalar_result = scalar_result

    def scalar(self, _query):
        return self.scalar_result


class SecurityValidationTests(unittest.TestCase):
    def test_hash_password_requires_min_length(self) -> None:
        with self.assertRaises(ValueError):
            hash_password("short")

    def test_hash_password_and_verify_roundtrip(self) -> None:
        pw_hash, salt = hash_password("long-password")
        self.assertTrue(verify_password("long-password", pw_hash, salt))
        self.assertFalse(verify_password("wrong-password", pw_hash, salt))

    def test_verify_password_handles_missing_inputs(self) -> None:
        self.assertFalse(verify_password("x", "", "abc"))
        self.assertFalse(verify_password("x", "abc", ""))

    def test_basic_validation_guards(self) -> None:
        ValidationService.require_non_empty("Name", "ok")
        with self.assertRaises(ValidationError):
            ValidationService.require_non_empty("Name", "")

        ValidationService.require_positive_int("Qty", 2)
        with self.assertRaises(ValidationError):
            ValidationService.require_positive_int("Qty", 0)

        ValidationService.require_non_negative_decimal("Price", Decimal("1.00"))
        ValidationService.require_non_negative_decimal("Price", None)
        with self.assertRaises(ValidationError):
            ValidationService.require_non_negative_decimal("Price", Decimal("-1.00"))

    def test_tracking_number_and_shipping_date_rules(self) -> None:
        ValidationService.validate_tracking_number("TRACK-12345")
        with self.assertRaises(ValidationError):
            ValidationService.validate_tracking_number("bad!")

        ValidationService.validate_sale_tracking_requirements("label_created", "")
        with self.assertRaises(ValidationError):
            ValidationService.validate_sale_tracking_requirements("delivered", "")

        ValidationService.validate_shipping_dates("label_created", None, None)
        with self.assertRaises(ValidationError):
            ValidationService.validate_shipping_dates("delivered", None, None)

    def test_unique_marketplace_listing_and_order_rules(self) -> None:
        ValidationService.ensure_unique_marketplace_listing(_FakeDB(None), "ebay", "X1")
        with self.assertRaises(ValidationError):
            ValidationService.ensure_unique_marketplace_listing(_FakeDB(SimpleNamespace(id=1)), "ebay", "X1")

        ValidationService.ensure_unique_marketplace_order(_FakeDB(None), "ebay", "O1")
        with self.assertRaises(ValidationError):
            ValidationService.ensure_unique_marketplace_order(_FakeDB(SimpleNamespace(id=1)), "ebay", "O1")

    def test_tracking_number_reuse_rules(self) -> None:
        ValidationService.ensure_tracking_number_not_reused(_FakeDB(None), "T1", "ORD-1")

        existing_same_order = SimpleNamespace(id=10, external_order_id="ORD-1")
        ValidationService.ensure_tracking_number_not_reused(_FakeDB(existing_same_order), "T1", "ORD-1")

        existing_other_order = SimpleNamespace(id=11, external_order_id="ORD-2")
        with self.assertRaises(ValidationError):
            ValidationService.ensure_tracking_number_not_reused(_FakeDB(existing_other_order), "T1", "ORD-1")

    def test_validate_listing_workflow_rules(self) -> None:
        ValidationService.validate_listing_workflow(
            listing_title="Item",
            listing_price=Decimal("10"),
            quantity_listed=1,
            listing_status="draft",
            media_count=0,
        )

        with self.assertRaises(ValidationError):
            ValidationService.validate_listing_workflow(
                listing_title="",
                listing_price=Decimal("10"),
                quantity_listed=1,
                listing_status="draft",
            )

        with self.assertRaises(ValidationError):
            ValidationService.validate_listing_workflow(
                listing_title="Item",
                listing_price=Decimal("0"),
                quantity_listed=1,
                listing_status="draft",
            )

        with self.assertRaises(ValidationError):
            ValidationService.validate_listing_workflow(
                listing_title="Item",
                listing_price=Decimal("10"),
                quantity_listed=1,
                listing_status="active",
                media_count=0,
            )

        with self.assertRaises(ValidationError):
            ValidationService.validate_listing_workflow(
                listing_title="Item",
                listing_price=Decimal("10"),
                quantity_listed=1,
                listing_status="active",
                media_count=1,
                external_listing_id="",
                marketplace_url="",
            )


if __name__ == "__main__":
    unittest.main()
