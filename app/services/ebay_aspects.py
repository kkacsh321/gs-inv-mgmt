import re


_BULLION_METALS = {"gold", "silver", "platinum", "palladium", "copper"}
_BULLION_CATEGORIES = {
    "bullion",
    "coin",
    "coins",
    "numismatic",
    "rounds",
    "bars",
}
_APPROVED_COIN_GRADERS = {
    "PCGS": "Professional Coin Grading Service (PCGS)",
    "NGC": "Numismatic Guaranty Company (NGC)",
    "ANACS": "American Numismatic Association Certification Service (ANACS)",
    "ICG": "Independent Coin Graders (ICG)",
    "CAC": "Certified Acceptance Corporation (CAC)",
    "ICCS": "International Coin Certification Services (ICCS)",
}
_UNCERTIFIED_VALUES = {"uncertified", "not certified", "none", "n/a", "na"}


def _norm_key(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _first(values: list[str]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


def _aspect_value_labels(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    labels: list[str] = []
    seen: set[str] = set()
    for row in values:
        if isinstance(row, dict):
            raw = row.get("localizedValue", row.get("value"))
        else:
            raw = row
        label = str(raw or "").strip()
        key = _norm_key(label)
        if not label or key in seen:
            continue
        labels.append(label)
        seen.add(key)
    return labels


def normalize_ebay_category_aspect_rows(raw_aspects: object) -> list[dict[str, object]]:
    rows = raw_aspects if isinstance(raw_aspects, list) else []
    normalized: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("localizedAspectName") or row.get("name") or "").strip()
        if not name:
            continue
        constraint = row.get("aspectConstraint") or {}
        if not isinstance(constraint, dict):
            constraint = {}
        values = _aspect_value_labels(row.get("aspectValues"))
        if not values and isinstance(row.get("values"), list):
            values = _aspect_value_labels(row.get("values"))
        required_value = (
            constraint.get("aspectRequired")
            if "aspectRequired" in constraint
            else row.get("required")
        )
        normalized.append(
            {
                "name": name,
                "required": _as_bool(required_value),
                "usage": str(constraint.get("aspectUsage") or row.get("usage") or "").strip(),
                "mode": str(constraint.get("aspectMode") or row.get("mode") or "").strip(),
                "cardinality": str(
                    constraint.get("itemToAspectCardinality") or row.get("cardinality") or ""
                ).strip(),
                "enabled_for_variations": _as_bool(
                    constraint.get("aspectEnabledForVariations", row.get("enabled_for_variations"))
                ),
                "advanced_data_type": str(
                    constraint.get("aspectAdvancedDataType") or row.get("advanced_data_type") or ""
                ).strip(),
                "expected_required_by_date": str(
                    constraint.get("expectedRequiredByDate") or row.get("expected_required_by_date") or ""
                ).strip(),
                "values": values,
            }
        )
    return normalized


def missing_required_ebay_aspects(
    category_aspects: object,
    existing_aspects: dict[str, list[str]] | None,
) -> list[dict[str, object]]:
    existing = existing_aspects or {}
    filled_keys = {
        _norm_key(key)
        for key, values in existing.items()
        if str(key or "").strip() and any(str(value or "").strip() for value in (values or []))
    }
    missing: list[dict[str, object]] = []
    for row in normalize_ebay_category_aspect_rows(category_aspects):
        if not bool(row.get("required")):
            continue
        if _norm_key(str(row.get("name") or "")) not in filled_keys:
            missing.append(row)
    return missing


def _shape_from_title(title: str) -> str:
    text = str(title or "").strip().lower()
    if "bar" in text:
        return "Bar"
    if "round" in text:
        return "Round"
    if "coin" in text:
        return "Coin"
    return "Round"


def _weight_label_oz(weight_oz: object) -> str:
    try:
        value = float(str(weight_oz or 0).strip() or 0)
    except Exception:
        value = 0.0
    if value <= 0:
        return ""
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value))} oz"
    return f"{value:g} oz"


def _fineness_from_text(text: str) -> str:
    raw = str(text or "")
    match = re.search(r"0?\.(9999|999|995|925|900)", raw)
    if not match:
        return ""
    value = match.group(0)
    if value.startswith("."):
        return f"0{value}"
    return value


def _approved_grader_from_text(text: str) -> tuple[str, str]:
    raw = str(text or "")
    for short_name, full_name in _APPROVED_COIN_GRADERS.items():
        if re.search(rf"\b{re.escape(short_name)}\b", raw, flags=re.IGNORECASE):
            return short_name, full_name
    return "", ""


def _coin_grade_from_text(text: str) -> str:
    raw = str(text or "")
    match = re.search(r"\b(MS|PF|PR|SP|AU|XF|EF|VF|F|VG|G|AG|FR|PO)[\s-]?([0-7]?\d)\b", raw, flags=re.IGNORECASE)
    if not match:
        return ""
    prefix = match.group(1).upper()
    numeric = int(match.group(2))
    if numeric < 1 or numeric > 70:
        return ""
    return f"{prefix} {numeric}"


