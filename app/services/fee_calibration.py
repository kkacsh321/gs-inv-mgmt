from __future__ import annotations

from typing import Any


def _safe_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def build_final_value_rate_calibration(
    rows: list[dict],
    *,
    current_final_value_rate_percent: float,
    min_gross: float = 1.0,
    outlier_floor_percent: float = -5.0,
    outlier_ceiling_percent: float = 30.0,
) -> dict[str, float]:
    candidates: list[float] = []
    for row in rows:
        if not bool(row.get("fee_estimate_present")):
            continue
        gross = _safe_float(row.get("sale_gross"))
        if gross < float(min_gross):
            continue
        implied = _safe_float(row.get("implied_final_value_rate_percent"))
        if implied < float(outlier_floor_percent) or implied > float(outlier_ceiling_percent):
            continue
        candidates.append(implied)

    sample_count = len(candidates)
    if sample_count <= 0:
        return {
            "sample_count": 0.0,
            "suggested_final_value_rate_percent": float(current_final_value_rate_percent),
            "median_implied_final_value_rate_percent": float(current_final_value_rate_percent),
            "mean_implied_final_value_rate_percent": float(current_final_value_rate_percent),
            "delta_percent": 0.0,
        }

    sorted_vals = sorted(candidates)
    mid = sample_count // 2
    if sample_count % 2 == 1:
        median_val = sorted_vals[mid]
    else:
        median_val = (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0
    mean_val = sum(candidates) / float(sample_count)
    suggested = max(0.0, min(30.0, median_val))
    return {
        "sample_count": float(sample_count),
        "suggested_final_value_rate_percent": round(suggested, 4),
        "median_implied_final_value_rate_percent": round(median_val, 4),
        "mean_implied_final_value_rate_percent": round(mean_val, 4),
        "delta_percent": round(suggested - float(current_final_value_rate_percent), 4),
    }

