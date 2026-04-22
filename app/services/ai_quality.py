from __future__ import annotations

from dataclasses import dataclass
import re

from app.services.runtime_settings import get_runtime_int, get_runtime_str


_PLACEHOLDER_VALUES = {
    "",
    "n/a",
    "na",
    "none",
    "unknown",
    "tbd",
    "ebay",
    "marketplace",
    "online marketplace",
    "-",
}


@dataclass(frozen=True)
class AIQualityPolicy:
    title_min_words: int = 3
    title_min_chars: int = 12
    details_min_words: int = 28
    details_min_chars: int = 180
    intake_min_words: int = 8
    intake_min_chars: int = 40
    forbidden_terms: tuple[str, ...] = (
        "guaranteed profit",
        "guaranteed return",
        "risk-free",
        "no risk",
        "investment advice",
        "financial advice",
    )


def _clamp_int(value: int, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def _parse_forbidden_terms(raw: str | None) -> tuple[str, ...]:
    text = str(raw or "")
    tokens: list[str] = []
    for item in re.split(r"[,;\n|]+", text):
        token = str(item or "").strip().lower()
        if token:
            tokens.append(token)
    seen: set[str] = set()
    deduped: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return tuple(deduped)


def load_ai_quality_policy(repo) -> AIQualityPolicy:
    defaults = AIQualityPolicy()
    terms_raw = get_runtime_str(
        repo,
        "ai_quality_forbidden_terms_csv",
        ",".join(defaults.forbidden_terms),
    )
    parsed_terms = _parse_forbidden_terms(terms_raw)
    return AIQualityPolicy(
        title_min_words=_clamp_int(
            get_runtime_int(repo, "ai_quality_title_min_words", defaults.title_min_words),
            defaults.title_min_words,
            1,
            20,
        ),
        title_min_chars=_clamp_int(
            get_runtime_int(repo, "ai_quality_title_min_chars", defaults.title_min_chars),
            defaults.title_min_chars,
            5,
            120,
        ),
        details_min_words=_clamp_int(
            get_runtime_int(repo, "ai_quality_listing_details_min_words", defaults.details_min_words),
            defaults.details_min_words,
            10,
            400,
        ),
        details_min_chars=_clamp_int(
            get_runtime_int(repo, "ai_quality_listing_details_min_chars", defaults.details_min_chars),
            defaults.details_min_chars,
            50,
            8000,
        ),
        intake_min_words=_clamp_int(
            get_runtime_int(repo, "ai_quality_intake_min_words", defaults.intake_min_words),
            defaults.intake_min_words,
            3,
            200,
        ),
        intake_min_chars=_clamp_int(
            get_runtime_int(repo, "ai_quality_intake_min_chars", defaults.intake_min_chars),
            defaults.intake_min_chars,
            20,
            4000,
        ),
        forbidden_terms=parsed_terms or defaults.forbidden_terms,
    )


def _normalize(value: str) -> str:
    return str(value or "").strip()


def is_placeholder_text(value: str) -> bool:
    normalized = _normalize(value).lower().strip(" .,:;!?")
    return normalized in _PLACEHOLDER_VALUES


def find_forbidden_terms(value: str, *, policy: AIQualityPolicy | None = None) -> list[str]:
    text = _normalize(value).lower()
    if not text:
        return []
    gate_policy = policy or AIQualityPolicy()
    matches: list[str] = []
    for term in gate_policy.forbidden_terms:
        token = str(term or "").strip().lower()
        if token and token in text:
            matches.append(token)
    return matches


def is_weak_listing_title(value: str, *, policy: AIQualityPolicy | None = None) -> bool:
    gate_policy = policy or AIQualityPolicy()
    text = _normalize(value)
    if not text:
        return True
    if is_placeholder_text(text):
        return True
    if find_forbidden_terms(text, policy=gate_policy):
        return True
    words = re.findall(r"\b[\w'-]+\b", text)
    if len(words) < gate_policy.title_min_words:
        return True
    if len(text) < gate_policy.title_min_chars:
        return True
    return False


def is_weak_listing_details(value: str, *, policy: AIQualityPolicy | None = None) -> bool:
    gate_policy = policy or AIQualityPolicy()
    text = _normalize(value)
    if not text:
        return True
    if is_placeholder_text(text):
        return True
    if find_forbidden_terms(text, policy=gate_policy):
        return True
    words = re.findall(r"\b[\w'-]+\b", text)
    if len(words) < gate_policy.details_min_words:
        return True
    if len(text) < gate_policy.details_min_chars:
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and all(line.startswith(("-", "*", "•")) for line in lines):
        return True
    return False


def is_weak_intake_text(value: str, *, policy: AIQualityPolicy | None = None) -> bool:
    gate_policy = policy or AIQualityPolicy()
    text = _normalize(value)
    if not text:
        return True
    if is_placeholder_text(text):
        return True
    if find_forbidden_terms(text, policy=gate_policy):
        return True
    words = re.findall(r"\b[\w'-]+\b", text)
    if len(words) < gate_policy.intake_min_words:
        return True
    if len(text) < gate_policy.intake_min_chars:
        return True
    return False
