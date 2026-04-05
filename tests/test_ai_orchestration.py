import unittest
from unittest.mock import patch

from app.services.ai_orchestration import (
    AIExecutionResult,
    _build_citation,
    _append_prompt_context,
    execute_comp_summary,
    execute_multimodal_task,
)
from app.services.llm_runtime import LLMRuntimeConfig


def _cfg(*, provider: str = "openai", model: str = "gpt-4o-mini") -> LLMRuntimeConfig:
    return LLMRuntimeConfig(
        source="db",
        enabled=True,
        provider=provider,
        model=model,
        multimodal_model=f"{model}-vision",
        base_url="https://api.example.com/v1",
        endpoint_type="responses",
        api_key="secret",
        temperature=0.2,
        max_output_tokens=800,
        timeout_seconds=60,
    )


class AIOrchestrationTests(unittest.TestCase):
    def test_append_prompt_context_variants(self) -> None:
        self.assertEqual(_append_prompt_context("", label="X", context_text=""), "")
        self.assertEqual(_append_prompt_context("", label="Rules", context_text="abc"), "Rules:\nabc")
        self.assertEqual(_append_prompt_context("base", label="Rules", context_text="abc"), "base\n\nRules:\nabc")

    def test_build_citation_defaults(self) -> None:
        cfg = _cfg()
        citation = _build_citation(tool_name=" comp_summary ", used_config=cfg, fallback_errors=[], context=None)
        self.assertEqual(citation["tool_name"], "comp_summary")
        self.assertEqual(citation["provider"], "openai")
        self.assertEqual(citation["text_model"], "gpt-4o-mini")
        self.assertEqual(citation["fallback_attempts"], 0)
        self.assertEqual(citation["fallback_errors"], [])
        self.assertEqual(citation["context"], {})

    @patch("app.services.ai_orchestration.generate_comp_ai_summary_with_fallback")
    @patch("app.services.ai_orchestration.resolve_comp_llm_runtime_chain")
    def test_execute_comp_summary_returns_result_with_citation(
        self,
        mock_chain,
        mock_generate,
    ) -> None:
        cfg = _cfg(provider="localai", model="llama")
        mock_chain.return_value = [cfg]
        mock_generate.return_value = ("summary", cfg, ["primary timeout"])

        result = execute_comp_summary(
            repo=object(),
            query="1 oz silver eagle",
            ebay_rows=[{"title": "A"}],
            web_rows=[{"title": "B"}, {"title": "C"}],
            spot_context={"silver": 31.23},
            system_message="sys",
            instruction="inst",
        )
        self.assertIsInstance(result, AIExecutionResult)
        self.assertEqual(result.text, "summary")
        self.assertEqual(result.used_config.provider, "localai")
        self.assertEqual(result.fallback_errors, ["primary timeout"])
        self.assertEqual(result.citation["tool_name"], "comp_summary")
        self.assertEqual(result.citation["fallback_attempts"], 1)
        self.assertEqual(result.citation["context"]["query"], "1 oz silver eagle")
        self.assertEqual(result.citation["context"]["ebay_rows"], 1)
        self.assertEqual(result.citation["context"]["web_rows"], 2)

    @patch("app.services.ai_orchestration.generate_comp_ai_summary_with_fallback")
    @patch("app.services.ai_orchestration.resolve_comp_llm_runtime_chain")
    @patch("app.services.ai_orchestration.build_comp_rules_context_from_web")
    @patch("app.services.ai_orchestration.get_runtime_str")
    def test_execute_comp_summary_context_resolution_paths(
        self,
        mock_runtime_str,
        mock_build_web,
        mock_chain,
        mock_generate,
    ) -> None:
        cfg = _cfg(provider="localai", model="llama")
        mock_chain.return_value = [cfg]
        mock_generate.return_value = ("summary", cfg, [])

        # Runtime setting wins.
        mock_runtime_str.return_value = "runtime comp rules"
        mock_build_web.return_value = "web rules"
        execute_comp_summary(
            repo=object(),
            query="x",
            ebay_rows=[],
            web_rows=[],
            spot_context=None,
            system_message="sys",
            instruction="inst",
        )
        _args, kwargs = mock_generate.call_args
        self.assertIn("Comp Rules Context:\nruntime comp rules", kwargs["instruction"])

        # Web fallback when runtime missing.
        mock_runtime_str.return_value = ""
        mock_build_web.return_value = "web rules"
        execute_comp_summary(
            repo=object(),
            query="x",
            ebay_rows=[],
            web_rows=[],
            spot_context=None,
            system_message="sys",
            instruction="inst",
        )
        _args, kwargs = mock_generate.call_args
        self.assertIn("Comp Rules Context:\nweb rules", kwargs["instruction"])

        # Default fallback when runtime and web are empty.
        mock_runtime_str.return_value = ""
        mock_build_web.return_value = ""
        execute_comp_summary(
            repo=object(),
            query="x",
            ebay_rows=[],
            web_rows=[],
            spot_context=None,
            system_message="sys",
            instruction="inst",
        )
        _args, kwargs = mock_generate.call_args
        self.assertIn("Comp Rules Context:\n", kwargs["instruction"])

    @patch("app.services.ai_orchestration.generate_multimodal_ai_markdown_with_fallback")
    @patch("app.services.ai_orchestration.resolve_comp_llm_runtime_chain")
    def test_execute_multimodal_task_passes_through_and_builds_citation(
        self,
        mock_chain,
        mock_generate,
    ) -> None:
        cfg = _cfg()
        mock_chain.return_value = [cfg]
        mock_generate.return_value = ("vision-result", cfg, [])

        result = execute_multimodal_task(
            repo=object(),
            tool_name="coin_identifier",
            system_message="sys",
            instruction="inst",
            image_bytes=b"123",
            image_content_type="image/png",
            additional_images=[(b"456", "image/jpeg")],
            max_output_tokens_override=1200,
            context={"run_id": 99},
        )

        self.assertEqual(result.text, "vision-result")
        self.assertEqual(result.citation["tool_name"], "coin_identifier")
        self.assertEqual(result.citation["context"], {"run_id": 99})
        self.assertEqual(result.citation["fallback_attempts"], 0)

        mock_generate.assert_called_once()
        _args, kwargs = mock_generate.call_args
        self.assertEqual(kwargs["image_content_type"], "image/png")
        self.assertEqual(kwargs["max_output_tokens_override"], 1200)
        self.assertEqual(len(kwargs["additional_images"]), 1)

    @patch("app.services.ai_orchestration.generate_multimodal_ai_markdown_with_fallback")
    @patch("app.services.ai_orchestration.resolve_comp_llm_runtime_chain")
    @patch("app.services.ai_orchestration.build_coin_grading_rules_context_from_web")
    @patch("app.services.ai_orchestration.get_runtime_str")
    def test_execute_multimodal_task_grader_context_resolution(
        self,
        mock_runtime_str,
        mock_build_web,
        mock_chain,
        mock_generate,
    ) -> None:
        cfg = _cfg()
        mock_chain.return_value = [cfg]
        mock_generate.return_value = ("graded", cfg, [])

        mock_runtime_str.return_value = "runtime grading rules"
        mock_build_web.return_value = "web grading rules"
        execute_multimodal_task(
            repo=object(),
            tool_name="coin_grader",
            system_message="sys",
            instruction="inst",
            image_bytes=b"123",
        )
        _args, kwargs = mock_generate.call_args
        self.assertIn("Grading Rules Context:\nruntime grading rules", kwargs["instruction"])

        mock_runtime_str.return_value = ""
        mock_build_web.return_value = "web grading rules"
        execute_multimodal_task(
            repo=object(),
            tool_name="coin_grader",
            system_message="sys",
            instruction="inst",
            image_bytes=b"123",
        )
        _args, kwargs = mock_generate.call_args
        self.assertIn("Grading Rules Context:\nweb grading rules", kwargs["instruction"])


if __name__ == "__main__":
    unittest.main()
