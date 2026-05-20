from datetime import date, datetime, timedelta
from collections import defaultdict

import pandas as pd
import streamlit as st

from app.components.ui_helpers import iso_or_none
from app.components.views.shared import render_help_panel
from app.repository import InventoryRepository
from app.utils.time import utc_today


US_STATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "AL": (32.806671, -86.791130),
    "AK": (61.370716, -152.404419),
    "AZ": (33.729759, -111.431221),
    "AR": (34.969704, -92.373123),
    "CA": (36.116203, -119.681564),
    "CO": (39.059811, -105.311104),
    "CT": (41.597782, -72.755371),
    "DE": (39.318523, -75.507141),
    "DC": (38.897438, -77.026817),
    "FL": (27.766279, -81.686783),
    "GA": (33.040619, -83.643074),
    "HI": (21.094318, -157.498337),
    "ID": (44.240459, -114.478828),
    "IL": (40.349457, -88.986137),
    "IN": (39.849426, -86.258278),
    "IA": (42.011539, -93.210526),
    "KS": (38.526600, -96.726486),
    "KY": (37.668140, -84.670067),
    "LA": (31.169546, -91.867805),
    "ME": (44.693947, -69.381927),
    "MD": (39.063946, -76.802101),
    "MA": (42.230171, -71.530106),
    "MI": (43.326618, -84.536095),
    "MN": (45.694454, -93.900192),
    "MS": (32.741646, -89.678696),
    "MO": (38.456085, -92.288368),
    "MT": (46.921925, -110.454353),
    "NE": (41.125370, -98.268082),
    "NV": (38.313515, -117.055374),
    "NH": (43.452492, -71.563896),
    "NJ": (40.298904, -74.521011),
    "NM": (34.840515, -106.248482),
    "NY": (42.165726, -74.948051),
    "NC": (35.630066, -79.806419),
    "ND": (47.528912, -99.784012),
    "OH": (40.388783, -82.764915),
    "OK": (35.565342, -96.928917),
    "OR": (44.572021, -122.070938),
    "PA": (40.590752, -77.209755),
    "RI": (41.680893, -71.511780),
    "SC": (33.856892, -80.945007),
    "SD": (44.299782, -99.438828),
    "TN": (35.747845, -86.692345),
    "TX": (31.054487, -97.563461),
    "UT": (40.150032, -111.862434),
    "VT": (44.045876, -72.710686),
    "VA": (37.769337, -78.169968),
    "WA": (47.400902, -121.490494),
    "WV": (38.491226, -80.954453),
    "WI": (44.268543, -89.616508),
    "WY": (42.755966, -107.302490),
}

US_STATE_NAMES = {
    "ALABAMA": "AL",
    "ALASKA": "AK",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "HAWAII": "HI",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
}

COUNTRY_CENTROIDS: dict[str, tuple[float, float]] = {
    "US": (39.8283, -98.5795),
    "USA": (39.8283, -98.5795),
    "UNITED STATES": (39.8283, -98.5795),
    "CA": (56.1304, -106.3468),
    "CANADA": (56.1304, -106.3468),
    "MX": (23.6345, -102.5528),
    "MEXICO": (23.6345, -102.5528),
}


def _normalize_state(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    if len(upper) == 2 and upper in US_STATE_CENTROIDS:
        return upper
    return US_STATE_NAMES.get(upper, "")


def _normalize_country(value: str) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"", "UNITED STATES", "UNITED STATES OF AMERICA"}:
        return "US"
    return raw


def _order_marketplace(order) -> str:
    return str(getattr(order, "marketplace", "") or "").strip().lower() or "unknown"


def _order_status(order) -> str:
    return str(getattr(order, "order_status", "") or "").strip().lower() or "unknown"


def _filter_orders_by_marketplaces(orders: list, selected_marketplaces: list[str] | set[str] | tuple[str, ...]) -> list:
    selected = {str(value or "").strip().lower() for value in (selected_marketplaces or []) if str(value or "").strip()}
    if not selected:
        return list(orders)
    return [order for order in orders if _order_marketplace(order) in selected]


