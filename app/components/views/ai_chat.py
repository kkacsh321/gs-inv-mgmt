import time
import re

import json
import streamlit as st

from app.auth import current_user, ensure_permission
from app.components.views.shared import render_help_panel
from app.config import settings
from app.repository import InventoryRepository
from app.services.ai_orchestration import execute_comp_summary
from app.services.chat_context_builders import (
    build_admin_snapshot,
    build_fallback_help,
    build_inventory_snapshot,
    build_listings_snapshot,
    build_orders_snapshot,
    build_reports_snapshot,
    build_sales_snapshot,
    build_shipping_snapshot,
    build_sync_snapshot,
)
from app.services.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_str, is_ai_domain_enabled
from app.services.voice_runtime import (
    resolve_voice_runtime_config,
    synthesize_speech_bytes,
    transcribe_audio_bytes,
)
from app.utils.time import utcnow_naive


def _normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _contains_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


DEFAULT_CHAT_ALLOWED_DOMAINS_BY_ROLE: dict[str, set[str]] = {
    "viewer": {"inventory", "listings", "sales", "shipping", "sync", "orders", "reports"},
    "ops": {"inventory", "listings", "sales", "shipping", "sync", "orders", "reports"},
    "admin": {"inventory", "listings", "sales", "shipping", "sync", "orders", "reports", "admin"},
}

GOLDY_AGENT_DOMAIN_SCOPE: dict[str, set[str]] = {
    "auto_router": {"inventory", "listings", "sales", "shipping", "sync", "orders", "reports", "admin"},
    "comps_agent": {"inventory", "listings", "sales", "reports"},
    "listings_agent": {"listings", "inventory", "sync", "reports"},
    "inventory_agent": {"inventory", "orders", "shipping", "reports"},
    "finance_agent": {"sales", "orders", "reports", "inventory"},
    "integrations_agent": {"sync", "shipping", "listings", "orders", "admin"},
}

GOLDY_AGENT_LABELS: dict[str, str] = {
    "auto_router": "Auto Router",
    "comps_agent": "Comps Agent",
    "listings_agent": "Listings Agent",
    "inventory_agent": "Inventory Agent",
    "finance_agent": "Finance Agent",
    "integrations_agent": "Integrations Agent",
}


def _parse_csv_tokens(value: str) -> set[str]:
    return {
        token.strip().lower()
        for token in str(value or "").replace("\n", ",").split(",")
        if token.strip()
    }


def _allowed_domains_for_role(repo: InventoryRepository, role: str) -> set[str]:
    resolved_role = str(role or "viewer").strip().lower()
    defaults = DEFAULT_CHAT_ALLOWED_DOMAINS_BY_ROLE.get(resolved_role, DEFAULT_CHAT_ALLOWED_DOMAINS_BY_ROLE["viewer"])
    runtime_value = get_runtime_str(
        repo,
        f"chat_allowed_domains_{resolved_role}_csv",
        ",".join(sorted(defaults)),
    )
    parsed = _parse_csv_tokens(runtime_value)
    return parsed or set(defaults)


def _is_write_intent(prompt: str) -> bool:
    normalized = _normalize(prompt)
    write_terms = [
        "delete ",
        "drop ",
        "truncate",
        "update ",
        "insert ",
        "create ",
        "modify ",
        "set status",
        "run migration",
        "execute job",
        "push to ebay",
        "publish",
        "end listing",
        "relist",
    ]
    return any(term in normalized for term in write_terms)


def _mask_tail(value: str, *, keep: int = 4, mask_char: str = "*") -> str:
    raw = str(value or "")
    if len(raw) <= keep:
        return mask_char * len(raw)
    return (mask_char * (len(raw) - keep)) + raw[-keep:]


