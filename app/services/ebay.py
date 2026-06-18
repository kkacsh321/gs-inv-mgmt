import base64
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timedelta, timezone
from html import unescape
from xml.sax.saxutils import escape as xml_escape
from urllib.parse import quote_plus, urlencode

import requests

from app.config import settings


EBAY_CONDITION_ID_TO_INVENTORY_CONDITION = {
    "1000": "NEW",
    "1500": "NEW_OTHER",
    "1750": "NEW_WITH_DEFECTS",
    "2000": "CERTIFIED_REFURBISHED",
    "2010": "EXCELLENT_REFURBISHED",
    "2020": "VERY_GOOD_REFURBISHED",
    "2030": "GOOD_REFURBISHED",
    "2500": "SELLER_REFURBISHED",
    "2750": "LIKE_NEW",
    "3000": "USED_EXCELLENT",
    "4000": "USED_VERY_GOOD",
    "5000": "USED_GOOD",
    "6000": "USED_ACCEPTABLE",
    "7000": "FOR_PARTS_OR_NOT_WORKING",
}

EBAY_INVENTORY_CONDITION_LABELS = {
    "NEW": "New",
    "NEW_OTHER": "New other",
    "NEW_WITH_DEFECTS": "New with defects",
    "CERTIFIED_REFURBISHED": "Certified refurbished",
    "EXCELLENT_REFURBISHED": "Excellent refurbished",
    "VERY_GOOD_REFURBISHED": "Very good refurbished",
    "GOOD_REFURBISHED": "Good refurbished",
    "SELLER_REFURBISHED": "Seller refurbished",
    "LIKE_NEW": "Like new",
    "USED_EXCELLENT": "Used excellent",
    "USED_VERY_GOOD": "Used very good",
    "USED_GOOD": "Used good",
    "USED_ACCEPTABLE": "Used acceptable",
    "FOR_PARTS_OR_NOT_WORKING": "For parts or not working",
}

EBAY_DEFAULT_INVENTORY_CONDITIONS = [
    "NEW",
    "NEW_OTHER",
    "LIKE_NEW",
    "USED_EXCELLENT",
    "USED_VERY_GOOD",
    "USED_GOOD",
    "USED_ACCEPTABLE",
    "FOR_PARTS_OR_NOT_WORKING",
]
EBAY_MAX_CONDITION_DESCRIPTION_CHARS = 1000
EBAY_MAX_INVENTORY_DESCRIPTION_CHARS = 4000
EBAY_INVENTORY_SKU_MAX_CHARS = 50


def build_ebay_inventory_item_sku(
    product_sku: str,
    *,
    listing_id: int | None = None,
    suffix: str = "",
) -> str:
    """Build a stable eBay Inventory API SKU for a local listing row.

    eBay treats Inventory API SKU as the inventory item identity. A single
    product can have multiple local marketplace listings, so direct publishes
    need a listing-specific SKU to avoid revising another live offer for the
    same product.
    """
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", str(product_sku or "").strip()).strip("-._")
    if not base:
        base = "GS-LISTING"

    suffix_parts: list[str] = []
    if listing_id is not None:
        try:
            clean_listing_id = int(listing_id)
        except (TypeError, ValueError):
            clean_listing_id = 0
        if clean_listing_id > 0:
            suffix_parts.append(f"L{clean_listing_id}")
    clean_suffix = re.sub(r"[^A-Za-z0-9._-]+", "-", str(suffix or "").strip()).strip("-._")
    if clean_suffix:
        suffix_parts.append(clean_suffix)

    if not suffix_parts:
        return base[:EBAY_INVENTORY_SKU_MAX_CHARS].strip("-._") or "GS-LISTING"

    suffix_text = "-" + "-".join(suffix_parts)
    max_base_len = max(1, EBAY_INVENTORY_SKU_MAX_CHARS - len(suffix_text))
    trimmed_base = base[:max_base_len].strip("-._") or "GS"
    return f"{trimmed_base}{suffix_text}"[:EBAY_INVENTORY_SKU_MAX_CHARS]


def ebay_condition_label(condition: str) -> str:
    normalized = str(condition or "").strip().upper()
    return EBAY_INVENTORY_CONDITION_LABELS.get(normalized, normalized.replace("_", " ").title())


def validate_inventory_item_condition_description(payload: dict) -> None:
    if not isinstance(payload, dict):
        return
    condition_description = str(payload.get("conditionDescription") or "")
    if len(condition_description) > EBAY_MAX_CONDITION_DESCRIPTION_CHARS:
        raise ValueError(
            "eBay condition description must be "
            f"{EBAY_MAX_CONDITION_DESCRIPTION_CHARS} characters or fewer "
            f"(currently {len(condition_description)})."
        )


def validate_inventory_item_product_description(payload: dict) -> None:
    if not isinstance(payload, dict):
        return
    product = payload.get("product") if isinstance(payload.get("product"), dict) else {}
    description = str(product.get("description") or "")
    if not description:
        raise ValueError("eBay inventory product description must be between 1 and 4000 characters.")
    if len(description) > EBAY_MAX_INVENTORY_DESCRIPTION_CHARS:
        raise ValueError(
            "eBay inventory product description must be "
            f"{EBAY_MAX_INVENTORY_DESCRIPTION_CHARS} characters or fewer "
            f"(currently {len(description)})."
        )


def normalize_ebay_condition_policy_rows(
    policies: list[dict] | dict | None,
    *,
    category_id: str = "",
) -> list[dict]:
    if isinstance(policies, dict):
        raw_policies = policies.get("itemConditionPolicies") or []
    else:
        raw_policies = policies or []
    if not isinstance(raw_policies, list):
        return []
    preferred_category_id = str(category_id or "").strip()
    selected_policy: dict | None = None
    for policy in raw_policies:
        if not isinstance(policy, dict):
            continue
        if preferred_category_id and str(policy.get("categoryId") or "").strip() == preferred_category_id:
            selected_policy = policy
            break
        if selected_policy is None:
            selected_policy = policy
    if not selected_policy:
        return []
    required = bool(selected_policy.get("itemConditionRequired"))
    rows: list[dict] = []
    seen: set[str] = set()
    for condition in selected_policy.get("itemConditions") or []:
        if not isinstance(condition, dict):
            continue
        condition_id = str(condition.get("conditionId") or "").strip()
        inventory_condition = EBAY_CONDITION_ID_TO_INVENTORY_CONDITION.get(condition_id, "")
        if not inventory_condition:
            continue
        if inventory_condition in seen:
            continue
        seen.add(inventory_condition)
        label = str(condition.get("conditionDescription") or "").strip() or ebay_condition_label(inventory_condition)
        rows.append(
            {
                "condition": inventory_condition,
                "label": label,
                "condition_id": condition_id,
                "category_id": str(selected_policy.get("categoryId") or "").strip(),
                "required": required,
                "usage": str(condition.get("usage") or "").strip(),
                "help_text": str(condition.get("conditionHelpText") or "").strip(),
                "descriptor_count": len(condition.get("conditionDescriptors") or []),
            }
        )
    return rows


