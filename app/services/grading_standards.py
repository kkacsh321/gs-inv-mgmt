from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import html
import re

import requests


PCGS_GRADES_URL = "https://www.pcgs.com/grades/proof"
NGC_GRADING_PROCESS_URL = "https://www.ngccoin.com/coin-grading/grading-process/ngc-grading-process.aspx"
NGC_DETAILS_URL = "https://www.ngccoin.com/coin-grading/details-grading/"
ANACS_FAQ_URL = "https://anacs.com/faqs/"
ICG_GUARANTEE_URL = "https://www.icgcoin.com/about/guarantee/"


FALLBACK_GRADING_CONTEXT = (
    "Reference major grading-service style guidance (PCGS, NGC, ANACS, ICG) and Sheldon-style 1-70 conventions. "
    "Score conservatively using wear/friction on high points, luster retention, strike quality, "
    "surface preservation (marks/hairlines), eye appeal, and signs of cleaning/damage/corrosion. "
    "If details/problem characteristics are present, clearly label as details-style rather than straight-grade."
)

FALLBACK_COMP_CONTEXT = (
    "For comps, prioritize sold results and keep certified-vs-raw populations separate. "
    "Do not mix unlike certification tiers (PCGS/NGC/ANACS/ICG) or distant grade bands without explicit adjustment notes. "
    "Flag likely outliers, weak title matches, or problem-coin risk."
)


CURATED_GRADING_BASELINE = (
    "Coin grading baseline (service-aware):\n"
    "1) Numeric backbone: use Sheldon-style 1-70 logic for problem-free coins (Poor through Mint State/Proof).\n"
    "2) Technical pillars: evaluate wear on high points, strike sharpness, luster/reflectivity, surface marks/hairlines, and eye appeal.\n"
    "3) High-grade discipline: in MS/PF bands, prioritize mark severity, luster quality, strike quality, and visual balance; avoid optimistic jumps.\n"
    "4) Circulated discipline: wear progression is primary, then marks/originality and remaining luster where applicable.\n"
    "5) Details/problem handling: if cleaned, scratched, holed, corroded, altered, or otherwise impaired, prefer details/problem outcome over straight grade.\n"
    "6) Service-context awareness:\n"
    "   - PCGS guidance emphasizes technical factors + eye-appeal adjustment.\n"
    "   - NGC scale/process and details workflow emphasize numeric vs details separation.\n"
    "   - ANACS explicitly grades/labels problem coins with detail grades.\n"
    "   - ICG policies include refusal or caution for questionable toning/altered surfaces.\n"
    "7) Output policy: provide conservative estimated grade range, confidence, and explicit reasons; separate \"raw estimate\" from any certified-market assumption."
)


CURATED_COMP_BASELINE = (
    "Comp baseline (grading-aware):\n"
    "1) Prefer sold comps over active asks; use active listings only as secondary market-pressure signals.\n"
    "2) Segment populations: do not blend raw, details/problem, and certified coins without an explicit adjustment note.\n"
    "3) Certified comps: compare by grading-service tier (PCGS/NGC/ANACS/ICG) and nearby grade bands first.\n"
    "4) Details/problem comps: compare primarily against similar impairment types and severity; avoid straight-grade anchoring.\n"
    "5) If match quality is weak (title mismatch, missing attribution, altered/cleaned risk), lower confidence and widen range.\n"
    "6) For bullion-like items, weight melt/spot context separately from collectible premium.\n"
    "7) Return a practical price range, preferred listing strategy (BIN/Auction), and explicit confidence factors."
)


def _strip_html(raw_html: str) -> str:
    data = str(raw_html or "")
    data = re.sub(r"(?is)<script.*?>.*?</script>", " ", data)
    data = re.sub(r"(?is)<style.*?>.*?</style>", " ", data)
    data = re.sub(r"(?is)<[^>]+>", " ", data)
    data = html.unescape(data)
    data = re.sub(r"\s+", " ", data).strip()
    return data


