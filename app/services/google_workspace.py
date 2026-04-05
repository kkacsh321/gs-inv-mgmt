import base64
from dataclasses import dataclass
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import uuid
from typing import Any

import requests

from app.services.runtime_settings import get_runtime_bool, get_runtime_str


@dataclass(frozen=True)
class GoogleWorkspaceConfig:
    enabled: bool
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes_csv: str
    sender_email: str
    drive_root_folder_id: str
    access_token: str
    refresh_token: str
    default_calendar_id: str
    default_timezone: str
    timeout_seconds: int


def resolve_google_workspace_config(repo: Any) -> GoogleWorkspaceConfig:
    return GoogleWorkspaceConfig(
        enabled=get_runtime_bool(repo, "google_integration_enabled", False),
        client_id=get_runtime_str(repo, "google_oauth_client_id", "").strip(),
        client_secret=get_runtime_str(repo, "google_oauth_client_secret", "").strip(),
        redirect_uri=get_runtime_str(repo, "google_oauth_redirect_uri", "").strip(),
        scopes_csv=get_runtime_str(repo, "google_workspace_scopes_csv", "").strip(),
        sender_email=get_runtime_str(repo, "google_default_sender_email", "sales@goldenstackers.com").strip(),
        drive_root_folder_id=get_runtime_str(repo, "google_drive_root_folder_id", "").strip(),
        access_token=get_runtime_str(repo, "google_oauth_access_token", "").strip(),
        refresh_token=get_runtime_str(repo, "google_oauth_refresh_token", "").strip(),
        default_calendar_id=get_runtime_str(repo, "google_default_calendar_id", "primary").strip() or "primary",
        default_timezone=get_runtime_str(repo, "google_default_timezone", "America/Denver").strip() or "America/Denver",
        timeout_seconds=max(5, min(120, int(get_runtime_str(repo, "google_http_timeout_seconds", "30") or "30"))),
    )


def _build_raw_gmail_message(
    *,
    sender: str,
    to: str,
    subject: str,
    body_text: str,
    body_html: str,
) -> str:
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["From"] = sender
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text or "", "plain", "utf-8"))
    msg.attach(MIMEText(body_html or "", "html", "utf-8"))
    raw_bytes = msg.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("utf-8")


def send_gmail_message(
    *,
    config: GoogleWorkspaceConfig,
    to_email: str,
    subject: str,
    body_html: str,
    body_text: str = "",
) -> dict[str, Any]:
    if not config.enabled:
        raise ValueError("Google integration is disabled (`google_integration_enabled=false`).")
    if not config.access_token:
        raise ValueError("Google access token is missing (`google_oauth_access_token`).")
    if not to_email.strip():
        raise ValueError("Recipient email is required.")
    if not subject.strip():
        raise ValueError("Subject is required.")

    sender = config.sender_email or "sales@goldenstackers.com"
    raw_message = _build_raw_gmail_message(
        sender=sender,
        to=to_email.strip(),
        subject=subject.strip(),
        body_text=body_text.strip() or "Please view the HTML version of this message.",
        body_html=body_html,
    )
    endpoint = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    resp = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {config.access_token}",
            "Content-Type": "application/json",
        },
        json={"raw": raw_message},
        timeout=config.timeout_seconds,
    )
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.text[:1000]
        except Exception:
            detail = "<unavailable>"
        raise RuntimeError(f"Gmail send failed: HTTP {resp.status_code}. Response: {detail}")
    data = resp.json()
    return {
        "id": str(data.get("id") or ""),
        "threadId": str(data.get("threadId") or ""),
        "labelIds": data.get("labelIds") or [],
        "sender": sender,
        "recipient": to_email.strip(),
        "subject": subject.strip(),
    }


def create_calendar_event(
    *,
    config: GoogleWorkspaceConfig,
    summary: str,
    start_iso: str,
    end_iso: str,
    description: str = "",
    timezone: str = "America/Denver",
    calendar_id: str = "primary",
) -> dict[str, Any]:
    if not config.enabled:
        raise ValueError("Google integration is disabled (`google_integration_enabled=false`).")
    if not config.access_token:
        raise ValueError("Google access token is missing (`google_oauth_access_token`).")
    if not summary.strip():
        raise ValueError("Event summary is required.")
    if not start_iso.strip() or not end_iso.strip():
        raise ValueError("Event start/end datetime values are required.")

    endpoint = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id or 'primary'}/events"
    payload = {
        "summary": summary.strip(),
        "description": description.strip(),
        "start": {"dateTime": start_iso.strip(), "timeZone": timezone.strip() or "America/Denver"},
        "end": {"dateTime": end_iso.strip(), "timeZone": timezone.strip() or "America/Denver"},
    }
    resp = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {config.access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=config.timeout_seconds,
    )
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.text[:1000]
        except Exception:
            detail = "<unavailable>"
        raise RuntimeError(f"Calendar create failed: HTTP {resp.status_code}. Response: {detail}")
    data = resp.json()
    return {
        "id": str(data.get("id") or ""),
        "htmlLink": str(data.get("htmlLink") or ""),
        "status": str(data.get("status") or ""),
        "calendar_id": calendar_id or "primary",
        "summary": summary.strip(),
    }


def upload_drive_file(
    *,
    config: GoogleWorkspaceConfig,
    file_name: str,
    file_bytes: bytes,
    mime_type: str,
    folder_id: str = "",
) -> dict[str, Any]:
    if not config.enabled:
        raise ValueError("Google integration is disabled (`google_integration_enabled=false`).")
    if not config.access_token:
        raise ValueError("Google access token is missing (`google_oauth_access_token`).")
    if not str(file_name or "").strip():
        raise ValueError("Drive upload file name is required.")
    if not isinstance(file_bytes, (bytes, bytearray)) or len(file_bytes) == 0:
        raise ValueError("Drive upload file content is empty.")

    metadata: dict[str, Any] = {"name": str(file_name).strip()}
    configured_drive_folder = (folder_id or "").strip() or (config.drive_root_folder_id or "").strip()
    if configured_drive_folder:
        metadata["parents"] = [configured_drive_folder]

    boundary = f"===============gsinv_{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime_type or 'application/octet-stream'}\r\n\r\n"
    ).encode("utf-8") + bytes(file_bytes) + f"\r\n--{boundary}--".encode("utf-8")

    endpoint = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink,webContentLink,mimeType"
    resp = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {config.access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        data=body,
        timeout=config.timeout_seconds,
    )
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.text[:1000]
        except Exception:
            detail = "<unavailable>"
        raise RuntimeError(f"Drive upload failed: HTTP {resp.status_code}. Response: {detail}")
    data = resp.json()
    return {
        "id": str(data.get("id") or ""),
        "name": str(data.get("name") or ""),
        "mimeType": str(data.get("mimeType") or ""),
        "webViewLink": str(data.get("webViewLink") or ""),
        "webContentLink": str(data.get("webContentLink") or ""),
    }
