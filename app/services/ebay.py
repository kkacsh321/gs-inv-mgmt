import base64
import json
from urllib.parse import urlencode

import requests

from app.config import settings


class EbayClient:
    """
    Lightweight eBay API client for OAuth bootstrapping and initial data pulls.

    This intentionally starts small: auth URL generation, token exchange, and
    a sample sell-account call. As requirements stabilize, extend into inventory,
    order ingestion, listing updates, and webhook handling.
    """

    SCOPES = [
        "https://api.ebay.com/oauth/api_scope",
        "https://api.ebay.com/oauth/api_scope/sell.inventory",
        "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
        "https://api.ebay.com/oauth/api_scope/sell.account",
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
            headers["Content-Language"] = content_language
        return headers

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
        response.raise_for_status()
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
        response.raise_for_status()
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
        endpoint = f"{self.api_host}/sell/inventory/v1/inventory_item/{sku}"
        headers = self._rest_headers(access_token, content_language=content_language)
        response = requests.put(endpoint, headers=headers, json=payload, timeout=45)
        self._raise_for_status_with_body(response)

    def create_offer(self, *, access_token: str, payload: dict) -> dict:
        endpoint = f"{self.api_host}/sell/inventory/v1/offer"
        headers = self._rest_headers(access_token)
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

    def publish_offer(self, *, access_token: str, offer_id: str) -> dict:
        endpoint = f"{self.api_host}/sell/inventory/v1/offer/{offer_id}/publish"
        headers = self._rest_headers(access_token)
        response = requests.post(endpoint, headers=headers, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json()

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

    def create_image_from_url(self, *, access_token: str, image_url: str) -> dict:
        endpoint = f"{self.media_host}/commerce/media/v1_beta/image/create_image_from_url"
        headers = self._rest_headers(access_token)
        response = requests.post(endpoint, headers=headers, json={"imageUrl": image_url}, timeout=45)
        self._raise_for_status_with_body(response)
        return response.json() if response.text else {}

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
        content_type: str = "video/mp4",
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

    def _finding_global_id(self) -> str:
        raw = (settings.ebay_marketplace_id or "EBAY_US").strip().upper()
        if raw == "EBAY_US":
            return "EBAY-US"
        if "_" in raw:
            return raw.replace("_", "-")
        return raw

    def find_completed_items(
        self,
        *,
        keywords: str,
        sold_only: bool = True,
        category_id: str = "",
        entries_per_page: int = 25,
        page_number: int = 1,
    ) -> list[dict]:
        query = (keywords or "").strip()
        if not query:
            return []
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
        if response.status_code >= 400 and params.get("GLOBAL-ID") == "EBAY-US":
            # Last-resort compatibility retry if gateway expects underscore style.
            retry_params = dict(params)
            retry_params["GLOBAL-ID"] = "EBAY_US"
            response = requests.get(endpoint, params=retry_params, timeout=45)
        self._raise_for_status_with_body(response)
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