def _split_sentences(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = re.split(r"(?<=[\.\!\?])\s+", raw)
    cleaned: list[str] = []
    for part in parts:
        sentence = str(part or "").strip()
        if len(sentence) < 30:
            continue
        if sentence not in cleaned:
            cleaned.append(sentence)
    return cleaned


def _find_sentences(text: str, patterns: list[str], limit: int = 3) -> list[str]:
    sentences = _split_sentences(text)
    matched: list[str] = []
    for sentence in sentences:
        lowered = sentence.lower()
        if any(re.search(pattern, lowered) for pattern in patterns):
            matched.append(sentence)
        if len(matched) >= max(1, int(limit)):
            break
    return matched


def _compact(text: str, limit: int = 220) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _fetch_page_text(url: str, timeout_seconds: int = 8) -> tuple[bool, str]:
    try:
        response = requests.get(
            url,
            timeout=timeout_seconds,
            headers={"User-Agent": "GoldenStackersInventory/1.0 (+grading-standards-fetch)"},
        )
        response.raise_for_status()
        return True, _strip_html(response.text)
    except Exception:
        return False, ""


def _contains_any(text: str, needles: list[str]) -> bool:
    lowered = str(text or "").lower()
    return any(str(n).lower() in lowered for n in needles)


@lru_cache(maxsize=1)
def fetch_standards_snapshot() -> dict:
    checked_at = datetime.now(timezone.utc).isoformat()
    sources: dict[str, dict[str, object]] = {}
    for key, url in [
        ("pcgs_grades", PCGS_GRADES_URL),
        ("ngc_process", NGC_GRADING_PROCESS_URL),
        ("ngc_details", NGC_DETAILS_URL),
        ("anacs_faq", ANACS_FAQ_URL),
        ("icg_guarantee", ICG_GUARANTEE_URL),
    ]:
        ok, text = _fetch_page_text(url)
        sources[key] = {"ok": ok, "url": url, "text": text}

    pcgs_text = str(sources["pcgs_grades"]["text"] or "")
    ngc_process_text = str(sources["ngc_process"]["text"] or "")
    ngc_details_text = str(sources["ngc_details"]["text"] or "")
    anacs_text = str(sources["anacs_faq"]["text"] or "")
    icg_text = str(sources["icg_guarantee"]["text"] or "")

    indicators = {
        "sheldon_scale_detected": (
            _contains_any(ngc_process_text, ["1 to 70", "scale of 1 to 70", "ms 60"])
            or _contains_any(pcgs_text, ["ms/pr-63", "au-58", "xf-45"])
        ),
        "details_grading_detected": _contains_any(ngc_details_text, ["details grading", "unc details", "au details"]),
        "anacs_problem_coin_detected": _contains_any(anacs_text, ["problem coins", "detail grade"]),
        "icg_problem_language_detected": _contains_any(icg_text, ["questionable toning", "altered surfaces"]),
    }
    extracted = {
        "pcgs": _find_sentences(
            pcgs_text,
            patterns=[
                r"\bpoor-1\b|\bpr-?\d+\b|\bms-?\d+\b|\bau-?\d+\b|\bxf-?\d+\b",
                r"\bstrike\b|\bluster\b|\bsurface\b|\beye appeal\b",
            ],
            limit=4,
        ),
        "ngc_process": _find_sentences(
            ngc_process_text,
            patterns=[
                r"\b1 to 70\b|\bscale\b|\bsheldon\b",
                r"\bstrike\b|\bluster\b|\bsurface\b|\beye appeal\b",
                r"\bgrading process\b|\bgrading\b",
            ],
            limit=4,
        ),
        "ngc_details": _find_sentences(
            ngc_details_text,
            patterns=[
                r"\bdetails grading\b|\bdetails\b|\bcleaned\b|\bdamaged\b|\bcorrosion\b|\bimproperly cleaned\b",
            ],
            limit=4,
        ),
        "anacs": _find_sentences(
            anacs_text,
            patterns=[
                r"\bproblem coins?\b|\bdetail grade\b|\bcleaned\b|\bdamaged\b|\bquestionable\b",
                r"\bgrading\b|\bauthenticity\b",
            ],
            limit=4,
        ),
        "icg": _find_sentences(
            icg_text,
            patterns=[
                r"\bquestionable toning\b|\baltered surfaces\b|\bcleaned\b|\bdamaged\b|\bcorrosion\b|\bdetails\b",
                r"\bgrade\b|\bguarantee\b",
            ],
            limit=4,
        ),
    }
    return {
        "checked_at_utc": checked_at,
        "sources": sources,
        "indicators": indicators,
        "extracted": extracted,
    }


def build_coin_grading_rules_context_from_web() -> str:
    snapshot = fetch_standards_snapshot()
    indicators = snapshot.get("indicators") or {}
    extracted = snapshot.get("extracted") or {}
    sources = snapshot.get("sources") or {}
    checked_at = str(snapshot.get("checked_at_utc") or "")
    coverage: list[str] = []
    if indicators.get("sheldon_scale_detected"):
        coverage.append("Sheldon-style numeric scale and adjectival ranges were detected in primary grading references.")
    if indicators.get("details_grading_detected"):
        coverage.append("Details/problem-coin style handling was detected (e.g., NGC Details style categories).")
    if indicators.get("anacs_problem_coin_detected"):
        coverage.append("ANACS problem/details grading language was detected.")
    if indicators.get("icg_problem_language_detected"):
        coverage.append("ICG policy language about questionable/altered/problem surfaces was detected.")

    service_lines: list[str] = []
    mapping = [
        ("PCGS", "pcgs", "pcgs_grades"),
        ("NGC Process", "ngc_process", "ngc_process"),
        ("NGC Details", "ngc_details", "ngc_details"),
        ("ANACS", "anacs", "anacs_faq"),
        ("ICG", "icg", "icg_guarantee"),
    ]
    for label, extracted_key, source_key in mapping:
        rows = [str(item).strip() for item in (extracted.get(extracted_key) or []) if str(item).strip()]
        source_url = str((sources.get(source_key) or {}).get("url") or "")
        if rows:
            digest = " | ".join(_compact(row, 180) for row in rows[:2])
            service_lines.append(f"{label} ({source_url}): {digest}")
        else:
            service_lines.append(f"{label} ({source_url}): no detailed snippet extracted.")

    if not any(":" in line and "no detailed snippet extracted" not in line for line in service_lines):
        return f"{CURATED_GRADING_BASELINE}\n\nFallback note: {FALLBACK_GRADING_CONTEXT}"

    base = CURATED_GRADING_BASELINE
    coverage_text = " ".join(coverage) if coverage else "No strong indicator set detected; use conservative fallback."
    return (
        f"{base}\n\nWeb snapshot ({checked_at}) indicators: {coverage_text}\n\n"
        "Service digests:\n- " + "\n- ".join(service_lines)
    )


def build_comp_rules_context_from_web() -> str:
    snapshot = fetch_standards_snapshot()
    indicators = snapshot.get("indicators") or {}
    extracted = snapshot.get("extracted") or {}
    sources = snapshot.get("sources") or {}
    checked_at = str(snapshot.get("checked_at_utc") or "")
    points = []
    if indicators.get("sheldon_scale_detected"):
        points.append("Compare comps within near grade bands (Sheldon-style) rather than broad condition jumps.")
    if indicators.get("details_grading_detected") or indicators.get("anacs_problem_coin_detected"):
        points.append("Separate straight-grade comps from details/problem-coin comps.")
    if indicators.get("icg_problem_language_detected"):
        points.append("Treat questionable toning/altered surface signals as risk flags in comp confidence.")

    snippets: list[str] = []
    for label, extracted_key, source_key in [
        ("PCGS", "pcgs", "pcgs_grades"),
        ("NGC Details", "ngc_details", "ngc_details"),
        ("ANACS", "anacs", "anacs_faq"),
        ("ICG", "icg", "icg_guarantee"),
    ]:
        rows = [str(item).strip() for item in (extracted.get(extracted_key) or []) if str(item).strip()]
        source_url = str((sources.get(source_key) or {}).get("url") or "")
        if rows:
            snippets.append(f"{label} ({source_url}): {_compact(rows[0], 190)}")

    base = CURATED_COMP_BASELINE
    if not points and not snippets:
        return f"{CURATED_COMP_BASELINE}\n\nFallback note: {FALLBACK_COMP_CONTEXT}"
    joined_points = " ".join(points) if points else "Use conservative grade/service segmentation."
    joined_snippets = "\n- " + "\n- ".join(snippets) if snippets else ""
    return f"{base}\n\nWeb snapshot ({checked_at}): {joined_points}{joined_snippets}"


def clear_standards_snapshot_cache() -> None:
    fetch_standards_snapshot.cache_clear()
