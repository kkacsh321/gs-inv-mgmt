import unittest
from unittest.mock import patch

from app.services.ai_accountant_web import search_ai_accountant_web, should_run_ai_accountant_web_research


class _Resp:
    text = """
    <html><body>
      <a class="result__a" href="/l/?uddg=https%3A%2F%2Ftax.example%2Fcoin-tax">Coin tax guide</a>
      <a class="result__snippet">State bullion exemption summary</a>
    </body></html>
    """

    def raise_for_status(self):
        return None


class AIAccountantWebTests(unittest.TestCase):
    def test_should_run_ai_accountant_web_research_for_tax_questions(self):
        self.assertTrue(should_run_ai_accountant_web_research("What is the state tax treatment for bullion?"))
        self.assertFalse(should_run_ai_accountant_web_research("Why did profit drop?"))

    def test_search_ai_accountant_web_parses_duckduckgo_html(self):
        with patch("app.services.ai_accountant_web.requests.get", return_value=_Resp()) as req:
            rows = search_ai_accountant_web("state bullion tax", limit=3)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Coin tax guide")
        self.assertEqual(rows[0]["url"], "https://tax.example/coin-tax")
        self.assertIn("bullion exemption", rows[0]["snippet"])
        self.assertEqual(req.call_args.kwargs["params"]["q"], "state bullion tax")


if __name__ == "__main__":
    unittest.main()
