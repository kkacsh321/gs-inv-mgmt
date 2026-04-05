from dataclasses import dataclass
from typing import Any

import requests

from app.config import settings
from app.services.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_str


@dataclass(frozen=True)
class VoiceRuntimeConfig:
    enabled: bool
    stt_enabled: bool
    tts_enabled: bool
    provider: str
    base_url: str
    api_key: str
    timeout_seconds: int
    stt_model: str
    stt_language: str
    tts_model: str
    tts_voice: str
    tts_response_format: str
    tts_max_chars: int


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


def resolve_voice_runtime_config(repo: Any) -> VoiceRuntimeConfig:
    default_base_url = (settings.comp_llm_base_url or "https://api.openai.com/v1").strip().rstrip("/")
    return VoiceRuntimeConfig(
        enabled=get_runtime_bool(repo, "ai_voice_enabled", False),
        stt_enabled=get_runtime_bool(repo, "ai_voice_stt_enabled", True),
        tts_enabled=get_runtime_bool(repo, "ai_voice_tts_enabled", False),
        provider=get_runtime_str(repo, "ai_voice_provider", "openai").strip().lower(),
        base_url=get_runtime_str(repo, "ai_voice_base_url", default_base_url).strip().rstrip("/"),
        api_key=get_runtime_str(repo, "ai_voice_api_key", settings.openai_api_key or "").strip(),
        timeout_seconds=max(5, _safe_int(get_runtime_int(repo, "ai_voice_timeout_seconds", 45), 45)),
        stt_model=get_runtime_str(repo, "ai_voice_stt_model", "gpt-4o-mini-transcribe").strip(),
        stt_language=get_runtime_str(repo, "ai_voice_stt_language", "").strip(),
        tts_model=get_runtime_str(repo, "ai_voice_tts_model", "gpt-4o-mini-tts").strip(),
        tts_voice=get_runtime_str(repo, "ai_voice_tts_voice", "alloy").strip(),
        tts_response_format=get_runtime_str(repo, "ai_voice_tts_response_format", "mp3").strip().lower(),
        tts_max_chars=max(200, _safe_int(get_runtime_int(repo, "ai_voice_tts_max_chars", 1400), 1400)),
    )


def _auth_headers(config: VoiceRuntimeConfig) -> dict[str, str]:
    headers: dict[str, str] = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return headers


def _endpoint_candidates(base_url: str, path_no_leading_slash: str) -> list[str]:
    base = (base_url or "").strip().rstrip("/")
    path = (path_no_leading_slash or "").strip().lstrip("/")
    if not base or not path:
        return []
    candidates = [f"{base}/{path}"]
    # LocalAI/OpenAI-compatible deployments vary between base URLs that include or exclude `/v1`.
    if base.endswith("/v1"):
        candidates.append(f"{base[:-3]}/{path}".rstrip("/"))
    else:
        candidates.append(f"{base}/v1/{path}")
    deduped: list[str] = []
    for item in candidates:
        if item not in deduped:
            deduped.append(item)
    return deduped


def transcribe_audio_bytes(
    config: VoiceRuntimeConfig,
    *,
    audio_bytes: bytes,
    filename: str = "voice_input.wav",
    content_type: str = "audio/wav",
) -> str:
    if not config.enabled or not config.stt_enabled:
        raise RuntimeError("Voice transcription is disabled in runtime settings.")
    if not config.base_url:
        raise RuntimeError("Voice base URL is not configured.")
    if not config.stt_model:
        raise RuntimeError("Voice STT model is not configured.")
    if config.provider == "openai" and not config.api_key:
        raise RuntimeError("OpenAI voice provider requires api key.")

    data = {"model": config.stt_model}
    if config.stt_language:
        data["language"] = config.stt_language
    files = {"file": (filename, audio_bytes, content_type)}
    last_error: Exception | None = None
    for endpoint in _endpoint_candidates(config.base_url, "audio/transcriptions"):
        try:
            response = requests.post(
                endpoint,
                headers=_auth_headers(config),
                data=data,
                files=files,
                timeout=config.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json() or {}
            text = str(payload.get("text") or "").strip()
            if not text:
                raise RuntimeError("STT response did not include transcribed text.")
            return text
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise RuntimeError(
            "STT request failed across all endpoint variants. "
            "Check LocalAI/OpenAI base URL and audio endpoint support."
        ) from last_error
    raise RuntimeError("STT request failed: no endpoint candidates were available.")


def synthesize_speech_bytes(
    config: VoiceRuntimeConfig,
    *,
    text: str,
) -> tuple[bytes, str]:
    if not config.enabled or not config.tts_enabled:
        raise RuntimeError("Voice speech synthesis is disabled in runtime settings.")
    if not config.base_url:
        raise RuntimeError("Voice base URL is not configured.")
    if not config.tts_model:
        raise RuntimeError("Voice TTS model is not configured.")
    if config.provider == "openai" and not config.api_key:
        raise RuntimeError("OpenAI voice provider requires api key.")

    trimmed = (text or "").strip()
    if not trimmed:
        raise RuntimeError("No text supplied for speech synthesis.")
    if len(trimmed) > config.tts_max_chars:
        trimmed = trimmed[: config.tts_max_chars].rstrip()

    fmt = config.tts_response_format if config.tts_response_format in {"mp3", "wav"} else "mp3"
    body = {
        "model": config.tts_model,
        "voice": config.tts_voice or "alloy",
        "input": trimmed,
        "response_format": fmt,
    }
    last_error: Exception | None = None
    for endpoint in _endpoint_candidates(config.base_url, "audio/speech"):
        try:
            response = requests.post(
                endpoint,
                headers={"Content-Type": "application/json", **_auth_headers(config)},
                json=body,
                timeout=config.timeout_seconds,
            )
            response.raise_for_status()
            mime = "audio/wav" if fmt == "wav" else "audio/mpeg"
            return response.content, mime
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise RuntimeError(
            "TTS request failed across all endpoint variants. "
            "Check LocalAI/OpenAI base URL and audio endpoint support."
        ) from last_error
    raise RuntimeError("TTS request failed: no endpoint candidates were available.")
