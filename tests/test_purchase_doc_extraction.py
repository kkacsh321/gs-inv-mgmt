import json
import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from app.services import purchase_doc_extraction as pde
except ModuleNotFoundError as exc:
    if exc.name != "boto3":
        raise
    fake_boto3 = types.ModuleType("boto3")
    fake_session_ns = types.SimpleNamespace(Session=lambda *args, **kwargs: None)
    fake_boto3.session = fake_session_ns
    sys.modules.setdefault("boto3", fake_boto3)
    pde = importlib.import_module("app.services.purchase_doc_extraction")


class PurchaseDocExtractionTests(unittest.TestCase):
    def test_to_decimal_like(self) -> None:
        self.assertEqual(pde._to_decimal_like("$12.34"), 12.34)
        self.assertEqual(pde._to_decimal_like(" -5 "), -5.0)
        self.assertIsNone(pde._to_decimal_like(""))
        self.assertIsNone(pde._to_decimal_like(None))
        self.assertIsNone(pde._to_decimal_like("n/a"))
        self.assertIsNone(pde._to_decimal_like("1.2.3"))

    def test_summary_fields_to_payload(self) -> None:
        payload = pde._summary_fields_to_payload(
            [
                {"Type": {"Text": "VENDOR_NAME"}, "ValueDetection": {"Text": "APMEX"}},
                {"Type": {"Text": "VENDOR_NAME"}, "ValueDetection": {"Text": "SHOULD-NOT-OVERRIDE"}},
                {"Type": {"Text": "INVOICE_RECEIPT_ID"}, "ValueDetection": {"Text": "INV-123"}},
                {"Type": {"Text": "TOTAL"}, "ValueDetection": {"Text": "$1,234.56"}},
                {"Type": {"Text": "TAX"}, "ValueDetection": {"Text": "$12.34"}},
                {"Type": {"Text": "CURRENCY"}, "ValueDetection": {"Text": "USD"}},
            ]
        )
        self.assertEqual(payload["vendor_name"], "APMEX")
        self.assertEqual(payload["invoice_number"], "INV-123")
        self.assertEqual(payload["total"], 1234.56)
        self.assertEqual(payload["tax"], 12.34)
        self.assertEqual(payload["currency"], "USD")
        self.assertEqual(payload["provider"], "aws_textract")

    def test_line_items_to_payload(self) -> None:
        out = pde._line_items_to_payload(
            [
                {
                    "LineItems": [
                        {
                            "LineItemExpenseFields": [
                                {"Type": {"Text": "ITEM"}, "ValueDetection": {"Text": "1 oz Silver Bar"}},
                                {"Type": {"Text": "QUANTITY"}, "ValueDetection": {"Text": "3"}},
                                {"Type": {"Text": "UNIT_PRICE"}, "ValueDetection": {"Text": "$35.50"}},
                                {"Type": {"Text": "AMOUNT"}, "ValueDetection": {"Text": "$106.50"}},
                            ]
                        }
                    ]
                }
            ]
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["description"], "1 oz Silver Bar")
        self.assertEqual(out[0]["quantity"], 3.0)
        self.assertEqual(out[0]["unit_price"], 35.5)
        self.assertEqual(out[0]["line_total"], 106.5)

    def test_line_items_to_payload_fallback_description_and_skip_empty(self) -> None:
        out = pde._line_items_to_payload(
            [
                {
                    "LineItems": [
                        {
                            "LineItemExpenseFields": [
                                {"Type": {"Text": "QTY"}, "ValueDetection": {"Text": "2"}},
                                {"Type": {"Text": "AMOUNT"}, "ValueDetection": {"Text": ""}},
                            ]
                        },
                        {
                            "LineItemExpenseFields": [
                                {"Type": {"Text": "ITEM"}, "ValueDetection": {"Text": ""}},
                            ]
                        },
                    ]
                }
            ]
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["description"], "Line item")
        self.assertEqual(out[0]["quantity"], 2.0)

    def test_merge_payloads_prefers_primary_non_empty(self) -> None:
        primary = {
            "vendor_name": "Primary Vendor",
            "invoice_number": "",
            "line_items": [{"description": "Primary Item"}],
            "tax": None,
        }
        fallback = {
            "vendor_name": "Fallback Vendor",
            "invoice_number": "INV-FB",
            "line_items": [{"description": "Fallback Item"}],
            "tax": 8.0,
        }
        merged = pde._merge_payloads(primary, fallback)
        self.assertEqual(merged["vendor_name"], "Primary Vendor")
        self.assertEqual(merged["invoice_number"], "INV-FB")
        self.assertEqual(merged["line_items"][0]["description"], "Primary Item")
        self.assertEqual(merged["tax"], 8.0)

    def test_merge_payloads_keeps_empty_string_only_when_key_missing(self) -> None:
        merged = pde._merge_payloads(
            {"invoice_number": "", "notes": ""},
            {"vendor_name": "Fallback"},
        )
        self.assertIn("invoice_number", merged)
        self.assertIn("notes", merged)
        self.assertEqual(merged["invoice_number"], "")

    def test_extract_with_textract_requires_bytes(self) -> None:
        with self.assertRaisesRegex(ValueError, "File bytes are required"):
            pde.extract_with_textract(b"", "application/pdf")

    @patch("app.services.purchase_doc_extraction.boto3.session.Session")
    def test_extract_with_textract_maps_response(self, session_cls: Mock) -> None:
        client = Mock()
        client.analyze_expense.return_value = {
            "ExpenseDocuments": [
                {
                    "SummaryFields": [
                        {"Type": {"Text": "VENDOR_NAME"}, "ValueDetection": {"Text": "Bullion Dealer"}},
                        {"Type": {"Text": "INVOICE_RECEIPT_ID"}, "ValueDetection": {"Text": "INV-777"}},
                        {"Type": {"Text": "TOTAL"}, "ValueDetection": {"Text": "$150.00"}},
                        {"Type": {"Text": "SHIPPING_HANDLING_CHARGE"}, "ValueDetection": {"Text": "$10.00"}},
                    ],
                    "LineItemGroups": [
                        {
                            "LineItems": [
                                {
                                    "LineItemExpenseFields": [
                                        {"Type": {"Text": "ITEM"}, "ValueDetection": {"Text": "Silver Shot"}},
                                        {"Type": {"Text": "QUANTITY"}, "ValueDetection": {"Text": "20"}},
                                        {"Type": {"Text": "AMOUNT"}, "ValueDetection": {"Text": "$140.00"}},
                                    ]
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        session = Mock()
        session.client.return_value = client
        session_cls.return_value = session

        with patch(
            "app.services.purchase_doc_extraction.settings",
            SimpleNamespace(aws_access_key_id="", aws_secret_access_key="", aws_region="us-east-1"),
        ):
            result = pde.extract_with_textract(file_bytes=b"pdf-bytes", content_type="application/pdf")

        self.assertEqual(result.raw_provider, "aws_textract")
        self.assertEqual(result.payload["vendor_name"], "Bullion Dealer")
        self.assertEqual(result.payload["invoice_number"], "INV-777")
        self.assertEqual(result.payload["shipping"], 10.0)
        self.assertEqual(len(result.payload["line_items"]), 1)
        parsed_summary = json.loads(result.summary_text)
        self.assertEqual(parsed_summary["vendor_name"], "Bullion Dealer")
        client.analyze_expense.assert_called_once()

    @patch("app.services.purchase_doc_extraction.boto3.session.Session")
    def test_extract_with_textract_no_documents(self, session_cls: Mock) -> None:
        client = Mock()
        client.analyze_expense.return_value = {"ExpenseDocuments": []}
        session = Mock()
        session.client.return_value = client
        session_cls.return_value = session
        with patch(
            "app.services.purchase_doc_extraction.settings",
            SimpleNamespace(aws_access_key_id="", aws_secret_access_key="", aws_region="us-east-1"),
        ):
            with self.assertRaisesRegex(RuntimeError, "no ExpenseDocuments"):
                pde.extract_with_textract(file_bytes=b"x", content_type="application/pdf")

    def test_merge_llm_and_textract_sets_provider(self) -> None:
        merged = pde.merge_llm_and_textract(
            {"vendor_name": "LLM", "line_items": [{"description": "L"}]},
            {"vendor_name": "TX", "invoice_number": "123"},
        )
        self.assertEqual(merged["provider"], "llm+aws_textract")
        self.assertEqual(merged["vendor_name"], "LLM")
        self.assertEqual(merged["invoice_number"], "123")


if __name__ == "__main__":
    unittest.main()