def _apply_sensitive_masking(repo: InventoryRepository, text: str) -> tuple[str, list[str]]:
    masked = str(text or "")
    applied_rules: list[str] = []
    if not get_runtime_bool(repo, "chat_mask_sensitive_enabled", True):
        return masked, applied_rules

    if get_runtime_bool(repo, "chat_mask_email_enabled", True):
        email_rx = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
        next_masked = email_rx.sub(r"\1***@\2", masked)
        if next_masked != masked:
            masked = next_masked
            applied_rules.append("email")

    if get_runtime_bool(repo, "chat_mask_phone_enabled", True):
        phone_rx = re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b")
        def _mask_phone(match: re.Match) -> str:
            digits = "".join(ch for ch in match.group(0) if ch.isdigit())
            return f"***-***-{digits[-4:]}" if len(digits) >= 4 else "***-***-****"
        next_masked = phone_rx.sub(_mask_phone, masked)
        if next_masked != masked:
            masked = next_masked
            applied_rules.append("phone")

    if get_runtime_bool(repo, "chat_mask_tracking_enabled", True):
        tracking_rx = re.compile(r"(?i)\b(tracking(?:\s*(?:number|#|id))?\s*[:#]?\s*)([A-Z0-9-]{8,})\b")
        def _mask_tracking(match: re.Match) -> str:
            prefix = match.group(1)
            token = match.group(2)
            return f"{prefix}{_mask_tail(token, keep=4)}"
        next_masked = tracking_rx.sub(_mask_tracking, masked)
        if next_masked != masked:
            masked = next_masked
            applied_rules.append("tracking")

    return masked, applied_rules


def _answer_query(
    repo: InventoryRepository,
    prompt: str,
    *,
    allowed_domains: set[str],
    max_scan_rows: int,
) -> tuple[str, list[dict], str]:
    normalized = _normalize(prompt)
    if _contains_any(normalized, ["inventory", "stock", "on hand", "qty"]):
        if "inventory" not in allowed_domains:
            return "Your role is not allowed to access `inventory` chat domain.", [], "denied_inventory"
        answer, citations = build_inventory_snapshot(repo, max_scan_rows=max_scan_rows)
        return answer, citations, "inventory_snapshot"
    if _contains_any(normalized, ["listing", "ebay", "draft", "review"]):
        if "listings" not in allowed_domains:
            return "Your role is not allowed to access `listings` chat domain.", [], "denied_listings"
        answer, citations = build_listings_snapshot(repo, max_scan_rows=max_scan_rows)
        return answer, citations, "listing_snapshot"
    if _contains_any(normalized, ["sale", "revenue", "margin", "gross", "net"]):
        if "sales" not in allowed_domains:
            return "Your role is not allowed to access `sales` chat domain.", [], "denied_sales"
        answer, citations = build_sales_snapshot(repo, max_scan_rows=max_scan_rows)
        return answer, citations, "sales_snapshot_30d"
    if _contains_any(normalized, ["shipping", "tracking", "delivered", "shipment", "exception"]):
        if "shipping" not in allowed_domains:
            return "Your role is not allowed to access `shipping` chat domain.", [], "denied_shipping"
        answer, citations = build_shipping_snapshot(repo, max_scan_rows=max_scan_rows)
        return answer, citations, "shipping_snapshot"
    if _contains_any(normalized, ["sync", "retry", "failed run", "job"]):
        if "sync" not in allowed_domains:
            return "Your role is not allowed to access `sync` chat domain.", [], "denied_sync"
        answer, citations = build_sync_snapshot(repo, max_scan_rows=max_scan_rows)
        return answer, citations, "sync_snapshot"
    if _contains_any(normalized, ["order", "fulfillment", "purchase"]):
        if "orders" not in allowed_domains:
            return "Your role is not allowed to access `orders` chat domain.", [], "denied_orders"
        answer, citations = build_orders_snapshot(repo, max_scan_rows=max_scan_rows)
        return answer, citations, "order_snapshot"
    if _contains_any(normalized, ["report", "reconciliation", "pnl", "profit", "kpi", "trend"]):
        if "reports" not in allowed_domains:
            return "Your role is not allowed to access `reports` chat domain.", [], "denied_reports"
        answer, citations = build_reports_snapshot(repo, max_scan_rows=max_scan_rows)
        return answer, citations, "reports_snapshot"
    if _contains_any(normalized, ["admin", "runtime", "config", "health score", "users", "roles", "permission"]):
        if "admin" not in allowed_domains:
            return "Your role is not allowed to access `admin` chat domain.", [], "denied_admin"
        answer, citations = build_admin_snapshot(repo, max_scan_rows=max_scan_rows)
        return answer, citations, "admin_snapshot"
    answer, citations = build_fallback_help()
    return answer, citations, "help_fallback"


