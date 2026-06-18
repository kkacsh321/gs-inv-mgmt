from dataclasses import dataclass
import json
import re
from typing import Any

import requests

from app.config import settings
from app.services.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_str


@dataclass(frozen=True)
class SlackNotifyConfig:
    enabled: bool
    bot_token: str
    default_channel: str
    timeout_seconds: int
    notify_sync_failures: bool
    notify_google_queue_failures: bool


def resolve_slack_notify_config(repo: Any) -> SlackNotifyConfig:
    return SlackNotifyConfig(
        enabled=get_runtime_bool(repo, "slack_notifications_enabled", True),
        bot_token=get_runtime_str(repo, "slack_bot_token", "").strip(),
        default_channel=get_runtime_str(repo, "slack_default_channel", "").strip(),
        timeout_seconds=max(3, min(60, int(get_runtime_int(repo, "slack_http_timeout_seconds", 15)))),
        notify_sync_failures=get_runtime_bool(repo, "slack_notify_sync_failures", True),
        notify_google_queue_failures=get_runtime_bool(repo, "slack_notify_google_queue_failures", True),
    )


def send_slack_message(
    repo: Any,
    *,
    text: str,
    channel: str = "",
    thread_ts: str = "",
) -> dict[str, Any]:
    cfg = resolve_slack_notify_config(repo)
    if not cfg.enabled:
        raise ValueError("Slack notifications are disabled (`slack_notifications_enabled=false`).")
    if not cfg.bot_token:
        raise ValueError("Slack bot token is not configured (`slack_bot_token`).")
    target_channel = (channel or cfg.default_channel).strip()
    if not target_channel:
        raise ValueError("Slack channel is required (or set `slack_default_channel`).")
    resolved_text = (text or "").strip()
    if has_unresolved_slack_template_placeholders(resolved_text):
        raise ValueError("Slack message contains unresolved template placeholders; refusing to send.")

    body = {
        "channel": target_channel,
        "text": resolved_text[:4000],
        "mrkdwn": True,
    }
    thread_ts_value = str(thread_ts or "").strip()
    if thread_ts_value:
        body["thread_ts"] = thread_ts_value

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {cfg.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json=body,
        timeout=cfg.timeout_seconds,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Slack API HTTP error {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    if not bool(data.get("ok")):
        raise RuntimeError(f"Slack API error: {data.get('error')}")
    return {
        "channel": str(data.get("channel") or target_channel),
        "ts": str(data.get("ts") or ""),
        "message": data.get("message") or {},
    }


def resolve_slack_channel(
    repo: Any,
    *,
    event_type: str = "",
    severity: str = "warning",
    override_channel: str = "",
) -> str:
    direct = (override_channel or "").strip()
    if direct:
        return direct
    env = str(settings.app_env or "local").strip().lower()
    event_key = str(event_type or "").strip().lower().replace(" ", "_")
    severity_key = str(severity or "warning").strip().lower()
    candidates = []
    if event_key:
        candidates.append(f"slack_channel_{env}_{event_key}")
        candidates.append(f"slack_channel_{event_key}")
    if severity_key:
        candidates.append(f"slack_channel_{severity_key}")
    candidates.append("slack_default_channel")
    for key in candidates:
        value = get_runtime_str(repo, key, "").strip()
        if value:
            return value
    return ""


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + str(key) + "}"


def has_unresolved_slack_template_placeholders(text: str) -> bool:
    return bool(
        re.search(
            r"\{(?:job_name|status|run_id|processed|failed|actor|env)\}",
            str(text or ""),
        )
    )


def build_slack_alert_text(
    repo: Any,
    *,
    event_type: str,
    default_template: str,
    context: dict[str, Any] | None = None,
) -> str:
    key = f"slack_template_{str(event_type or '').strip().lower()}"
    template = get_runtime_str(repo, key, default_template or "").strip() or str(default_template or "")
    values = {k: str(v) for k, v in (context or {}).items()}
    values.setdefault("env", str(settings.app_env or "local"))
    try:
        rendered = template.format_map(_SafeFormatDict(values))
        if "{{" in template or "}}" in template:
            rendered = rendered.format_map(_SafeFormatDict(values))
        return rendered
    except Exception:
        return template


def dispatch_slack_alert(
    repo: Any,
    *,
    actor: str,
    text: str,
    event_type: str,
    severity: str = "warning",
    override_channel: str = "",
) -> dict[str, Any]:
    channel = resolve_slack_channel(
        repo,
        event_type=event_type,
        severity=severity,
        override_channel=override_channel,
    )
    try:
        result = send_slack_message(repo, text=text, channel=channel)
        try:
            repo.log_integration_event(
                actor=actor,
                integration="slack",
                action=f"dispatch_{event_type}",
                status="success",
                details={
                    "event_type": event_type,
                    "severity": severity,
                    "channel": result.get("channel", ""),
                    "ts": result.get("ts", ""),
                    "queued": False,
                },
            )
        except Exception:
            pass
        return {"status": "sent", "channel": result.get("channel", ""), "ts": result.get("ts", "")}
    except Exception as exc:
        queue_enabled = get_runtime_bool(repo, "slack_queue_enabled", True)
        if not queue_enabled:
            raise
        max_retries = max(0, min(20, int(get_runtime_int(repo, "slack_queue_max_retries", 5))))
        payload = {
            "text": (text or "").strip(),
            "channel": channel,
            "event_type": event_type,
            "severity": severity,
        }
        queued = repo.create_integration_queue_job(
            environment=settings.app_env,
            integration="slack",
            action="post_message",
            payload_json=json.dumps(payload),
            requested_by=(actor or "system").strip() or "system",
            max_retries=max_retries,
            actor=(actor or "system").strip() or "system",
        )
        try:
            repo.log_integration_event(
                actor=actor,
                integration="slack",
                action=f"dispatch_{event_type}",
                status="queued",
                details={
                    "event_type": event_type,
                    "severity": severity,
                    "queue_job_id": int(queued.id),
                    "error": str(exc)[:300],
                    "queued": True,
                },
            )
        except Exception:
            pass
        return {"status": "queued", "queue_job_id": int(queued.id), "error": str(exc)}


def check_slack_connectivity(repo: Any) -> dict[str, Any]:
    cfg = resolve_slack_notify_config(repo)
    if not cfg.enabled:
        return {"ok": False, "reason": "disabled", "details": "Slack notifications are disabled."}
    if not cfg.bot_token:
        return {"ok": False, "reason": "missing_token", "details": "Slack bot token is missing."}
    if not cfg.default_channel:
        return {"ok": False, "reason": "missing_channel", "details": "Slack default channel is missing."}

    resp = requests.post(
        "https://slack.com/api/auth.test",
        headers={
            "Authorization": f"Bearer {cfg.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={},
        timeout=cfg.timeout_seconds,
    )
    if resp.status_code >= 400:
        return {
            "ok": False,
            "reason": "http_error",
            "details": f"HTTP {resp.status_code}: {resp.text[:300]}",
        }
    data = resp.json()
    if not bool(data.get("ok")):
        return {"ok": False, "reason": str(data.get("error") or "api_error"), "details": str(data)}
    return {
        "ok": True,
        "reason": "ok",
        "details": f"team={data.get('team')} user={data.get('user')} url={data.get('url')}",
        "team": data.get("team"),
        "user": data.get("user"),
        "url": data.get("url"),
    }