def _filter_orders_by_statuses(orders: list, selected_statuses: list[str] | set[str] | tuple[str, ...]) -> list:
    selected = {str(value or "").strip().lower() for value in (selected_statuses or []) if str(value or "").strip()}
    if not selected:
        return list(orders)
    return [order for order in orders if _order_status(order) in selected]


SHIPPED_STATUS_VALUES = {"shipped", "delivered", "in_transit", "label_created"}


def _is_shipped_order(order) -> bool:
    if getattr(order, "shipped_at", None) is not None or getattr(order, "delivered_at", None) is not None:
        return True
    status_values = {
        str(getattr(order, "order_status", "") or "").strip().lower(),
        str(getattr(order, "tracking_status", "") or "").strip().lower(),
    }
    return bool(status_values & SHIPPED_STATUS_VALUES)


def _order_dt(order, *, include_unshipped: bool = False) -> datetime | None:
    value = getattr(order, "shipped_at", None) or getattr(order, "delivered_at", None)
    if value is None and (include_unshipped or _is_shipped_order(order)):
        value = getattr(order, "sold_at", None)
    return value if isinstance(value, datetime) else None


def _order_in_window(order, *, start_date: date, end_date: date, include_unshipped: bool = False) -> bool:
    if not include_unshipped and not _is_shipped_order(order):
        return False
    value = _order_dt(order, include_unshipped=include_unshipped)
    if value is None:
        return False
    order_date = value.date()
    return start_date <= order_date <= end_date