def _resolve_goldy_domains(
    allowed_domains: set[str],
    selected_agent: str,
) -> set[str]:
    requested_scope = GOLDY_AGENT_DOMAIN_SCOPE.get(
        str(selected_agent or "auto_router").strip().lower(),
        GOLDY_AGENT_DOMAIN_SCOPE["auto_router"],
    )
    return set(sorted(set(allowed_domains).intersection(requested_scope)))


def _build_goldy_plan(
    *,
    prompt: str,
    mode: str,
    selected_agent: str,
    allowed_domains: set[str],
) -> dict:
    normalized_mode = str(mode or "single").strip().lower()
    role_key = str(selected_agent or "auto_router").strip().lower()
    role_label = GOLDY_AGENT_LABELS.get(role_key, "Auto Router")
    plan_steps: list[str] = [
        f"Classify prompt intent and route via `{role_label}`.",
        f"Limit query to allowed domains: {', '.join(sorted(allowed_domains)) or 'none'}.",
        "Execute read-only snapshot builders and synthesize response with citations.",
    ]
    if normalized_mode == "multi":
        plan_steps = [
            "Coordinator parses task and creates per-domain sub-tasks.",
            "Specialist agents return domain snapshots in parallel (read-only).",
            "Coordinator merges results and resolves conflicts before response.",
        ]
    return {
        "prompt_preview": str(prompt or "")[:160],
        "mode": normalized_mode,
        "agent_role": role_key,
        "agent_label": role_label,
        "steps": plan_steps,
        "allowed_domains": sorted(allowed_domains),
        "requires_approval_for_writes": True,
        "write_guardrail": "blocked",
    }


