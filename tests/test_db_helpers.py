import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


def _load_session_module():
    with patch("sqlalchemy.create_engine", return_value=object()), patch(
        "sqlalchemy.orm.sessionmaker", return_value=object()
    ):
        import app.db.session as db_session_module

        return importlib.reload(db_session_module)


def _load_migrate_module():
    if "alembic" not in sys.modules:
        fake_alembic = types.ModuleType("alembic")
        fake_command = types.ModuleType("alembic.command")
        fake_config = types.ModuleType("alembic.config")

        class _FakeConfig:
            def __init__(self, path: str):
                self.path = path

        fake_config.Config = _FakeConfig
        # Provide placeholder callables so patch.object can target them.
        fake_command.upgrade = lambda *args, **kwargs: None
        fake_command.downgrade = lambda *args, **kwargs: None
        fake_command.current = lambda *args, **kwargs: None
        fake_command.history = lambda *args, **kwargs: None
        fake_command.revision = lambda *args, **kwargs: None
        fake_alembic.command = fake_command
        fake_alembic.config = fake_config
        sys.modules["alembic"] = fake_alembic
        sys.modules["alembic.command"] = fake_command
        sys.modules["alembic.config"] = fake_config

    import app.db.migrate as migrate_module

    return importlib.reload(migrate_module)


class DbHelpersTests(unittest.TestCase):
    def test_session_module_builds_engine_and_sessionmaker(self) -> None:
        fake_engine = object()
        fake_sessionmaker = object()
        with patch("sqlalchemy.create_engine", return_value=fake_engine) as mock_create_engine, patch(
            "sqlalchemy.orm.sessionmaker", return_value=fake_sessionmaker
        ) as mock_sessionmaker:
            import app.db.session as db_session_module

            reloaded = importlib.reload(db_session_module)
            self.assertIs(reloaded.engine, fake_engine)
            self.assertIs(reloaded.SessionLocal, fake_sessionmaker)
            self.assertGreaterEqual(mock_create_engine.call_count, 1)
            self.assertGreaterEqual(mock_sessionmaker.call_count, 1)

    def test_init_db_executes_select_1(self) -> None:
        _load_session_module()
        import app.db.init_db as init_db_module
        init_db_module = importlib.reload(init_db_module)

        conn = Mock()
        ctx = Mock()
        ctx.__enter__ = Mock(return_value=conn)
        ctx.__exit__ = Mock(return_value=False)
        with patch.object(init_db_module, "engine", Mock(connect=Mock(return_value=ctx))):
            init_db_module.init_db()
        conn.execute.assert_called_once()
        self.assertIn("SELECT 1", str(conn.execute.call_args[0][0]))

    def test_migrate_helpers_call_alembic_commands(self) -> None:
        migrate_module = _load_migrate_module()

        with patch.object(migrate_module, "alembic_config", return_value="cfg"), patch.object(
            migrate_module.command, "upgrade"
        ) as m_upgrade, patch.object(
            migrate_module.command, "downgrade"
        ) as m_downgrade, patch.object(
            migrate_module.command, "current"
        ) as m_current, patch.object(
            migrate_module.command, "history"
        ) as m_history, patch.object(
            migrate_module.command, "revision"
        ) as m_revision:
            migrate_module.upgrade()
            migrate_module.upgrade("base")
            migrate_module.downgrade("base")
            migrate_module.current(verbose=True)
            migrate_module.history(verbose=False)
            migrate_module.revision("msg", autogenerate=False)

        m_upgrade.assert_any_call("cfg", "head")
        m_upgrade.assert_any_call("cfg", "base")
        m_downgrade.assert_called_once_with("cfg", "base")
        m_current.assert_called_once_with("cfg", verbose=True)
        m_history.assert_called_once_with("cfg", verbose=False)
        m_revision.assert_called_once_with("cfg", message="msg", autogenerate=False)

    def test_migrate_main_dispatch(self) -> None:
        migrate_module = _load_migrate_module()

        for args, expected in [
            (SimpleNamespace(command="upgrade"), "upgrade"),
            (SimpleNamespace(command="downgrade", revision="base"), "downgrade"),
            (SimpleNamespace(command="current", verbose=True), "current"),
            (SimpleNamespace(command="history", verbose=False), "history"),
            (
                SimpleNamespace(command="revision", message="hello", no_autogenerate=True),
                "revision",
            ),
        ]:
            with patch.object(migrate_module.argparse.ArgumentParser, "parse_args", return_value=args), patch.object(
                migrate_module, "upgrade"
            ) as m_upgrade, patch.object(
                migrate_module, "downgrade"
            ) as m_downgrade, patch.object(
                migrate_module, "current"
            ) as m_current, patch.object(
                migrate_module, "history"
            ) as m_history, patch.object(
                migrate_module, "revision"
            ) as m_revision:
                migrate_module.main()
                if expected == "upgrade":
                    m_upgrade.assert_called_once_with()
                elif expected == "downgrade":
                    m_downgrade.assert_called_once_with("base")
                elif expected == "current":
                    m_current.assert_called_once_with(verbose=True)
                elif expected == "history":
                    m_history.assert_called_once_with(verbose=False)
                elif expected == "revision":
                    m_revision.assert_called_once_with(message="hello", autogenerate=False)


if __name__ == "__main__":
    unittest.main()
