import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services import llm_runtime
from app.services.llm_runtime import LLMRuntimeConfig


class LLMRuntimeTests(unittest.TestCase):
    def test_safe_numeric_helpers(self) -> None:
        self.assertEqual(llm_runtime._safe_int("12", 0), 12)
        self.assertEqual(llm_runtime._safe_int("bad", 7), 7)
        self.assertAlmostEqual(llm_runtime._safe_float("1.25", 0.0), 1.25)
        self.assertAlmostEqual(llm_runtime._safe_float("bad", 2.5), 2.5)

    def test_runtime_bool_and_int_from_repo(self) -> None:
        class Repo:
            def get_runtime_setting(self, *, environment: str, key: str, active_only: bool):
                mapping = {
                    "true_key": SimpleNamespace(value="yes"),
                    "false_key": SimpleNamespace(value="off"),
                    "unknown_bool_key": SimpleNamespace(value="maybe"),
                    "int_key": SimpleNamespace(value="42"),
                    "bad_int_key": SimpleNamespace(value="oops"),
                }
                return mapping.get(key)

        repo = Repo()
        self.assertTrue(llm_runtime._runtime_bool_from_repo(repo, "true_key", False))
        self.assertFalse(llm_runtime._runtime_bool_from_repo(repo, "false_key", True))
        self.assertFalse(llm_runtime._runtime_bool_from_repo(repo, "unknown_bool_key", False))
        self.assertTrue(llm_runtime._runtime_bool_from_repo(repo, "missing", True))
        self.assertEqual(llm_runtime._runtime_int_from_repo(repo, "int_key", 5), 42)
        self.assertEqual(llm_runtime._runtime_int_from_repo(repo, "bad_int_key", 5), 5)
        self.assertEqual(llm_runtime._runtime_int_from_repo(repo, "missing", 5), 5)

    def test_runtime_bool_and_int_from_repo_exception_path(self) -> None:
        class RepoError:
            def get_runtime_setting(self, *, environment: str, key: str, active_only: bool):
                raise RuntimeError("db error")

        repo = RepoError()
        self.assertTrue(llm_runtime._runtime_bool_from_repo(repo, "x", True))
        self.assertEqual(llm_runtime._runtime_int_from_repo(repo, "x", 7), 7)

    def test_resolve_comp_llm_runtime_config_prefers_db_then_env(self) -> None:
        db_row = SimpleNamespace(
            is_active=True,
            provider="localai",
            model="glm-vision",
            multimodal_model="",
            base_url="http://localai:8080/v1/",
            endpoint_type="chat_completions",
            api_key="",
            temperature="0.3",
            max_output_tokens="700",
            timeout_seconds="40",
        )

        class RepoWithDb:
            def get_default_ai_provider_config(self, *, environment: str):
                return db_row

        db_cfg = llm_runtime.resolve_comp_llm_runtime_config(RepoWithDb())
        self.assertEqual(db_cfg.source, "db")
        self.assertEqual(db_cfg.provider, "localai")
        self.assertEqual(db_cfg.multimodal_model, "glm-vision")
        self.assertEqual(db_cfg.base_url, "http://localai:8080/v1")

        class RepoWithoutDb:
            def get_default_ai_provider_config(self, *, environment: str):
                return None

        env_cfg = llm_runtime.resolve_comp_llm_runtime_config(RepoWithoutDb())
        self.assertEqual(env_cfg.source, "env")
        self.assertTrue(bool(env_cfg.model))

    def test_resolve_comp_llm_runtime_config_repo_exception_falls_back_env(self) -> None:
        class RepoBoom:
            def get_default_ai_provider_config(self, *, environment: str):
                raise RuntimeError("db down")

        cfg = llm_runtime.resolve_comp_llm_runtime_config(RepoBoom())
        self.assertEqual(cfg.source, "env")

    @patch("app.services.llm_runtime._runtime_int_from_repo")
    @patch("app.services.llm_runtime._runtime_bool_from_repo")
    @patch("app.services.llm_runtime.resolve_comp_llm_runtime_config")
    def test_resolve_chain_handles_fallback_dedupe_and_limits(
        self,
        mock_primary,
        mock_fallback_enabled,
        mock_max_profiles,
    ) -> None:
        primary = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="openai",
            model="gpt-4o-mini",
            multimodal_model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            endpoint_type="responses",
            api_key="k",
            temperature=0.2,
            max_output_tokens=600,
            timeout_seconds=60,
        )
        mock_primary.return_value = primary
        mock_fallback_enabled.return_value = True
        mock_max_profiles.return_value = 2

        rows = [
            SimpleNamespace(
                is_default=True,
                is_active=True,
                provider="openai",
                model="gpt-4o-mini",
                multimodal_model="",
                base_url="https://api.openai.com/v1",
                endpoint_type="responses",
                api_key="k",
                temperature=0.2,
                max_output_tokens=600,
                timeout_seconds=60,
            ),
            SimpleNamespace(
                is_default=False,
                is_active=True,
                provider="localai",
                model="llama",
                multimodal_model="",
                base_url="http://localai:8080/v1",
                endpoint_type="chat_completions",
                api_key="",
                temperature=0.1,
                max_output_tokens=800,
                timeout_seconds=45,
            ),
            # Duplicate signature (should be deduped)
            SimpleNamespace(
                is_default=False,
                is_active=True,
                provider="localai",
                model="llama",
                multimodal_model="vision",
                base_url="http://localai:8080/v1",
                endpoint_type="chat_completions",
                api_key="",
                temperature=0.1,
                max_output_tokens=800,
                timeout_seconds=45,
            ),
        ]

        class Repo:
            def list_ai_provider_configs(self, *, environment: str, active_only: bool):
                return rows

        chain = llm_runtime.resolve_comp_llm_runtime_chain(Repo())
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].provider, "openai")
        self.assertEqual(chain[1].provider, "localai")

    @patch("app.services.llm_runtime._runtime_int_from_repo")
    @patch("app.services.llm_runtime._runtime_bool_from_repo")
    @patch("app.services.llm_runtime.resolve_comp_llm_runtime_config")
    def test_resolve_chain_short_circuit_paths(self, mock_primary, mock_fallback_enabled, mock_max_profiles) -> None:
        primary_env = LLMRuntimeConfig(
            source="env",
            enabled=True,
            provider="openai",
            model="gpt-4o-mini",
            multimodal_model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            endpoint_type="responses",
            api_key="k",
            temperature=0.2,
            max_output_tokens=600,
            timeout_seconds=60,
        )
        mock_primary.return_value = primary_env
        mock_fallback_enabled.return_value = True
        mock_max_profiles.return_value = 3
        self.assertEqual(llm_runtime.resolve_comp_llm_runtime_chain(object()), [primary_env])

        primary_db = LLMRuntimeConfig(**{**primary_env.__dict__, "source": "db"})
        mock_primary.return_value = primary_db
        mock_fallback_enabled.return_value = False
        self.assertEqual(llm_runtime.resolve_comp_llm_runtime_chain(object()), [primary_db])

        class RepoErr:
            def list_ai_provider_configs(self, *, environment: str, active_only: bool):
                raise RuntimeError("boom")

        mock_fallback_enabled.return_value = True
        self.assertEqual(llm_runtime.resolve_comp_llm_runtime_chain(RepoErr()), [primary_db])

        class RepoEmpty:
            def list_ai_provider_configs(self, *, environment: str, active_only: bool):
                return []

        self.assertEqual(llm_runtime.resolve_comp_llm_runtime_chain(RepoEmpty()), [primary_db])

    @patch("app.services.llm_runtime._runtime_int_from_repo", return_value=8)
    @patch("app.services.llm_runtime._runtime_bool_from_repo", return_value=True)
    @patch("app.services.llm_runtime.resolve_comp_llm_runtime_config")
    def test_resolve_chain_hits_dedupe_continue(self, mock_primary, _mock_fallback_enabled, _mock_max_profiles) -> None:
        primary = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="openai",
            model="gpt-4o-mini",
            multimodal_model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            endpoint_type="responses",
            api_key="k",
            temperature=0.2,
            max_output_tokens=600,
            timeout_seconds=60,
        )
        mock_primary.return_value = primary
        dup = SimpleNamespace(
            is_default=False,
            is_active=True,
            provider="localai",
            model="llama",
            multimodal_model="",
            base_url="http://localai:8080/v1",
            endpoint_type="chat_completions",
            api_key="",
            temperature=0.1,
            max_output_tokens=800,
            timeout_seconds=45,
        )

        class Repo:
            def list_ai_provider_configs(self, *, environment: str, active_only: bool):
                return [dup, dup]

        chain = llm_runtime.resolve_comp_llm_runtime_chain(Repo())
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0].provider, "localai")

    def test_generate_comp_ai_summary_validates_and_parses_chat_and_responses(self) -> None:
        disabled_cfg = LLMRuntimeConfig(
            source="db",
            enabled=False,
            provider="openai",
            model="gpt-4o-mini",
            multimodal_model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            endpoint_type="responses",
            api_key="k",
            temperature=0.2,
            max_output_tokens=600,
            timeout_seconds=60,
        )
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            llm_runtime.generate_comp_ai_summary(disabled_cfg, query="q", ebay_rows=[], web_rows=[])

        missing_key_cfg = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="openai",
            model="gpt-4o-mini",
            multimodal_model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            endpoint_type="responses",
            api_key="",
            temperature=0.2,
            max_output_tokens=600,
            timeout_seconds=60,
        )
        with self.assertRaisesRegex(RuntimeError, "requires an API key"):
            llm_runtime.generate_comp_ai_summary(missing_key_cfg, query="q", ebay_rows=[], web_rows=[])

        chat_cfg = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="localai",
            model="llama",
            multimodal_model="llava",
            base_url="http://localai:8080/v1",
            endpoint_type="chat_completions",
            api_key="",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        chat_resp = Mock()
        chat_resp.raise_for_status.return_value = None
        chat_resp.json.return_value = {"choices": [{"message": {"content": "chat ok"}}]}
        with patch("app.services.llm_runtime.requests.post", return_value=chat_resp):
            text = llm_runtime.generate_comp_ai_summary(chat_cfg, query="q", ebay_rows=[], web_rows=[])
            self.assertEqual(text, "chat ok")

        resp_cfg = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="localai",
            model="llama",
            multimodal_model="llava",
            base_url="http://localai:8080/v1",
            endpoint_type="responses",
            api_key="",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        resp_obj = Mock()
        resp_obj.raise_for_status.return_value = None
        resp_obj.json.return_value = {"output": [{"content": [{"text": "responses ok"}]}]}
        with patch("app.services.llm_runtime.requests.post", return_value=resp_obj):
            text = llm_runtime.generate_comp_ai_summary(resp_cfg, query="q", ebay_rows=[], web_rows=[])
            self.assertEqual(text, "responses ok")

    def test_generate_comp_ai_summary_additional_error_paths(self) -> None:
        cfg = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="localai",
            model="",
            multimodal_model="llava",
            base_url="http://localai:8080/v1",
            endpoint_type="responses",
            api_key="token",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        with self.assertRaisesRegex(RuntimeError, "Text model is required"):
            llm_runtime.generate_comp_ai_summary(cfg, query="q", ebay_rows=[], web_rows=[])

        chat_cfg = LLMRuntimeConfig(**{**cfg.__dict__, "model": "m1", "endpoint_type": "chat_completions"})
        chat_resp = Mock()
        chat_resp.raise_for_status.return_value = None
        chat_resp.json.return_value = {"choices": [{"message": {"content": ""}}]}
        with patch("app.services.llm_runtime.requests.post", return_value=chat_resp):
            with self.assertRaisesRegex(RuntimeError, "chat completion text"):
                llm_runtime.generate_comp_ai_summary(chat_cfg, query="q", ebay_rows=[], web_rows=[])

        resp_cfg = LLMRuntimeConfig(**{**cfg.__dict__, "model": "m2", "endpoint_type": "responses"})
        resp_obj = Mock()
        resp_obj.raise_for_status.return_value = None
        resp_obj.json.return_value = {"output": {"content": []}}
        with patch("app.services.llm_runtime.requests.post", return_value=resp_obj):
            with self.assertRaisesRegex(RuntimeError, "did not contain text output"):
                llm_runtime.generate_comp_ai_summary(resp_cfg, query="q", ebay_rows=[], web_rows=[])

    @patch("app.services.llm_runtime.generate_comp_ai_summary")
    def test_generate_comp_ai_summary_with_fallback(self, mock_generate) -> None:
        cfg1 = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="openai",
            model="gpt-4o-mini",
            multimodal_model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            endpoint_type="responses",
            api_key="k",
            temperature=0.2,
            max_output_tokens=600,
            timeout_seconds=60,
        )
        cfg2 = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="localai",
            model="llama",
            multimodal_model="llava",
            base_url="http://localai:8080/v1",
            endpoint_type="chat_completions",
            api_key="",
            temperature=0.2,
            max_output_tokens=600,
            timeout_seconds=60,
        )

        mock_generate.side_effect = [RuntimeError("boom"), "ok"]
        text, used_cfg, errors = llm_runtime.generate_comp_ai_summary_with_fallback(
            [cfg1, cfg2],
            query="q",
            ebay_rows=[],
            web_rows=[],
        )
        self.assertEqual(text, "ok")
        self.assertEqual(used_cfg.model, "llama")
        self.assertEqual(len(errors), 1)
        self.assertIn("openai:gpt-4o-mini", errors[0])

        with self.assertRaisesRegex(RuntimeError, "No enabled AI runtime profiles available"):
            llm_runtime.generate_comp_ai_summary_with_fallback(
                [LLMRuntimeConfig(**{**cfg1.__dict__, "enabled": False})],
                query="q",
                ebay_rows=[],
                web_rows=[],
            )

        mock_generate.side_effect = RuntimeError("all bad")
        with self.assertRaisesRegex(RuntimeError, "All AI runtime fallback attempts failed"):
            llm_runtime.generate_comp_ai_summary_with_fallback(
                [cfg1],
                query="q",
                ebay_rows=[],
                web_rows=[],
            )

    def test_validate_llm_runtime_config_chat_and_responses(self) -> None:
        cfg_chat = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="localai",
            model="llama",
            multimodal_model="llava",
            base_url="http://localai:8080/v1",
            endpoint_type="chat_completions",
            api_key="",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        chat_resp = Mock()
        chat_resp.status_code = 200
        chat_resp.raise_for_status.return_value = None
        chat_resp.json.return_value = {"id": "chat_ok"}
        with patch("app.services.llm_runtime.requests.post", return_value=chat_resp):
            out = llm_runtime.validate_llm_runtime_config(cfg_chat)
            self.assertEqual(out["endpoint_type"], "chat_completions")
            self.assertEqual(out["response_id"], "chat_ok")

        cfg_resp = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="openai",
            model="gpt-4o-mini",
            multimodal_model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            endpoint_type="responses",
            api_key="k",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        resp = Mock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"id": "resp_ok"}
        with patch("app.services.llm_runtime.requests.post", return_value=resp):
            out = llm_runtime.validate_llm_runtime_config(cfg_resp)
            self.assertEqual(out["endpoint_type"], "responses")
            self.assertEqual(out["response_id"], "resp_ok")

    def test_models_endpoints_and_fetch_available_models(self) -> None:
        self.assertEqual(
            llm_runtime._models_endpoint_candidates("https://api.openai.com/v1"),
            ["https://api.openai.com/v1/models", "https://api.openai.com/models"],
        )
        self.assertEqual(
            llm_runtime._models_endpoint_candidates("https://api.example.com"),
            ["https://api.example.com/models", "https://api.example.com/v1/models"],
        )
        self.assertEqual(llm_runtime._models_endpoint_candidates(""), [])

        ok_resp = Mock()
        ok_resp.raise_for_status.return_value = None
        ok_resp.json.return_value = {"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-5"}]}
        with patch("app.services.llm_runtime.requests.get", return_value=ok_resp):
            models = llm_runtime.fetch_available_models(base_url="https://api.openai.com/v1", api_key="k", timeout_seconds=30)
            self.assertEqual(models, ["gpt-4o-mini", "gpt-5"])

        empty_resp = Mock()
        empty_resp.raise_for_status.return_value = None
        empty_resp.json.return_value = {"data": []}
        with patch("app.services.llm_runtime.requests.get", return_value=empty_resp):
            with self.assertRaisesRegex(RuntimeError, "Unable to load models from endpoint"):
                llm_runtime.fetch_available_models(base_url="https://api.openai.com/v1")

        with self.assertRaisesRegex(RuntimeError, "base URL is not configured"):
            llm_runtime.fetch_available_models(base_url="")

    def test_fetch_available_models_payload_variants(self) -> None:
        models_resp = Mock()
        models_resp.raise_for_status.return_value = None
        models_resp.json.return_value = {"models": ["m1", {"id": "m2"}, {"name": "m3"}]}
        with patch("app.services.llm_runtime.requests.get", return_value=models_resp):
            out = llm_runtime.fetch_available_models(base_url="http://x/v1", timeout_seconds=1)
        self.assertEqual(out, ["m1", "m2", "m3"])

        model_list_resp = Mock()
        model_list_resp.raise_for_status.return_value = None
        model_list_resp.json.return_value = {"model_list": [{"model_name": "z1"}, {"model": "z2"}, 5]}
        with patch("app.services.llm_runtime.requests.get", return_value=model_list_resp):
            out2 = llm_runtime.fetch_available_models(base_url="http://x/v1", timeout_seconds=1)
        self.assertEqual(out2, ["z1", "z2"])

    def test_extract_text_helpers(self) -> None:
        self.assertEqual(llm_runtime._extract_text_from_llm_payload({"output_text": "ok"}), "ok")
        self.assertEqual(
            llm_runtime._extract_text_from_llm_payload({"output": [{"content": [{"text": "a"}, {"text": "b"}]}]}),
            "a\nb",
        )
        self.assertEqual(
            llm_runtime._extract_text_from_llm_payload({"choices": [{"message": {"content": "chat text"}}]}),
            "chat text",
        )
        self.assertEqual(
            llm_runtime._extract_text_from_llm_payload(
                {"choices": [{"message": {"content": [{"text": "x"}, {"text": "y"}]}}]}
            ),
            "x\ny",
        )
        self.assertEqual(llm_runtime._extract_text_from_llm_payload({}), "")
        self.assertTrue(
            llm_runtime._looks_like_no_vision_capability_response("I cannot process or analyze images right now.")
        )
        self.assertFalse(llm_runtime._looks_like_no_vision_capability_response(""))
        self.assertFalse(llm_runtime._looks_like_no_vision_capability_response("Looks like a silver coin."))

    def test_generate_multimodal_ai_markdown_validations(self) -> None:
        disabled = LLMRuntimeConfig(
            source="db",
            enabled=False,
            provider="openai",
            model="gpt-4o-mini",
            multimodal_model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            endpoint_type="responses",
            api_key="k",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            llm_runtime.generate_multimodal_ai_markdown(
                disabled,
                system_message="sys",
                instruction="inst",
                image_bytes=b"x",
            )

        missing_key = LLMRuntimeConfig(**{**disabled.__dict__, "enabled": True, "api_key": ""})
        with self.assertRaisesRegex(RuntimeError, "requires an API key"):
            llm_runtime.generate_multimodal_ai_markdown(
                missing_key,
                system_message="sys",
                instruction="inst",
                image_bytes=b"x",
            )

        local_bad_endpoint = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="localai",
            model="llama",
            multimodal_model="llava",
            base_url="http://localai:8080/v1",
            endpoint_type="responses",
            api_key="",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        with self.assertRaisesRegex(RuntimeError, "chat_completions"):
            llm_runtime.generate_multimodal_ai_markdown(
                local_bad_endpoint,
                system_message="sys",
                instruction="inst",
                image_bytes=b"x",
            )

        missing_instruction = LLMRuntimeConfig(**{**local_bad_endpoint.__dict__, "endpoint_type": "chat_completions"})
        with self.assertRaisesRegex(RuntimeError, "Instruction is required"):
            llm_runtime.generate_multimodal_ai_markdown(
                missing_instruction,
                system_message="sys",
                instruction="",
                image_bytes=b"x",
            )

    def test_generate_multimodal_ai_markdown_chat_and_responses(self) -> None:
        chat_cfg = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="localai",
            model="glm",
            multimodal_model="glm-vision",
            base_url="http://localai:8080/v1",
            endpoint_type="chat_completions",
            api_key="",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        chat_resp = Mock()
        chat_resp.raise_for_status.return_value = None
        chat_resp.json.return_value = {"choices": [{"message": {"content": "vision ok"}}]}
        with patch("app.services.llm_runtime.requests.post", return_value=chat_resp):
            out = llm_runtime.generate_multimodal_ai_markdown(
                chat_cfg,
                system_message="sys",
                instruction="inst",
                image_bytes=b"img",
                additional_images=[(b"", "image/png"), (b"img2", "image/png")],
                max_output_tokens_override=900,
            )
            self.assertEqual(out, "vision ok")

        chat_empty = Mock()
        chat_empty.raise_for_status.return_value = None
        chat_empty.json.return_value = {"choices": [{"message": {"content": ""}}]}
        with patch("app.services.llm_runtime.requests.post", return_value=chat_empty):
            with self.assertRaisesRegex(RuntimeError, "did not contain text output"):
                llm_runtime.generate_multimodal_ai_markdown(
                    chat_cfg,
                    system_message="sys",
                    instruction="inst",
                    image_bytes=b"img",
                )

        bad_vision_resp = Mock()
        bad_vision_resp.raise_for_status.return_value = None
        bad_vision_resp.json.return_value = {
            "choices": [{"message": {"content": "I cannot process or analyze images. Please describe them."}}]
        }
        with patch("app.services.llm_runtime.requests.post", return_value=bad_vision_resp):
            with self.assertRaisesRegex(RuntimeError, "vision-capable"):
                llm_runtime.generate_multimodal_ai_markdown(
                    chat_cfg,
                    system_message="sys",
                    instruction="inst",
                    image_bytes=b"img",
                )

        responses_cfg = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="openai",
            model="gpt-4o-mini",
            multimodal_model="gpt-4o",
            base_url="https://api.openai.com/v1",
            endpoint_type="responses",
            api_key="k",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        responses_resp = Mock()
        responses_resp.raise_for_status.return_value = None
        responses_resp.json.return_value = {"output_text": "responses vision ok"}
        with patch("app.services.llm_runtime.requests.post", return_value=responses_resp):
            out = llm_runtime.generate_multimodal_ai_markdown(
                responses_cfg,
                system_message="sys",
                instruction="inst",
                image_bytes=b"img",
            )
            self.assertEqual(out, "responses vision ok")

        responses_no_text = Mock()
        responses_no_text.raise_for_status.return_value = None
        responses_no_text.json.return_value = {"output_text": "I cannot process or analyze images."}
        with patch("app.services.llm_runtime.requests.post", return_value=responses_no_text):
            with self.assertRaisesRegex(RuntimeError, "vision-capable"):
                llm_runtime.generate_multimodal_ai_markdown(
                    responses_cfg,
                    system_message="sys",
                    instruction="inst",
                    image_bytes=b"img",
                )

    def test_generate_comp_ai_summary_responses_direct_output_text(self) -> None:
        cfg = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="localai",
            model="llama",
            multimodal_model="llava",
            base_url="http://localai:8080/v1",
            endpoint_type="responses",
            api_key="",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        resp_obj = Mock()
        resp_obj.raise_for_status.return_value = None
        resp_obj.json.return_value = {"output_text": "direct text"}
        with patch("app.services.llm_runtime.requests.post", return_value=resp_obj):
            out = llm_runtime.generate_comp_ai_summary(cfg, query="q", ebay_rows=[], web_rows=[])
            self.assertEqual(out, "direct text")

    def test_generate_multimodal_ai_markdown_missing_model(self) -> None:
        cfg = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="localai",
            model="",
            multimodal_model="",
            base_url="http://localai:8080/v1",
            endpoint_type="chat_completions",
            api_key="",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        with self.assertRaisesRegex(RuntimeError, "Multimodal model is required"):
            llm_runtime.generate_multimodal_ai_markdown(
                cfg,
                system_message="sys",
                instruction="inst",
                image_bytes=b"img",
            )

    @patch("app.services.llm_runtime.generate_multimodal_ai_markdown")
    def test_generate_multimodal_with_fallback(self, mock_generate) -> None:
        cfg1 = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="localai",
            model="glm",
            multimodal_model="glm-vision",
            base_url="http://localai:8080/v1",
            endpoint_type="chat_completions",
            api_key="",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        cfg2 = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="openai",
            model="gpt-4o-mini",
            multimodal_model="gpt-4o",
            base_url="https://api.openai.com/v1",
            endpoint_type="responses",
            api_key="k",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        mock_generate.side_effect = [RuntimeError("bad"), "ok"]
        text, used, errors = llm_runtime.generate_multimodal_ai_markdown_with_fallback(
            [cfg1, cfg2],
            system_message="sys",
            instruction="inst",
            image_bytes=b"img",
        )
        self.assertEqual(text, "ok")
        self.assertEqual(used.provider, "openai")
        self.assertEqual(len(errors), 1)
        self.assertIn("localai:glm-vision", errors[0])

        with self.assertRaisesRegex(RuntimeError, "No enabled AI runtime profiles available"):
            llm_runtime.generate_multimodal_ai_markdown_with_fallback(
                [LLMRuntimeConfig(**{**cfg1.__dict__, "enabled": False})],
                system_message="sys",
                instruction="inst",
                image_bytes=b"img",
            )

    def test_generate_multimodal_with_fallback_all_fail(self) -> None:
        cfg = LLMRuntimeConfig(
            source="db",
            enabled=True,
            provider="localai",
            model="glm",
            multimodal_model="glm-vision",
            base_url="http://localai:8080/v1",
            endpoint_type="chat_completions",
            api_key="",
            temperature=0.2,
            max_output_tokens=400,
            timeout_seconds=20,
        )
        with patch("app.services.llm_runtime.generate_multimodal_ai_markdown", side_effect=RuntimeError("x")):
            with self.assertRaisesRegex(RuntimeError, "All multimodal fallback attempts failed"):
                llm_runtime.generate_multimodal_ai_markdown_with_fallback(
                    [cfg],
                    system_message="sys",
                    instruction="inst",
                    image_bytes=b"img",
                )


if __name__ == "__main__":
    unittest.main()