class EbayClient:
    """
    Lightweight eBay API client for OAuth bootstrapping and initial data pulls.

    This intentionally starts small: auth URL generation, token exchange, and
    a sample sell-account call. As requirements stabilize, extend into inventory,
    order ingestion, listing updates, and webhook handling.
    """

    MARKETPLACE_INSIGHTS_SCOPE = "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"

    SCOPES = [
        "https://api.ebay.com/oauth/api_scope",
        "https://api.ebay.com/oauth/api_scope/sell.inventory",
        "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
        "https://api.ebay.com/oauth/api_scope/sell.account",
        "https://api.ebay.com/oauth/api_scope/sell.finances",
        "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly",
    ]

    def __init__(self) -> None:
        self.environment = (settings.ebay_environment or "sandbox").strip().lower()
        if self.environment == "production":
            self.auth_host = "https://auth.ebay.com"
            self.api_host = "https://api.ebay.com"
            self.media_host = "https://apim.ebay.com"
            self.identity_host = "https://apiz.ebay.com"
        else:
            self.auth_host = "https://auth.sandbox.ebay.com"
            self.api_host = "https://api.sandbox.ebay.com"
            self.media_host = "https://apim.sandbox.ebay.com"
            self.identity_host = "https://apiz.sandbox.ebay.com"

    def is_configured(self) -> bool:
        return bool(settings.ebay_client_id and settings.ebay_client_secret and settings.ebay_ru_name)

    def _basic_auth_header(self) -> str:
        raw = f"{settings.ebay_client_id}:{settings.ebay_client_secret}".encode("utf-8")
        return base64.b64encode(raw).decode("utf-8")

    def _token_endpoint(self) -> str:
        return f"{self.api_host}/identity/v1/oauth2/token"

    def _rest_headers(self, access_token: str, content_language: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {access_token.strip()}",
            "Content-Type": "application/json",
        }
        if content_language:
            headers["Content-Language"] = self._normalize_content_language(content_language)
        return headers

    def _normalize_content_language(self, value: str | None) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "en-US"
        if not self._CONTENT_LANGUAGE_RE.match(raw):
            return "en-US"
        if "-" in raw:
            left, right = raw.split("-", 1)
            return f"{left.lower()}-{right.upper()}"
        return raw.lower()

    def _raise_for_status_with_body(self, response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            body = (response.text or "").strip()
            body_preview = body[:1000] if body else "no response body"
            raise requests.HTTPError(
                f"{exc}. eBay response body: {body_preview}",
                response=response,
            ) from exc

    @staticmethod
    def payment_policy_requires_immediate_payment(payload: object) -> bool:
        immediate_keys = {
            "immediatepay",
            "immediatepayment",
            "immediatepaymentrequired",
            "requiresimmediatepayment",
        }
        if isinstance(payload, dict):
            for key, value in payload.items():
                normalized_key = str(key or "").strip().lower().replace("_", "").replace("-", "")
                if normalized_key in immediate_keys and bool(value):
                    return True
                if isinstance(value, (dict, list)) and EbayClient.payment_policy_requires_immediate_payment(value):
                    return True
        if isinstance(payload, list):
            return any(EbayClient.payment_policy_requires_immediate_payment(item) for item in payload)
        return False

    def authorize_url(self, state: str = "goldenstackers") -> str:
        params = {
            "client_id": settings.ebay_client_id,
            "redirect_uri": settings.ebay_ru_name,
            "response_type": "code",
            "scope": " ".join(self.SCOPES),
            "state": state,
        }
        return f"{self.auth_host}/oauth2/authorize?{urlencode(params)}"

    def exchange_code_for_tokens(self, auth_code: str) -> dict:
        token_endpoint = self._token_endpoint()
        basic_auth = self._basic_auth_header()

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic_auth}",
        }

        payload = {
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": settings.ebay_ru_name,
        }

        response = requests.post(token_endpoint, headers=headers, data=payload, timeout=30)
        self._raise_for_status_with_body(response)
        return response.json()

    def fetch_application_token(self, scopes: list[str] | None = None) -> dict:
        token_endpoint = self._token_endpoint()
        basic_auth = self._basic_auth_header()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic_auth}",
        }
        scope_values = scopes or [self.SCOPES[0]]
        payload = {
            "grant_type": "client_credentials",
            "scope": " ".join(scope_values),
        }
        response = requests.post(token_endpoint, headers=headers, data=payload, timeout=30)
        self._raise_for_status_with_body(response)
        return response.json()

    def refresh_user_token(self, refresh_token: str, scopes: list[str] | None = None) -> dict:
        token_endpoint = self._token_endpoint()
        basic_auth = self._basic_auth_header()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic_auth}",
        }
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": str(refresh_token or "").strip(),
        }
        scope_values = scopes or self.SCOPES
        if scope_values:
            payload["scope"] = " ".join(scope_values)
        response = requests.post(token_endpoint, headers=headers, data=payload, timeout=30)
        self._raise_for_status_with_body(response)
        return response.json()

    def decode_access_token_claims(self, access_token: str) -> dict:
        token = str(access_token or "").strip()
        if not token or "." not in token:
            return {}
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1].strip()
        if not payload:
            return {}
        padding = "=" * (-len(payload) % 4)
        try:
            raw = base64.urlsafe_b64decode((payload + padding).encode("utf-8"))
            data = json.loads(raw.decode("utf-8", errors="ignore"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def get_account_privileges(self, access_token: str) -> dict:
        endpoint = f"{self.api_host}/sell/account/v1/privilege"
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.get(endpoint, headers=headers, timeout=30)
        self._raise_for_status_with_body(response)
        return response.json()

    def get_account_user_preferences(self, *, access_token: str, marketplace_id: str = "EBAY_US") -> dict:
        endpoint = f"{self.api_host}/sell/account/v2/user_preferences"
        headers = {
            "Authorization": f"Bearer {access_token.strip()}",
            "X-EBAY-C-MARKETPLACE-ID": str(marketplace_id or "EBAY_US").strip().upper(),
        }
        response = requests.get(endpoint, headers=headers, timeout=30)
        self._raise_for_status_with_body(response)
        payload = response.json()
        return payload if isinstance(payload, dict) else {"response": payload}

    def get_default_category_tree_id(self, *, access_token: str, marketplace_id: str = "EBAY_US") -> str:
        endpoint = f"{self.api_host}/commerce/taxonomy/v1/get_default_category_tree_id"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        params = {"marketplace_id": str(marketplace_id or "EBAY_US").strip().upper()}
        response = requests.get(endpoint, headers=headers, params=params, timeout=30)
        self._raise_for_status_with_body(response)
        payload = response.json() if response.text else {}
        return str(payload.get("categoryTreeId") or "").strip()

    def get_category_suggestions(
        self,
        *,
        access_token: str,
        query: str,
        marketplace_id: str = "EBAY_US",
        limit: int = 20,
    ) -> list[dict]:
        keyword = str(query or "").strip()
        if not keyword:
            return []
        category_tree_id = self.get_default_category_tree_id(
            access_token=access_token,
            marketplace_id=marketplace_id,
        )
        if not category_tree_id:
            return []
        endpoint = (
            f"{self.api_host}/commerce/taxonomy/v1/category_tree/"
            f"{category_tree_id}/get_category_suggestions"
        )
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        params = {"q": keyword}
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        self._raise_for_status_with_body(response)
        payload = response.json() if response.text else {}
        rows = payload.get("categorySuggestions") or []
        suggestions: list[dict] = []
        for row in rows[: max(1, min(int(limit), 100))]:
            category = row.get("category") or {}
            ancestors = row.get("categoryTreeNodeAncestors") or []
            ancestor_names = [
                str((ancestor.get("category") or {}).get("categoryName") or "").strip()
                for ancestor in ancestors
                if isinstance(ancestor, dict)
            ]
            ancestor_names = [name for name in ancestor_names if name]
            category_id = str(category.get("categoryId") or "").strip()
            category_name = str(category.get("categoryName") or "").strip()
            if not category_id:
                continue
            path = " > ".join([*ancestor_names, category_name] if category_name else ancestor_names)
            suggestions.append(
                {
                    "category_id": category_id,
                    "category_name": category_name,
                    "path": path,
                }
            )
        return suggestions

    def get_item_aspects_for_category(
        self,
        *,
        access_token: str,
        category_id: str,
        marketplace_id: str = "EBAY_US",
    ) -> list[dict]:
        resolved_category_id = str(category_id or "").strip()
        if not resolved_category_id:
            return []
        category_tree_id = self.get_default_category_tree_id(
            access_token=access_token,
            marketplace_id=marketplace_id,
        )
        if not category_tree_id:
            return []
        endpoint = (
            f"{self.api_host}/commerce/taxonomy/v1/category_tree/"
            f"{category_tree_id}/get_item_aspects_for_category"
        )
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        params = {"category_id": resolved_category_id}
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        self._raise_for_status_with_body(response)
        payload = response.json() if response.text else {}
        aspects = payload.get("aspects") if isinstance(payload, dict) else []
        return aspects if isinstance(aspects, list) else []

    def get_item_condition_policies(
        self,
        *,
        access_token: str,
        category_id: str,
        marketplace_id: str = "EBAY_US",
    ) -> list[dict]:
        resolved_category_id = str(category_id or "").strip()
        if not resolved_category_id:
            return []
        endpoint = (
            f"{self.api_host}/sell/metadata/v1/marketplace/"
            f"{str(marketplace_id or 'EBAY_US').strip().upper()}/get_item_condition_policies"
        )
        headers = {
            "Authorization": f"Bearer {access_token.strip()}",
            "Accept-Encoding": "gzip",
        }
        params = {"filter": f"categoryIds:{{{resolved_category_id}}}"}
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        self._raise_for_status_with_body(response)
        payload = response.json() if response.text else {}
        policies = payload.get("itemConditionPolicies") if isinstance(payload, dict) else []
        return policies if isinstance(policies, list) else []

    def get_identity_user(self, access_token: str) -> dict:
        endpoint = f"{self.identity_host}/commerce/identity/v1/user/"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        response = requests.get(endpoint, headers=headers, timeout=30)
        self._raise_for_status_with_body(response)
        return response.json()

    def list_payment_policies(self, *, access_token: str, marketplace_id: str) -> list[dict]:
        endpoint = f"{self.api_host}/sell/account/v1/payment_policy"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        params = {"marketplace_id": marketplace_id}
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        self._raise_for_status_with_body(response)
        payload = response.json()
        return payload.get("paymentPolicies") or []

    def list_fulfillment_policies(self, *, access_token: str, marketplace_id: str) -> list[dict]:
        endpoint = f"{self.api_host}/sell/account/v1/fulfillment_policy"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        params = {"marketplace_id": marketplace_id}
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        self._raise_for_status_with_body(response)
        payload = response.json()
        return payload.get("fulfillmentPolicies") or []

    def list_return_policies(self, *, access_token: str, marketplace_id: str) -> list[dict]:
        endpoint = f"{self.api_host}/sell/account/v1/return_policy"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        params = {"marketplace_id": marketplace_id}
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        self._raise_for_status_with_body(response)
        payload = response.json()
        return payload.get("returnPolicies") or []

    def list_inventory_locations(self, *, access_token: str, limit: int = 200, offset: int = 0) -> list[dict]:
        endpoint = f"{self.api_host}/sell/inventory/v1/location"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        params = {"limit": max(1, min(int(limit), 100)), "offset": max(0, int(offset))}
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        if response.status_code == 400:
            # Sandbox can return input errors on paged query params for this endpoint.
            # Fallback to a plain request to still return data when possible.
            response = requests.get(endpoint, headers=headers, timeout=45)
        self._raise_for_status_with_body(response)
        payload = response.json()
        return payload.get("locations") or payload.get("inventoryLocations") or []

    def pull_recent_orders(self, access_token: str, limit: int = 25, offset: int = 0) -> dict:
        endpoint = f"{self.api_host}/sell/fulfillment/v1/order"
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"limit": max(1, min(int(limit), 200)), "offset": max(0, int(offset))}
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json()

    def get_order(self, *, access_token: str, order_id: str) -> dict:
        endpoint = f"{self.api_host}/sell/fulfillment/v1/order/{order_id}"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        response = requests.get(endpoint, headers=headers, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json()

    def list_shipping_fulfillments(self, *, access_token: str, order_id: str) -> list[dict]:
        endpoint = f"{self.api_host}/sell/fulfillment/v1/order/{order_id}/shipping_fulfillment"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        response = requests.get(endpoint, headers=headers, timeout=45)
        self._raise_for_status_with_body(response)
        payload = response.json() if response.text else {}
        if isinstance(payload, list):
            return payload
        rows = payload.get("fulfillments")
        if isinstance(rows, list):
            return rows
        rows = payload.get("shippingFulfillments")
        if isinstance(rows, list):
            return rows
        return []

    def list_finance_transactions_for_order(
        self,
        *,
        access_token: str,
        order_id: str,
        limit: int = 100,
    ) -> list[dict]:
        endpoint = f"{self.identity_host}/sell/finances/v1/transaction"
        headers = {
            "Authorization": f"Bearer {access_token.strip()}",
            "X-EBAY-C-MARKETPLACE-ID": str(settings.ebay_marketplace_id or "EBAY_US").strip().upper() or "EBAY_US",
        }
        wanted_order_id = str(order_id or "").strip()
        if not wanted_order_id:
            return []
        params = {
            "limit": max(1, min(int(limit), 200)),
            "filter": f"orderId:{{{wanted_order_id}}}",
        }
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        # Some accounts/environments reject filter syntax. Fall back to a small
        # unfiltered page and let caller-side parsing match the order id.
        if response.status_code in {400, 404}:
            response = requests.get(
                endpoint,
                headers=headers,
                params={"limit": max(1, min(int(limit), 200))},
                timeout=45,
            )
        self._raise_for_status_with_body(response)
        payload = response.json() if response.text else {}
        rows = payload.get("transactions")
        if isinstance(rows, list):
            return rows
        if isinstance(payload, list):
            return payload
        return []

    def list_finance_transactions(
        self,
        *,
        access_token: str,
        limit: int = 25,
    ) -> list[dict]:
        endpoint = f"{self.identity_host}/sell/finances/v1/transaction"
        headers = {
            "Authorization": f"Bearer {access_token.strip()}",
            "X-EBAY-C-MARKETPLACE-ID": str(settings.ebay_marketplace_id or "EBAY_US").strip().upper() or "EBAY_US",
        }
        params = {"limit": max(1, min(int(limit), 200))}
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        self._raise_for_status_with_body(response)
        payload = response.json() if response.text else {}
        rows = payload.get("transactions")
        if isinstance(rows, list):
            return rows
        if isinstance(payload, list):
            return payload
        return []

    def create_shipping_fulfillment(self, *, access_token: str, order_id: str, payload: dict) -> dict:
        endpoint = f"{self.api_host}/sell/fulfillment/v1/order/{order_id}/shipping_fulfillment"
        headers = self._rest_headers(access_token)
        response = requests.post(endpoint, headers=headers, json=payload, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json() if response.text else {}

    def create_or_replace_inventory_item(
        self,
        *,
        access_token: str,
        sku: str,
        payload: dict,
        content_language: str = "en-US",
    ) -> None:
        validate_inventory_item_condition_description(payload)
        validate_inventory_item_product_description(payload)
        endpoint = f"{self.api_host}/sell/inventory/v1/inventory_item/{sku}"
        headers = self._rest_headers(access_token, content_language=content_language)
        response = requests.put(endpoint, headers=headers, json=payload, timeout=45)
        self._raise_for_status_with_body(response)

    def get_inventory_item(self, *, access_token: str, sku: str, content_language: str = "en-US") -> dict:
        endpoint = f"{self.api_host}/sell/inventory/v1/inventory_item/{sku}"
        headers = self._rest_headers(access_token, content_language=content_language)
        response = requests.get(endpoint, headers=headers, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json() if response.text else {}

    def create_offer(self, *, access_token: str, payload: dict, content_language: str = "en-US") -> dict:
        endpoint = f"{self.api_host}/sell/inventory/v1/offer"
        headers = self._rest_headers(access_token, content_language=content_language)
        response = requests.post(endpoint, headers=headers, json=payload, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json()

    def get_offer(self, *, access_token: str, offer_id: str) -> dict:
        endpoint = f"{self.api_host}/sell/inventory/v1/offer/{offer_id}"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        response = requests.get(endpoint, headers=headers, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json()

    def get_offers(self, *, access_token: str, sku: str) -> dict:
        endpoint = f"{self.api_host}/sell/inventory/v1/offer"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        response = requests.get(endpoint, headers=headers, params={"sku": sku}, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json()

    def update_offer(
        self,
        *,
        access_token: str,
        offer_id: str,
        payload: dict,
        content_language: str = "en-US",
    ) -> dict:
        endpoint = f"{self.api_host}/sell/inventory/v1/offer/{offer_id}"
        headers = self._rest_headers(access_token, content_language=content_language)
        response = requests.put(endpoint, headers=headers, json=payload, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json() if response.text else {}

    @staticmethod
    def is_inventory_product_not_found_publish_error(exc: Exception) -> bool:
        message = str(exc or "").lower()
        return (
            "25604" in message
            or "product not found" in message
            or "seller inventory service can not publish the data" in message
        )

    def publish_offer(
        self,
        *,
        access_token: str,
        offer_id: str,
        inventory_sku: str = "",
        content_language: str = "en-US",
        retry_product_not_found_attempts: int = 3,
        retry_product_not_found_delay_seconds: float = 1.0,
    ) -> dict:
        endpoint = f"{self.api_host}/sell/inventory/v1/offer/{offer_id}/publish"
        headers = self._rest_headers(access_token)
        attempts = max(1, int(retry_product_not_found_attempts or 1))
        delay_seconds = max(0.0, float(retry_product_not_found_delay_seconds or 0.0))
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            response = requests.post(endpoint, headers=headers, timeout=45)
            try:
                self._raise_for_status_with_body(response)
                return response.json()
            except Exception as exc:
                last_exc = exc
                if attempt >= attempts or not self.is_inventory_product_not_found_publish_error(exc):
                    raise
                sku = str(inventory_sku or "").strip()
                if sku:
                    try:
                        self.get_inventory_item(
                            access_token=access_token,
                            sku=sku,
                            content_language=content_language,
                        )
                    except Exception:
                        pass
                if delay_seconds > 0:
                    time.sleep(delay_seconds)
        if last_exc is not None:
            raise last_exc
        return {}

    def withdraw_offer(self, *, access_token: str, offer_id: str) -> dict:
        endpoint = f"{self.api_host}/sell/inventory/v1/offer/{offer_id}/withdraw"
        headers = self._rest_headers(access_token)
        response = requests.post(endpoint, headers=headers, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json() if response.text else {}

    def listing_url_for_id(self, listing_id: str) -> str:
        if self.environment == "production":
            return f"https://www.ebay.com/itm/{listing_id}"
        return f"https://www.sandbox.ebay.com/itm/{listing_id}"

    def seller_hub_listings_url(self) -> str:
        if self.environment == "production":
            return "https://www.ebay.com/sh/lst/active"
        return "https://www.sandbox.ebay.com/sh/lst/active"

    def create_image_from_url(self, *, access_token: str, image_url: str) -> dict:
        endpoint = f"{self.media_host}/commerce/media/v1_beta/image/create_image_from_url"
        headers = self._rest_headers(access_token)
        response = requests.post(endpoint, headers=headers, json={"imageUrl": image_url}, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json() if response.text else {}

    def create_image_from_file(
        self,
        *,
        access_token: str,
        file_bytes: bytes,
        filename: str = "image.jpg",
        content_type: str = "image/jpeg",
    ) -> dict:
        endpoint = f"{self.media_host}/commerce/media/v1_beta/image/create_image_from_file"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        safe_name = str(filename or "image.jpg").strip() or "image.jpg"
        safe_type = str(content_type or "image/jpeg").strip() or "image/jpeg"
        # eBay expects multipart file upload for private/local image sources.
        files = {"image": (safe_name, file_bytes, safe_type)}
        response = requests.post(endpoint, headers=headers, files=files, timeout=90)
        self._raise_for_status_with_body(response)
        return response.json() if response.text else {}

    def get_inventory_location(self, *, access_token: str, merchant_location_key: str) -> dict:
        endpoint = f"{self.api_host}/sell/inventory/v1/location/{merchant_location_key.strip()}"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        response = requests.get(endpoint, headers=headers, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json() if response.text else {}

    def create_or_replace_inventory_location(
        self,
        *,
        access_token: str,
        merchant_location_key: str,
        payload: dict,
    ) -> dict:
        endpoint = f"{self.api_host}/sell/inventory/v1/location/{merchant_location_key.strip()}"
        headers = self._rest_headers(access_token)
        # eBay createInventoryLocation uses POST on /location/{merchantLocationKey}.
        response = requests.post(endpoint, headers=headers, json=payload, timeout=45)
        if response.status_code >= 400:
            # Retry with a minimal fallback payload shape that some accounts enforce.
            try:
                location_obj = dict((payload or {}).get("location") or {})
                address_obj = dict(location_obj.get("address") or {})
                fallback_payload = {
                    "location": {
                        "address": {
                            "addressLine1": str(address_obj.get("addressLine1") or "").strip(),
                            "city": str(address_obj.get("city") or "").strip(),
                            "stateOrProvince": str(address_obj.get("stateOrProvince") or "").strip(),
                            "postalCode": str(address_obj.get("postalCode") or "").strip(),
                            "country": str(address_obj.get("country") or "").strip(),
                        },
                    },
                    "name": str((payload or {}).get("name") or "").strip(),
                    "merchantLocationStatus": str(
                        (payload or {}).get("merchantLocationStatus") or "ENABLED"
                    ).strip()
                    or "ENABLED",
                    "locationTypes": list((payload or {}).get("locationTypes") or ["WAREHOUSE"]),
                }
                retry = requests.post(endpoint, headers=headers, json=fallback_payload, timeout=45)
                self._raise_for_status_with_body(retry)
                return retry.json() if retry.text else {}
            except Exception:
                self._raise_for_status_with_body(response)
        self._raise_for_status_with_body(response)
        return response.json() if response.text else {}

    def resolve_merchant_location_key(self, *, access_token: str, merchant_location_key: str) -> str:
        raw = str(merchant_location_key or "").strip()
        if not raw:
            return ""
        try:
            payload = self.get_inventory_location(access_token=access_token, merchant_location_key=raw)
            resolved = str(payload.get("merchantLocationKey") or "").strip()
            if resolved:
                return resolved
        except Exception:
            pass

        def _norm(value: str) -> str:
            return " ".join(str(value or "").strip().lower().split())

        wanted = _norm(raw)
        try:
            rows = self.list_inventory_locations(access_token=access_token, limit=200, offset=0)
        except Exception:
            return raw

        for row in rows:
            key = str((row or {}).get("merchantLocationKey") or "").strip()
            if not key:
                continue
            if _norm(key) == wanted:
                return key

            location_obj = (row or {}).get("location") or {}
            name = str(location_obj.get("name") or row.get("name") or "").strip()
            address = location_obj.get("address") or {}
            label_parts = [
                name,
                str(address.get("city") or "").strip(),
                str(address.get("stateOrProvince") or "").strip(),
                str(address.get("country") or "").strip(),
            ]
            label = ", ".join([part for part in label_parts if part])
            norm_label = _norm(label)
            if norm_label and (norm_label == wanted or wanted in norm_label or norm_label in wanted):
                return key
        return raw

    def get_payment_policy(self, *, access_token: str, payment_policy_id: str, marketplace_id: str) -> dict:
        endpoint = f"{self.api_host}/sell/account/v1/payment_policy/{payment_policy_id.strip()}"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        params = {"marketplace_id": str(marketplace_id or "EBAY_US").strip().upper()}
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json() if response.text else {}

    def get_fulfillment_policy(
        self,
        *,
        access_token: str,
        fulfillment_policy_id: str,
        marketplace_id: str,
    ) -> dict:
        endpoint = f"{self.api_host}/sell/account/v1/fulfillment_policy/{fulfillment_policy_id.strip()}"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        params = {"marketplace_id": str(marketplace_id or "EBAY_US").strip().upper()}
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json() if response.text else {}

    def get_return_policy(self, *, access_token: str, return_policy_id: str, marketplace_id: str) -> dict:
        endpoint = f"{self.api_host}/sell/account/v1/return_policy/{return_policy_id.strip()}"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        params = {"marketplace_id": str(marketplace_id or "EBAY_US").strip().upper()}
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json() if response.text else {}

    def get_category_subtree(
        self,
        *,
        access_token: str,
        marketplace_id: str,
        category_id: str,
    ) -> dict:
        category_tree_id = self.get_default_category_tree_id(
            access_token=access_token,
            marketplace_id=marketplace_id,
        )
        if not category_tree_id:
            raise RuntimeError("No default category tree id found for marketplace.")
        endpoint = (
            f"{self.api_host}/commerce/taxonomy/v1/category_tree/"
            f"{category_tree_id}/get_category_subtree"
        )
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        params = {"category_id": str(category_id or "").strip()}
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json() if response.text else {}

    def verify_publish_dependencies(
        self,
        *,
        access_token: str,
        marketplace_id: str,
        category_id: str,
        merchant_location_key: str,
        payment_policy_id: str,
        fulfillment_policy_id: str,
        return_policy_id: str,
        format_type: str | None = None,
        auction_buy_now_price: float | None = None,
        condition: str | None = None,
    ) -> dict:
        blockers: list[str] = []
        warnings: list[str] = []
        checks: list[dict] = []

        def _record(name: str, ok: bool, detail: str) -> None:
            checks.append({"check": name, "ok": bool(ok), "detail": str(detail or "")})

        def _contains_category_id(payload: object, expected: str) -> bool:
            target = str(expected or "").strip()
            if not target:
                return False
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if str(key).strip().lower() == "categoryid" and str(value).strip() == target:
                        return True
                    if _contains_category_id(value, target):
                        return True
                return False
            if isinstance(payload, list):
                return any(_contains_category_id(item, target) for item in payload)
            return False

        def _status_and_text(exc: Exception) -> tuple[int, str]:
            response = getattr(exc, "response", None)
            status_code = int(getattr(response, "status_code", 0) or 0)
            text = str(getattr(response, "text", "") or "").strip()
            return status_code, text[:400]

        def _handle_exc(name: str, exc: Exception) -> None:
            status_code, body_preview = _status_and_text(exc)
            detail = f"{type(exc).__name__}: {exc}"
            if body_preview:
                detail += f" | {body_preview}"
            _record(name, False, detail)
            # Taxonomy subtree validation can reject otherwise publishable categories
            # (for example category-tree drift or marketplace-tree mismatch during checks).
            # Do not hard-block publish/revise on this preflight-only probe.
            if name == "category_id" and (
                '"errorId":62005' in body_preview
                or "does not belong to specified category tree" in body_preview.lower()
            ):
                warnings.append(f"{name}: {detail}")
                return
            if 400 <= status_code < 500:
                blockers.append(f"{name}: {detail}")
            else:
                warnings.append(f"{name}: {detail}")

        if merchant_location_key.strip():
            try:
                resolved_location_key = self.resolve_merchant_location_key(
                    access_token=access_token,
                    merchant_location_key=merchant_location_key,
                )
                payload = self.get_inventory_location(
                    access_token=access_token,
                    merchant_location_key=resolved_location_key,
                )
                _record(
                    "merchant_location",
                    True,
                    str(payload.get("merchantLocationKey") or resolved_location_key).strip(),
                )
            except Exception as exc:
                _handle_exc("merchant_location", exc)
        else:
            blockers.append("merchant_location: missing value")
            _record("merchant_location", False, "missing value")

        if payment_policy_id.strip():
            try:
                payload = self.get_payment_policy(
                    access_token=access_token,
                    payment_policy_id=payment_policy_id,
                    marketplace_id=marketplace_id,
                )
                immediate_pay_required = self.payment_policy_requires_immediate_payment(payload)
                _record(
                    "payment_policy",
                    True,
                    (
                        f"{str(payload.get('paymentPolicyId') or payment_policy_id).strip()}; "
                        f"immediate_pay_required={str(bool(immediate_pay_required)).lower()}"
                    ),
                )
                if (
                    immediate_pay_required
                    and str(format_type or "").strip().upper() == "AUCTION"
                    and float(auction_buy_now_price or 0.0) <= 0
                ):
                    blockers.append(
                        "payment_policy: immediate payment requires an Auction Buy It Now price for live auction publish"
                    )
            except Exception as exc:
                _handle_exc("payment_policy", exc)
        else:
            blockers.append("payment_policy: missing value")
            _record("payment_policy", False, "missing value")

        if fulfillment_policy_id.strip():
            try:
                payload = self.get_fulfillment_policy(
                    access_token=access_token,
                    fulfillment_policy_id=fulfillment_policy_id,
                    marketplace_id=marketplace_id,
                )
                _record(
                    "fulfillment_policy",
                    True,
                    str(payload.get("fulfillmentPolicyId") or fulfillment_policy_id).strip(),
                )
            except Exception as exc:
                _handle_exc("fulfillment_policy", exc)
        else:
            blockers.append("fulfillment_policy: missing value")
            _record("fulfillment_policy", False, "missing value")

        if return_policy_id.strip():
            try:
                payload = self.get_return_policy(
                    access_token=access_token,
                    return_policy_id=return_policy_id,
                    marketplace_id=marketplace_id,
                )
                _record(
                    "return_policy",
                    True,
                    str(payload.get("returnPolicyId") or return_policy_id).strip(),
                )
            except Exception as exc:
                _handle_exc("return_policy", exc)
        else:
            blockers.append("return_policy: missing value")
            _record("return_policy", False, "missing value")

        if category_id.strip():
            try:
                payload = self.get_category_subtree(
                    access_token=access_token,
                    marketplace_id=marketplace_id,
                    category_id=category_id,
                )
                expected_category_id = str(category_id).strip()
                node = payload.get("categoryTreeNode") or {}
                resolved_id = str((node.get("category") or {}).get("categoryId") or "").strip()
                if (
                    (resolved_id and resolved_id == expected_category_id)
                    or _contains_category_id(payload, expected_category_id)
                ):
                    _record("category_id", True, expected_category_id)
                else:
                    warnings.append(
                        f"category_id: eBay category check returned unexpected shape for `{category_id}` "
                        "(continuing; verify category manually if publish fails)."
                    )
                    _record("category_id", True, f"unexpected shape; input accepted: {category_id}")
            except Exception as exc:
                _handle_exc("category_id", exc)
        else:
            blockers.append("category_id: missing value")
            _record("category_id", False, "missing value")

        selected_condition = str(condition or "").strip().upper()
        if category_id.strip() and selected_condition:
            try:
                payload = self.get_item_condition_policies(
                    access_token=access_token,
                    category_id=category_id,
                    marketplace_id=marketplace_id,
                )
                condition_rows = normalize_ebay_condition_policy_rows(payload, category_id=category_id)
                allowed_conditions = {
                    str((row or {}).get("condition") or "").strip().upper()
                    for row in condition_rows
                    if str((row or {}).get("condition") or "").strip()
                }
                if not allowed_conditions:
                    warnings.append(
                        f"category_condition: eBay returned no condition policy rows for category `{category_id}`."
                    )
                    _record("category_condition", True, "no condition policy rows returned")
                elif selected_condition in allowed_conditions:
                    _record(
                        "category_condition",
                        True,
                        f"{selected_condition} valid for category {str(category_id).strip()}",
                    )
                else:
                    allowed_preview = ", ".join(sorted(allowed_conditions))
                    blockers.append(
                        f"category_condition: selected condition `{selected_condition}` is not valid for "
                        f"category `{str(category_id).strip()}`. Allowed: {allowed_preview}"
                    )
                    _record(
                        "category_condition",
                        False,
                        f"{selected_condition} not in allowed conditions: {allowed_preview}",
                    )
            except Exception as exc:
                _handle_exc("category_condition", exc)
        elif category_id.strip():
            warnings.append("category_condition: no selected condition supplied for category policy validation.")
            _record("category_condition", True, "condition not supplied")

        return {
            "blockers": blockers,
            "warnings": warnings,
            "checks": checks,
        }

    def create_video(
        self,
        *,
        access_token: str,
        title: str,
        size_bytes: int,
        description: str = "",
    ) -> str:
        endpoint = f"{self.media_host}/commerce/media/v1_beta/video"
        headers = self._rest_headers(access_token)
        payload = {
            "title": title[:80] if title else "GoldenStackers Listing Video",
            "size": int(size_bytes),
            "classification": ["ITEM"],
        }
        if description.strip():
            payload["description"] = description.strip()[:500]
        response = requests.post(endpoint, headers=headers, json=payload, timeout=45)
        self._raise_for_status_with_body(response)

        location = (response.headers.get("Location") or "").strip()
        if location:
            return location.rstrip("/").split("/")[-1]
        if response.text:
            data = response.json()
            video_id = str(data.get("videoId") or "").strip()
            if video_id:
                return video_id
        raise RuntimeError("createVideo succeeded but no video ID was returned.")

    def upload_video(
        self,
        *,
        access_token: str,
        video_id: str,
        file_bytes: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        endpoint = f"{self.media_host}/commerce/media/v1_beta/video/{video_id}/upload"
        headers = {
            "Authorization": f"Bearer {access_token.strip()}",
            "Content-Type": content_type,
        }
        response = requests.post(endpoint, headers=headers, data=file_bytes, timeout=120)
        self._raise_for_status_with_body(response)

    def get_video(self, *, access_token: str, video_id: str) -> dict:
        endpoint = f"{self.media_host}/commerce/media/v1_beta/video/{video_id}"
        headers = {"Authorization": f"Bearer {access_token.strip()}"}
        response = requests.get(endpoint, headers=headers, timeout=30)
        self._raise_for_status_with_body(response)
        return response.json()

    @staticmethod
    def trading_site_id_for_marketplace(marketplace_id: str) -> str:
        marketplace = str(marketplace_id or "").strip().upper()
        return {
            "EBAY_US": "0",
            "EBAY_MOTORS_US": "100",
            "EBAY_GB": "3",
            "EBAY_UK": "3",
            "EBAY_CA": "2",
            "EBAY_AU": "15",
            "EBAY_DE": "77",
            "EBAY_FR": "71",
            "EBAY_IT": "101",
            "EBAY_ES": "186",
        }.get(marketplace, "0")

    def get_trading_item_video_ids(
        self,
        *,
        access_token: str,
        item_id: str,
        marketplace_id: str = "EBAY_US",
        compatibility_level: str = "1193",
    ) -> dict:
        resolved_item_id = str(item_id or "").strip()
        if not resolved_item_id:
            raise RuntimeError("Trading GetItem requires an item/listing ID.")
        site_id = self.trading_site_id_for_marketplace(marketplace_id)
        endpoint = f"{self.api_host}/ws/api.dll"
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "X-EBAY-API-IAF-TOKEN": str(access_token or "").strip(),
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-SITEID": site_id,
            "X-EBAY-API-COMPATIBILITY-LEVEL": str(compatibility_level or "1193").strip() or "1193",
        }
        payload = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            "<ErrorLanguage>en_US</ErrorLanguage>"
            "<WarningLevel>High</WarningLevel>"
            "<DetailLevel>ReturnAll</DetailLevel>"
            f"<ItemID>{xml_escape(resolved_item_id)}</ItemID>"
            "</GetItemRequest>"
        )
        response = requests.post(endpoint, headers=headers, data=payload.encode("utf-8"), timeout=45)
        self._raise_for_status_with_body(response)
        raw_xml = response.text or ""
        root = ET.fromstring(raw_xml.encode("utf-8"))
        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
        ack = (root.findtext("e:Ack", default="", namespaces=ns) or "").strip()
        errors = [
            {
                "code": (err.findtext("e:ErrorCode", default="", namespaces=ns) or "").strip(),
                "short": (err.findtext("e:ShortMessage", default="", namespaces=ns) or "").strip(),
                "long": (err.findtext("e:LongMessage", default="", namespaces=ns) or "").strip(),
                "severity": (err.findtext("e:SeverityCode", default="", namespaces=ns) or "").strip(),
            }
            for err in root.findall("e:Errors", ns)
        ]
        if ack.upper() == "FAILURE":
            summary = "; ".join(
                f"{err.get('code')}: {err.get('short') or err.get('long')}" for err in errors if err
            )
            raise RuntimeError(f"Trading GetItem failed for item {resolved_item_id}: {summary or raw_xml[:500]}")
        video_ids = [
            (node.text or "").strip()
            for node in root.findall(".//e:Item/e:VideoDetails/e:VideoID", ns)
            if (node.text or "").strip()
        ]
        return {
            "ack": ack,
            "item_id": resolved_item_id,
            "marketplace_id": str(marketplace_id or "").strip() or "EBAY_US",
            "site_id": site_id,
            "video_ids": video_ids,
            "errors": errors,
        }

    def get_store_categories(
        self,
        *,
        access_token: str,
        marketplace_id: str = "EBAY_US",
        level_limit: int = 3,
        compatibility_level: str = "1193",
    ) -> dict:
        site_id = self.trading_site_id_for_marketplace(marketplace_id)
        endpoint = f"{self.api_host}/ws/api.dll"
        resolved_level_limit = max(1, min(int(level_limit or 3), 3))
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "X-EBAY-API-IAF-TOKEN": str(access_token or "").strip(),
            "X-EBAY-API-CALL-NAME": "GetStore",
            "X-EBAY-API-SITEID": site_id,
            "X-EBAY-API-COMPATIBILITY-LEVEL": str(compatibility_level or "1193").strip() or "1193",
        }
        payload = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetStoreRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            "<ErrorLanguage>en_US</ErrorLanguage>"
            "<WarningLevel>High</WarningLevel>"
            "<CategoryStructureOnly>true</CategoryStructureOnly>"
            f"<LevelLimit>{resolved_level_limit}</LevelLimit>"
            "</GetStoreRequest>"
        )
        response = requests.post(endpoint, headers=headers, data=payload.encode("utf-8"), timeout=45)
        self._raise_for_status_with_body(response)
        raw_xml = response.text or ""
        root = ET.fromstring(raw_xml.encode("utf-8"))
        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
        ack = (root.findtext("e:Ack", default="", namespaces=ns) or "").strip()
        errors = [
            {
                "code": (err.findtext("e:ErrorCode", default="", namespaces=ns) or "").strip(),
                "short": (err.findtext("e:ShortMessage", default="", namespaces=ns) or "").strip(),
                "long": (err.findtext("e:LongMessage", default="", namespaces=ns) or "").strip(),
                "severity": (err.findtext("e:SeverityCode", default="", namespaces=ns) or "").strip(),
            }
            for err in root.findall("e:Errors", ns)
        ]
        if ack.upper() == "FAILURE":
            summary = "; ".join(
                f"{err.get('code')}: {err.get('short') or err.get('long')}" for err in errors if err
            )
            raise RuntimeError(f"Trading GetStore failed: {summary or raw_xml[:500]}")

        def _parse_category(node: ET.Element, parent_parts: list[str], level: int) -> list[dict]:
            name = (node.findtext("e:Name", default="", namespaces=ns) or "").strip()
            category_id = (node.findtext("e:CategoryID", default="", namespaces=ns) or "").strip()
            order_raw = (node.findtext("e:Order", default="", namespaces=ns) or "").strip()
            try:
                sort_order = int(order_raw or 0)
            except ValueError:
                sort_order = 0
            current_parts = [*parent_parts, name] if name else list(parent_parts)
            path = "/" + "/".join(current_parts) if current_parts else ""
            rows: list[dict] = []
            if path:
                rows.append(
                    {
                        "category_name": name,
                        "category_path": path,
                        "parent_path": "/" + "/".join(parent_parts) if parent_parts else "",
                        "external_category_id": category_id,
                        "sort_order": sort_order,
                        "level": level,
                    }
                )
            for child in node.findall("e:ChildCategory", ns):
                rows.extend(_parse_category(child, current_parts, level + 1))
            return rows

        category_rows: list[dict] = []
        for node in root.findall(".//e:CustomCategories/e:CustomCategory", ns):
            category_rows.extend(_parse_category(node, [], 1))
        return {
            "ack": ack,
            "marketplace_id": str(marketplace_id or "").strip() or "EBAY_US",
            "site_id": site_id,
            "categories": category_rows,
            "errors": errors,
        }

    def _finding_global_id(self) -> str:
        raw = (settings.ebay_marketplace_id or "EBAY_US").strip().upper()
        if raw == "EBAY_US":
            return "EBAY-US"
        if "_" in raw:
            return raw.replace("_", "-")
        return raw

    @classmethod
    def _finding_rate_limit_cooldown_seconds(cls) -> int:
        # Keep a local cooldown after 429/RateLimiter responses to avoid rapid repeated failures.
        return max(30, int(getattr(settings, "ebay_finding_rate_limit_cooldown_seconds", 600)))

    @classmethod
    def _finding_rate_limit_severe_cooldown_seconds(cls) -> int:
        # Use a longer cooldown when eBay explicitly reports operation quota exhaustion.
        base = cls._finding_rate_limit_cooldown_seconds()
        return max(
            base,
            int(getattr(settings, "ebay_finding_rate_limit_severe_cooldown_seconds", 3600)),
        )

    @classmethod
    def _finding_rate_limit_probe_interval_seconds(cls) -> int:
        # While local cooldown is active, permit occasional probe calls so
        # the app can recover quickly when eBay quota opens back up.
        return max(30, int(getattr(settings, "ebay_finding_rate_limit_probe_interval_seconds", 120)))

    @classmethod
    def _finding_rate_limit_until(cls) -> datetime | None:
        value = getattr(cls, "_finding_rate_limited_until", None)
        return value if isinstance(value, datetime) else None

    @classmethod
    def _set_finding_rate_limit_cooldown(cls, *, seconds: int | None = None) -> None:
        cooldown_seconds = int(seconds if seconds is not None else cls._finding_rate_limit_cooldown_seconds())
        cooldown_seconds = max(30, cooldown_seconds)
        cls._finding_rate_limited_until = datetime.now(timezone.utc) + timedelta(
            seconds=cooldown_seconds
        )

    @classmethod
    def finding_rate_limit_cooldown_remaining_seconds(cls) -> int:
        until = cls._finding_rate_limit_until()
        if until is None:
            return 0
        remaining = int((until - datetime.now(timezone.utc)).total_seconds())
        if remaining <= 0:
            cls._finding_rate_limited_until = None
            return 0
        return remaining

    @classmethod
    def clear_finding_rate_limit_cooldown(cls) -> None:
        cls._finding_rate_limited_until = None

    @classmethod
    def _set_finding_last_error(cls, payload: dict) -> None:
        cls._finding_last_error = dict(payload or {})
        cls._finding_last_error["at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    @classmethod
    def finding_last_error(cls) -> dict:
        return dict(getattr(cls, "_finding_last_error", {}) or {})

    @classmethod
    def _record_finding_call(
        cls,
        *,
        source: str,
        phase: str,
        params: dict,
        status_code: int,
        note: str = "",
    ) -> None:
        cls._finding_call_log.append(
            {
                "at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source": str(source or "unknown").strip().lower() or "unknown",
                "phase": str(phase or "").strip() or "request",
                "status_code": int(status_code or 0),
                "global_id": str(params.get("GLOBAL-ID") or "").strip(),
                "keywords": str(params.get("keywords") or "").strip()[:140],
                "sold_only": str(params.get("itemFilter(0).value") or "").strip().lower() == "true",
                "note": str(note or "").strip(),
            }
        )

    @classmethod
    def finding_call_snapshot(cls, *, window_seconds: int = 600) -> dict:
        now = datetime.now(timezone.utc)
        window = max(60, int(window_seconds))
        recent_rows: list[dict] = []
        by_source: dict[str, int] = {}
        for row in reversed(list(cls._finding_call_log)):
            ts_raw = str(row.get("at_utc") or "").strip()
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw)
            except Exception:
                continue
            age = (now - ts).total_seconds()
            if age > window:
                continue
            recent_rows.append(dict(row))
            source = str(row.get("source") or "unknown")
            by_source[source] = int(by_source.get(source, 0) + 1)
        return {
            "window_seconds": window,
            "count": len(recent_rows),
            "by_source": by_source,
            "recent": recent_rows[:50],
        }

    def find_completed_items(
        self,
        *,
        keywords: str,
        sold_only: bool = True,
        category_id: str = "",
        entries_per_page: int = 25,
        page_number: int = 1,
        source: str = "unknown",
    ) -> list[dict]:
        query = (keywords or "").strip()
        if not query:
            return []
        cooldown_remaining = self.finding_rate_limit_cooldown_remaining_seconds()
        if cooldown_remaining > 0:
            now_utc = datetime.now(timezone.utc)
            last_probe = getattr(self.__class__, "_finding_last_probe_at", None)
            probe_interval = int(self._finding_rate_limit_probe_interval_seconds())
            allow_probe = not isinstance(last_probe, datetime) or (
                now_utc - last_probe
            ).total_seconds() >= probe_interval
            if allow_probe:
                self.__class__._finding_last_probe_at = now_utc
                self._set_finding_last_error(
                    {
                        "type": "local_cooldown_probe",
                        "remaining_seconds": int(cooldown_remaining),
                        "environment": str(self.environment or ""),
                        "keywords": query[:140],
                        "probe_interval_seconds": int(probe_interval),
                    }
                )
            else:
                self._set_finding_last_error(
                    {
                        "type": "local_cooldown",
                        "remaining_seconds": int(cooldown_remaining),
                        "environment": str(self.environment or ""),
                        "keywords": query[:140],
                        "probe_interval_seconds": int(probe_interval),
                    }
                )
                raise RuntimeError(
                    f"EBAY_FINDING_RATE_LIMITED: cooldown active ({cooldown_remaining}s remaining)."
                )
        if self.environment == "production":
            endpoint = "https://svcs.ebay.com/services/search/FindingService/v1"
        else:
            endpoint = "https://svcs.sandbox.ebay.com/services/search/FindingService/v1"
        params = {
            "OPERATION-NAME": "findCompletedItems",
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": settings.ebay_client_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "true",
            "GLOBAL-ID": self._finding_global_id(),
            "keywords": query,
            "paginationInput.entriesPerPage": max(1, min(int(entries_per_page), 100)),
            "paginationInput.pageNumber": max(1, int(page_number)),
            "sortOrder": "EndTimeSoonest",
        }
        if sold_only:
            params["itemFilter(0).name"] = "SoldItemsOnly"
            params["itemFilter(0).value"] = "true"
        if (category_id or "").strip():
            params["categoryId"] = category_id.strip()

        response = requests.get(endpoint, params=params, timeout=45)
        self._record_finding_call(
            source=source,
            phase="initial",
            params=params,
            status_code=response.status_code,
        )

        def _is_global_id_error(resp: requests.Response) -> bool:
            if resp.status_code < 400:
                return False
            body = (getattr(resp, "text", "") or "").lower()
            if "global-id" in body and ("valid" in body or "input error" in body):
                return True
            return False

        if _is_global_id_error(response):
            original_gid = str(params.get("GLOBAL-ID") or "").strip()
            alt_gid = ""
            if "_" in original_gid:
                alt_gid = original_gid.replace("_", "-")
            elif "-" in original_gid:
                alt_gid = original_gid.replace("-", "_")
            if alt_gid and alt_gid != original_gid:
                retry_params = dict(params)
                retry_params["GLOBAL-ID"] = alt_gid
                response = requests.get(endpoint, params=retry_params, timeout=45)
                self._record_finding_call(
                    source=source,
                    phase="retry_global_id_alt",
                    params=retry_params,
                    status_code=response.status_code,
                )
            if _is_global_id_error(response):
                retry_params = dict(params)
                retry_params.pop("GLOBAL-ID", None)
                response = requests.get(endpoint, params=retry_params, timeout=45)
                self._record_finding_call(
                    source=source,
                    phase="retry_no_global_id",
                    params=retry_params,
                    status_code=response.status_code,
                )

        def _is_rate_limited(resp: requests.Response) -> bool:
            if resp.status_code < 400:
                return False
            raw = (getattr(resp, "text", "") or "").lower()
            if "ratelimiter" in raw or "exceeded the number of times" in raw:
                return True
            if '"errorid":["10001"]' in raw:
                return True
            return False

        if _is_rate_limited(response):
            raw_text = str(getattr(response, "text", "") or "")
            raw_lower = raw_text.lower()
            severe_quota_exhausted = "exceeded the number of times" in raw_lower
            cooldown_seconds = (
                int(self._finding_rate_limit_severe_cooldown_seconds())
                if severe_quota_exhausted
                else int(self._finding_rate_limit_cooldown_seconds())
            )
            self._set_finding_rate_limit_cooldown(seconds=cooldown_seconds)
            self.__class__._finding_last_probe_at = datetime.now(timezone.utc)
            self._set_finding_last_error(
                {
                    "type": "remote_rate_limited",
                    "status_code": int(response.status_code or 0),
                    "environment": str(self.environment or ""),
                    "keywords": query[:140],
                    "global_id": str(params.get("GLOBAL-ID") or ""),
                    "response_excerpt": raw_text[:600],
                    "cooldown_seconds": int(cooldown_seconds),
                    "rate_limit_scope": "severe_quota_exhausted" if severe_quota_exhausted else "standard",
                }
            )
            self._record_finding_call(
                source=source,
                phase="rate_limited",
                params=params,
                status_code=response.status_code,
                note=(
                    "rate_limited_severe_quota_exhausted"
                    if severe_quota_exhausted
                    else "rate_limited"
                ),
            )
            raise RuntimeError(
                "EBAY_FINDING_RATE_LIMITED: eBay Finding API rate limit exceeded for findCompletedItems."
            )
        self._raise_for_status_with_body(response)
        # Successful request means cooldown can be cleared immediately.
        if self.finding_rate_limit_cooldown_remaining_seconds() > 0:
            self.clear_finding_rate_limit_cooldown()
        payload = response.json()
        root = (payload.get("findCompletedItemsResponse") or [{}])[0]
        ack = str((root.get("ack") or [""])[0]).strip().lower()
        if ack not in {"success", "warning"}:
            raise RuntimeError(f"eBay Finding API returned ack={ack or 'unknown'}.")
        items = ((root.get("searchResult") or [{}])[0].get("item") or [])
        rows: list[dict] = []
        for item in items:
            title = str((item.get("title") or [""])[0]).strip()
            item_id = str((item.get("itemId") or [""])[0]).strip()
            view_url = str((item.get("viewItemURL") or [""])[0]).strip()
            gallery_url = str((item.get("galleryURL") or [""])[0]).strip()
            condition = str(((item.get("condition") or [{}])[0].get("conditionDisplayName") or [""])[0]).strip()
            listing_info = (item.get("listingInfo") or [{}])[0]
            end_time = str((listing_info.get("endTime") or [""])[0]).strip()
            selling_status = (item.get("sellingStatus") or [{}])[0]
            price_obj = (selling_status.get("currentPrice") or [{}])[0]
            try:
                sold_price = float(price_obj.get("__value__") or 0.0)
            except Exception:
                sold_price = 0.0
            currency = str(price_obj.get("@currencyId") or "USD").strip()
            shipping_info = (item.get("shippingInfo") or [{}])[0]
            ship_obj = (shipping_info.get("shippingServiceCost") or [{}])[0]
            try:
                shipping_cost = float(ship_obj.get("__value__") or 0.0)
            except Exception:
                shipping_cost = 0.0
            rows.append(
                {
                    "item_id": item_id,
                    "title": title,
                    "sold_price": sold_price,
                    "shipping_cost": shipping_cost,
                    "total_price": sold_price + shipping_cost,
                    "currency": currency,
                    "condition": condition,
                    "end_time": end_time,
                    "view_url": view_url,
                    "gallery_url": gallery_url,
                }
            )
        return rows

    @staticmethod
    def _marketplace_insights_money_value(value: object) -> float:
        if isinstance(value, dict):
            raw = value.get("value")
        else:
            raw = value
        try:
            return float(str(raw or "0").replace(",", "").replace("$", "").strip() or 0.0)
        except Exception:
            return 0.0

    @staticmethod
    def _marketplace_insights_money_currency(value: object, default: str = "USD") -> str:
        if isinstance(value, dict):
            currency = str(value.get("currency") or value.get("currencyId") or "").strip()
            if currency:
                return currency
        return default

    def search_marketplace_insights_item_sales(
        self,
        *,
        access_token: str,
        query: str,
        marketplace_id: str = "EBAY_US",
        category_id: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        normalized_query = " ".join(str(query or "").split()).strip()
        if not normalized_query:
            return {}
        endpoint = f"{self.api_host}/buy/marketplace_insights/v1_beta/item_sales/search"
        params: dict[str, object] = {
            "q": normalized_query,
            "limit": max(1, min(int(limit or 25), 200)),
            "offset": max(0, int(offset or 0)),
        }
        if str(category_id or "").strip():
            params["category_ids"] = str(category_id or "").strip()
        headers = self._rest_headers(access_token)
        headers["X-EBAY-C-MARKETPLACE-ID"] = str(marketplace_id or "EBAY_US").strip() or "EBAY_US"
        response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        self._raise_for_status_with_body(response)
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def search_marketplace_insights_sold_comps(
        self,
        *,
        access_token: str,
        query: str,
        marketplace_id: str = "EBAY_US",
        category_id: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> list[dict]:
        payload = self.search_marketplace_insights_item_sales(
            access_token=access_token,
            query=query,
            marketplace_id=marketplace_id,
            category_id=category_id,
            limit=limit,
            offset=offset,
        )
        items = payload.get("itemSales") or payload.get("itemSummaries") or payload.get("items") or []
        if not isinstance(items, list):
            return []
        rows: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            price_obj = item.get("price") or item.get("soldPrice") or item.get("currentBidPrice") or {}
            sold_price = self._marketplace_insights_money_value(price_obj)
            shipping_cost = 0.0
            shipping_options = item.get("shippingOptions") or []
            if isinstance(shipping_options, list) and shipping_options:
                first_shipping = shipping_options[0] if isinstance(shipping_options[0], dict) else {}
                shipping_cost = self._marketplace_insights_money_value(first_shipping.get("shippingCost"))
            if shipping_cost <= 0:
                shipping_cost = self._marketplace_insights_money_value(
                    item.get("shippingCost") or item.get("shippingPrice")
                )
            total_price = sold_price + shipping_cost
            if total_price <= 0:
                continue
            condition = ""
            condition_obj = item.get("condition") or item.get("conditionId") or ""
            if isinstance(condition_obj, dict):
                condition = str(
                    condition_obj.get("conditionDisplayName")
                    or condition_obj.get("localizedAspectValue")
                    or condition_obj.get("name")
                    or ""
                ).strip()
            else:
                condition = str(condition_obj or "").strip()
            rows.append(
                {
                    "item_id": str(item.get("itemId") or item.get("legacyItemId") or "").strip(),
                    "title": str(item.get("title") or "").strip(),
                    "sold_price": sold_price,
                    "shipping_cost": shipping_cost,
                    "total_price": total_price,
                    "currency": self._marketplace_insights_money_currency(price_obj),
                    "condition": condition,
                    "end_time": str(
                        item.get("itemEndDate")
                        or item.get("lastSoldDate")
                        or item.get("itemCreationDate")
                        or item.get("dateSold")
                        or ""
                    ).strip(),
                    "view_url": str(item.get("itemWebUrl") or item.get("itemHref") or "").strip(),
                    "gallery_url": str(item.get("image", {}).get("imageUrl") if isinstance(item.get("image"), dict) else "").strip(),
                    "source": "ebay_marketplace_insights",
                    "evidence": "sold_market",
                    "marketplace_id": str(marketplace_id or "EBAY_US").strip() or "EBAY_US",
                }
            )
        return rows

    @staticmethod
    def _extract_price_hints_simple(raw_text: str) -> list[float]:
        text = unescape(str(raw_text or "")).replace("\xa0", " ").strip()
        if not text:
            return []
        matches = re.findall(
            r"(?i)(?:US\$|USD|\$)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
            text,
        )
        prices: list[float] = []
        for raw in matches:
            try:
                prices.append(float(str(raw).replace(",", "").strip()))
            except Exception:
                continue
        deduped: list[float] = []
        seen: set[float] = set()
        for value in prices:
            if value <= 0 or value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        return deduped

    def search_sold_items_html(self, *, keywords: str, limit: int = 25) -> list[dict]:
        query = " ".join(str(keywords or "").split()).strip()
        if not query:
            return []
        self.__class__._sold_html_last_error = {}
        endpoint = (
            "https://www.ebay.com/sch/i.html"
            f"?_nkw={quote_plus(query)}&LH_Sold=1&LH_Complete=1&rt=nc"
        )
        try:
            response = requests.get(
                endpoint,
                headers={"User-Agent": "Mozilla/5.0 (compatible; GoldenStackersComp/1.0)"},
                timeout=30,
            )
            response.raise_for_status()
            html_text = str(response.text or "")
        except Exception as exc:
            response_obj = getattr(exc, "response", None)
            self.__class__._sold_html_last_error = {
                "type": type(exc).__name__,
                "status_code": int(getattr(response_obj, "status_code", 0) or 0),
                "keywords": query[:140],
                "response_excerpt": str(getattr(response_obj, "text", "") or "")[:300],
            }
            return []
        row_limit = max(1, min(int(limit), 100))
        item_blocks = re.findall(
            r'<li[^>]*class=["\'][^"\']*\bs-item\b[^"\']*["\'][^>]*>(.*?)</li>',
            html_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        rows: list[dict] = []
        for block in item_blocks:
            if len(rows) >= row_limit:
                break
            title_match = re.search(
                r'<[^>]*class=["\'][^"\']*\bs-item__title\b[^"\']*["\'][^>]*>(.*?)</[^>]+>',
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            title = unescape(re.sub(r"<[^>]+>", " ", str(title_match.group(1) if title_match else ""))).strip()
            if not title or title.lower().startswith("shop on ebay"):
                continue
            href_match = re.search(
                r'<a[^>]*class=["\'][^"\']*\bs-item__link\b[^"\']*["\'][^>]*href=["\']([^"\']+)["\']',
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            view_url = unescape(str(href_match.group(1) if href_match else "")).strip()
            price_match = re.search(
                r'<[^>]*class=["\'][^"\']*\bs-item__price\b[^"\']*["\'][^>]*>(.*?)</[^>]+>',
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            shipping_match = re.search(
                r'<[^>]*class=["\'][^"\']*\bs-item__shipping\b[^"\']*["\'][^>]*>(.*?)</[^>]+>',
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            price_text = unescape(re.sub(r"<[^>]+>", " ", str(price_match.group(1) if price_match else ""))).strip()
            shipping_text = unescape(
                re.sub(r"<[^>]+>", " ", str(shipping_match.group(1) if shipping_match else ""))
            ).strip()
            sold_hints = self._extract_price_hints_simple(price_text)
            shipping_hints = self._extract_price_hints_simple(shipping_text)
            sold_price = float(sold_hints[0]) if sold_hints else 0.0
            shipping_cost = float(shipping_hints[0]) if shipping_hints else 0.0
            if sold_price <= 0:
                continue
            item_id = ""
            item_id_match = re.search(r"/itm/([0-9]{9,20})", view_url)
            if item_id_match:
                item_id = str(item_id_match.group(1) or "").strip()
            rows.append(
                {
                    "item_id": item_id,
                    "title": title,
                    "sold_price": sold_price,
                    "shipping_cost": shipping_cost,
                    "total_price": float(sold_price + shipping_cost),
                    "currency": "USD",
                    "condition": "",
                    "end_time": "",
                    "view_url": view_url,
                    "gallery_url": "",
                    "source": "ebay_sold_html",
                }
            )
        if not rows:
            self.__class__._sold_html_last_error = {
                "type": "no_parsed_rows",
                "status_code": int(getattr(response, "status_code", 0) or 0),
                "keywords": query[:140],
                "html_bytes": len(html_text),
                "item_blocks": len(item_blocks),
            }
        return rows

    @classmethod
    def sold_html_last_error(cls) -> dict:
        return dict(getattr(cls, "_sold_html_last_error", {}) or {})

    def find_completed_items_with_fallback(
        self,
        *,
        keywords: str,
        sold_only: bool = True,
        category_id: str = "",
        entries_per_page: int = 25,
        page_number: int = 1,
        source: str = "unknown",
        auto_broaden: bool = True,
        allow_html_fallback: bool = True,
    ) -> dict:
        outcome = {"rows": [], "mode": "none", "rate_limited_note": ""}
        html_rows = self.search_sold_items_html(
            keywords=keywords,
            limit=max(1, min(int(entries_per_page), 100)),
        )
        if html_rows:
            outcome["rows"] = list(html_rows)
            outcome["mode"] = "ebay_sold_html_primary"
            return outcome
        # Finding API fallback is intentionally disabled by default because sold
        # comps are now sourced primarily from eBay sold-result HTML paths.
        if (not allow_html_fallback) and auto_broaden:
            outcome["mode"] = "html_disabled"
        return outcome
    _finding_call_log: deque[dict] = deque(maxlen=500)
    _finding_rate_limited_until: datetime | None = None
    _finding_last_probe_at: datetime | None = None
    _finding_last_error: dict = {}
    _sold_html_last_error: dict = {}
    _CONTENT_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2}(?:-[A-Za-z]{2})?$")
