import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


def _bootstrap_views_package() -> None:
    if "boto3" not in sys.modules:
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.session = types.SimpleNamespace(Session=lambda *args, **kwargs: None)
        sys.modules["boto3"] = fake_boto3
    if "botocore" not in sys.modules:
        sys.modules["botocore"] = types.ModuleType("botocore")
    if "botocore.config" not in sys.modules:
        fake_botocore_config = types.ModuleType("botocore.config")
        fake_botocore_config.Config = lambda *args, **kwargs: None
        sys.modules["botocore.config"] = fake_botocore_config
    if "botocore.exceptions" not in sys.modules:
        fake_botocore_exceptions = types.ModuleType("botocore.exceptions")
        fake_botocore_exceptions.BotoCoreError = Exception
        fake_botocore_exceptions.ClientError = Exception
        sys.modules["botocore.exceptions"] = fake_botocore_exceptions
    if "app.components.views" not in sys.modules:
        pkg = types.ModuleType("app.components.views")
        pkg.__path__ = []
        sys.modules["app.components.views"] = pkg

    root = Path(__file__).resolve().parents[1]
    for name in ("shared", "workspace_shell", "entity_ops"):
        full_name = f"app.components.views.{name}"
        if full_name in sys.modules:
            continue
        mod_path = root / "app" / "components" / "views" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(full_name, mod_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        sys.modules[full_name] = module


def _load_module(name: str):
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    module_path = root / "app" / "components" / "views" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


coin_intake_wizard = _load_module("coin_intake_wizard")
inventory_intake_wizard = _load_module("inventory_intake_wizard")


class _FakeSt:
    def __init__(self, session_state=None):
        self.session_state = dict(session_state or {})


class IntakeWizardPrefillHelperTests(unittest.TestCase):
    def test_coin_prefill_force_identifier_updates_form_fields(self):
        original_st = coin_intake_wizard.st
        try:
            coin_intake_wizard.st = _FakeSt(
                {
                    "coin_intake_prefill_title": "1909-S VDB Lincoln Cent",
                    "coin_intake_prefill_metal": "copper",
                    "coin_intake_prefill_description": (
                        "Key date cent with clear diagnostics, strong strike characteristics, and "
                        "detailed observed condition notes suitable for listing copy."
                    ),
                    "coin_intake_prefill_ai_description": "AI draft description",
                    "coin_intake_form_product_title": "Old",
                    "coin_intake_form_metal_type": "silver",
                    "coin_intake_form_product_description": "Old desc",
                    "coin_intake_form_ai_description": "Old ai",
                }
            )
            coin_intake_wizard._apply_coin_intake_prefill_to_form_state(
                selected_ref=None,
                force_identifier=True,
                force_grader=False,
                force_comp=False,
                quality_policy={},
            )
            state = coin_intake_wizard.st.session_state
            self.assertEqual(state["coin_intake_form_product_title"], "1909-S VDB Lincoln Cent")
            self.assertEqual(state["coin_intake_form_metal_type"], "copper")
            self.assertEqual(
                state["coin_intake_form_product_description"],
                (
                    "Key date cent with clear diagnostics, strong strike characteristics, and "
                    "detailed observed condition notes suitable for listing copy."
                ),
            )
            self.assertEqual(state["coin_intake_form_ai_description"], "AI draft description")
        finally:
            coin_intake_wizard.st = original_st

    def test_coin_prefill_force_grader_and_comp_updates_ai_fields(self):
        original_st = coin_intake_wizard.st
        try:
            coin_intake_wizard.st = _FakeSt(
                {
                    "coin_intake_prefill_ai_grading": "Likely AU-55 with light rub.",
                    "coin_intake_prefill_ai_comp": "Recent sold comps: $120-$145.",
                    "coin_intake_form_ai_graded": False,
                    "coin_intake_form_ai_grading_description": "",
                    "coin_intake_form_ai_comp": "",
                }
            )
            coin_intake_wizard._apply_coin_intake_prefill_to_form_state(
                selected_ref=None,
                force_identifier=False,
                force_grader=True,
                force_comp=True,
                quality_policy={},
            )
            state = coin_intake_wizard.st.session_state
            self.assertTrue(state["coin_intake_form_ai_graded"])
            self.assertEqual(state["coin_intake_form_ai_grading_description"], "Likely AU-55 with light rub.")
            self.assertEqual(state["coin_intake_form_ai_comp"], "Recent sold comps: $120-$145.")
        finally:
            coin_intake_wizard.st = original_st

    def test_inventory_prefill_no_force_preserves_existing_fields(self):
        original_st = inventory_intake_wizard.st
        try:
            inventory_intake_wizard.st = _FakeSt(
                {
                    "inv_intake_default_title": "1oz Silver Round",
                    "inv_intake_default_metal_type": "silver",
                    "inv_intake_default_description": "Default description",
                    "inv_intake_default_ai_description": "Default AI description",
                    "coin_grader_last_result": "MS-63",
                    "inv_intake_default_ai_comp": "Comp text",
                    "inv_intake_form_title": "Existing title",
                    "inv_intake_form_metal_type": "gold",
                    "inv_intake_form_description": "Existing desc",
                    "inv_intake_form_ai_description": "Existing ai desc",
                    "inv_intake_form_ai_grading_description": "Existing grade",
                    "inv_intake_form_ai_comp": "Existing comp",
                }
            )
            inventory_intake_wizard._apply_inventory_intake_ai_defaults_to_form_state(
                force_identifier=False,
                force_grader=False,
                force_comp=False,
            )
            state = inventory_intake_wizard.st.session_state
            self.assertEqual(state["inv_intake_form_title"], "Existing title")
            self.assertEqual(state["inv_intake_form_metal_type"], "gold")
            self.assertEqual(state["inv_intake_form_description"], "Existing desc")
            self.assertEqual(state["inv_intake_form_ai_description"], "Existing ai desc")
            self.assertEqual(state["inv_intake_form_ai_grading_description"], "Existing grade")
            self.assertEqual(state["inv_intake_form_ai_comp"], "Existing comp")
        finally:
            inventory_intake_wizard.st = original_st

    def test_inventory_prefill_force_grader_sets_ai_graded(self):
        original_st = inventory_intake_wizard.st
        try:
            inventory_intake_wizard.st = _FakeSt(
                {
                    "coin_grader_last_result": "AU Details - Cleaned",
                    "inv_intake_form_ai_graded": False,
                    "inv_intake_form_ai_grading_description": "",
                }
            )
            inventory_intake_wizard._apply_inventory_intake_ai_defaults_to_form_state(
                force_identifier=False,
                force_grader=True,
                force_comp=False,
            )
            state = inventory_intake_wizard.st.session_state
            self.assertTrue(state["inv_intake_form_ai_graded"])
            self.assertEqual(state["inv_intake_form_ai_grading_description"], "AU Details - Cleaned")
        finally:
            inventory_intake_wizard.st = original_st

    def test_coin_prefill_force_identifier_uses_selected_ref_fallback_when_prefill_weak(self):
        original_st = coin_intake_wizard.st
        try:
            coin_intake_wizard.st = _FakeSt(
                {
                    "coin_intake_prefill_title": "coin",
                    "coin_intake_prefill_description": "coin",
                    "coin_intake_form_product_title": "",
                    "coin_intake_form_product_description": "",
                }
            )
            selected_ref = SimpleNamespace(
                coin_name="Morgan Dollar",
                country="United States",
                denomination="$1",
                series="Morgan",
                year_start=1878,
                year_end=1921,
                metal_type="silver",
            )
            coin_intake_wizard._apply_coin_intake_prefill_to_form_state(
                selected_ref=selected_ref,
                force_identifier=True,
                force_grader=False,
                force_comp=False,
                quality_policy={},
            )
            state = coin_intake_wizard.st.session_state
            self.assertEqual(state["coin_intake_form_product_title"], "Morgan Dollar")
            self.assertIn("United States", state["coin_intake_form_product_description"])
        finally:
            coin_intake_wizard.st = original_st


if __name__ == "__main__":
    unittest.main()
