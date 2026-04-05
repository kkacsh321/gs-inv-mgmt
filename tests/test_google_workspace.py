import base64
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services import google_workspace


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def _cfg(**overrides):
    base = {
        "enabled": True,
        "client_id": "cid",
        "client_secret": "sec",
        "redirect_uri": "https://x",
        "scopes_csv": "gmail.send",
        "sender_email": "sales@goldenstackers.com",
        "drive_root_folder_id": "",
        "access_token": "tok",
        "refresh_token": "rtok",
        "default_calendar_id": "primary",
        "default_timezone": "America/Denver",
        "timeout_seconds": 30,
    }
    base.update(overrides)
    return google_workspace.GoogleWorkspaceConfig(**base)


class GoogleWorkspaceTests(unittest.TestCase):
    def test_resolve_google_workspace_config(self) -> None:
        with patch("app.services.google_workspace.get_runtime_bool", return_value=True), patch(
            "app.services.google_workspace.get_runtime_str",
            side_effect=[
                "id",
                "secret",
                "https://redirect",
                "scope1,scope2",
                "sender@goldenstackers.com",
                "root",
                "atok",
                "rtok",
                "calendar1",
                "UTC",
                "999",
            ],
        ):
            cfg = google_workspace.resolve_google_workspace_config(object())
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.client_id, "id")
        self.assertEqual(cfg.default_calendar_id, "calendar1")
        self.assertEqual(cfg.default_timezone, "UTC")
        self.assertEqual(cfg.timeout_seconds, 120)

    def test_build_raw_gmail_message(self) -> None:
        raw = google_workspace._build_raw_gmail_message(
            sender="sales@goldenstackers.com",
            to="x@example.com",
            subject="Hello",
            body_text="plain",
            body_html="<b>html</b>",
        )
        decoded = base64.urlsafe_b64decode(raw.encode("utf-8"))
        self.assertIn(b"Subject: Hello", decoded)
        self.assertIn(b"To: x@example.com", decoded)

    def test_send_gmail_message_validation(self) -> None:
        with self.assertRaises(ValueError):
            google_workspace.send_gmail_message(config=_cfg(enabled=False), to_email="x@y.com", subject="s", body_html="h")
        with self.assertRaises(ValueError):
            google_workspace.send_gmail_message(config=_cfg(access_token=""), to_email="x@y.com", subject="s", body_html="h")
        with self.assertRaises(ValueError):
            google_workspace.send_gmail_message(config=_cfg(), to_email="", subject="s", body_html="h")
        with self.assertRaises(ValueError):
            google_workspace.send_gmail_message(config=_cfg(), to_email="x@y.com", subject="", body_html="h")

    def test_send_gmail_message_success_and_http_error(self) -> None:
        with patch(
            "app.services.google_workspace.requests.post",
            return_value=_FakeResp(payload={"id": "m1", "threadId": "t1", "labelIds": ["SENT"]}),
        ) as post:
            out = google_workspace.send_gmail_message(
                config=_cfg(), to_email="to@example.com", subject="Hi", body_html="<p>Hi</p>", body_text="Hi"
            )
        self.assertEqual(out["id"], "m1")
        self.assertEqual(out["recipient"], "to@example.com")
        self.assertIn("raw", post.call_args.kwargs["json"])

        with patch("app.services.google_workspace.requests.post", return_value=_FakeResp(status_code=500, text="boom")):
            with self.assertRaises(RuntimeError):
                google_workspace.send_gmail_message(config=_cfg(), to_email="to@example.com", subject="Hi", body_html="<p>Hi</p>")

    def test_create_calendar_event_validation_and_success(self) -> None:
        with self.assertRaises(ValueError):
            google_workspace.create_calendar_event(config=_cfg(enabled=False), summary="x", start_iso="a", end_iso="b")
        with self.assertRaises(ValueError):
            google_workspace.create_calendar_event(config=_cfg(access_token=""), summary="x", start_iso="a", end_iso="b")
        with self.assertRaises(ValueError):
            google_workspace.create_calendar_event(config=_cfg(), summary="", start_iso="a", end_iso="b")
        with self.assertRaises(ValueError):
            google_workspace.create_calendar_event(config=_cfg(), summary="x", start_iso="", end_iso="b")

        with patch(
            "app.services.google_workspace.requests.post",
            return_value=_FakeResp(payload={"id": "e1", "htmlLink": "http://x", "status": "confirmed"}),
        ):
            out = google_workspace.create_calendar_event(
                config=_cfg(),
                summary="Follow-up",
                start_iso="2026-03-30T10:00:00",
                end_iso="2026-03-30T10:30:00",
            )
        self.assertEqual(out["id"], "e1")

    def test_create_calendar_event_http_error(self) -> None:
        with patch("app.services.google_workspace.requests.post", return_value=_FakeResp(status_code=400, text="bad")):
            with self.assertRaises(RuntimeError):
                google_workspace.create_calendar_event(
                    config=_cfg(), summary="x", start_iso="2026-03-30T10:00:00", end_iso="2026-03-30T11:00:00"
                )

    def test_upload_drive_file_validation_and_success(self) -> None:
        with self.assertRaises(ValueError):
            google_workspace.upload_drive_file(config=_cfg(enabled=False), file_name="a.txt", file_bytes=b"x", mime_type="text/plain")
        with self.assertRaises(ValueError):
            google_workspace.upload_drive_file(config=_cfg(access_token=""), file_name="a.txt", file_bytes=b"x", mime_type="text/plain")
        with self.assertRaises(ValueError):
            google_workspace.upload_drive_file(config=_cfg(), file_name="", file_bytes=b"x", mime_type="text/plain")
        with self.assertRaises(ValueError):
            google_workspace.upload_drive_file(config=_cfg(), file_name="a.txt", file_bytes=b"", mime_type="text/plain")

        with patch(
            "app.services.google_workspace.requests.post",
            return_value=_FakeResp(payload={"id": "f1", "name": "a.txt", "mimeType": "text/plain"}),
        ) as post:
            out = google_workspace.upload_drive_file(
                config=_cfg(drive_root_folder_id="folder1"), file_name="a.txt", file_bytes=b"hello", mime_type="text/plain"
            )
        self.assertEqual(out["id"], "f1")
        headers = post.call_args.kwargs["headers"]
        self.assertIn("multipart/related", headers["Content-Type"])

    def test_upload_drive_file_http_error(self) -> None:
        with patch("app.services.google_workspace.requests.post", return_value=_FakeResp(status_code=500, text="boom")):
            with self.assertRaises(RuntimeError):
                google_workspace.upload_drive_file(config=_cfg(), file_name="a.txt", file_bytes=b"x", mime_type="text/plain")


if __name__ == "__main__":
    unittest.main()