def title_has_numerical_coin_grade(title: str) -> bool:
    return bool(_coin_grade_from_text(title))


def aspects_have_approved_grader_evidence(aspects: dict[str, list[str]] | None) -> bool:
    payload = aspects or {}
    approved_short = {key.lower() for key in _APPROVED_COIN_GRADERS}
    approved_text = {
        text.lower()
        for short_name, full_name in _APPROVED_COIN_GRADERS.items()
        for text in (short_name, full_name)
    }
    for key, values in payload.items():
        key_norm = _norm_key(key)
        if key_norm not in {"certification", "professional grader", "grader", "grading service"}:
            continue
        for value in values or []:
            value_norm = _norm_key(str(value or ""))
            if value_norm in approved_short or value_norm in approved_text:
                return True
            if any(re.search(rf"\b{re.escape(short_name)}\b", str(value or ""), flags=re.IGNORECASE) for short_name in _APPROVED_COIN_GRADERS):
                return True
    return False


def _set_aspect_if_missing_or_uncertified(
    payload: dict[str, list[str]],
    normalized_existing_keys: dict[str, str],
    *,
    key: str,
    value: str,
) -> bool:
    existing_key = normalized_existing_keys.get(_norm_key(key))
    if existing_key:
        existing_values = payload.get(existing_key) or []
        has_value = any(str(item or "").strip() for item in existing_values)
        first_value = _first([str(item or "") for item in existing_values])
        if has_value and _norm_key(first_value) not in _UNCERTIFIED_VALUES:
            return False
        payload[existing_key] = [value]
        return True
    payload[key] = [value]
    normalized_existing_keys[_norm_key(key)] = key
    return True


def _append_added_key(added_keys: list[str], key: str) -> None:
    if key not in added_keys:
        added_keys.append(key)


def is_bullion_like_product(*, category: str, metal_type: str, title: str) -> bool:
    cat = _norm_key(category)
    metal = _norm_key(metal_type)
    txt = _norm_key(title)
    if metal in _BULLION_METALS:
        return True
    if cat in _BULLION_CATEGORIES:
        return True
    return any(token in txt for token in ["coin", "round", "bar", "bullion", "oz"])


def merge_ebay_aspects_defaults(
    *,
    category: str,
    metal_type: str,
    title: str,
    weight_oz: object,
    existing_aspects: dict[str, list[str]] | None,
) -> tuple[dict[str, list[str]], list[str]]:
    source = dict(existing_aspects or {})
    if not is_bullion_like_product(category=category, metal_type=metal_type, title=title):
        return source, []

    metal_value = _first([str(metal_type or "").strip().title(), "Copper"])
    shape = _shape_from_title(title)
    weight_label = _weight_label_oz(weight_oz)
    fineness = _fineness_from_text(" ".join([str(title or ""), str(metal_type or "")])) or "0.999"

    defaults: dict[str, list[str]] = {
        "Certification": ["Uncertified"],
        "Circulated/Uncirculated": ["Uncirculated"],
        "Metal Type": [metal_value],
        "Composition": [metal_value],
        "Shape": [shape],
        "Type": [shape],
        "Brand/Mint": ["Unbranded"],
        "Fineness": [fineness],
        "Year": ["Undated"],
        "Country of Origin": ["United States"],
        "Unit Quantity": ["1"],
    }
    if weight_label:
        defaults["Precious Metal Content per Unit"] = [weight_label]
        defaults["Total Precious Metal Content"] = [weight_label.replace("oz", "Oz.")]
        defaults["Unit Type"] = ["oz"]

    updated = dict(source)
    added_keys: list[str] = []
    normalized_existing_keys = {_norm_key(key): key for key in updated.keys()}
    for key, value in defaults.items():
        if _norm_key(key) in normalized_existing_keys:
            continue
        updated[key] = value
        _append_added_key(added_keys, key)

    grader_short, grader_full = _approved_grader_from_text(title)
    grade_value = _coin_grade_from_text(title)
    if grader_short:
        if _set_aspect_if_missing_or_uncertified(
            updated,
            normalized_existing_keys,
            key="Certification",
            value=grader_short,
        ):
            _append_added_key(added_keys, "Certification")
        if _set_aspect_if_missing_or_uncertified(
            updated,
            normalized_existing_keys,
            key="Professional Grader",
            value=grader_full,
        ):
            _append_added_key(added_keys, "Professional Grader")
        if grade_value and _set_aspect_if_missing_or_uncertified(
            updated,
            normalized_existing_keys,
            key="Grade",
            value=grade_value,
        ):
            _append_added_key(added_keys, "Grade")
    return updated, added_keys
