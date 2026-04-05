import importlib.util
import unittest
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / "workspace_shell.py"
    spec = importlib.util.spec_from_file_location("test_workspace_shell_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


ws = _load_module()


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeSt:
    def __init__(self, *, button_values=None):
        self.button_values = list(button_values or [])
        self.infos = []
        self.errors = []
        self.successes = []

    def caption(self, *_a, **_k):
        return None

    def markdown(self, *_a, **_k):
        return None

    def columns(self, n):
        count = len(n) if isinstance(n, (list, tuple)) else int(n)
        return [_Ctx() for _ in range(count)]

    def radio(self, _label, options, **_k):
        return list(options)[0]

    def text_input(self, _label, value="", **_k):
        return value

    def selectbox(self, _label, options, **_k):
        opts = list(options)
        if not opts:
            return None
        if "Sync Run" in str(_label) and len(opts) > 1:
            return opts[1]
        return opts[0]

    def button(self, *_a, **_k):
        if self.button_values:
            return bool(self.button_values.pop(0))
        return False

    def success(self, msg):
        self.successes.append(str(msg))

    def error(self, msg):
        self.errors.append(str(msg))

    def info(self, msg):
        self.infos.append(str(msg))


class WorkspaceShellTests(unittest.TestCase):
    def test_status_semantic_helpers(self):
        self.assertEqual(ws.normalize_status_semantic("draft"), "needs_action")
        self.assertEqual(ws.normalize_status_semantic("active"), "in_progress")
        self.assertEqual(ws.normalize_status_semantic("failed"), "blocked")
        self.assertEqual(ws.normalize_status_semantic("completed"), "done")
        self.assertEqual(ws.normalize_status_semantic("other"), "unknown")
        chip = ws.status_semantic_chip("active")
        self.assertIn("in progress", chip)

    def test_feedback_and_task_completion(self):
        fake_st = _FakeSt(button_values=[True, True])
        calls = []

        class Repo:
            def record_audit_event(self, **kwargs):
                calls.append(kwargs)

        repo = Repo()
        orig = ws.st
        try:
            ws.st = fake_st
            ws.render_workspace_feedback(repo=repo, actor="admin", workspace_key="ops")
            ws.render_workspace_task_completion(
                repo=repo,
                actor="admin",
                workflow_key="ops",
                tasks=[("Did thing", "did_thing")],
            )
        finally:
            ws.st = orig
        self.assertEqual(len(calls), 2)
        self.assertTrue(fake_st.successes)

    def test_task_completion_without_tasks(self):
        fake_st = _FakeSt()
        orig = ws.st
        try:
            ws.st = fake_st
            ws.render_workspace_task_completion(
                repo=object(),
                actor="admin",
                workflow_key="ops",
                tasks=[],
            )
        finally:
            ws.st = orig
        self.assertTrue(fake_st.infos)

    def test_command_rail_state(self):
        fake_st = _FakeSt(button_values=[True, False, True, False, True, False, True, False, True])
        orig = ws.st
        try:
            ws.st = fake_st
            state = ws.render_ebay_command_rail(
                key_prefix="ebay_ops",
                selected_count=2,
                sandbox_seller_ops_blocked=False,
                sync_run_options={"Run 1": 101},
            )
        finally:
            ws.st = orig
        self.assertTrue(state.run_end_selected)
        self.assertTrue(state.run_add_selected_to_revise)
        self.assertTrue(state.clear_revise_queue)
        self.assertFalse(state.retry_run_now)
        self.assertTrue(state.resolve_run_errors)
        self.assertEqual(state.selected_sync_run_id, 101)


if __name__ == "__main__":
    unittest.main()
