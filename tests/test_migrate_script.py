from __future__ import annotations

import importlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class _FakeConfig:
    def __init__(self, path: str) -> None:
        self.path = path


class MigrateScriptTests(unittest.TestCase):
    @staticmethod
    def _load_module_with_fakes(calls: list[tuple]):
        def _record(name):
            def _inner(*args, **kwargs):
                calls.append((name, args, kwargs))
                return None

            return _inner

        fake_alembic_command = SimpleNamespace(
            upgrade=_record("upgrade"),
            downgrade=_record("downgrade"),
            current=_record("current"),
            history=_record("history"),
            revision=_record("revision"),
        )
        fake_alembic_config = SimpleNamespace(Config=_FakeConfig)
        sys.modules.pop("app.db.migrate", None)
        with patch.dict(
            sys.modules,
            {
                "alembic": SimpleNamespace(command=fake_alembic_command),
                "alembic.command": fake_alembic_command,
                "alembic.config": fake_alembic_config,
            },
        ):
            return importlib.import_module("app.db.migrate")

    def test_alembic_wrappers_call_expected_commands(self):
        calls: list[tuple] = []
        migrate = self._load_module_with_fakes(calls)

        migrate.upgrade("head")
        migrate.downgrade("-1")
        migrate.current(verbose=True)
        migrate.history(verbose=True)
        migrate.revision("msg", autogenerate=False)

        names = [row[0] for row in calls]
        self.assertEqual(names, ["upgrade", "downgrade", "current", "history", "revision"])
        self.assertTrue(all(isinstance(row[1][0], _FakeConfig) for row in calls))

    def test_main_dispatches_each_subcommand(self):
        calls: list[tuple] = []
        migrate = self._load_module_with_fakes(calls)

        argv_sets = [
            ["migrate.py", "upgrade"],
            ["migrate.py", "downgrade", "-1"],
            ["migrate.py", "current", "--verbose"],
            ["migrate.py", "history", "--verbose"],
            ["migrate.py", "revision", "-m", "hello", "--no-autogenerate"],
        ]
        for argv in argv_sets:
            with patch("sys.argv", argv):
                migrate.main()

        names = [row[0] for row in calls]
        self.assertEqual(names, ["upgrade", "downgrade", "current", "history", "revision"])
        # Verify argument mapping for two representative commands.
        self.assertEqual(calls[1][1][1], "-1")  # downgrade revision arg
        self.assertEqual(calls[4][2]["message"], "hello")
        self.assertFalse(calls[4][2]["autogenerate"])


if __name__ == "__main__":
    unittest.main()
