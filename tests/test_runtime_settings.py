import unittest
from types import SimpleNamespace

from app.services import runtime_settings


class _FakeRepo:
    def __init__(self, rows: dict[str, SimpleNamespace] | None = None, raise_error: bool = False) -> None:
        self.rows = rows or {}
        self.raise_error = raise_error

    def get_runtime_setting(self, *, environment: str, key: str, active_only: bool = True):
        if self.raise_error:
            raise RuntimeError("db unavailable")
        return self.rows.get(key)

    def list_runtime_settings(self, *, environment: str, active_only: bool = False):
        if self.raise_error:
            raise RuntimeError("db unavailable")
        return [
            SimpleNamespace(key=key, value=row.value, value_type=row.value_type)
            for key, row in self.rows.items()
        ]


class RuntimeSettingsTests(unittest.TestCase):
    def test_to_bool_variants(self) -> None:
        self.assertTrue(runtime_settings._to_bool("yes", False))
        self.assertFalse(runtime_settings._to_bool("off", True))
        self.assertTrue(runtime_settings._to_bool("unknown", True))

    def test_to_int_and_to_float_fallback(self) -> None:
        self.assertEqual(runtime_settings._to_int("42", 0), 42)
        self.assertEqual(runtime_settings._to_int("bad", 7), 7)
        self.assertAlmostEqual(runtime_settings._to_float("3.14", 0.0), 3.14)
        self.assertAlmostEqual(runtime_settings._to_float("bad", 2.5), 2.5)

    def test_get_runtime_value_returns_default_on_repo_error(self) -> None:
        repo = _FakeRepo(raise_error=True)
        self.assertEqual(runtime_settings.get_runtime_value(repo, "missing", "fallback"), "fallback")

    def test_get_runtime_value_supports_typed_rows(self) -> None:
        repo = _FakeRepo(
            rows={
                "flag": SimpleNamespace(value="true", value_type="bool"),
                "count": SimpleNamespace(value="12", value_type="int"),
                "ratio": SimpleNamespace(value="2.75", value_type="float"),
                "obj": SimpleNamespace(value='{"a":1}', value_type="json"),
                "text": SimpleNamespace(value="hello", value_type="str"),
            }
        )
        self.assertTrue(runtime_settings.get_runtime_value(repo, "flag", False))
        self.assertEqual(runtime_settings.get_runtime_value(repo, "count", 0), 12)
        self.assertAlmostEqual(runtime_settings.get_runtime_value(repo, "ratio", 0.0), 2.75)
        self.assertEqual(runtime_settings.get_runtime_value(repo, "obj", {}), {"a": 1})
        self.assertEqual(runtime_settings.get_runtime_value(repo, "text", "x"), "hello")

    def test_get_runtime_value_handles_bad_json(self) -> None:
        repo = _FakeRepo(rows={"obj": SimpleNamespace(value="{oops", value_type="json")})
        self.assertEqual(runtime_settings.get_runtime_value(repo, "obj", {"k": "v"}), {"k": "v"})

    def test_get_runtime_bool_int_float_str(self) -> None:
        repo = _FakeRepo(
            rows={
                "bool_key": SimpleNamespace(value="1", value_type="str"),
                "int_key": SimpleNamespace(value="5", value_type="str"),
                "float_key": SimpleNamespace(value="1.25", value_type="str"),
                "str_key": SimpleNamespace(value=None, value_type="str"),
            }
        )
        self.assertTrue(runtime_settings.get_runtime_bool(repo, "bool_key", False))
        self.assertEqual(runtime_settings.get_runtime_int(repo, "int_key", 0), 5)
        self.assertAlmostEqual(runtime_settings.get_runtime_float(repo, "float_key", 0.0), 1.25)
        self.assertEqual(runtime_settings.get_runtime_str(repo, "str_key", "fallback"), "fallback")

    def test_get_runtime_values_bulk_resolution(self) -> None:
        repo = _FakeRepo(
            rows={
                "flag": SimpleNamespace(value="true", value_type="bool"),
                "count": SimpleNamespace(value="12", value_type="int"),
                "obj": SimpleNamespace(value='{"a":1}', value_type="json"),
            }
        )
        resolved = runtime_settings.get_runtime_values(
            repo,
            {"flag": False, "count": 0, "obj": {}, "missing": "fallback"},
        )
        self.assertEqual(
            resolved,
            {"flag": True, "count": 12, "obj": {"a": 1}, "missing": "fallback"},
        )

    def test_get_runtime_values_returns_defaults_on_repo_error(self) -> None:
        repo = _FakeRepo(raise_error=True)
        resolved = runtime_settings.get_runtime_values(repo, {"a": "x", "b": 2})
        self.assertEqual(resolved, {"a": "x", "b": 2})

    def test_is_ai_domain_enabled_uses_default_and_override(self) -> None:
        empty_repo = _FakeRepo()
        self.assertTrue(runtime_settings.is_ai_domain_enabled(empty_repo, "chat"))
        self.assertTrue(runtime_settings.is_ai_domain_enabled(empty_repo, "unknown_domain"))

        repo = _FakeRepo(
            rows={"ai_domain_chat_enabled": SimpleNamespace(value="false", value_type="bool")}
        )
        self.assertFalse(runtime_settings.is_ai_domain_enabled(repo, "chat"))


if __name__ == "__main__":
    unittest.main()
