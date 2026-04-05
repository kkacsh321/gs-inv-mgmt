import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services import slack_notify


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _FakeRepo:
    def __init__(self):
        self.events = []
        self.jobs = []

    def log_integration_event(self, **kwargs):
        self.events.append(kwargs)

    def create_integration_queue_job(self, **kwargs):
        self.jobs.append(kwargs)
        return SimpleNamespace(id=123)


class SlackNotifyTests(unittest.TestCase):
    def test_resolve_config_clamps_timeout(self) -> None:
        with patch("app.services.slack_notify.get_runtime_bool", side_effect=[True, True, False]), patch(
            "app.services.slack_notify.get_runtime_str", side_effect=["token", "#ops"]
        ), patch("app.services.slack_notify.get_runtime_int", return_value=999):
            cfg = slack_notify.resolve_slack_notify_config(object())
        self.assertEqual(cfg.timeout_seconds, 60)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.default_channel, "#ops")

    def test_send_slack_message_validation_errors(self) -> None:
        with patch(
            "app.services.slack_notify.resolve_slack_notify_config",
            return_value=SimpleNamespace(enabled=False, bot_token="", default_channel="", timeout_seconds=10),
        ):
            with self.assertRaises(ValueError):
                slack_notify.send_slack_message(object(), text="hello")

        with patch(
            "app.services.slack_notify.resolve_slack_notify_config",
            return_value=SimpleNamespace(enabled=True, bot_token="", default_channel="", timeout_seconds=10),
        ):
            with self.assertRaises(ValueError):
                slack_notify.send_slack_message(object(), text="hello")

        with patch(
            "app.services.slack_notify.resolve_slack_notify_config",
            return_value=SimpleNamespace(enabled=True, bot_token="tok", default_channel="", timeout_seconds=10),
        ):
            with self.assertRaises(ValueError):
                slack_notify.send_slack_message(object(), text="hello", channel="")

    def test_send_slack_message_success(self) -> None:
        cfg = SimpleNamespace(enabled=True, bot_token="x", default_channel="#default", timeout_seconds=10)
        with patch("app.services.slack_notify.resolve_slack_notify_config", return_value=cfg), patch(
            "app.services.slack_notify.requests.post",
            return_value=_FakeResp(payload={"ok": True, "channel": "C1", "ts": "123", "message": {"text": "ok"}}),
        ) as post:
            out = slack_notify.send_slack_message(object(), text="hello", channel="")
        self.assertEqual(out["channel"], "C1")
        self.assertEqual(out["ts"], "123")
        self.assertEqual(post.call_args.kwargs["json"]["channel"], "#default")

    def test_send_slack_message_http_and_api_error(self) -> None:
        cfg = SimpleNamespace(enabled=True, bot_token="x", default_channel="#default", timeout_seconds=10)
        with patch("app.services.slack_notify.resolve_slack_notify_config", return_value=cfg), patch(
            "app.services.slack_notify.requests.post", return_value=_FakeResp(status_code=500, text="server err")
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP error"):
                slack_notify.send_slack_message(object(), text="hello")

        with patch("app.services.slack_notify.resolve_slack_notify_config", return_value=cfg), patch(
            "app.services.slack_notify.requests.post", return_value=_FakeResp(payload={"ok": False, "error": "bad_auth"})
        ):
            with self.assertRaisesRegex(RuntimeError, "Slack API error"):
                slack_notify.send_slack_message(object(), text="hello")

    def test_resolve_slack_channel_precedence(self) -> None:
        values = {
            "slack_channel_local_sync_failure": "#sync-local",
            "slack_channel_warning": "#warn",
            "slack_default_channel": "#default",
        }

        def _get(_repo, key, default):
            return values.get(key, default)

        with patch("app.services.slack_notify.settings", SimpleNamespace(app_env="local")), patch(
            "app.services.slack_notify.get_runtime_str", side_effect=_get
        ):
            c1 = slack_notify.resolve_slack_channel(object(), event_type="sync_failure", severity="warning")
            c2 = slack_notify.resolve_slack_channel(object(), event_type="other", severity="warning")
            c3 = slack_notify.resolve_slack_channel(
                object(), event_type="sync_failure", severity="warning", override_channel="#override"
            )
        self.assertEqual(c1, "#sync-local")
        self.assertEqual(c2, "#warn")
        self.assertEqual(c3, "#override")

    def test_build_slack_alert_text_formats_with_fallback(self) -> None:
        with patch("app.services.slack_notify.get_runtime_str", return_value="Alert {env} {job_id} {missing}"), patch(
            "app.services.slack_notify.settings", SimpleNamespace(app_env="dev")
        ):
            text = slack_notify.build_slack_alert_text(
                object(),
                event_type="sync_failure",
                default_template="default",
                context={"job_id": 42},
            )
        self.assertIn("dev", text)
        self.assertIn("42", text)
        self.assertIn("{missing}", text)

    def test_build_slack_alert_text_returns_template_on_format_error(self) -> None:
        with patch("app.services.slack_notify.get_runtime_str", return_value="Alert {env"), patch(
            "app.services.slack_notify.settings", SimpleNamespace(app_env="dev")
        ):
            text = slack_notify.build_slack_alert_text(
                object(),
                event_type="sync_failure",
                default_template="default",
                context={"job_id": 42},
            )
        self.assertEqual(text, "Alert {env")

    def test_dispatch_slack_alert_sent(self) -> None:
        repo = _FakeRepo()
        with patch("app.services.slack_notify.resolve_slack_channel", return_value="#ops"), patch(
            "app.services.slack_notify.send_slack_message", return_value={"channel": "#ops", "ts": "1"}
        ), patch.object(repo, "log_integration_event", side_effect=RuntimeError("log failed")):
            out = slack_notify.dispatch_slack_alert(
                repo,
                actor="qa",
                text="hello",
                event_type="sync_failure",
                severity="warning",
            )
        self.assertEqual(out["status"], "sent")

    def test_dispatch_slack_alert_queues_on_failure(self) -> None:
        repo = _FakeRepo()
        with patch("app.services.slack_notify.resolve_slack_channel", return_value="#ops"), patch(
            "app.services.slack_notify.send_slack_message", side_effect=RuntimeError("boom")
        ), patch("app.services.slack_notify.get_runtime_bool", return_value=True), patch(
            "app.services.slack_notify.get_runtime_int", return_value=7
        ), patch("app.services.slack_notify.settings", SimpleNamespace(app_env="local")):
            out = slack_notify.dispatch_slack_alert(
                repo,
                actor="qa",
                text="hello",
                event_type="sync_failure",
            )
        self.assertEqual(out["status"], "queued")
        self.assertEqual(len(repo.jobs), 1)
        self.assertTrue(any(e.get("status") == "queued" for e in repo.events))

    def test_dispatch_slack_alert_queue_clamps_max_retries(self) -> None:
        repo = _FakeRepo()
        with patch("app.services.slack_notify.resolve_slack_channel", return_value="#ops"), patch(
            "app.services.slack_notify.send_slack_message", side_effect=RuntimeError("boom")
        ), patch("app.services.slack_notify.get_runtime_bool", return_value=True), patch(
            "app.services.slack_notify.get_runtime_int", return_value=999
        ), patch("app.services.slack_notify.settings", SimpleNamespace(app_env="local")):
            out = slack_notify.dispatch_slack_alert(repo, actor="qa", text="hello", event_type="sync_failure")
        self.assertEqual(out["status"], "queued")
        self.assertEqual(repo.jobs[0]["max_retries"], 20)

    def test_dispatch_slack_alert_raises_when_queue_disabled(self) -> None:
        repo = _FakeRepo()
        with patch("app.services.slack_notify.resolve_slack_channel", return_value="#ops"), patch(
            "app.services.slack_notify.send_slack_message", side_effect=RuntimeError("boom")
        ), patch("app.services.slack_notify.get_runtime_bool", return_value=False):
            with self.assertRaises(RuntimeError):
                slack_notify.dispatch_slack_alert(
                    repo,
                    actor="qa",
                    text="hello",
                    event_type="sync_failure",
                )

    def test_check_slack_connectivity_paths(self) -> None:
        disabled_cfg = SimpleNamespace(enabled=False, bot_token="", default_channel="", timeout_seconds=10)
        with patch("app.services.slack_notify.resolve_slack_notify_config", return_value=disabled_cfg):
            out = slack_notify.check_slack_connectivity(object())
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "disabled")

        missing_token_cfg = SimpleNamespace(enabled=True, bot_token="", default_channel="#ops", timeout_seconds=10)
        with patch("app.services.slack_notify.resolve_slack_notify_config", return_value=missing_token_cfg):
            out = slack_notify.check_slack_connectivity(object())
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "missing_token")

        missing_channel_cfg = SimpleNamespace(enabled=True, bot_token="t", default_channel="", timeout_seconds=10)
        with patch("app.services.slack_notify.resolve_slack_notify_config", return_value=missing_channel_cfg):
            out = slack_notify.check_slack_connectivity(object())
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "missing_channel")

        bad_http_cfg = SimpleNamespace(enabled=True, bot_token="t", default_channel="#ops", timeout_seconds=10)
        with patch("app.services.slack_notify.resolve_slack_notify_config", return_value=bad_http_cfg), patch(
            "app.services.slack_notify.requests.post", return_value=_FakeResp(status_code=500, text="server")
        ):
            out = slack_notify.check_slack_connectivity(object())
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "http_error")

        with patch("app.services.slack_notify.resolve_slack_notify_config", return_value=bad_http_cfg), patch(
            "app.services.slack_notify.requests.post", return_value=_FakeResp(payload={"ok": False, "error": "invalid_auth"})
        ):
            out = slack_notify.check_slack_connectivity(object())
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "invalid_auth")

        with patch("app.services.slack_notify.resolve_slack_notify_config", return_value=bad_http_cfg), patch(
            "app.services.slack_notify.requests.post",
            return_value=_FakeResp(payload={"ok": True, "team": "GS", "user": "bot", "url": "https://x.slack.com"}),
        ):
            out = slack_notify.check_slack_connectivity(object())
            self.assertTrue(out["ok"])
            self.assertEqual(out["reason"], "ok")


if __name__ == "__main__":
    unittest.main()
