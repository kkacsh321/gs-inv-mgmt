import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _bootstrap_view_imports() -> None:
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

    pkg_name = "app.components.views"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = []
        sys.modules[pkg_name] = pkg


def _load_view(name: str):
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"app.components.views.{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    sys.modules[f"app.components.views.{name}"] = module
    return module


class ViewImportCoverageTests(unittest.TestCase):
    def test_import_core_view_modules(self) -> None:
        _bootstrap_view_imports()
        ordered = [
            "shared",
            "workspace_shell",
            "entity_ops",
            "ebay_context",
            "search_edit",
            "system_health",
            "ai_chat",
            "lots",
            "ebay",
            "ebay_ops",
            "orders",
            "sales",
            "sync",
            "shipping",
            "operations_home",
            "products",
            "listings",
            "documents",
            "coin_intake_wizard",
            "inventory_intake_wizard",
            "tools",
            "ebay_workspace",
        ]
        loaded = {name: _load_view(name) for name in ordered}

        expected_render_attrs = {
            "search_edit": "render_search_edit",
            "system_health": "render_system_health",
            "ai_chat": "render_ai_chat",
            "lots": "render_lots",
            "ebay": "render_ebay",
            "ebay_ops": "render_ebay_ops",
            "orders": "render_orders",
            "sales": "render_sales",
            "sync": "render_sync",
            "shipping": "render_shipping",
            "operations_home": "render_operations_home",
            "products": "render_products",
            "listings": "render_listings",
            "documents": "render_documents",
            "coin_intake_wizard": "render_coin_intake_wizard",
            "inventory_intake_wizard": "render_inventory_intake_wizard",
            "tools": "render_tools",
            "ebay_workspace": "render_ebay_workspace",
        }
        for module_name, attr in expected_render_attrs.items():
            self.assertTrue(
                hasattr(loaded[module_name], attr),
                f"{module_name} missing expected callable {attr}",
            )


if __name__ == "__main__":
    unittest.main()
