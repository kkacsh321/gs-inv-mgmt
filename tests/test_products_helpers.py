import importlib.util
import sys
import types
import unittest
from pathlib import Path


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


def _load_products_module():
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    products_path = root / "app" / "components" / "views" / "products.py"
    spec = importlib.util.spec_from_file_location("test_products_module", products_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


products = _load_products_module()


class ProductsHelpersTests(unittest.TestCase):
    def test_validate_product_create_inputs(self):
        self.assertEqual(
            products._validate_product_create_inputs(
                sku="",
                title="Silver Bar",
                ebay_purchase=False,
                ebay_purchase_item_id="",
            ),
            "SKU and title are required.",
        )
        self.assertEqual(
            products._validate_product_create_inputs(
                sku="SKU-1",
                title="Silver Bar",
                ebay_purchase=True,
                ebay_purchase_item_id="",
            ),
            "eBay Purchase Item ID is required when Purchased On eBay is enabled.",
        )
        self.assertIsNone(
            products._validate_product_create_inputs(
                sku="SKU-1",
                title="Silver Bar",
                ebay_purchase=True,
                ebay_purchase_item_id="123456",
            )
        )
        self.assertIsNone(
            products._validate_product_create_inputs(
                sku="SKU-1",
                title="Silver Bar",
                ebay_purchase=False,
                ebay_purchase_item_id="",
            )
        )

    def test_validate_product_edit_ebay_inputs(self):
        self.assertEqual(
            products._validate_product_edit_ebay_inputs(True, ""),
            "eBay Purchase Item ID is required when Purchased On eBay is enabled.",
        )
        self.assertIsNone(products._validate_product_edit_ebay_inputs(True, "123456"))
        self.assertIsNone(products._validate_product_edit_ebay_inputs(False, ""))

    def test_product_ebay_fields_are_editable_in_create_and_edit(self):
        self.assertFalse(products._product_ebay_fields_disabled(ebay_purchase=False, context="create"))
        self.assertFalse(products._product_ebay_fields_disabled(ebay_purchase=True, context="create"))
        self.assertFalse(products._product_ebay_fields_disabled(ebay_purchase=False, context="edit"))
        self.assertFalse(products._product_ebay_fields_disabled(ebay_purchase=True, context="edit"))


if __name__ == "__main__":
    unittest.main()
