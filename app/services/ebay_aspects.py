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


def _norm_key(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _first(values: list[str]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


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
        added_keys.append(key)
    return updated, added_keys