def render_ai_chat(repo: InventoryRepository) -> None:
    user = current_user()
    env_key = str(settings.app_env or "local").strip().lower()
    user_key = str(user.username or "employee").strip().lower()
    scoped_messages_key = f"ask_gs_messages::{env_key}::{user_key}"
    scoped_pending_prompt_key = f"ask_gs_pending_prompt::{env_key}::{user_key}"
    scoped_pending_prompt_meta_key = f"ask_gs_pending_prompt_meta::{env_key}::{user_key}"
    scoped_tts_cache_key = f"ask_gs_tts_cache::{env_key}::{user_key}"
    scoped_ai_refine_override_key = f"ask_gs_ai_refine_override::{env_key}::{user_key}"
    scoped_goldy_mode_key = f"ask_gs_goldy_mode::{env_key}::{user_key}"
    scoped_goldy_agent_key = f"ask_gs_goldy_agent::{env_key}::{user_key}"
    scoped_goldy_show_plan_key = f"ask_gs_goldy_show_plan::{env_key}::{user_key}"
    allowed_domains = _allowed_domains_for_role(repo, user.role)
    voice_config = resolve_voice_runtime_config(repo)
    st.subheader("Ask GoldenStackers (Read-Only Data Chat)")
    render_help_panel(
        section_title="Ask GoldenStackers",
        goal="Get quick operational answers from app data through a safe read-only chat interface.",
        steps=[
            "Ask an operations question (inventory, listings, sales, shipping, sync, orders).",
            "Review answer + citations (source tables/filters/row counts).",
            "Use suggestions for common daily triage questions.",
            "Export transcript for handoff/audit notes when needed.",
        ],
        roadmap_phase="v0.5 AI Operations Copilot + Data Chat",
    )
    st.caption(
        "Current mode is read-only intent routing over app repository data. "
        "No direct write actions are executed from chat."
    )

    if not ensure_permission(user, "read", "Use Ask GoldenStackers Chat"):
        st.stop()
    if not ensure_permission(user, "ai_chat_use", "Use AI Chat"):
        st.stop()
    if not is_ai_domain_enabled(repo, "chat"):
        st.info("Ask GoldenStackers chat is currently disabled by Admin AI domain toggle.")
        return

    if scoped_messages_key not in st.session_state:
        st.session_state[scoped_messages_key] = []
    if scoped_tts_cache_key not in st.session_state:
        st.session_state[scoped_tts_cache_key] = {}
    max_prompt_chars = max(100, min(10000, get_runtime_int(repo, "chat_max_prompt_chars", 1200)))
    max_scan_rows = max(50, min(5000, get_runtime_int(repo, "chat_max_scan_rows", 1000)))
    soft_timeout_ms = max(200, min(30000, get_runtime_int(repo, "chat_soft_timeout_ms", 4000)))
    chat_ai_refine_enabled = str(
        get_runtime_str(repo, "chat_ai_refine_enabled", "false")
    ).strip().lower() in {"1", "true", "yes", "on", "y"}
    if scoped_ai_refine_override_key not in st.session_state:
        st.session_state[scoped_ai_refine_override_key] = bool(chat_ai_refine_enabled)
    if scoped_goldy_mode_key not in st.session_state:
        st.session_state[scoped_goldy_mode_key] = "single"
    if scoped_goldy_agent_key not in st.session_state:
        st.session_state[scoped_goldy_agent_key] = "auto_router"
    if scoped_goldy_show_plan_key not in st.session_state:
        st.session_state[scoped_goldy_show_plan_key] = True
    st.caption(
        f"Chat scope: env=`{env_key}` user=`{user.username}` role=`{user.role}` "
        f"allowed_domains=`{', '.join(sorted(allowed_domains))}` "
        f"max_prompt_chars=`{max_prompt_chars}` max_scan_rows=`{max_scan_rows}` soft_timeout_ms=`{soft_timeout_ms}` "
        f"ai_refine_enabled=`{bool(st.session_state.get(scoped_ai_refine_override_key))}`"
    )
    st.checkbox(
        "Enable AI refinement for this session",
        key=scoped_ai_refine_override_key,
        help="Session-only toggle. Admin runtime setting remains unchanged.",
    )
    with st.expander("Goldy Orchestration (Preview)", expanded=False):
        st.caption(
            "First-pass role-safe orchestration controls. Write actions remain blocked in chat and require explicit operator approval in workspace tools."
        )
        gm1, gm2 = st.columns(2)
        with gm1:
            st.selectbox(
                "Agent mode",
                options=["single", "multi"],
                format_func=lambda x: "Single-Agent" if x == "single" else "Multi-Agent",
                key=scoped_goldy_mode_key,
            )
            st.selectbox(
                "Primary agent",
                options=list(GOLDY_AGENT_LABELS.keys()),
                format_func=lambda k: GOLDY_AGENT_LABELS.get(k, k),
                key=scoped_goldy_agent_key,
            )
        with gm2:
            st.checkbox("Read-only guardrail enforced", value=True, disabled=True)
            st.checkbox("Require explicit approval for write actions", value=True, disabled=True)
            st.checkbox("Attach plan to answer", key=scoped_goldy_show_plan_key)
        goldy_effective_domains = _resolve_goldy_domains(
            allowed_domains,
            str(st.session_state.get(scoped_goldy_agent_key) or "auto_router"),
        )
        st.caption(
            "Goldy effective domains: "
            + (", ".join(sorted(goldy_effective_domains)) if goldy_effective_domains else "(none)")
        )
        recent_goldy = repo.list_ai_chat_interactions(
            limit=20,
            actor=user.username,
            event_type="goldy_orchestration",
        )
        if recent_goldy:
            st.caption("Recent Goldy traces")
            st.dataframe(recent_goldy, use_container_width=True, hide_index=True)
        else:
            st.info("No Goldy traces yet for this user/environment.")
    with st.expander("Voice (Beta)", expanded=False):
        st.caption(
            "Optional speech input/output for Ask GoldenStackers. "
            "Voice requests still use the same read-only guardrails and audit trail."
        )
        st.caption(
            f"voice_enabled=`{voice_config.enabled}` stt=`{voice_config.stt_enabled}` "
            f"tts=`{voice_config.tts_enabled}` provider=`{voice_config.provider}`"
        )
        if not voice_config.enabled:
            st.info("Voice features are disabled. Enable `ai_voice_enabled` in Admin Runtime Settings.")
        elif not hasattr(st, "audio_input"):
            st.warning(
                "This Streamlit version does not support `st.audio_input`. "
                "Upgrade Streamlit to use in-browser microphone capture."
            )
        elif voice_config.stt_enabled:
            voice_blob = st.audio_input("Voice question (optional)", key=f"ask_gs_voice_blob::{env_key}::{user_key}")
            if voice_blob is not None:
                if st.button("Transcribe Voice Prompt", key=f"ask_gs_transcribe::{env_key}::{user_key}"):
                    try:
                        transcribed = transcribe_audio_bytes(
                            voice_config,
                            audio_bytes=voice_blob.getvalue(),
                            filename=getattr(voice_blob, "name", "voice_input.wav") or "voice_input.wav",
                            content_type=getattr(voice_blob, "type", "audio/wav") or "audio/wav",
                        )
                        st.session_state[scoped_pending_prompt_key] = transcribed
                        st.session_state[scoped_pending_prompt_meta_key] = {
                            "input_mode": "voice_stt",
                            "voice_provider": voice_config.provider,
                            "voice_stt_model": voice_config.stt_model,
                        }
                        st.success("Voice prompt transcribed. Submitting to chat now.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Voice transcription failed: {exc}")
        else:
            st.info("Speech-to-text is disabled (`ai_voice_stt_enabled=false`).")

    prompt_suggestions = [
        "inventory snapshot",
        "listing draft and review status",
        "sales last 30 days",
        "shipping exceptions",
        "sync failures",
        "orders status",
        "reports summary",
    ]
    if "admin" in allowed_domains:
        prompt_suggestions.append("admin config status")
    s1, s2, s3 = st.columns(3)
    for idx, suggestion in enumerate(prompt_suggestions):
        target_col = [s1, s2, s3][idx % 3]
        with target_col:
            if st.button(suggestion, key=f"ask_gs_suggestion_{idx}"):
                st.session_state[scoped_pending_prompt_key] = suggestion

    for idx, msg in enumerate(st.session_state[scoped_messages_key]):
        role = msg.get("role", "assistant")
        with st.chat_message(role):
            st.markdown(msg.get("content", ""))
            if role == "assistant" and bool(msg.get("ai_refined")):
                refine_citation = msg.get("ai_refine_citation") if isinstance(msg.get("ai_refine_citation"), dict) else {}
                refine_provider = str(refine_citation.get("provider") or "").strip()
                refine_text_model = str(refine_citation.get("text_model") or "").strip()
                if refine_provider and refine_text_model:
                    st.caption(f"AI refined • `{refine_provider}` / `{refine_text_model}`")
                else:
                    st.caption("AI refined")
            citations = msg.get("citations") or []
            if citations:
                st.caption("Sources")
                st.code(json.dumps(citations, indent=2), language="json")
            if role == "assistant" and voice_config.enabled and voice_config.tts_enabled:
                msg_key = f"{idx}:{msg.get('answered_at_utc', '')}:{msg.get('intent', '')}"
                c1, c2 = st.columns([1, 3])
                with c1:
                    if st.button("Speak", key=f"ask_gs_speak::{msg_key}"):
                        try:
                            audio_bytes, mime = synthesize_speech_bytes(
                                voice_config,
                                text=str(msg.get("content") or ""),
                            )
                            st.session_state[scoped_tts_cache_key][msg_key] = {
                                "bytes": audio_bytes,
                                "mime": mime,
                            }
                            try:
                                repo.log_ai_chat_interaction(
                                    actor=user.username,
                                    prompt="",
                                    intent="tts_playback_generated",
                                    allowed_domains=sorted(allowed_domains),
                                    citations=[],
                                    answer_preview=str(msg.get("content") or ""),
                                    denied=False,
                                    elapsed_ms=0,
                                    metadata={
                                        "event_type": "tts",
                                        "source_message_key": msg_key,
                                        "voice_provider": voice_config.provider,
                                        "voice_tts_model": voice_config.tts_model,
                                        "voice_tts_voice": voice_config.tts_voice,
                                        "voice_tts_response_format": voice_config.tts_response_format,
                                        "scope_env": env_key,
                                        "scope_user": user_key,
                                    },
                                )
                            except Exception:
                                pass
                        except Exception as exc:
                            st.error(f"TTS failed: {exc}")
                cached_audio = st.session_state[scoped_tts_cache_key].get(msg_key)
                if cached_audio:
                    with c2:
                        st.audio(cached_audio["bytes"], format=cached_audio["mime"])

    prompt = st.chat_input("Ask a read-only operations question...")
    prompt_meta = st.session_state.pop(scoped_pending_prompt_meta_key, {}) or {}
    if prompt is None:
        prompt = str(st.session_state.pop(scoped_pending_prompt_key, "") or "").strip()
        if prompt:
            prompt_meta.setdefault("input_mode", "suggestion")
    else:
        prompt = prompt.strip()
        if prompt:
            prompt_meta.setdefault("input_mode", "typed")

    if not prompt:
        c1, c2 = st.columns(2)
        with c1:
            transcript = json.dumps(st.session_state.get(scoped_messages_key, []), indent=2).encode("utf-8")
            st.download_button(
                "Download Transcript JSON",
                data=transcript,
                file_name=f"ask_goldenstackers_transcript_{env_key}_{user_key}.json",
                mime="application/json",
                key="ask_gs_download_transcript",
                disabled=not bool(st.session_state.get(scoped_messages_key)),
            )
        with c2:
            if st.button("Clear Chat", key="ask_gs_clear_chat_btn"):
                st.session_state[scoped_messages_key] = []
                st.rerun()
        return

    if len(prompt) > max_prompt_chars:
        st.error(
            f"Prompt too long ({len(prompt)} chars). Maximum allowed is {max_prompt_chars}. "
            "Please shorten your question."
        )
        return

    if _is_write_intent(prompt):
        selected_goldy_mode = str(st.session_state.get(scoped_goldy_mode_key) or "single").strip().lower()
        selected_goldy_agent = str(st.session_state.get(scoped_goldy_agent_key) or "auto_router").strip().lower()
        goldy_allowed_domains = _resolve_goldy_domains(allowed_domains, selected_goldy_agent)
        goldy_plan = _build_goldy_plan(
            prompt=prompt,
            mode=selected_goldy_mode,
            selected_agent=selected_goldy_agent,
            allowed_domains=goldy_allowed_domains,
        )
        denied_msg = (
            "Write/action intent detected. Goldy chat orchestration is read-only and will not execute "
            "updates, publishes, or deletes. Ask for analysis/recommendations instead."
        )
        st.session_state[scoped_messages_key].append({"role": "user", "content": prompt})
        st.session_state[scoped_messages_key].append(
            {
                "role": "assistant",
                "content": denied_msg,
                "citations": [],
                "intent": "denied_write_intent",
                "answered_at_utc": utcnow_naive().isoformat(),
                "scope_env": env_key,
                "scope_user": user_key,
            }
        )
        try:
            repo.log_ai_chat_interaction(
                actor=user.username,
                prompt=prompt,
                intent="denied_write_intent",
                allowed_domains=sorted(allowed_domains),
                citations=[],
                answer_preview=denied_msg,
                denied=True,
                elapsed_ms=0,
                metadata={
                    "scope_env": env_key,
                    "scope_user": user_key,
                    "event_type": "goldy_orchestration",
                    "goldy_mode": selected_goldy_mode,
                    "goldy_role": selected_goldy_agent,
                    "goldy_plan_status": "blocked_write_intent",
                    "goldy_plan": goldy_plan,
                    **prompt_meta,
                },
            )
        except Exception:
            pass
        st.rerun()

    st.session_state[scoped_messages_key].append({"role": "user", "content": prompt})
    started = time.perf_counter()
    ai_refine_applied = False
    ai_refine_citation: dict = {}
    selected_goldy_mode = str(st.session_state.get(scoped_goldy_mode_key) or "single").strip().lower()
    selected_goldy_agent = str(st.session_state.get(scoped_goldy_agent_key) or "auto_router").strip().lower()
    goldy_allowed_domains = _resolve_goldy_domains(allowed_domains, selected_goldy_agent)
    goldy_plan = _build_goldy_plan(
        prompt=prompt,
        mode=selected_goldy_mode,
        selected_agent=selected_goldy_agent,
        allowed_domains=goldy_allowed_domains,
    )
    try:
        answer, citations, intent_key = _answer_query(
            repo,
            prompt,
            allowed_domains=goldy_allowed_domains,
            max_scan_rows=max_scan_rows,
        )
        if bool(st.session_state.get(scoped_ai_refine_override_key)) and not str(intent_key).startswith("denied_"):
            chat_refine_system_message = get_runtime_str(
                repo,
                "chat_ai_refine_system_message",
                (
                    "You are GoldenStackers' read-only operations copilot. "
                    "Preserve factual values from the provided draft answer and citations."
                ),
            ).strip()
            chat_refine_instruction = get_runtime_str(
                repo,
                "chat_ai_refine_instruction",
                (
                    "Rewrite the draft answer for clarity and operator usefulness. "
                    "Do not invent values. Keep output concise markdown with short bullets."
                ),
            ).strip()
            refine_result = execute_comp_summary(
                repo,
                query=prompt,
                ebay_rows=[],
                web_rows=[],
                spot_context={
                    "chat_intent": intent_key,
                    "draft_answer": answer,
                    "citations": citations,
                    "allowed_domains": sorted(allowed_domains),
                },
                system_message=chat_refine_system_message,
                instruction=chat_refine_instruction,
            )
            answer = refine_result.text
            ai_refine_applied = True
            ai_refine_citation = dict(refine_result.citation or {})
            citations = [
                *citations,
                {
                    "table": "ai_orchestration",
                    "filters": "chat_ai_refine_enabled=true",
                    "rows_considered": 1,
                    "as_of_utc": utcnow_naive().isoformat(),
                    "ai_citation": refine_result.citation,
                },
            ]
        if bool(st.session_state.get(scoped_goldy_show_plan_key)):
            plan_lines = "\n".join(f"- {line}" for line in goldy_plan.get("steps", []))
            answer = (
                f"Goldy Plan (`{goldy_plan.get('mode')}` • `{goldy_plan.get('agent_label')}`)\n"
                f"{plan_lines}\n\n"
                f"{answer}"
            )
            citations = [
                {
                    "table": "goldy_orchestration",
                    "filters": f"mode={goldy_plan.get('mode')};agent={goldy_plan.get('agent_role')}",
                    "rows_considered": len(goldy_plan.get("steps", [])),
                    "as_of_utc": utcnow_naive().isoformat(),
                },
                *citations,
            ]
    except Exception as exc:
        answer = (
            "I hit a safe query guardrail or runtime error while preparing this answer. "
            "Try a narrower question (domain + timeframe)."
        )
        citations = [
            {
                "table": "n/a",
                "filters": "error",
                "rows_considered": 0,
                "error": str(exc),
                "as_of_utc": utcnow_naive().isoformat(),
            }
        ]
        intent_key = "safe_failure"
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if elapsed_ms > soft_timeout_ms:
        answer += (
            f"\n\nNote: query response time was `{elapsed_ms}ms`, above soft limit `{soft_timeout_ms}ms`. "
            "Consider asking a narrower question."
        )
    answer, masking_rules = _apply_sensitive_masking(repo, answer)
    if masking_rules:
        answer += (
            "\n\nSensitive values were masked in this response: "
            + ", ".join(sorted(set(masking_rules)))
            + "."
        )
    st.session_state[scoped_messages_key].append(
        {
            "role": "assistant",
            "content": answer,
            "citations": citations,
            "intent": intent_key,
            "answered_at_utc": utcnow_naive().isoformat(),
            "scope_env": env_key,
            "scope_user": user_key,
            "elapsed_ms": elapsed_ms,
            "ai_refined": bool(ai_refine_applied),
            "ai_refine_citation": ai_refine_citation,
        }
    )
    try:
        repo.log_ai_chat_interaction(
            actor=user.username,
            prompt=prompt,
            intent=intent_key,
            allowed_domains=sorted(allowed_domains),
            citations=citations,
            answer_preview=answer,
            denied=str(intent_key).startswith("denied_"),
            elapsed_ms=elapsed_ms,
            metadata={
                "scope_env": env_key,
                "scope_user": user_key,
                "event_type": "goldy_orchestration",
                "goldy_mode": selected_goldy_mode,
                "goldy_role": selected_goldy_agent,
                "goldy_plan_status": "completed" if intent_key != "safe_failure" else "safe_failure",
                "goldy_plan": goldy_plan,
                "ai_refined": bool(ai_refine_applied),
                "ai_refine_provider": str(ai_refine_citation.get("provider") or ""),
                "ai_refine_text_model": str(ai_refine_citation.get("text_model") or ""),
                "ai_refine_multimodal_model": str(ai_refine_citation.get("multimodal_model") or ""),
                "ai_refine_endpoint_type": str(ai_refine_citation.get("endpoint_type") or ""),
                "ai_refine_fallback_attempts": int(ai_refine_citation.get("fallback_attempts") or 0),
                "masking_rules": sorted(set(masking_rules)),
                **prompt_meta,
            },
        )
    except Exception:
        pass
    st.rerun()
