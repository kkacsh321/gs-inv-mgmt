import base64
import json
import sys
import unittest
from datetime import timedelta
from typing import Any
from types import SimpleNamespace
from unittest.mock import patch

from app.services import integration_queue
from app.utils.time import utcnow_naive


class _FakeDB:
    def __init__(self, rows: dict[int, object] | None = None) -> None:
        self.rows = rows or {}

    def get(self, _model, row_id: int):
        return self.rows.get(int(row_id))


class _FakeRepo:
    def __init__(self) -> None:
        self.db = _FakeDB()
        self.updated_sales: list[tuple[int, dict, str]] = []
        self.updated_jobs: list[tuple[int, dict, str]] = []
        self.logged_events: list[dict] = []
        self.queue_rows: list[object] = []
        self.created_media: list[dict] = []
        self.created_documents: list[dict] = []
        self.created_listings: list[dict] = []
        self.created_products: list[dict] = []

    def update_sale(self, sale_id: int, updates: dict, *, actor: str):
        self.updated_sales.append((sale_id, updates, actor))

    def update_integration_queue_job(self, job_id: int, updates: dict, *, actor: str):
        self.updated_jobs.append((job_id, updates, actor))
        row = self.db.get(None, int(job_id))
        if row is not None:
            for key, value in updates.items():
                setattr(row, key, value)

    def log_integration_event(self, **kwargs):
        self.logged_events.append(kwargs)

    def list_integration_queue_jobs(self, **_kwargs):
        return list(self.queue_rows)

    def create_media_asset(self, **kwargs):
        media_row = dict(kwargs)
        media_row["id"] = len(self.created_media) + 1
        self.created_media.append(media_row)
        return SimpleNamespace(**media_row)

    def create_purchase_document(self, **kwargs):
        self.created_documents.append(dict(kwargs))
        return SimpleNamespace(id=len(self.created_documents), **kwargs)

    def dashboard_metrics(self):
        return {
            "product_count": 12,
            "listing_count": 5,
            "sale_count": 3,
            "inventory_cost": 1234.56,
        }

    def create_listing(self, **kwargs):
        self.created_listings.append(dict(kwargs))
        listing_id = len(self.created_listings)
        return SimpleNamespace(id=listing_id, **kwargs)

    def create_product(self, **kwargs):
        self.created_products.append(dict(kwargs))
        product_id = 100 + len(self.created_products)
        return SimpleNamespace(id=product_id, **kwargs)

    def bulk_update_media_assets(self, media_ids, updates, actor="system"):
        updated_ids = [int(v) for v in (media_ids or [])]
        for media in self.created_media:
            media_id = int(media.get("id") or 0)
            if media_id in updated_ids:
                media.update(dict(updates or {}))
        return {"updated_ids": updated_ids, "missing_ids": []}


