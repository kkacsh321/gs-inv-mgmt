from __future__ import annotations

import importlib
import io
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

class _FakeDB:
    def __init__(self) -> None:
        self.closed = False
        self._scalars: list[object | None] = []

    def set_scalars(self, values: list[object | None]) -> None:
        self._scalars = list(values)

    def scalar(self, _query):
        if self._scalars:
            return self._scalars.pop(0)
        return None

    def execute(self, _stmt):
        return None

    def commit(self):
        return None

    def close(self):
        self.closed = True


class _FakeRepo:
    def __init__(self, db: _FakeDB) -> None:
        self.db = db
        self._next_id = 1
        self._role_permissions: dict[str, set[str]] = {}

    def _obj(self, **kwargs):
        obj = SimpleNamespace(id=self._next_id, **kwargs)
        self._next_id += 1
        return obj

    def upsert_app_user(self, **_kwargs):
        return self._obj()

    def list_role_permissions(self):
        return {role: set(perms) for role, perms in self._role_permissions.items()}

    def set_role_permissions(self, role: str, permissions: set[str], **_kwargs):
        self._role_permissions[str(role)] = set(permissions or set())
        return None

    def create_purchase_lot(self, **_kwargs):
        return self._obj()

    def create_product(self, **kwargs):
        return self._obj(
            current_quantity=kwargs.get("current_quantity", 0),
            acquisition_cost=kwargs.get("acquisition_cost"),
        )

    def assign_product_to_lot(self, **_kwargs):
        return self._obj()

    def create_listing(self, **_kwargs):
        return self._obj()

    def create_sale(self, **_kwargs):
        return self._obj()

    def create_media_asset(self, **_kwargs):
        return self._obj()

    def create_coin_reference(self, **_kwargs):
        return self._obj()


class SeedTests(unittest.TestCase):
    @staticmethod
    def _load_seed_module():
        sys.modules.pop("app.db.seed", None)
        fake_session = SimpleNamespace(SessionLocal=lambda: _FakeDB())
        fake_repository = SimpleNamespace(InventoryRepository=_FakeRepo)
        with patch.dict(
            sys.modules,
            {
                "app.db.session": fake_session,
                "app.repository": fake_repository,
            },
        ):
            return importlib.import_module("app.db.seed")

    def test_utc_returns_naive_datetime(self):
        seed = self._load_seed_module()
        value = seed._utc(1)
        self.assertIsInstance(value, datetime)
        self.assertIsNone(value.tzinfo)

    def test_assert_not_prod_raises_for_prod(self):
        seed = self._load_seed_module()
        with patch.object(seed, "settings", SimpleNamespace(app_env="prod")):
            with self.assertRaises(RuntimeError):
                seed._assert_not_prod()

    def test_seed_dev_data_populates_expected_counts(self):
        seed = self._load_seed_module()
        fake_db = _FakeDB()

        # Seed flow calls db.scalar repeatedly to check for existing records.
        # Returning None for all checks forces inserts across all seed domains.
        fake_db.set_scalars([None] * 200)

        with (
            patch.object(seed, "SessionLocal", return_value=fake_db),
            patch.object(seed, "InventoryRepository", _FakeRepo),
            patch.object(seed, "_assert_not_prod", return_value=None),
        ):
            counts = seed.seed_dev_data(wipe=False)

        self.assertTrue(fake_db.closed)
        self.assertEqual(
            counts,
            {
                "lots": 3,
                "products": 6,
                "assignments": 6,
                "listings": 4,
                "sales": 3,
                "media": 3,
                "coin_refs": 3,
                "app_users": 2,
            },
        )

    def test_main_prints_seed_summary(self):
        seed = self._load_seed_module()
        out = io.StringIO()
        with (
            patch.object(
                seed,
                "seed_dev_data",
                return_value={
                    "lots": 1,
                    "products": 2,
                    "assignments": 3,
                    "listings": 4,
                    "sales": 5,
                    "media": 6,
                    "coin_refs": 7,
                    "app_users": 8,
                },
            ),
            patch("sys.argv", ["seed.py", "--wipe"]),
            redirect_stdout(out),
        ):
            seed.main()

        text = out.getvalue()
        self.assertIn("Seed complete.", text)
        self.assertIn("lots=1", text)
        self.assertIn("app_users=8", text)


if __name__ == "__main__":
    unittest.main()
