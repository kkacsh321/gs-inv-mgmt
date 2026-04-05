import json
from typing import Any


def extract_json_object(raw_text: str) -> dict[str, Any]:
    text = str(raw_text or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        snippet = text[first : last + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def normalize_ai_text(raw_text: str, *, preferred_keys: tuple[str, ...] = ("notes", "summary", "description")) -> str:
    raw = str(raw_text or "").strip()
    if not raw:
        return ""
    payload = extract_json_object(raw)
    if not payload:
        return raw

    for key in preferred_keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value

    parts: list[str] = []
    for key, label in (
        ("coin_name", "Coin"),
        ("possible_country_or_mint", "Country/Mint"),
        ("year_or_period", "Year/Period"),
        ("denomination", "Denomination"),
        ("metal", "Metal"),
        ("confidence", "Confidence"),
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            parts.append(f"{label}: {value}")

    keywords = payload.get("search_keywords")
    if isinstance(keywords, list):
        tokens = [str(item).strip() for item in keywords if str(item).strip()]
        if tokens:
            parts.append(f"Keywords: {', '.join(tokens[:12])}")

    if parts:
        return "\n".join(parts)
    return raw


def parse_coin_grader_structured(raw_text: str) -> dict[str, Any]:
    payload = extract_json_object(raw_text)
    if not isinstance(payload, dict):
        return {}
    decision = str(
        payload.get("submit_for_professional_grading")
        or payload.get("professional_grading_recommendation")
        or ""
    ).strip().upper()
    if decision not in {"YES", "NO", "CONDITIONAL"}:
        decision = ""
    return {
        "estimated_grade_range": str(payload.get("estimated_grade_range") or "").strip(),
        "confidence_0_100": payload.get("confidence_0_100"),
        "key_observations": (
            [str(item).strip() for item in (payload.get("key_observations") or []) if str(item).strip()]
            if isinstance(payload.get("key_observations"), list)
            else []
        ),
        "red_flags": (
            [str(item).strip() for item in (payload.get("red_flags") or []) if str(item).strip()]
            if isinstance(payload.get("red_flags"), list)
            else []
        ),
        "estimated_as_is_value_usd": payload.get("estimated_as_is_value_usd"),
        "estimated_post_grade_value_usd": payload.get("estimated_post_grade_value_usd"),
        "estimated_grading_total_cost_usd": payload.get("estimated_grading_total_cost_usd"),
        "estimated_net_upside_usd": payload.get("estimated_net_upside_usd"),
        "submit_for_professional_grading": decision,
        "recommendation_rationale": str(payload.get("recommendation_rationale") or "").strip(),
        "suggested_grade_service_priority": (
            [str(item).strip() for item in (payload.get("suggested_grade_service_priority") or []) if str(item).strip()]
            if isinstance(payload.get("suggested_grade_service_priority"), list)
            else []
        ),
        "notes": str(payload.get("notes") or "").strip(),
    }


def coin_grader_structured_to_text(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict) or not payload:
        return ""
    lines: list[str] = []
    if str(payload.get("estimated_grade_range") or "").strip():
        lines.append(f"Estimated Grade Range: {str(payload.get('estimated_grade_range') or '').strip()}")
    confidence = payload.get("confidence_0_100")
    if confidence is not None and str(confidence).strip():
        lines.append(f"Confidence: {confidence}")
    decision = str(payload.get("submit_for_professional_grading") or "").strip()
    if decision:
        lines.append(f"Submit For Professional Grading: {decision}")
    rationale = str(payload.get("recommendation_rationale") or "").strip()
    if rationale:
        lines.append(f"Recommendation Rationale: {rationale}")
    for key, label in (
        ("estimated_as_is_value_usd", "Estimated As-Is Value USD"),
        ("estimated_post_grade_value_usd", "Estimated Post-Grade Value USD"),
        ("estimated_grading_total_cost_usd", "Estimated Grading Total Cost USD"),
        ("estimated_net_upside_usd", "Estimated Net Upside USD"),
    ):
        value = payload.get(key)
        if value is not None and str(value).strip():
            lines.append(f"{label}: {value}")
    observations = payload.get("key_observations") or []
    if observations:
        lines.append("Key Observations: " + "; ".join([str(item).strip() for item in observations if str(item).strip()][:8]))
    red_flags = payload.get("red_flags") or []
    if red_flags:
        lines.append("Red Flags: " + "; ".join([str(item).strip() for item in red_flags if str(item).strip()][:8]))
    services = payload.get("suggested_grade_service_priority") or []
    if services:
        lines.append("Suggested Service Priority: " + ", ".join([str(item).strip() for item in services if str(item).strip()][:6]))
    notes = str(payload.get("notes") or "").strip()
    if notes:
        lines.append(f"Notes: {notes}")
    return "\n".join([line for line in lines if line.strip()]).strip()
