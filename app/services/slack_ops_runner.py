from __future__ import annotations

import time
from datetime import datetime, UTC
from typing import Any

from app.config import settings
from app.services.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_str
from app.services.slack_ops_bot import SUPPORTED_INTENTS
from app.utils.time import utcnow_naive


def _log(message: str) -> None:
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"[slack-ops-runner] {stamp} {message}", flush=True)


def _csv_normalized_set(raw: str) -> set[str]:
    values: set[str] = set()
    for token in str(raw or "").replace("\n", ",").split(","):
        value = str(token or "").strip().lower()
        if value:
            values.add(value)
    return values


def _role_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in str(raw or "").replace("\n", ",").split(","):
        pair = str(token or "").strip()
        if ":" not in pair:
            continue
        left, right = pair.split(":", 1)
        key = str(left or "").strip().lower()
        role = str(right or "").strip().lower()
        if key and role in {"admin", "ops", "viewer"}:
            out[key] = role
    return out


def _resolve_role(*, slack_user_id: str, slack_username: str, fallback_role: str, role_map: dict[str, str]) -> str:
    fallback = str(fallback_role or "viewer").strip().lower()
    if fallback not in {"admin", "ops", "viewer"}:
        fallback = "viewer"
    user_id = str(slack_user_id or "").strip().lower()
    username = str(slack_username or "").strip().lower()
    if user_id and user_id in role_map:
        return role_map[user_id]
    if username and username in role_map:
        return role_map[username]
    return fallback


def _strip_mention_prefix(text: str, *, bot_user_id: str) -> str:
    value = str(text or "").strip()
    bot_id = str(bot_user_id or "").strip()
    if bot_id:
        token = f"<@{bot_id}>"
        if value.startswith(token):
            value = value[len(token) :].strip()
    return value


def _normalize_command_text(text: str, *, bot_user_id: str, command_prefix: str) -> str:
    value = _strip_mention_prefix(text, bot_user_id=bot_user_id)
    prefix = str(command_prefix or "").strip().lower()
    if prefix:
        lowered = value.lower()
        if lowered.startswith(prefix + " "):
            value = value[len(prefix) + 1 :].strip()
        elif lowered == prefix:
            value = ""
    return value


def _help_text() -> str:
    intents = ", ".join(sorted(SUPPORTED_INTENTS))
    return (
        "*GoldenStackers Slack Ops*\n"
        "Use one of these intents: "
        f"`{intents}`\n"
        "Examples:\n"
        "- `comp 1oz copper round`\n"
        "- `intake 1881 Morgan dollar`\n"
        "- `status sync`\n"
        "- `operations run due queue`\n"
        "You can attach images/files for comp/intake."
    )


def _post_reply(client: Any, *, channel_id: str, text: str, thread_ts: str = "") -> None:
    if not channel_id:
        return
    payload: dict[str, Any] = {"channel": channel_id, "text": (text or "")[:4000], "mrkdwn": True}
    thread_value = str(thread_ts or "").strip()
    if thread_value:
        payload["thread_ts"] = thread_value
    client.web_client.chat_postMessage(**payload)