class IntegrationQueueTests(unittest.TestCase):
    def test_calc_backoff_seconds_google_and_shipping(self) -> None:
        with patch("app.services.integration_queue.get_runtime_int", side_effect=[120, 1000]):
            self.assertEqual(integration_queue._calc_backoff_seconds(object(), 1, integration="google"), 240)
        with patch("app.services.integration_queue.get_runtime_int", side_effect=[60, 3600]):
            self.assertEqual(integration_queue._calc_backoff_seconds(object(), 2, integration="shipping"), 240)
        with patch("app.services.integration_queue.get_runtime_int", side_effect=[30, 300]):
            self.assertEqual(integration_queue._calc_backoff_seconds(object(), 3, integration="slack"), 240)

    def test_capture_queue_execute_exception_tolerates_log_errors(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(id=1, integration="google", action="gmail")
        with patch.object(repo, "log_integration_event", side_effect=RuntimeError("log-failed")):
            message = integration_queue._capture_queue_execute_exception(
                repo,
                actor="qa",
                job=job,
                exc=RuntimeError("boom"),
            )
        self.assertIn("boom", message)

    def test_emit_terminal_queue_failure_alert_guardrails(self) -> None:
        repo = _FakeRepo()
        job_google = SimpleNamespace(id=10, integration="google", action="gmail_send_document_email", max_retries=3)
        job_other = SimpleNamespace(id=11, integration="shipping", action="purchase_label", max_retries=3)

        # slack disabled -> no dispatch
        with patch("app.services.integration_queue.resolve_slack_notify_config", return_value=SimpleNamespace(enabled=False)):
            integration_queue._emit_terminal_queue_failure_alert(
                repo, actor="qa", job=job_google, retry_count=4, error_text="x"
            )

        # google integration with both toggles false -> no dispatch
        with patch("app.services.integration_queue.resolve_slack_notify_config", return_value=SimpleNamespace(enabled=True)), patch(
            "app.services.integration_queue.get_runtime_bool", side_effect=[False, False]
        ), patch("app.services.integration_queue.dispatch_slack_alert") as dispatch:
            integration_queue._emit_terminal_queue_failure_alert(
                repo, actor="qa", job=job_google, retry_count=4, error_text="x"
            )
        dispatch.assert_not_called()

        # non-google with general toggle false -> no dispatch
        with patch("app.services.integration_queue.resolve_slack_notify_config", return_value=SimpleNamespace(enabled=True)), patch(
            "app.services.integration_queue.get_runtime_bool", return_value=False
        ), patch("app.services.integration_queue.dispatch_slack_alert") as dispatch2:
            integration_queue._emit_terminal_queue_failure_alert(
                repo, actor="qa", job=job_other, retry_count=4, error_text="x"
            )
        dispatch2.assert_not_called()

        # enabled path -> dispatch called
        with patch("app.services.integration_queue.resolve_slack_notify_config", return_value=SimpleNamespace(enabled=True)), patch(
            "app.services.integration_queue.get_runtime_bool", side_effect=[True, True]
        ), patch("app.services.integration_queue.build_slack_alert_text", return_value="alert"), patch(
            "app.services.integration_queue.dispatch_slack_alert"
        ) as dispatch3:
            integration_queue._emit_terminal_queue_failure_alert(
                repo, actor="qa", job=job_google, retry_count=4, error_text="x"
            )
        dispatch3.assert_called_once()

    def test_execute_integration_queue_job_rejects_unsupported(self) -> None:
        job = SimpleNamespace(integration="other", action="noop", payload_json="{}")
        ok, message = integration_queue.execute_integration_queue_job(_FakeRepo(), job, actor="qa")
        self.assertFalse(ok)
        self.assertIn("Unsupported integration", message)

    def test_execute_integration_queue_job_bad_payload_json(self) -> None:
        job = SimpleNamespace(integration="slack", action="post_message", payload_json="{bad-json")
        with patch("app.services.integration_queue.send_slack_message") as send_slack:
            ok, message = integration_queue.execute_integration_queue_job(_FakeRepo(), job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("Slack post completed", message)
        send_slack.assert_called_once()

    def test_execute_integration_queue_job_slack_post(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            integration="slack",
            action="post_message",
            payload_json=json.dumps({"text": "hello", "channel": "#ops"}),
        )
        with patch("app.services.integration_queue.send_slack_message") as send_slack:
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("Slack post completed", message)
        send_slack.assert_called_once()

    def test_execute_integration_queue_job_slack_unsupported_action(self) -> None:
        job = SimpleNamespace(integration="slack", action="other", payload_json="{}")
        ok, message = integration_queue.execute_integration_queue_job(_FakeRepo(), job, actor="qa")
        self.assertFalse(ok)
        self.assertIn("Unsupported slack action", message)

    def test_execute_integration_queue_job_slack_ops_command_ingest_media(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "raw_payload": {"product_id": 46, "listing_id": 35},
                        "files": [
                            {
                                "name": "coin.jpg",
                                "mimetype": "image/jpeg",
                                "content_b64": base64.b64encode(b"img-bytes").decode("utf-8"),
                            }
                        ],
                    },
                    "request_context": {"app_username": "ops-user"},
                }
            ),
        )
        storage = SimpleNamespace(
            enabled=True,
            ensure_bucket=lambda: None,
            upload_file=lambda **_kwargs: SimpleNamespace(
                bucket="bucket",
                key="media/x-coin.jpg",
                url="https://storage/bucket/media/x-coin.jpg",
                content_type="image/jpeg",
                size_bytes=9,
            ),
        )
        fake_media_module = SimpleNamespace(MediaStorageService=lambda: storage)
        with patch.dict(sys.modules, {"app.services.media_storage": fake_media_module}):
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("media=1", message)
        self.assertEqual(len(repo.created_media), 1)
        self.assertEqual(repo.created_media[0]["product_id"], 46)
        self.assertEqual(repo.created_media[0]["listing_id"], 35)
        self.assertEqual(repo.created_media[0]["uploaded_by"], "ops-user")

    def test_execute_integration_queue_job_slack_ops_command_ingest_document(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "raw_payload": {"product_id": 99, "document_kind": "incoming_invoice"},
                        "files": [
                            {
                                "name": "invoice.pdf",
                                "mimetype": "application/pdf",
                                "content_b64": base64.b64encode(b"pdf-bytes").decode("utf-8"),
                            }
                        ],
                    },
                    "request_context": {"slack_username": "keith"},
                }
            ),
        )
        storage = SimpleNamespace(
            enabled=True,
            ensure_bucket=lambda: None,
            upload_file=lambda **_kwargs: SimpleNamespace(
                bucket="bucket",
                key="media/x-invoice.pdf",
                url="https://storage/bucket/media/x-invoice.pdf",
                content_type="application/pdf",
                size_bytes=9,
            ),
        )
        fake_media_module = SimpleNamespace(MediaStorageService=lambda: storage)
        with patch.dict(sys.modules, {"app.services.media_storage": fake_media_module}):
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("documents=1", message)
        self.assertEqual(len(repo.created_documents), 1)
        self.assertEqual(repo.created_documents[0]["product_id"], 99)
        self.assertEqual(repo.created_documents[0]["document_kind"], "incoming_invoice")
        self.assertEqual(repo.created_documents[0]["uploaded_by"], "keith")

    def test_execute_integration_queue_job_slack_ops_command_ingest_no_files(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(integration="slack_ops", action="command_ingest", payload_json=json.dumps({"command": {}}))
        ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("no file attachments", message)

    def test_execute_integration_queue_job_slack_ops_comp_ai_summary_persisted(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=77,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1909 vdb penny",
                        "raw_payload": {"product_id": 46},
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[77] = job

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=lambda *_args, **_kwargs: SimpleNamespace(text="Suggested range: $120-$150"),
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )
        fake_ebay_module = SimpleNamespace(
            EbayClient=lambda: SimpleNamespace(
                is_configured=lambda: False,
                search_sold_items_html=lambda **_kwargs: [],
            )
        )
        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ):
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("AI summary generated", message)
        payload = json.loads(str(job.payload_json))
        self.assertEqual(payload["ai_response"]["intent"], "comp")
        self.assertIn("**Suggested Range:** Unavailable", payload["ai_response"]["summary"])
        self.assertIn("Product #46", payload["ai_response"]["links"])
        self.assertIn("eBay rows: 0", payload["ai_response"]["links"])

    def test_execute_integration_queue_job_slack_ops_comp_fetches_ebay_rows_when_configured(self) -> None:
        repo = _FakeRepo()
        captured = {"ebay_rows": [], "query": ""}
        job = SimpleNamespace(
            id=92,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1oz silver bar ampex",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[92] = job

        def _exec_comp(*_args, **kwargs):
            captured["ebay_rows"] = list(kwargs.get("ebay_rows") or [])
            captured["query"] = str(kwargs.get("query") or "")
            return SimpleNamespace(text="Comp summary from fetched rows")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return True

            def search_sold_items_html(self, **kwargs):
                keywords = str(kwargs.get("keywords") or "").strip().lower()
                if "apmex" in keywords:
                    return [{"title": "APMEX 1 oz Silver Bar", "sold_price": 39.99, "shipping_cost": 4.99}]
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)
        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ):
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("AI summary generated", message)
        self.assertEqual(len(captured["ebay_rows"]), 1)
        self.assertEqual(captured["query"], "1oz silver bar ampex")
        payload = json.loads(str(job.payload_json))
        self.assertIn("eBay rows: 1", payload["ai_response"]["links"])
        self.assertIn("Query: 1oz silver bar ampex", payload["ai_response"]["links"])
        self.assertIn("Comps:", payload["ai_response"]["summary"])
        self.assertIn("Top comps:", payload["ai_response"]["summary"])

    def test_execute_integration_queue_job_slack_ops_comp_honors_overrides(self) -> None:
        repo = _FakeRepo()
        seen_calls: list[dict[str, Any]] = []
        job = SimpleNamespace(
            id=93,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1oz silver bar ampex sold_only=false limit=10 category_id=1111",
                        "args": ["1oz", "silver", "bar", "ampex", "sold_only=false", "limit=10", "category_id=1111"],
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[93] = job

        def _exec_comp(*_args, **_kwargs):
            return SimpleNamespace(text="Comp summary from override rows")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return True

            def search_sold_items_html(self, **kwargs):
                seen_calls.append(dict(kwargs))
                return [{"title": "Row", "sold_price": 20.0}]

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)
        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertGreaterEqual(len(seen_calls), 1)
        first = seen_calls[0]
        self.assertEqual(int(first.get("limit") or 0), 10)

    def test_execute_integration_queue_job_slack_ops_comp_uses_runtime_band_percentages(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=931,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1oz silver bar",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[931] = job

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=lambda *_args, **_kwargs: SimpleNamespace(text="Comp summary"),
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return True

            def search_sold_items_html(self, **_kwargs):
                return [{"title": "1 oz Silver Bar", "sold_price": 40.0, "shipping_cost": 4.0}]

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)
        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch(
            "app.services.integration_queue.get_runtime_bool",
            side_effect=lambda _repo, key, default=False: (
                False
                if str(key)
                in {"slack_ops_comp_min_qualified_rows_gate_enabled", "slack_ops_comp_min_confidence_gate_enabled"}
                else default
            ),
        ), patch(
            "app.services.integration_queue.get_runtime_float",
            side_effect=lambda _repo, key, default: (
                85.0
                if str(key).endswith("_low_pct")
                else (120.0 if str(key).endswith("_high_pct") else default)
            ),
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        payload = json.loads(str(job.payload_json))
        self.assertIn("Suggested list band $37.40-$52.80", payload["ai_response"]["summary"])
        self.assertIn("Qualified comps: 1", payload["ai_response"]["summary"])
        self.assertIn("Distinct sources: 1", payload["ai_response"]["summary"])

    def test_execute_integration_queue_job_slack_ops_comp_web_fallback_rows(self) -> None:
        repo = _FakeRepo()
        captured = {"web_rows": []}
        job = SimpleNamespace(
            id=94,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1oz silver round",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[94] = job

        def _exec_comp(*_args, **kwargs):
            captured["web_rows"] = list(kwargs.get("web_rows") or [])
            return SimpleNamespace(text="Comp summary with web fallback")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://example.com/item1">1 oz Silver Round Deal</a>
        <div class="result__snippet">Great round for only $29.99 shipped</div>
        """
        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.requests.get", return_value=_Resp(ddg_html)):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertGreaterEqual(len(captured["web_rows"]), 1)
        self.assertGreater(float(captured["web_rows"][0].get("listed_price") or 0), 0.0)

    def test_execute_integration_queue_job_slack_ops_comp_web_fallback_prefers_structured_page_price(self) -> None:
        repo = _FakeRepo()
        captured = {"web_rows": []}
        job = SimpleNamespace(
            id=941,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar apmex .999",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[941] = job

        def _exec_comp(*_args, **kwargs):
            captured["web_rows"] = list(kwargs.get("web_rows") or [])
            return SimpleNamespace(text="Comp summary with structured web fallback")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.apmex.com%2Fproduct%2F12345%2Fsample">Buy 1 oz Silver Bars | Free Shipping on Orders $199+</a>
        <div class="result__snippet">Free Shipping on Orders $199+ at APMEX</div>
        """
        product_html = """
        <html><head><meta property="product:price:amount" content="90.70"></head><body>Sample</body></html>
        """
        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch(
            "app.services.integration_queue.requests.get",
            side_effect=[_Resp(ddg_html), _Resp(product_html)],
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertGreaterEqual(len(captured["web_rows"]), 1)
        first = captured["web_rows"][0]
        self.assertAlmostEqual(float(first.get("listed_price") or 0), 90.70, places=2)
        self.assertEqual(str(first.get("price_hint_source") or ""), "structured_page")

    def test_execute_integration_queue_job_slack_ops_comp_web_fallback_extracts_jsonld_offer_price(self) -> None:
        repo = _FakeRepo()
        captured = {"web_rows": []}
        job = SimpleNamespace(
            id=946,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar random design",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[946] = job

        def _exec_comp(*_args, **kwargs):
            captured["web_rows"] = list(kwargs.get("web_rows") or [])
            return SimpleNamespace(text="Comp summary with jsonld extraction")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://www.apmex.com/product/12345/sample">1 oz Silver Bar Random Design</a>
        <div class="result__snippet">Current inventory listing</div>
        """
        product_html = """
        <html>
          <head>
            <script type="application/ld+json">
              {
                "@context":"https://schema.org",
                "@type":"Product",
                "name":"1 oz Silver Bar Random Design",
                "offers":{
                  "@type":"Offer",
                  "priceCurrency":"USD",
                  "price":"90.70"
                }
              }
            </script>
          </head>
          <body>Sample</body>
        </html>
        """
        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch(
            "app.services.integration_queue.requests.get",
            side_effect=[_Resp(ddg_html), _Resp(product_html)],
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertGreaterEqual(len(captured["web_rows"]), 1)
        first = captured["web_rows"][0]
        self.assertAlmostEqual(float(first.get("listed_price") or 0), 90.70, places=2)
        self.assertEqual(str(first.get("price_hint_source") or ""), "structured_page")

    def test_execute_integration_queue_job_slack_ops_comp_top_snippet_prefers_confidence_over_price(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=947,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar random design",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[947] = job

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=lambda *_args, **_kwargs: SimpleNamespace(text="Comp summary ordering test"),
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://www.apmex.com/product/11111/high-price-snippet">High Price Snippet Product</a>
        <div class="result__snippet">Current listed price $150.00</div>
        <a class="result__a" href="https://www.apmex.com/product/22222/structured-price-product">Structured Price Product</a>
        <div class="result__snippet">Current listed price $90.70</div>
        """
        first_product_html = "<html><body>No structured price fields here</body></html>"
        second_product_html = """
        <html><head><meta property="product:price:amount" content="90.70"></head><body>Sample</body></html>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_fallback_limit":
                return 2
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 2
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.requests.get",
            side_effect=[_Resp(ddg_html), _Resp(first_product_html), _Resp(second_product_html)],
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        payload = json.loads(str(job.payload_json))
        summary = str(payload.get("ai_response", {}).get("summary") or "")
        self.assertIn("Top comps:", summary)
        high_idx = summary.find("High Price Snippet Product")
        structured_idx = summary.find("Structured Price Product")
        self.assertGreaterEqual(high_idx, 0)
        self.assertGreaterEqual(structured_idx, 0)
        self.assertLess(structured_idx, high_idx)
        self.assertIn("[", summary)
        self.assertIn("Evidence confidence", summary)

    def test_execute_integration_queue_job_slack_ops_comp_low_confidence_gate_message(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=948,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar random design",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[948] = job

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=lambda *_args, **_kwargs: SimpleNamespace(text="Comp summary gate test"),
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://example.com/listing">Loose web listing</a>
        <div class="result__snippet">Possible market listing around $50.00</div>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 0
            return default

        def _runtime_float(_repo, key, default=0.0):
            if str(key) == "slack_ops_comp_min_confidence_score":
                return 9.0
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.get_runtime_float",
            side_effect=_runtime_float,
        ), patch(
            "app.services.integration_queue.requests.get",
            return_value=_Resp(ddg_html),
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        payload = json.loads(str(job.payload_json))
        summary = str(payload.get("ai_response", {}).get("summary") or "")
        self.assertIn("Comp evidence gate triggered", summary)
        self.assertIn(
            "Suggested list band unavailable (insufficient evidence confidence and comp count)",
            summary,
        )
        links = payload.get("ai_response", {}).get("links") or []
        self.assertTrue(
            any("Fetch mode:" in str(link) and "evidence_gate_confidence_rows" in str(link) for link in links)
        )

    def test_execute_integration_queue_job_slack_ops_comp_min_rows_gate_message(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=949,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar random design",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[949] = job

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=lambda *_args, **_kwargs: SimpleNamespace(
                text="**Confidence:** Medium\nComp summary min rows test"
            ),
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://www.apmex.com/product/12345/sample">1 oz Silver Bar Random Design</a>
        <div class="result__snippet">Current listed price $90.70</div>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 0
            if str(key) == "slack_ops_comp_min_qualified_rows":
                return 3
            return default

        def _runtime_bool(_repo, key, default=False):
            if str(key) == "slack_ops_comp_min_qualified_rows_gate_enabled":
                return True
            if str(key) == "slack_ops_comp_min_confidence_gate_enabled":
                return False
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.get_runtime_bool",
            side_effect=_runtime_bool,
        ), patch(
            "app.services.integration_queue.requests.get",
            return_value=_Resp(ddg_html),
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        payload = json.loads(str(job.payload_json))
        summary = str(payload.get("ai_response", {}).get("summary") or "")
        self.assertIn("Comp evidence gate triggered", summary)
        self.assertIn("qualified comp row count is below minimum threshold (1 < 3)", summary)
        self.assertIn("Suggested list band unavailable (insufficient qualified comps)", summary)
        self.assertIn("Evidence confidence medium (single-comp; row-gated)", summary)
        self.assertIn("**Confidence:** Medium (rule-based)", summary)
        self.assertNotIn("**Confidence:** Medium Comp summary", summary)
        links = payload.get("ai_response", {}).get("links") or []
        self.assertTrue(any("Min qualified comps: 3" in str(link) for link in links))

    def test_execute_integration_queue_job_slack_ops_comp_gate_rewrites_ai_suggested_range(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=950,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar random design",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[950] = job

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=lambda *_args, **_kwargs: SimpleNamespace(
                text=(
                    "**Confidence:** Medium\n"
                    "**Suggested Range:** $89.45 - $94.22\n"
                    "**Recommendation:** Price at $94 now.\n"
                    "Comp summary"
                )
            ),
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://www.apmex.com/product/12345/sample">1 oz Silver Bar Random Design</a>
        <div class="result__snippet">Current listed price $90.70</div>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 0
            if str(key) == "slack_ops_comp_min_qualified_rows":
                return 3
            return default

        def _runtime_bool(_repo, key, default=False):
            if str(key) == "slack_ops_comp_min_qualified_rows_gate_enabled":
                return True
            if str(key) == "slack_ops_comp_min_confidence_gate_enabled":
                return False
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.get_runtime_bool",
            side_effect=_runtime_bool,
        ), patch(
            "app.services.integration_queue.requests.get",
            return_value=_Resp(ddg_html),
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        payload = json.loads(str(job.payload_json))
        summary = str(payload.get("ai_response", {}).get("summary") or "")
        self.assertIn("**Suggested Range:** Unavailable (insufficient qualified comps)", summary)
        self.assertNotIn("**Suggested Range:** $89.45 - $94.22", summary)
        self.assertIn(
            "**Recommendation:** Directional-only comp. Hold final pricing until stronger sold/product evidence is available.",
            summary,
        )
        self.assertNotIn("**Recommendation:** Price at $94 now.", summary)

    def test_execute_integration_queue_job_slack_ops_comp_gate_rewrites_inline_suggested_range_field_chain(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=951,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar apmex .999",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[951] = job

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=lambda *_args, **_kwargs: SimpleNamespace(
                text=(
                    "### 1 oz Silver Bar APMEX .999 - Resale Comp Analysis "
                    "**Confidence:** High "
                    "**Suggested Range:** $89.40 - $94.17 "
                    "**Current Listing:** $90.89 (Free shipping) "
                    "**Recommendation:** Proceed with standard pricing, but verify current spot pricing before finalizing."
                )
            ),
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://www.apmex.com/product/27086/1-oz-silver-bar-apmex">1 oz Silver Bar APMEX</a>
        <div class="result__snippet">Current listed price $90.89</div>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 0
            if str(key) == "slack_ops_comp_min_qualified_rows":
                return 2
            return default

        def _runtime_bool(_repo, key, default=False):
            if str(key) == "slack_ops_comp_min_qualified_rows_gate_enabled":
                return True
            if str(key) == "slack_ops_comp_min_confidence_gate_enabled":
                return False
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.get_runtime_bool",
            side_effect=_runtime_bool,
        ), patch(
            "app.services.integration_queue.requests.get",
            return_value=_Resp(ddg_html),
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        payload = json.loads(str(job.payload_json))
        summary = str(payload.get("ai_response", {}).get("summary") or "")
        self.assertIn("**Suggested Range:** Unavailable (insufficient qualified comps)", summary)
        self.assertIn("**Current Listing:** $90.89 (Free shipping)", summary)
        self.assertIn(
            "**Recommendation:** Directional-only comp. Hold final pricing until stronger sold/product evidence is available.",
            summary,
        )
        self.assertNotIn("**Suggested Range:** $89.40 - $94.17", summary)
        self.assertNotIn("**Recommendation:** Proceed with standard pricing", summary)

    def test_execute_integration_queue_job_slack_ops_comp_web_fallback_honors_detail_fetch_limit(self) -> None:
        repo = _FakeRepo()
        captured = {"web_rows": []}
        job = SimpleNamespace(
            id=942,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar apmex .999",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[942] = job

        def _exec_comp(*_args, **kwargs):
            captured["web_rows"] = list(kwargs.get("web_rows") or [])
            return SimpleNamespace(text="Comp summary with snippet-only fallback")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://www.apmex.com/product/12345/sample">Buy 1 oz Silver Bars</a>
        <div class="result__snippet">Sample listing shown at $90.70</div>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 0
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.requests.get",
            return_value=_Resp(ddg_html),
        ) as mock_get:
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertEqual(mock_get.call_count, 1)
        self.assertGreaterEqual(len(captured["web_rows"]), 1)
        first = captured["web_rows"][0]
        self.assertEqual(str(first.get("price_hint_source") or ""), "snippet_or_url")

    def test_execute_integration_queue_job_slack_ops_comp_web_fallback_filters_shipping_threshold_price(self) -> None:
        repo = _FakeRepo()
        captured = {"web_rows": []}
        job = SimpleNamespace(
            id=943,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar apmex .999",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[943] = job

        def _exec_comp(*_args, **kwargs):
            captured["web_rows"] = list(kwargs.get("web_rows") or [])
            return SimpleNamespace(text="Comp summary with threshold filter")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://www.apmex.com/product/12345/sample">Buy 1 oz Silver Bars | APMEX</a>
        <div class="result__snippet">Deal price $90.70 today. Free shipping on orders $199.</div>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 0
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.requests.get",
            return_value=_Resp(ddg_html),
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertGreaterEqual(len(captured["web_rows"]), 1)
        first = captured["web_rows"][0]
        self.assertAlmostEqual(float(first.get("listed_price") or 0), 90.70, places=2)
        self.assertAlmostEqual(float(first.get("listed_price_high") or 0), 90.70, places=2)

    def test_execute_integration_queue_job_slack_ops_comp_web_fallback_prefers_product_pages(self) -> None:
        repo = _FakeRepo()
        captured = {"web_rows": []}
        job = SimpleNamespace(
            id=944,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar apmex .999",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[944] = job

        def _exec_comp(*_args, **kwargs):
            captured["web_rows"] = list(kwargs.get("web_rows") or [])
            return SimpleNamespace(text="Comp summary preferring product URLs")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://www.apmex.com/category/25625/1-oz-silver-bars">Buy 1 oz Silver Bars | Free Shipping on Orders $199+</a>
        <div class="result__snippet">Category listing around $199.</div>
        <a class="result__a" href="https://www.apmex.com/product/12345/1-oz-silver-bar-random">1 oz Silver Bar Random Design</a>
        <div class="result__snippet">Current listed price $90.70.</div>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 0
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.requests.get",
            return_value=_Resp(ddg_html),
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertEqual(len(captured["web_rows"]), 1)
        first = captured["web_rows"][0]
        self.assertIn("/product/", str(first.get("item_url") or ""))
        self.assertAlmostEqual(float(first.get("listed_price") or 0), 90.70, places=2)

    def test_execute_integration_queue_job_slack_ops_comp_web_fallback_ignores_non_product_snippet_price(self) -> None:
        repo = _FakeRepo()
        captured = {"web_rows": []}
        job = SimpleNamespace(
            id=9441,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar apmex .999",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[9441] = job

        def _exec_comp(*_args, **kwargs):
            captured["web_rows"] = list(kwargs.get("web_rows") or [])
            return SimpleNamespace(text="Comp summary with non-product snippet suppression")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://www.apmex.com/category/25625/1-oz-silver-bars">Buy 1 oz Silver Bars | Free Shipping on Orders $199+</a>
        <div class="result__snippet">Category listing around $199.</div>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 0
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.requests.get",
            return_value=_Resp(ddg_html),
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertEqual(len(captured["web_rows"]), 1)
        first = captured["web_rows"][0]
        self.assertIn("/category/", str(first.get("item_url") or ""))
        self.assertAlmostEqual(float(first.get("listed_price") or 0), 0.0, places=2)
        self.assertEqual(str(first.get("price_hint_source") or ""), "none")

    def test_execute_integration_queue_job_slack_ops_comp_web_fallback_ignores_non_product_structured_price_noise(self) -> None:
        repo = _FakeRepo()
        captured = {"web_rows": []}
        job = SimpleNamespace(
            id=9442,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar apmex .999",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[9442] = job

        def _exec_comp(*_args, **kwargs):
            captured["web_rows"] = list(kwargs.get("web_rows") or [])
            return SimpleNamespace(text="Comp summary with non-product structured suppression")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://www.apmex.com/category/25625/1-oz-silver-bars">Buy 1 oz Silver Bars | Free Shipping on Orders $199+</a>
        <div class="result__snippet">Category listing results.</div>
        """
        category_html = """
        <html>
          <head>
            <script type="application/json">
              {"promo":{"price":"199"}}
            </script>
          </head>
          <body>Category page marketing content only.</body>
        </html>
        """

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch(
            "app.services.integration_queue.requests.get",
            side_effect=[_Resp(ddg_html), _Resp(category_html)],
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertEqual(len(captured["web_rows"]), 1)
        first = captured["web_rows"][0]
        self.assertIn("/category/", str(first.get("item_url") or ""))
        self.assertAlmostEqual(float(first.get("listed_price") or 0), 0.0, places=2)
        self.assertEqual(str(first.get("price_hint_source") or ""), "none")

    def test_execute_integration_queue_job_slack_ops_comp_web_fallback_prefers_jm_product_slug(self) -> None:
        repo = _FakeRepo()
        captured = {"web_rows": []}
        job = SimpleNamespace(
            id=945,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar jm bullion .999",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[945] = job

        def _exec_comp(*_args, **kwargs):
            captured["web_rows"] = list(kwargs.get("web_rows") or [])
            return SimpleNamespace(text="Comp summary preferring JM product slug")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://www.jmbullion.com/silver/silver-bars/">JM Bullion Silver Bars</a>
        <div class="result__snippet">Browse category prices around $199.</div>
        <a class="result__a" href="https://www.jmbullion.com/1-oz-silver-bar-random-design/">1 oz Silver Bar (Random Design)</a>
        <div class="result__snippet">Current listed price $39.99.</div>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 0
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.requests.get",
            return_value=_Resp(ddg_html),
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertEqual(len(captured["web_rows"]), 1)
        first = captured["web_rows"][0]
        self.assertIn("jmbullion.com/1-oz-silver-bar-random-design", str(first.get("item_url") or ""))
        self.assertAlmostEqual(float(first.get("listed_price") or 0), 39.99, places=2)

    def test_execute_integration_queue_job_slack_ops_comp_trusted_sources_only_filters_web_rows(self) -> None:
        repo = _FakeRepo()
        captured = {"web_rows": []}
        job = SimpleNamespace(
            id=950,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar random design",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[950] = job

        def _exec_comp(*_args, **kwargs):
            captured["web_rows"] = list(kwargs.get("web_rows") or [])
            return SimpleNamespace(text="Comp summary trusted-source test")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://example.com/random-silver-bar">Random Silver Bar Listing</a>
        <div class="result__snippet">Some listing around $115.00</div>
        <a class="result__a" href="https://www.apmex.com/product/12345/1-oz-silver-bar-random">1 oz Silver Bar Random Design</a>
        <div class="result__snippet">Current listed price $90.70</div>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 0
            return default

        def _runtime_bool(_repo, key, default=False):
            if str(key) == "slack_ops_comp_trusted_sources_only_enabled":
                return True
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.get_runtime_bool",
            side_effect=_runtime_bool,
        ), patch(
            "app.services.integration_queue.requests.get",
            return_value=_Resp(ddg_html),
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertEqual(len(captured["web_rows"]), 1)
        first = captured["web_rows"][0]
        self.assertIn("apmex.com", str(first.get("item_url") or ""))
        payload = json.loads(str(job.payload_json))
        links = payload.get("ai_response", {}).get("links") or []
        self.assertTrue(any("Trusted-source web filter: on" in str(link) for link in links))

    def test_execute_integration_queue_job_slack_ops_comp_trusted_only_override_true(self) -> None:
        repo = _FakeRepo()
        captured = {"web_rows": []}
        job = SimpleNamespace(
            id=951,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar random design",
                        "args": ["trusted_only=true"],
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[951] = job

        def _exec_comp(*_args, **kwargs):
            captured["web_rows"] = list(kwargs.get("web_rows") or [])
            return SimpleNamespace(text="Comp summary trusted-only override")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://example.com/random-silver-bar">Random Silver Bar Listing</a>
        <div class="result__snippet">Some listing around $115.00</div>
        <a class="result__a" href="https://www.apmex.com/product/12345/1-oz-silver-bar-random">1 oz Silver Bar Random Design</a>
        <div class="result__snippet">Current listed price $90.70</div>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 0
            return default

        def _runtime_bool(_repo, key, default=False):
            if str(key) == "slack_ops_comp_trusted_sources_only_enabled":
                return False
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.get_runtime_bool",
            side_effect=_runtime_bool,
        ), patch(
            "app.services.integration_queue.requests.get",
            return_value=_Resp(ddg_html),
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertEqual(len(captured["web_rows"]), 1)
        first = captured["web_rows"][0]
        self.assertIn("apmex.com", str(first.get("item_url") or ""))
        payload = json.loads(str(job.payload_json))
        links = payload.get("ai_response", {}).get("links") or []
        self.assertTrue(any("Trusted-source override: true" in str(link) for link in links))

    def test_execute_integration_queue_job_slack_ops_comp_gate_overrides_disable_gates(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=952,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1oz silver bar",
                        "args": [
                            "confidence_gate=false",
                            "rows_gate=false",
                            "min_confidence=9.0",
                            "min_rows=5",
                        ],
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[952] = job

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=lambda *_args, **_kwargs: SimpleNamespace(text="Comp summary"),
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return True

            def search_sold_items_html(self, **_kwargs):
                return [{"title": "1 oz Silver Bar", "sold_price": 40.0, "shipping_cost": 4.0}]

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        payload = json.loads(str(job.payload_json))
        summary = str(payload.get("ai_response", {}).get("summary") or "")
        self.assertIn("Suggested list band $39.60-$48.40", summary)
        self.assertNotIn("Comp evidence gate triggered", summary)
        links = payload.get("ai_response", {}).get("links") or []
        self.assertTrue(any("Confidence-gate override: false" in str(link) for link in links))
        self.assertTrue(any("Rows-gate override: false" in str(link) for link in links))
        self.assertTrue(any("Min-confidence override: 9.00" in str(link) for link in links))
        self.assertTrue(any("Min-rows override: 5" in str(link) for link in links))

    def test_execute_integration_queue_job_slack_ops_comp_single_row_confidence_dampened(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=955,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1oz silver bar",
                        "args": ["confidence_gate=false", "rows_gate=false"],
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[955] = job

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=lambda *_args, **_kwargs: SimpleNamespace(text="Comp summary"),
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return True

            def search_sold_items_html(self, **_kwargs):
                return [
                    {
                        "title": "1 oz Silver Bar",
                        "sold_price": 90.0,
                        "shipping_cost": 0.0,
                        "item_url": "https://www.ebay.com/itm/1234567890",
                    }
                ]

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        payload = json.loads(str(job.payload_json))
        summary = str(payload.get("ai_response", {}).get("summary") or "")
        self.assertIn("Qualified comps: 1", summary)
        self.assertIn("Distinct sources: 1", summary)
        self.assertIn("Evidence confidence medium", summary)
        self.assertNotIn("Evidence confidence high", summary)

    def test_execute_integration_queue_job_slack_ops_comp_trusted_domains_override(self) -> None:
        repo = _FakeRepo()
        captured = {"web_rows": []}
        job = SimpleNamespace(
            id=953,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar random design",
                        "args": ["trusted_only=true", "trusted_domains=jmbullion.com"],
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[953] = job

        def _exec_comp(*_args, **kwargs):
            captured["web_rows"] = list(kwargs.get("web_rows") or [])
            return SimpleNamespace(text="Comp summary trusted-domain override")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://www.apmex.com/product/12345/1-oz-silver-bar-random">APMEX 1 oz Silver Bar</a>
        <div class="result__snippet">Current listed price $90.70</div>
        <a class="result__a" href="https://www.jmbullion.com/1-oz-silver-bar-random-design/">JM Bullion 1 oz Silver Bar</a>
        <div class="result__snippet">Current listed price $39.99</div>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 0
            return default

        def _runtime_bool(_repo, key, default=False):
            if str(key) == "slack_ops_comp_trusted_sources_only_enabled":
                return False
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.get_runtime_bool",
            side_effect=_runtime_bool,
        ), patch(
            "app.services.integration_queue.requests.get",
            return_value=_Resp(ddg_html),
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertEqual(len(captured["web_rows"]), 1)
        first = captured["web_rows"][0]
        self.assertIn("jmbullion.com", str(first.get("item_url") or ""))
        payload = json.loads(str(job.payload_json))
        links = payload.get("ai_response", {}).get("links") or []
        self.assertTrue(any("Trusted-source override: true" in str(link) for link in links))
        self.assertTrue(any("Trusted-domains override: jmbullion.com" in str(link) for link in links))

    def test_execute_integration_queue_job_slack_ops_comp_trusted_domains_override_enables_filter(self) -> None:
        repo = _FakeRepo()
        captured = {"web_rows": []}
        job = SimpleNamespace(
            id=954,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1 oz silver bar random design",
                        "args": ["trusted_domains=jmbullion.com"],
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[954] = job

        def _exec_comp(*_args, **kwargs):
            captured["web_rows"] = list(kwargs.get("web_rows") or [])
            return SimpleNamespace(text="Comp summary trusted-domain implicit filter")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return []

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ddg_html = """
        <a class="result__a" href="https://www.apmex.com/product/12345/1-oz-silver-bar-random">APMEX 1 oz Silver Bar</a>
        <div class="result__snippet">Current listed price $90.70</div>
        <a class="result__a" href="https://www.jmbullion.com/1-oz-silver-bar-random-design/">JM Bullion 1 oz Silver Bar</a>
        <div class="result__snippet">Current listed price $39.99</div>
        """

        def _runtime_int(_repo, key, default=0):
            if str(key) == "slack_ops_comp_web_detail_fetch_limit":
                return 0
            return default

        def _runtime_bool(_repo, key, default=False):
            if str(key) == "slack_ops_comp_trusted_sources_only_enabled":
                return False
            return default

        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch("app.services.integration_queue.get_runtime_int", side_effect=_runtime_int), patch(
            "app.services.integration_queue.get_runtime_bool",
            side_effect=_runtime_bool,
        ), patch(
            "app.services.integration_queue.requests.get",
            return_value=_Resp(ddg_html),
        ):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertEqual(len(captured["web_rows"]), 1)
        first = captured["web_rows"][0]
        self.assertIn("jmbullion.com", str(first.get("item_url") or ""))
        payload = json.loads(str(job.payload_json))
        links = payload.get("ai_response", {}).get("links") or []
        self.assertTrue(any("Trusted-source web filter: on" in str(link) for link in links))
        self.assertTrue(any("Trusted-domains override: jmbullion.com" in str(link) for link in links))

    def test_execute_integration_queue_job_slack_ops_comp_ebay_html_fallback_rows(self) -> None:
        repo = _FakeRepo()
        captured = {"ebay_rows": []}
        job = SimpleNamespace(
            id=95,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "comp",
                        "command_text": "comp 1oz silver bar apmex",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[95] = job

        def _exec_comp(*_args, **kwargs):
            captured["ebay_rows"] = list(kwargs.get("ebay_rows") or [])
            return SimpleNamespace(text="Comp summary with ebay html fallback")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=_exec_comp,
            execute_multimodal_task=lambda *_args, **_kwargs: SimpleNamespace(text=""),
        )

        class _FakeEbayClient:
            def is_configured(self):
                return False

            def search_sold_items_html(self, **_kwargs):
                return [
                    {
                        "title": "APMEX 1 oz Silver Bar .999 Fine",
                        "sold_price": 39.95,
                        "shipping_cost": 4.99,
                        "total_price": 44.94,
                        "item_url": "https://www.ebay.com/itm/137217809542",
                    }
                ]

        fake_ebay_module = SimpleNamespace(EbayClient=_FakeEbayClient)

        class _Resp:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        ebay_html = """
        <li class="s-item">
          <a class="s-item__link" href="https://www.ebay.com/itm/137217809542">
            <h3 class="s-item__title">APMEX 1 oz Silver Bar .999 Fine</h3>
          </a>
          <span class="s-item__price">$39.95</span>
          <span class="s-item__shipping">$4.99 shipping</span>
        </li>
        """
        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.ebay": fake_ebay_module},
        ), patch(
            "app.services.integration_queue.get_runtime_bool",
            side_effect=lambda _repo, key, default=False: (
                False if str(key) == "slack_ops_comp_min_qualified_rows_gate_enabled" else default
            ),
        ), patch("app.services.integration_queue.requests.get", return_value=_Resp(ebay_html)):
            ok, _message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertGreaterEqual(len(captured["ebay_rows"]), 1)
        payload = json.loads(str(job.payload_json))
        self.assertIn("eBay rows: 1", payload["ai_response"]["links"])
        self.assertIn("Fetch mode: ebay_sold_html_fallback", payload["ai_response"]["links"])

    def test_execute_integration_queue_job_slack_ops_status_intent(self) -> None:
        repo = _FakeRepo()
        repo.queue_rows = [
            SimpleNamespace(status="queued", next_attempt_at=None),
            SimpleNamespace(status="failed", next_attempt_at=utcnow_naive()),
        ]
        job = SimpleNamespace(
            id=88,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "status",
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[88] = job
        with patch("app.services.integration_queue.get_runtime_bool", return_value=False):
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("status summary", message.lower())
        payload = json.loads(str(job.payload_json))
        self.assertIn("GoldenStackers Status", payload["ai_response"]["summary"])

    def test_execute_integration_queue_job_slack_ops_operations_run_due_alias(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=89,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "operations",
                        "args": ["run_sync", "slack_ops", "7"],
                    },
                    "request_context": {"channel_id": "COPS"},
                }
            ),
        )
        repo.db.rows[89] = job
        with patch(
            "app.services.integration_queue.process_due_integration_queue_jobs",
            return_value={"processed": 2, "success": 2, "queued": 0, "failed": 0, "blocked": 0},
        ) as run_due:
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("Run due executed", message)
        run_due.assert_called_once()
        payload = json.loads(str(job.payload_json))
        self.assertIn("Run Due Result", payload["ai_response"]["summary"])

    def test_execute_integration_queue_job_slack_ops_operations_create_ebay_draft(self) -> None:
        repo = _FakeRepo()
        repo.db.rows[46] = SimpleNamespace(id=46, title="Copper Round", acquisition_cost=10, current_quantity=3)
        job = SimpleNamespace(
            id=90,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "operations",
                        "args": ["create_ebay_draft", "46", "19.99", "2"],
                    },
                    "request_context": {"channel_id": "COPS", "app_username": "ops-user"},
                }
            ),
        )
        repo.db.rows[90] = job
        ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("Created draft listing", message)
        self.assertEqual(len(repo.created_listings), 1)
        payload = json.loads(str(job.payload_json))
        self.assertIn("Created eBay Draft Listing", payload["ai_response"]["summary"])

    def test_execute_integration_queue_job_slack_ops_intake_creates_product_draft(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(
            id=91,
            integration="slack_ops",
            action="command_ingest",
            payload_json=json.dumps(
                {
                    "command": {
                        "intent": "intake",
                        "command_text": "intake vintage copper round",
                        "args": ["qty=2", "cost=15.50", "category=bullion"],
                        "files": [
                            {
                                "name": "coin.jpg",
                                "mimetype": "image/jpeg",
                                "content_b64": base64.b64encode(b"img-bytes").decode("utf-8"),
                            }
                        ],
                    },
                    "request_context": {"channel_id": "COPS", "app_username": "ops-user"},
                }
            ),
        )
        repo.db.rows[91] = job
        storage = SimpleNamespace(
            enabled=True,
            ensure_bucket=lambda: None,
            upload_file=lambda **_kwargs: SimpleNamespace(
                bucket="bucket",
                key="media/x-coin.jpg",
                url="https://storage/bucket/media/x-coin.jpg",
                content_type="image/jpeg",
                size_bytes=9,
            ),
        )

        def _mm(*_args, **kwargs):
            if str(kwargs.get("tool_name") or "") == "slack_intake_product_builder":
                return SimpleNamespace(
                    text=json.dumps(
                        {
                            "suggested_title": "Vintage Copper Round",
                            "suggested_category": "bullion",
                            "suggested_description": "Detailed collectible copper round.",
                            "suggested_metal_type": "Copper",
                            "suggested_weight_oz": "1",
                        }
                    )
                )
            return SimpleNamespace(text="AI intake summary")

        fake_ai_module = SimpleNamespace(
            execute_comp_summary=lambda *_args, **_kwargs: SimpleNamespace(text=""),
            execute_multimodal_task=_mm,
        )
        fake_media_module = SimpleNamespace(MediaStorageService=lambda: storage)
        with patch.dict(
            sys.modules,
            {"app.services.ai_orchestration": fake_ai_module, "app.services.media_storage": fake_media_module},
        ):
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("Created product draft", message)
        self.assertEqual(len(repo.created_products), 1)
        created = repo.created_products[0]
        self.assertEqual(created["title"], "Vintage Copper Round")
        self.assertEqual(created["category"], "bullion")
        self.assertEqual(int(created["current_quantity"]), 2)
        self.assertEqual(str(created["acquisition_cost"]), "15.50")
        self.assertEqual(repo.created_media[0]["product_id"], 101)

    def test_execute_integration_queue_job_shipping_dry_run(self) -> None:
        repo = _FakeRepo()
        sale = SimpleNamespace(id=10, tracking_status="")
        repo.db.rows[10] = sale
        job = SimpleNamespace(
            id=1,
            integration="shipping",
            action="purchase_label",
            payload_json=json.dumps({"sale_id": 10, "dry_run": True}),
        )
        with patch("app.services.integration_queue.get_runtime_bool", return_value=True):
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("dry-run", message)
        self.assertEqual(repo.updated_sales, [])

    def test_execute_integration_queue_job_shipping_validation_branches(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(id=1, integration="shipping", action="other", payload_json="{}")
        ok, msg = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertFalse(ok)
        self.assertIn("Unsupported shipping action", msg)

        job2 = SimpleNamespace(id=2, integration="shipping", action="purchase_label", payload_json="{}")
        with patch("app.services.integration_queue.get_runtime_bool", return_value=False):
            ok, msg = integration_queue.execute_integration_queue_job(repo, job2, actor="qa")
        self.assertFalse(ok)
        self.assertIn("Shipping queue is disabled", msg)

        # queue enabled, purchase disabled
        with patch("app.services.integration_queue.get_runtime_bool", side_effect=[True, False]):
            ok, msg = integration_queue.execute_integration_queue_job(repo, job2, actor="qa")
        self.assertFalse(ok)
        self.assertIn("purchase is disabled", msg)

        # invalid sale payload
        with patch("app.services.integration_queue.get_runtime_bool", side_effect=[True, True]):
            ok, msg = integration_queue.execute_integration_queue_job(
                repo,
                SimpleNamespace(id=3, integration="shipping", action="purchase_label", payload_json='{"sale_id":"x"}'),
                actor="qa",
            )
        self.assertFalse(ok)
        self.assertIn("Missing/invalid `sale_id`", msg)

        # missing sale row
        with patch("app.services.integration_queue.get_runtime_bool", side_effect=[True, True]):
            ok, msg = integration_queue.execute_integration_queue_job(
                repo,
                SimpleNamespace(id=4, integration="shipping", action="purchase_label", payload_json='{"sale_id":999}'),
                actor="qa",
            )
        self.assertFalse(ok)
        self.assertIn("not found", msg)

    def test_execute_integration_queue_job_shipping_scaffold_updates_sale(self) -> None:
        repo = _FakeRepo()
        sale = SimpleNamespace(id=11, tracking_status="")
        repo.db.rows[11] = sale
        payload = {
            "sale_id": 11,
            "shipping_provider": "usps",
            "tracking_number": "TRACK123",
            "shipping_service": "Ground",
            "shipping_package_type": "Box",
        }
        job = SimpleNamespace(
            id=2,
            integration="shipping",
            action="purchase_label",
            payload_json=json.dumps(payload),
        )
        with patch("app.services.integration_queue.get_runtime_bool") as runtime_bool:
            runtime_bool.side_effect = lambda *_args, **_kwargs: False if _args[1] == "shipping_label_live_provider_calls_enabled" else True
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("scaffold", message)
        self.assertEqual(len(repo.updated_sales), 1)
        _, updates, _ = repo.updated_sales[0]
        self.assertEqual(updates["tracking_number"], "TRACK123")
        self.assertEqual(updates["shipping_provider"], "usps")
        self.assertEqual(updates["tracking_status"], "label_created")

    def test_execute_integration_queue_job_shipping_live_provider_path(self) -> None:
        repo = _FakeRepo()
        sale = SimpleNamespace(id=20, tracking_status="")
        repo.db.rows[20] = sale
        job = SimpleNamespace(
            id=20,
            integration="shipping",
            action="purchase_label",
            payload_json=json.dumps({"sale_id": 20, "shipping_provider": "usps"}),
        )

        def _runtime_bool(_repo, key, default=True):
            if key == "shipping_queue_enabled":
                return True
            if key == "shipping_label_purchase_enabled":
                return True
            if key == "shipping_label_provider_usps_enabled":
                return True
            if key == "shipping_label_live_provider_calls_enabled":
                return True
            return default

        provider_result = SimpleNamespace(
            label_id="LBL-1",
            label_url="https://x/label.pdf",
            label_cost=4.5,
            label_currency="USD",
            tracking_number="TRACKX",
        )
        with patch("app.services.integration_queue.get_runtime_bool", side_effect=_runtime_bool), patch(
            "app.services.integration_queue.purchase_shipping_label", return_value=provider_result
        ):
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("completed", message)
        _, updates, _ = repo.updated_sales[0]
        self.assertEqual(updates["shipping_label_id"], "LBL-1")
        self.assertEqual(updates["tracking_number"], "TRACKX")

    def test_execute_integration_queue_job_shipping_provider_disabled(self) -> None:
        repo = _FakeRepo()
        repo.db.rows[21] = SimpleNamespace(id=21, tracking_status="")
        job = SimpleNamespace(
            id=21,
            integration="shipping",
            action="purchase_label",
            payload_json=json.dumps({"sale_id": 21, "shipping_provider": "usps"}),
        )

        def _runtime_bool(_repo, key, default=True):
            if key in {"shipping_queue_enabled", "shipping_label_purchase_enabled"}:
                return True
            if key == "shipping_label_provider_usps_enabled":
                return False
            return default

        with patch("app.services.integration_queue.get_runtime_bool", side_effect=_runtime_bool):
            ok, message = integration_queue.execute_integration_queue_job(repo, job, actor="qa")
        self.assertFalse(ok)
        self.assertIn("provider `usps` is disabled", message)

    def test_execute_integration_queue_job_google_drive_missing_payload(self) -> None:
        job = SimpleNamespace(integration="google", action="drive_upload_artifact", payload_json="{}")
        with patch("app.services.integration_queue.resolve_google_workspace_config", return_value=SimpleNamespace()):
            ok, message = integration_queue.execute_integration_queue_job(_FakeRepo(), job, actor="qa")
        self.assertFalse(ok)
        self.assertIn("Missing `file_b64` payload", message)

    def test_execute_integration_queue_job_google_drive_upload(self) -> None:
        file_bytes = b"hello"
        job = SimpleNamespace(
            integration="google",
            action="drive_upload_artifact",
            payload_json=json.dumps(
                {
                    "file_b64": base64.b64encode(file_bytes).decode("utf-8"),
                    "file_name": "x.txt",
                    "mime_type": "text/plain",
                    "folder_id": "abc",
                }
            ),
        )
        with patch("app.services.integration_queue.resolve_google_workspace_config", return_value=SimpleNamespace()), patch(
            "app.services.integration_queue.upload_drive_file"
        ) as upload:
            ok, message = integration_queue.execute_integration_queue_job(_FakeRepo(), job, actor="qa")
        self.assertTrue(ok)
        self.assertIn("Drive upload completed", message)
        upload.assert_called_once()

    def test_execute_integration_queue_job_google_routes_gmail_and_calendar(self) -> None:
        repo = _FakeRepo()
        gmail_job = SimpleNamespace(
            integration="google",
            action="gmail_send_document_email",
            payload_json=json.dumps({"to_email": "x@y.com", "subject": "s", "body_html": "<p>x</p>"}),
        )
        cal_job = SimpleNamespace(
            integration="google",
            action="calendar_create_event",
            payload_json=json.dumps({"summary": "s", "start_iso": "2026-01-01T00:00:00", "end_iso": "2026-01-01T01:00:00"}),
        )
        with patch("app.services.integration_queue.resolve_google_workspace_config", return_value=SimpleNamespace(default_timezone="UTC", default_calendar_id="primary")), patch(
            "app.services.integration_queue.send_gmail_message"
        ) as send_gmail, patch("app.services.integration_queue.create_calendar_event") as create_event:
            ok1, _ = integration_queue.execute_integration_queue_job(repo, gmail_job, actor="qa")
            ok2, _ = integration_queue.execute_integration_queue_job(repo, cal_job, actor="qa")
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        send_gmail.assert_called_once()
        create_event.assert_called_once()

    def test_execute_integration_queue_job_google_unsupported_action(self) -> None:
        job = SimpleNamespace(integration="google", action="other", payload_json="{}")
        with patch("app.services.integration_queue.resolve_google_workspace_config", return_value=SimpleNamespace()):
            ok, message = integration_queue.execute_integration_queue_job(_FakeRepo(), job, actor="qa")
        self.assertFalse(ok)
        self.assertIn("Unsupported integration action", message)

    def test_process_integration_queue_job_success_path(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(id=30, integration="google", action="gmail_send_document_email", retry_count=0, max_retries=3)
        repo.db.rows[30] = job
        with patch("app.services.integration_queue.execute_integration_queue_job", return_value=(True, "ok")):
            ok, _ = integration_queue.process_integration_queue_job(repo, job_id=30, actor="qa")
        self.assertTrue(ok)
        self.assertTrue(any(u[1].get("status") == "success" for u in repo.updated_jobs))
        self.assertTrue(any(e.get("status") == "success" for e in repo.logged_events))

    def test_process_integration_queue_job_exception_captured(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(id=33, integration="google", action="gmail_send_document_email", retry_count=0, max_retries=1)
        repo.db.rows[33] = job
        with patch("app.services.integration_queue.execute_integration_queue_job", side_effect=RuntimeError("boom")):
            ok, message = integration_queue.process_integration_queue_job(repo, job_id=33, actor="qa")
        self.assertFalse(ok)
        self.assertIn("boom", message)

    def test_process_integration_queue_job_failure_requeues_when_retry_left(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(id=31, integration="google", action="gmail_send_document_email", retry_count=0, max_retries=2)
        repo.db.rows[31] = job
        with patch("app.services.integration_queue.execute_integration_queue_job", return_value=(False, "bad")):
            ok, _ = integration_queue.process_integration_queue_job(repo, job_id=31, actor="qa")
        self.assertFalse(ok)
        queued_updates = [u for u in repo.updated_jobs if u[1].get("status") == "queued"]
        self.assertEqual(len(queued_updates), 1)
        self.assertIn("next_attempt_at", queued_updates[0][1])

    def test_process_integration_queue_job_failure_terminal(self) -> None:
        repo = _FakeRepo()
        job = SimpleNamespace(id=32, integration="google", action="gmail_send_document_email", retry_count=2, max_retries=2)
        repo.db.rows[32] = job
        with patch("app.services.integration_queue.execute_integration_queue_job", return_value=(False, "bad")), patch(
            "app.services.integration_queue._emit_terminal_queue_failure_alert"
        ) as emit_alert:
            ok, _ = integration_queue.process_integration_queue_job(repo, job_id=32, actor="qa")
        self.assertFalse(ok)
        self.assertTrue(any(u[1].get("status") == "failed" for u in repo.updated_jobs))
        emit_alert.assert_called_once()

    def test_process_integration_queue_job_not_found_raises(self) -> None:
        with self.assertRaises(ValueError):
            integration_queue.process_integration_queue_job(_FakeRepo(), job_id=999, actor="qa")

    def test_process_due_integration_queue_jobs_handles_blocked_and_processed(self) -> None:
        repo = _FakeRepo()
        now = utcnow_naive()
        row1 = SimpleNamespace(id=41, next_attempt_at=now - timedelta(minutes=1))
        row2 = SimpleNamespace(id=42, next_attempt_at=now - timedelta(minutes=1))
        job1 = SimpleNamespace(id=41, integration="google", action="gmail_send_document_email")
        job2 = SimpleNamespace(id=42, integration="google", action="gmail_send_document_email")
        repo.queue_rows = [row1, row2]
        repo.db.rows[41] = job1
        repo.db.rows[42] = job2

        def fake_eval(_repo, job, actor, trigger_status):
            if job.id == 41:
                return {"matched_rule_ids": [1], "applied_rule_ids": [], "approval_gated_rule_ids": [1], "blocked": True, "blocked_reason": "needs approval"}
            return {"matched_rule_ids": [2], "applied_rule_ids": [2], "approval_gated_rule_ids": [], "blocked": False}

        with patch("app.services.integration_queue.evaluate_and_apply_rules_for_job", side_effect=fake_eval), patch(
            "app.services.integration_queue.process_integration_queue_job", return_value=(True, "ok")
        ):
            summary = integration_queue.process_due_integration_queue_jobs(
                repo,
                integration="google",
                actor="qa",
                limit=10,
            )
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["success"], 1)
        self.assertEqual(summary["rules_matched"], 2)

    def test_process_due_integration_queue_jobs_counts_queued_and_failed(self) -> None:
        repo = _FakeRepo()
        now = utcnow_naive()
        row1 = SimpleNamespace(id=51, next_attempt_at=now - timedelta(minutes=1))
        row2 = SimpleNamespace(id=52, next_attempt_at=now - timedelta(minutes=1))
        job1 = SimpleNamespace(id=51, integration="google", action="gmail_send_document_email", status="queued")
        job2 = SimpleNamespace(id=52, integration="google", action="gmail_send_document_email", status="failed")
        repo.queue_rows = [row1, row2]
        repo.db.rows[51] = job1
        repo.db.rows[52] = job2
        with patch("app.services.integration_queue.evaluate_and_apply_rules_for_job", return_value={"matched_rule_ids": [], "applied_rule_ids": [], "approval_gated_rule_ids": [], "blocked": False}), patch(
            "app.services.integration_queue.process_integration_queue_job", return_value=(False, "bad")
        ):
            summary = integration_queue.process_due_integration_queue_jobs(
                repo,
                integration="google",
                actor="qa",
                limit=10,
            )
        # first refreshed row currently queued => queued bucket, second => failed bucket
        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["failed"], 1)

    def test_process_due_google_queue_jobs_wrapper(self) -> None:
        with patch("app.services.integration_queue.process_due_integration_queue_jobs", return_value={"processed": 1}) as proc:
            out = integration_queue.process_due_google_queue_jobs(_FakeRepo(), actor="qa", limit=5)
        self.assertEqual(out["processed"], 1)
        proc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