def _build_order_destination_rows(
    orders: list,
    *,
    start_date: date,
    end_date: date,
    include_unshipped: bool = False,
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, float, float], dict[str, object]] = {}
    cities_by_key: defaultdict[tuple[str, str, float, float], set[str]] = defaultdict(set)
    postal_by_key: defaultdict[tuple[str, str, float, float], set[str]] = defaultdict(set)
    marketplaces_by_key: defaultdict[tuple[str, str, float, float], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    statuses_by_key: defaultdict[tuple[str, str, float, float], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    skipped = 0

    for order in orders:
        if not _order_in_window(
            order,
            start_date=start_date,
            end_date=end_date,
            include_unshipped=include_unshipped,
        ):
            continue
        state = _normalize_state(getattr(order, "ship_to_state", ""))
        country = _normalize_country(getattr(order, "ship_to_country", ""))
        lat_lon = US_STATE_CENTROIDS.get(state) if country in {"US", "USA", "UNITED STATES"} else None
        if lat_lon is None:
            lat_lon = COUNTRY_CENTROIDS.get(country)
        if lat_lon is None:
            skipped += 1
            continue

        location_label = state if state else country
        key = (country, location_label, lat_lon[0], lat_lon[1])
        if key not in grouped:
            grouped[key] = {
                "country": country,
                "destination": location_label,
                "lat": lat_lon[0],
                "lon": lat_lon[1],
                "order_count": 0,
                "total_amount": 0.0,
                "latest_order_at": "",
                "skipped_unmapped": 0,
            }
        row = grouped[key]
        row["order_count"] = int(row["order_count"]) + 1
        row["total_amount"] = float(row["total_amount"]) + float(getattr(order, "total_amount", 0.0) or 0.0)
        latest = _order_dt(order, include_unshipped=include_unshipped)
        latest_iso = iso_or_none(latest) or ""
        if latest_iso and latest_iso > str(row.get("latest_order_at") or ""):
            row["latest_order_at"] = latest_iso
        city = str(getattr(order, "ship_to_city", "") or "").strip()
        postal = str(getattr(order, "ship_to_postal_code", "") or "").strip()
        if city:
            cities_by_key[key].add(city)
        if postal:
            postal_by_key[key].add(postal[:5])
        marketplaces_by_key[key][_order_marketplace(order)] += 1
        statuses_by_key[key][_order_status(order)] += 1

    rows = []
    for key, row in grouped.items():
        cities = sorted(cities_by_key.get(key, set()))
        postals = sorted(postal_by_key.get(key, set()))
        marketplace_counts = marketplaces_by_key.get(key, {})
        status_counts = statuses_by_key.get(key, {})
        row["cities"] = ", ".join(cities[:12]) + (" ..." if len(cities) > 12 else "")
        row["postal_prefixes"] = ", ".join(postals[:12]) + (" ..." if len(postals) > 12 else "")
        row["marketplaces"] = ", ".join(
            f"{name}:{count}" for name, count in sorted(marketplace_counts.items(), key=lambda item: (-item[1], item[0]))
        )
        row["statuses"] = ", ".join(
            f"{name}:{count}" for name, count in sorted(status_counts.items(), key=lambda item: (-item[1], item[0]))
        )
        rows.append(row)
    rows.sort(key=lambda item: (int(item["order_count"]), float(item["total_amount"])), reverse=True)
    if skipped and rows:
        rows[0]["skipped_unmapped"] = skipped
    return rows


def render_order_map(repo: InventoryRepository) -> None:
    st.subheader("Order Map")
    render_help_panel(
        section_title="Order Map",
        goal="See where shipped orders are going at a glance.",
        steps=[
            "Review destination pins aggregated by state or country.",
            "Use the table to inspect cities, postal prefixes, order counts, and revenue by destination.",
            "Treat pins as approximate because this view uses offline state/country centroids instead of customer addresses.",
        ],
        roadmap_phase="Operational analytics",
    )

    orders = repo.list_orders()
    if not orders:
        st.info("No orders yet.")
        return

    today = utc_today()
    default_start = today - timedelta(days=365)
    c1, c2 = st.columns(2)
    with c1:
        start_date = st.date_input("From", value=default_start, key="order_map_from")
    with c2:
        end_date = st.date_input("To", value=today, key="order_map_to")
    if start_date > end_date:
        st.warning("From date is after To date.")
        return
    marketplace_options = sorted({_order_marketplace(order) for order in orders})
    selected_marketplaces = st.multiselect(
        "Marketplace",
        options=marketplace_options,
        default=marketplace_options,
        key="order_map_marketplaces",
    )
    status_options = sorted({_order_status(order) for order in orders})
    selected_statuses = st.multiselect(
        "Order Status",
        options=status_options,
        default=status_options,
        key="order_map_statuses",
    )
    include_unshipped = st.checkbox(
        "Include unshipped/paid orders",
        value=False,
        key="order_map_include_unshipped",
        help="Default map pins only orders with shipment evidence or shipped/in-transit status.",
    )

    filtered_orders = _filter_orders_by_marketplaces(orders, selected_marketplaces)
    filtered_orders = _filter_orders_by_statuses(filtered_orders, selected_statuses)
    rows = _build_order_destination_rows(
        filtered_orders,
        start_date=start_date,
        end_date=end_date,
        include_unshipped=bool(include_unshipped),
    )
    if not rows:
        st.info("No mappable order destinations for the selected range.")
        return

    df = pd.DataFrame(rows)
    m1, m2, m3 = st.columns(3)
    m1.metric("Mapped Destinations", len(df))
    m2.metric("Mapped Orders", int(df["order_count"].sum()))
    m3.metric("Mapped Revenue", f"${float(df['total_amount'].sum()):,.2f}")
    skipped = int(df["skipped_unmapped"].max()) if "skipped_unmapped" in df.columns else 0
    if skipped:
        st.warning(f"{skipped} order destination(s) were skipped because no offline map coordinate was available.")

    display_columns = [
        "destination",
        "country",
        "order_count",
        "total_amount",
        "latest_order_at",
        "marketplaces",
        "statuses",
        "cities",
        "postal_prefixes",
    ]
    map_df = df[["lat", "lon"]].copy()
    map_df["size"] = df["order_count"].clip(lower=1) * 50
    st.map(map_df, latitude="lat", longitude="lon", size="size", zoom=3)
    st.download_button(
        "Download Destination CSV",
        data=df[display_columns].to_csv(index=False).encode("utf-8"),
        file_name=f"order_map_destinations_{start_date}_{end_date}.csv",
        mime="text/csv",
        key="order_map_download_destinations",
    )
    st.dataframe(
        df[display_columns],
        use_container_width=True,
        hide_index=True,
    )