def _handle_command(
    *,
    repo: Any,
    client: Any,
    command_payload: dict[str, Any],
    actor: str,
) -> None:
    from app.services.slack_ops_bot import ingest_slack_command_request

    response = ingest_slack_command_request(
        repo,
        payload=command_payload,
        default_env=settings.app_env,
        actor=actor,
    )
    status = str(response.get("status") or "").strip().lower()
    reason = str(response.get("reason") or "").strip()
    queue_job_id = int(response.get("queue_job_id") or 0)
    channel_id = str(command_payload.get("channel_id") or "").strip()
    thread_ts = str(command_payload.get("thread_ts") or command_payload.get("message_ts") or "").strip()
    command_text = str(command_payload.get("text") or "").strip()

    if not command_text:
        _post_reply(client, channel_id=channel_id, thread_ts=thread_ts, text=_help_text())
        return

    if status in {"queued", "duplicate"}:
        msg = (
            ":white_check_mark: Request accepted and queued."
            if status == "queued"
            else ":information_source: Duplicate request detected; reusing existing queue job."
        )
        detail = f"\n- Queue Job: `#{queue_job_id}`" if queue_job_id > 0 else ""
        _post_reply(client, channel_id=channel_id, thread_ts=thread_ts, text=f"{msg}{detail}")
        return

    if status == "blocked":
        _post_reply(
            client,
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=(
                ":pause_button: Request received but blocked pending approval."
                + (f"\n- Reason: `{reason}`" if reason else "")
                + (f"\n- Queue Job: `#{queue_job_id}`" if queue_job_id > 0 else "")
            ),
        )
        return

    if status in {"denied", "rejected"}:
        _post_reply(
            client,
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=(
                ":no_entry: Request not accepted."
                + (f"\n- Reason: `{reason}`" if reason else "")
                + "\n- Allowed intents: `" + ", ".join(sorted(SUPPORTED_INTENTS)) + "`"
            ),
        )
        return

    _post_reply(
        client,
        channel_id=channel_id,
        thread_ts=thread_ts,
        text=":warning: Request received, but no actionable outcome was returned.",
    )


def _process_due_slack_ops_jobs(*, actor: str) -> None:
    from app.db.session import SessionLocal
    from app.repository import InventoryRepository
    from app.services.integration_queue import process_due_integration_queue_jobs

    db = SessionLocal()
    try:
        repo = InventoryRepository(db)
        if not get_runtime_bool(repo, "slack_ops_process_queue_enabled", True):
            return
        limit = max(1, min(200, int(get_runtime_int(repo, "slack_ops_process_queue_limit", 25))))
        summary = process_due_integration_queue_jobs(
            repo,
            integration="slack_ops",
            actor=actor,
            limit=limit,
        )
        processed = int(summary.get("processed") or 0)
        if processed > 0:
            _log(
                "Processed due slack_ops jobs: "
                f"processed={processed} success={int(summary.get('success') or 0)} "
                f"queued={int(summary.get('queued') or 0)} failed={int(summary.get('failed') or 0)} "
                f"blocked={int(summary.get('blocked') or 0)}"
            )
    except Exception as exc:
        _log(f"Slack ops queue processing failed: {exc}")
    finally:
        db.close()


def _ingest_and_reply(*, client: Any, command_payload: dict[str, Any], actor: str) -> None:
    from app.db.session import SessionLocal
    from app.repository import InventoryRepository

    db = SessionLocal()
    try:
        repo = InventoryRepository(db)
        _handle_command(
            repo=repo,
            client=client,
            command_payload=command_payload,
            actor=actor,
        )
    except Exception as exc:
        channel_id = str(command_payload.get("channel_id") or "").strip()
        thread_ts = str(command_payload.get("thread_ts") or command_payload.get("message_ts") or "").strip()
        _post_reply(
            client,
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=f":x: Slack ops ingest failed: `{str(exc)[:200]}`",
        )
    finally:
        db.close()


