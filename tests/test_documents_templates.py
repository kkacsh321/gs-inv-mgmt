import unittest

from app.components.document_templates import build_document_html


class DocumentTemplateTests(unittest.TestCase):
    def test_build_invoice_html_includes_core_fields(self) -> None:
        html = build_document_html(
            doc_type="invoice",
            template_name="Classic",
            accent_color="#000000",
            company_name="GoldenStackers",
            company_email="ops@goldenstackers.com",
            company_phone="+1-000-000-0000",
            company_website="https://goldenstackers.com",
            customer_label="Marketplace Buyer",
            document_number="INV-20260323-1",
            document_date="2026-03-23",
            source_label="Order",
            source_number="EBAY-123",
            source_marketplace="ebay",
            sold_at="2026-03-23T10:00:00",
            notes="Thank you.",
            items=[
                {"sku": "GS-001", "title": "Silver Coin", "qty": 1, "unit_price": 35.0, "line_total": 35.0}
            ],
            subtotal=35.0,
            fees=3.0,
            shipping_cost=5.0,
            total=35.0,
        )
        self.assertIn("INVOICE", html)
        self.assertIn("INV-20260323-1", html)
        self.assertIn("Silver Coin", html)
        self.assertIn("window.print()", html)


if __name__ == "__main__":
    unittest.main()