def run_forever() -> None:
    from app.db.session import SessionLocal
    from app.repository import InventoryRepository

    actor = "slack_ops_runner"
    db = SessionLocal()
    try:
        repo = InventoryRepository(db)
        if not get_runtime_bool(repo, "slack_ops_runner_enabled", False):
            _log("Slack ops runner disabled (`slack_ops_runner_enabled=false`).")
            return
        app_token = str(get_runtime_str(repo, "slack_app_token", "") or "").strip()
        bot_token = str(get_runtime_str(repo, "slack_bot_token", "") or "").strip()
        if not app_token:
            _log("Missing `slack_app_token`; cannot start Slack Socket Mode runner.")
            return
        if not bot_token:
            _log("Missing `slack_bot_token`; cannot start Slack Socket Mode runner.")
            return
        default_role = str(get_runtime_str(repo, "slack_ops_default_role", "ops") or "ops").strip().lower()
        role_map = _role_map(get_runtime_str(repo, "slack_ops_user_role_map", ""))
        command_prefix = str(get_runtime_str(repo, "slack_ops_command_prefix", "") or "").strip()
        bot_user_id = str(get_runtime_str(repo, "slack_bot_user_id", "") or "").strip()
        poll_seconds = max(2, min(300, int(get_runtime_int(repo, "slack_ops_poll_interval_seconds", 5))))
    finally:
        db.close()

    try:
        from slack_sdk.socket_mode.builtin.client import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk.web import WebClient
    except Exception as exc:
        _log(
            "Unable to start Slack Socket Mode runner because Slack SDK is unavailable. "
            f"Install `slack_sdk`. error={exc}"
        )
        return

    client = SocketModeClient(app_token=app_token, web_client=WebClient(token=bot_token))
    if not bot_user_id:
        try:
            auth_info = client.web_client.auth_test()
            bot_user_id = str(auth_info.get("user_id") or "").strip()
        except Exception:
            bot_user_id = ""

    def _listener(sm_client: Any, request: Any) -> None:
        if request is None:
            return
        try:
            sm_client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        except Exception:
            pass
        if not isinstance(request, SocketModeRequest):
            return

        request_type = str(getattr(request, "type", "") or "").strip().lower()
        payload = request.payload if isinstance(getattr(request, "payload", None), dict) else {}
        now = utcnow_naive()

        if request_type == "events_api":
            event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
            event_type = str(event.get("type") or "").strip().lower()
            if event_type != "app_mention":
                return
            command_text = _normalize_command_text(
                str(event.get("text") or ""),
                bot_user_id=bot_user_id,
                command_prefix=command_prefix,
            )
            slack_user_id = str(event.get("user") or "").strip()
            slack_username = str(event.get("username") or "").strip()
            _ingest_and_reply(
                client=sm_client,
                command_payload={
                    "environment": settings.app_env,
                    "command": "/goldenstackers",
                    "text": command_text,
                    "team_id": str(payload.get("team_id") or "").strip(),
                    "channel_id": str(event.get("channel") or "").strip(),
                    "channel_name": "",
                    "thread_ts": str(event.get("thread_ts") or event.get("ts") or "").strip(),
                    "message_ts": str(event.get("ts") or "").strip(),
                    "user_id": slack_user_id,
                    "user_name": slack_username,
                    "app_username": slack_username,
                    "app_role": _resolve_role(
                        slack_user_id=slack_user_id,
                        slack_username=slack_username,
                        fallback_role=default_role,
                        role_map=role_map,
                    ),
                    "files": event.get("files") if isinstance(event.get("files"), list) else [],
                    "received_at": now.isoformat(timespec="seconds"),
                },
                actor=actor,
            )
            return

        if request_type == "slash_commands":
            slack_user_id = str(payload.get("user_id") or "").strip()
            slack_username = str(payload.get("user_name") or "").strip()
            command_text = _normalize_command_text(
                str(payload.get("text") or ""),
                bot_user_id=bot_user_id,
                command_prefix=command_prefix,
            )
            _ingest_and_reply(
                client=sm_client,
                command_payload={
                    "environment": settings.app_env,
                    "command": str(payload.get("command") or "/goldenstackers").strip(),
                    "text": command_text,
                    "team_id": str(payload.get("team_id") or "").strip(),
                    "channel_id": str(payload.get("channel_id") or "").strip(),
                    "channel_name": str(payload.get("channel_name") or "").strip(),
                    "thread_ts": str(payload.get("thread_ts") or "").strip(),
                    "message_ts": str(payload.get("message_ts") or "").strip(),
                    "user_id": slack_user_id,
                    "user_name": slack_username,
                    "app_username": slack_username,
                    "app_role": _resolve_role(
                        slack_user_id=slack_user_id,
                        slack_username=slack_username,
                        fallback_role=default_role,
                        role_map=role_map,
                    ),
                    "files": [],
                    "received_at": now.isoformat(timespec="seconds"),
                },
                actor=actor,
            )
            return

    client.socket_mode_request_listeners.append(_listener)
    client.connect()
    _log("Slack Socket Mode connected; listening for app mentions/slash commands.")
    while True:
        _process_due_slack_ops_jobs(actor=actor)
        time.sleep(poll_seconds)


if __name__ == "__main__":
    run_forever()
