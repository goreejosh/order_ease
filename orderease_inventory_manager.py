#!/usr/bin/env python3
"""
OrderEase Inventory Manager - v2 API
This script helps you manage your inventory in OrderEase using their v2 API.
"""

import os
import sys
import json
import csv
import time
import base64
import difflib
import requests
from typing import Dict, List, Optional, Any, Iterable, Tuple
from datetime import datetime
from dotenv import load_dotenv

# Image bucket URL for Michaels Wholesale Nursery plant images
PLANT_IMAGE_BUCKET_URL = "https://manaplant-images.s3.us-east-1.amazonaws.com"
PLANT_IMAGE_TYPES = ["Single", "Top", "Foilage", "Collection"]


class OrderEaseAPIError(Exception):
    """Base exception for OrderEase API errors."""


class OrderEaseAuthError(OrderEaseAPIError):
    """Authentication / authorization error."""


class OrderEaseOperationError(OrderEaseAPIError):
    """API returned success=false or an operational error."""


class OrderEaseHTTPError(OrderEaseAPIError):
    """Non-success HTTP response."""


class OrderEaseAPI:
    """OrderEase API v2 Client"""
    
    def __init__(
        self,
        base_url: str,
        api_version: str = "2.0",
        *,
        # Backward-compat / explicit values
        integration_key: Optional[str] = None,
        bearer_token: Optional[str] = None,
        # v2 credentials provided by OrderEase (names as given)
        v2_api_key: Optional[str] = None,
        client_key: Optional[str] = None,
        client_secret: Optional[str] = None,
        timeout_seconds: int = 30,
    ):
        """
        Initialize the OrderEase API client
        
        Args:
            base_url: Base URL for OrderEase API (e.g., https://api.orderease.com)
            api_version: API version to use (default: 2.0)
            integration_key: Value for X-Integration-Key header (if applicable)
            bearer_token: Value for Authorization: Bearer <token> header (if applicable)
            v2_api_key: OrderEase-provided V2_API_Key (used as an auth fallback)
            client_key: OrderEase-provided ClientKey (currently unused – no token endpoint in spec)
            client_secret: OrderEase-provided ClientSecret (currently unused – no token endpoint in spec)
            timeout_seconds: Request timeout
        """
        self.base_url = base_url.rstrip('/')
        self.api_version = api_version
        self.integration_key = integration_key
        self.bearer_token = bearer_token
        self.v2_api_key = v2_api_key
        self.client_key = client_key
        self.client_secret = client_secret
        self.timeout_seconds = timeout_seconds
        self._cached_auth_headers: Optional[Dict[str, str]] = None
        self._base_headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
    
    def _auth_header_variants(self) -> List[Dict[str, str]]:
        """
        Build a list of plausible auth header combinations.

        The OpenAPI spec declares a global Bearer auth scheme and most endpoints also
        explicitly include X-Integration-Key. Since OrderEase supplied V2_API_Key,
        ClientKey, ClientSecret (and the token-issuing endpoint is not in the spec),
        we try a small set of safe combinations and cache whichever works.
        """
        integration_candidates: List[str] = []
        for k in [self.integration_key, self.v2_api_key]:
            if k and k not in integration_candidates:
                integration_candidates.append(k)

        bearer_candidates: List[str] = []
        for t in [self.bearer_token, self.v2_api_key]:
            if t and t not in bearer_candidates:
                bearer_candidates.append(t)

        variants: List[Dict[str, str]] = []

        # Prefer explicit pair if both are provided.
        if self.integration_key and self.bearer_token:
            variants.append({
                "X-Integration-Key": self.integration_key,
                "Authorization": f"Bearer {self.bearer_token}",
            })

        # Historically, docs show X-Integration-Key as required, so try that early.
        for k in integration_candidates:
            variants.append({"X-Integration-Key": k})

        # Try Bearer-only if needed.
        for t in bearer_candidates:
            variants.append({"Authorization": f"Bearer {t}"})

        # Try both combinations last (covers cases where both are required).
        for k in integration_candidates:
            for t in bearer_candidates:
                variants.append({
                    "X-Integration-Key": k,
                    "Authorization": f"Bearer {t}",
                })

        # De-duplicate
        deduped: List[Dict[str, str]] = []
        seen: set = set()
        for h in variants:
            key = tuple(sorted(h.items()))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(h)

        return deduped

    def _parse_response_body(self, response: requests.Response) -> Any:
        if not response.content:
            return None
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type.lower():
            return response.json()
        # Try JSON anyway (some servers mislabel content-type)
        try:
            return response.json()
        except Exception:
            return response.text

    def _unwrap_operation_result(self, payload: Any) -> Any:
        """
        Many endpoints return an OperationResult-like payload:
          { success: bool, error?: str, errors?: string[], result?: any }
        """
        if not isinstance(payload, dict):
            return payload

        success_key = None
        if "success" in payload:
            success_key = "success"
        elif "Success" in payload:
            success_key = "Success"

        if not success_key:
            return payload

        success = payload.get(success_key)
        error = payload.get("error") or payload.get("Error")
        errors = payload.get("errors") or payload.get("Errors") or []

        if success is False:
            msg_parts: List[str] = []
            if error:
                msg_parts.append(str(error))
            if errors:
                msg_parts.extend([str(e) for e in errors])
            raise OrderEaseOperationError("; ".join(msg_parts) or "Operation failed")

        # success True
        if "result" in payload:
            return payload.get("result")
        if "Result" in payload:
            return payload.get("Result")
        return payload

    def _make_request(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: Any = None,
        params: Optional[Dict[str, Any]] = None,
        unwrap_operation_result: bool = True,
    ) -> Any:
        """Make an API request and optionally unwrap OperationResult payloads."""
        url = f"{self.base_url}{endpoint}"

        # Add api-version to params
        if params is None:
            params = {}
        params["api-version"] = self.api_version

        # If we already found a working auth header combo, use it.
        auth_variants = [self._cached_auth_headers] if self._cached_auth_headers else self._auth_header_variants()
        if not auth_variants:
            auth_variants = [{}]

        last_response: Optional[requests.Response] = None
        last_payload: Any = None

        for idx, auth_headers in enumerate(auth_variants):
            headers = dict(self._base_headers)
            headers.update(auth_headers or {})

            try:
                response = requests.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=self.timeout_seconds,
                )
            except requests.exceptions.RequestException as e:
                raise OrderEaseHTTPError(f"Request failed: {e}") from e

            last_response = response
            last_payload = self._parse_response_body(response)

            # Auth retry only for auth-related status codes, and only if there are more variants.
            if response.status_code in (401, 403) and idx < (len(auth_variants) - 1):
                continue

            if not response.ok:
                if response.status_code in (401, 403):
                    raise OrderEaseAuthError(
                        f"Auth failed ({response.status_code}). Response: {last_payload}"
                    )
                raise OrderEaseHTTPError(
                    f"HTTP {response.status_code} calling {endpoint}. Response: {last_payload}"
                )

            # Success – cache the auth headers if we were cycling.
            if not self._cached_auth_headers and auth_headers:
                self._cached_auth_headers = auth_headers

            payload = last_payload
            if unwrap_operation_result:
                payload = self._unwrap_operation_result(payload)
            return payload

        # Should not reach here
        raise OrderEaseHTTPError(f"Request failed. Last response: {last_payload}")
    
    # Catalog Management
    def get_all_catalogs(self, supplier_id: int, get_inactive: bool = False) -> List[Dict]:
        """Get all catalogs for a supplier"""
        result = self._make_request(
            'GET',
            f'/api/Catalog/GetAll/{supplier_id}',
            params={'getInactive': get_inactive, 'simple': False},
            unwrap_operation_result=True,
        )
        return result or []

    def get_catalog_by_id(self, catalog_id: int) -> Dict[str, Any]:
        """Fetch catalog details by ID (useful to derive catalogRef/integrationReference)."""
        return self._make_request(
            "GET",
            f"/api/Catalog/ById/{catalog_id}",
            unwrap_operation_result=True,
        ) or {}
    
    def get_catalog_items(self, skus: List[str], company_id: int, 
                         catalog_id: Optional[int] = None) -> List[Dict]:
        """Get catalog items by SKUs"""
        params = {
            'skus': skus,
            'companyId': company_id
        }
        if catalog_id:
            params['catalogId'] = catalog_id
            
        return self._make_request('GET', '/api/Catalog/GetItem', params=params, unwrap_operation_result=False)
    
    # Inventory Management
    def get_all_inventories(self, company_id: Optional[int] = None) -> List[Dict]:
        """Get all inventories"""
        params: Dict[str, Any] = {}
        if company_id is not None:
            params["companyId"] = company_id
        result = self._make_request('GET', '/api/Inventory/GetAllInventories', params=params, unwrap_operation_result=True)
        return result or []
    
    def get_inventory(self, inventory_id: int) -> Dict:
        """Get a specific inventory by ID"""
        return self._make_request('GET', f'/api/Inventory/GetInventory/{inventory_id}')
    
    def create_inventory(self, name: str, description: str = "", 
                        start_date: Optional[str] = None,
                        end_date: Optional[str] = None,
                        company_id: Optional[int] = None,
                        supplier_warehouse_id: Optional[int] = None) -> Dict:
        """
        Create or update an inventory
        
        Args:
            name: Inventory name
            description: Inventory description
            start_date: Start date (ISO format)
            end_date: End date (ISO format)
            company_id: Company ID
            supplier_warehouse_id: Supplier warehouse ID
        """
        data = {
            'name': name,
            'description': description
        }
        
        if start_date:
            data['startDate'] = start_date
        if end_date:
            data['endDate'] = end_date
        if company_id is not None:
            data['companyId'] = company_id
        if supplier_warehouse_id is not None:
            data['supplierWarehouseId'] = supplier_warehouse_id
            
        return self._make_request('POST', '/api/Inventory/AddOrUpdateInventory', json_body=data, unwrap_operation_result=False)
    
    def assign_products_to_inventory(self, inventory_id: int, item_ids: List[int]) -> Dict:
        """Assign products to an inventory"""
        return self._make_request(
            'POST',
            f'/api/Inventory/AssignProductsToInventory/{inventory_id}',
            json_body=item_ids,
            unwrap_operation_result=False,
        )
    
    def get_catalog_inventories(self, catalog_id: int) -> List[Dict]:
        """Get all inventories associated with a catalog"""
        return self._make_request('GET', f'/api/Inventory/GetCatalogInventories/{catalog_id}')
    
    def assign_catalog_to_inventories(self, catalog_id: int, inventory_ids: List[int]) -> Dict:
        """Assign a catalog to multiple inventories"""
        return self._make_request(
            'POST',
            f'/api/Inventory/AssignCatalogToInventories/{catalog_id}',
            json_body=inventory_ids,
            unwrap_operation_result=False,
        )
    
    def get_supplier_inventory(self, supplier_id: int) -> List[Dict]:
        """
        Legacy helper (kept for backward-compat). The /api/Inventory/GetSupplierInventory endpoint
        returns a nested structure; for a flat product list prefer export_supplier_products().
        """
        return self._make_request(
            'GET',
            f'/api/Inventory/GetSupplierInventory/{supplier_id}',
            unwrap_operation_result=False,
        )

    # Supplier Inventory (Products)
    def export_supplier_products(self, supplier_id: int) -> List[Dict[str, Any]]:
        """Export supplier products with price/quantity fields."""
        result = self._make_request(
            "GET",
            f"/api/SupplierInventory/ExportProducts/{supplier_id}",
            unwrap_operation_result=True,
        )
        return result or []

    def lookup_or_create_item(
        self,
        *,
        private_sku: str,
        description: str,
        unit_price: Optional[float] = None,
        pack_quantity: Optional[float] = None,
        pack_description: Optional[str] = None,
        upc: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create (or lookup) an item by SKU.
        Endpoint: POST /api/SupplierOrder/LookupOrCreateItem
        """
        body: Dict[str, Any] = {
            "privateSKU": private_sku,
            "description": description,
        }
        if upc is not None:
            body["upc"] = upc
        if pack_quantity is not None:
            body["packQuantity"] = pack_quantity
        if pack_description is not None:
            body["packDescription"] = pack_description
        if unit_price is not None:
            body["unitPrice"] = unit_price

        # This endpoint does NOT return an OperationResult wrapper
        result = self._make_request(
            "POST",
            "/api/SupplierOrder/LookupOrCreateItem",
            json_body=body,
            unwrap_operation_result=False,
        )
        return result or {}

    def add_skus_to_catalog_by_ref(self, catalog_ref: str, skus: List[str]) -> Any:
        """Bulk-add a list of SKUs to a catalog via catalogRef."""
        return self._make_request(
            "POST",
            f"/api/SupplierInventory/AddToCatalog/byRef/{catalog_ref}/sku",
            json_body=skus,
            unwrap_operation_result=True,
        )

    def upsert_supplier_inventory_item(
        self,
        *,
        private_sku: str,
        description: str,
        category_id: int,
        net_price: float,
        open_size_description: Optional[str] = None,
        comments: Optional[str] = None,
        quantity_available: Optional[float] = None,
        external_source: Optional[str] = None,
        external_reference: Optional[str] = None,
        inactive: Optional[bool] = None,
    ) -> Any:
        """
        Create or update a supplier inventory product.
        Endpoint: POST /api/SupplierInventory/AddOrUpdate
        NOTE: Spec defines a dynamic object payload (JToken map).
        """
        payload: Dict[str, Any] = {
            "privateSKU": private_sku,
            "description": description,
            "categoryId": int(category_id),
            "netPrice": float(net_price),
        }
        if open_size_description is not None:
            payload["openSizeDescription"] = open_size_description
        if comments is not None:
            payload["comments"] = comments
        if quantity_available is not None:
            payload["quantityAvailable"] = float(quantity_available)
        if external_source is not None:
            payload["externalSource"] = external_source
        if external_reference is not None:
            payload["externalReference"] = external_reference
        if inactive is not None:
            payload["inactive"] = bool(inactive)

        return self._make_request(
            "POST",
            "/api/SupplierInventory/AddOrUpdate",
            json_body=payload,
            unwrap_operation_result=True,
        )

    def lookup_ids_by_sku(self, skus: List[str]) -> List[Dict[str, Any]]:
        """Lookup product IDs by SKU."""
        result = self._make_request(
            "GET",
            "/api/SupplierInventory/LookupIdBySku",
            params={"skus": skus},
            unwrap_operation_result=True,
        )
        return result or []

    # Pricing
    def update_pricing(self, updates: List[Dict[str, Any]]) -> Any:
        """
        Bulk update pricing for items by itemId.
        Endpoint: POST /api/SupplierInventory/UpdatePricing

        Body: [{ itemId: int, netPrice: double?, suggestedRetailPrice: double? }, ...]
        """
        return self._make_request(
            "POST",
            "/api/SupplierInventory/UpdatePricing",
            json_body=updates,
            unwrap_operation_result=True,
        )

    # Media / Images
    def _candidate_plant_image_urls(self, sku: str) -> List[str]:
        """
        Build candidate image URLs for a given SKU using the same conventions as the site JS.
        Prefer large images, fall back to small.
        """
        sku = _clean_str(sku)
        urls: List[str] = []
        for t in PLANT_IMAGE_TYPES:
            urls.append(f"{PLANT_IMAGE_BUCKET_URL}/{sku}-{t}-large.jpg")
            urls.append(f"{PLANT_IMAGE_BUCKET_URL}/{sku}-{t}-small.jpg")
        return urls

    def _fetch_first_available_image(
        self,
        source_skus: List[str],
        *,
        timeout_seconds: int = 20,
    ) -> Tuple[Optional[bytes], Optional[str]]:
        """
        Try to fetch the first available plant image for any of the provided source SKUs.

        Returns:
            (image_bytes, chosen_url) or (None, None)
        """
        headers = {"User-Agent": "OrderEaseInventoryManager/1.0"}
        for s in [(_clean_str(x) or "") for x in source_skus]:
            if not s:
                continue
            for url in self._candidate_plant_image_urls(s):
                try:
                    resp = requests.get(url, timeout=timeout_seconds, headers=headers)
                    if resp.status_code in (403, 404):
                        continue
                    resp.raise_for_status()
                    return (resp.content, url)
                except requests.RequestException:
                    continue
        return (None, None)

    def upload_product_images_base64(
        self,
        product_images: List[Dict[str, str]],
        *,
        set_as_primary: bool = True,
        skip_existing: bool = True,
        clear_existing: bool = False,
        is_fuzzy_match_mode: bool = True,
    ) -> None:
        """
        Upload base64-encoded images to products via AddBulkProductMediaAsync.

        product_images format:
          [{ "productSKU": "...", "imageData": "base64..." }, ...]
        """
        payload = {"productImages": product_images}
        self._make_request(
            "POST",
            "/api/Media/AddBulkProductMediaAsync",
            json_body=payload,
            params={
                "setImageAsPrimary": str(set_as_primary).lower(),
                "clearExisting": str(clear_existing).lower(),
                "isFuzzyMatchMode": str(is_fuzzy_match_mode).lower(),
                "skipExisting": str(skip_existing).lower(),
            },
            unwrap_operation_result=True,
        )

    def add_product_image_from_url(
        self,
        product_sku: str,
        image_url: str,
        *,
        set_as_primary: bool = True,
        skip_existing: bool = True,
        clear_existing: bool = False,
        is_fuzzy_match_mode: bool = True,
        timeout_seconds: int = 30,
    ) -> bool:
        """
        Fetch an image from a URL and upload it to OrderEase for a product.
        
        Args:
            product_sku: The product's SKU
            image_url: URL to fetch the image from
            set_as_primary: Whether to set this image as primary
            skip_existing: Skip if product already has an image
            timeout_seconds: Timeout for fetching the image
            
        Returns:
            True if successful, False if image not found or upload failed
        """
        # Fetch the image. If the provided URL fails, fall back to other known variants.
        try:
            headers = {"User-Agent": "OrderEaseInventoryManager/1.0"}
            resp = requests.get(image_url, timeout=timeout_seconds, headers=headers)
            if resp.status_code in (403, 404):
                resp = None
                for alt_url in self._candidate_plant_image_urls(product_sku):
                    if alt_url == image_url:
                        continue
                    try:
                        r2 = requests.get(alt_url, timeout=timeout_seconds, headers=headers)
                        if r2.status_code in (403, 404):
                            continue
                        r2.raise_for_status()
                        resp = r2
                        break
                    except requests.RequestException:
                        continue
                if resp is None:
                    return False  # None of the variants worked
            else:
                resp.raise_for_status()
        except requests.RequestException:
            return False
        
        # Base64 encode the image data
        image_data_b64 = base64.b64encode(resp.content).decode("utf-8")
        
        # Upload via AddBulkProductMediaAsync
        payload = {
            "productImages": [
                {
                    "productSKU": product_sku,
                    "imageData": image_data_b64,
                }
            ]
        }
        
        try:
            self._make_request(
                "POST",
                "/api/Media/AddBulkProductMediaAsync",
                json_body=payload,
                params={
                    "setImageAsPrimary": str(set_as_primary).lower(),
                    "clearExisting": str(clear_existing).lower(),
                    "isFuzzyMatchMode": str(is_fuzzy_match_mode).lower(),
                    "skipExisting": str(skip_existing).lower(),
                },
                unwrap_operation_result=True,
            )
            return True
        except OrderEaseAPIError:
            return False

    def add_bulk_product_images_from_urls(
        self,
        sku_url_pairs: List[Tuple[str, str]],
        *,
        set_as_primary: bool = True,
        skip_existing: bool = True,
        clear_existing: bool = False,
        is_fuzzy_match_mode: bool = True,
        timeout_seconds: int = 30,
    ) -> Tuple[int, int, List[str]]:
        """
        Fetch and upload images for multiple products in a single API call.
        
        Args:
            sku_url_pairs: List of (sku, image_url) tuples
            set_as_primary: Whether to set images as primary
            skip_existing: Skip products that already have images
            timeout_seconds: Timeout for fetching each image
            
        Returns:
            Tuple of (uploaded_count, fetch_failed_count, uploaded_skus)
        """
        product_images = []
        failed = 0
        uploaded_skus: List[str] = []
        
        headers = {"User-Agent": "OrderEaseInventoryManager/1.0"}
        for sku, image_url in sku_url_pairs:
            try:
                # Try provided URL first, then fall back to known variants.
                resp = requests.get(image_url, timeout=timeout_seconds, headers=headers)
                if resp.status_code in (403, 404):
                    resp = None
                    for alt_url in self._candidate_plant_image_urls(sku):
                        if alt_url == image_url:
                            continue
                        try:
                            r2 = requests.get(alt_url, timeout=timeout_seconds, headers=headers)
                            if r2.status_code in (403, 404):
                                continue
                            r2.raise_for_status()
                            resp = r2
                            break
                        except requests.RequestException:
                            continue
                    if resp is None:
                        failed += 1
                        continue
                else:
                    resp.raise_for_status()
                image_data_b64 = base64.b64encode(resp.content).decode("utf-8")
                product_images.append({
                    "productSKU": sku,
                    "imageData": image_data_b64,
                })
                uploaded_skus.append(sku)
            except requests.RequestException:
                failed += 1
                continue
        
        if not product_images:
            return (0, failed, [])
        
        payload = {"productImages": product_images}
        
        try:
            self._make_request(
                "POST",
                "/api/Media/AddBulkProductMediaAsync",
                json_body=payload,
                params={
                    "setImageAsPrimary": str(set_as_primary).lower(),
                    "clearExisting": str(clear_existing).lower(),
                    "isFuzzyMatchMode": str(is_fuzzy_match_mode).lower(),
                    "skipExisting": str(skip_existing).lower(),
                },
                unwrap_operation_result=True,
            )
            return (len(product_images), failed, uploaded_skus)
        except OrderEaseAPIError:
            return (0, failed + len(product_images), [])

    def get_product_media(self, product_ids: List[int]) -> List[Dict[str, Any]]:
        """
        Fetch media attached to one or more products.
        Endpoint: GET /api/Media/GetProductMedia?ids=...&ids=...
        """
        if not product_ids:
            return []

        result = self._make_request(
            "GET",
            "/api/Media/GetProductMedia",
            params={"ids": [int(pid) for pid in product_ids]},
            unwrap_operation_result=True,
        )

        # Expected shape: RemotePagedList { items: [...] }
        if isinstance(result, dict):
            items = result.get("items") or result.get("Items") or []
            return items or []
        if isinstance(result, list):
            return result
        return []

    def set_primary_product_media(self, *, product_id: int, product_media_id: int) -> None:
        """
        Set the primary media for a product.
        Endpoint: POST /api/Media/SetPrimaryProductMedia?productId=..&productMediaId=..
        """
        self._make_request(
            "POST",
            "/api/Media/SetPrimaryProductMedia",
            params={"productId": int(product_id), "productMediaId": int(product_media_id)},
            unwrap_operation_result=True,
        )

    # Categories
    def get_categories(self) -> List[Dict[str, Any]]:
        """Get all categories."""
        result = self._make_request(
            "GET",
            "/api/Category/GetAll",
            unwrap_operation_result=True,
        )
        return result or []

    def create_category(
        self,
        *,
        name: str,
        parent_category_id: Optional[int] = None,
        language_id: Optional[int] = None,
        external_reference: Optional[str] = None,
        sort_order: Optional[int] = None,
    ) -> int:
        """
        Create a category.
        Endpoint: POST /api/Category/Add
        Response: int32 category id (per spec)
        """
        # The endpoint expects PrivateCategory.PrivateCategoryViewModelWithChildren.
        # In some tenants, omitting list fields can trigger a server-side 500
        # ("Value cannot be null. (Parameter 'source')") when their code calls LINQ on null.
        payload: Dict[str, Any] = {
            "name": name,
            "children": [],
            "translations": [],
        }
        if parent_category_id is not None:
            payload["parentCategoryId"] = int(parent_category_id)
            payload["parentId"] = int(parent_category_id)
        else:
            # Some implementations treat 0 as root; providing it avoids nulls.
            payload["parentCategoryId"] = 0
            payload["parentId"] = 0

        if language_id is not None:
            payload["languageId"] = int(language_id)
        if external_reference is not None:
            payload["externalReference"] = external_reference
        if sort_order is not None:
            payload["sortOrder"] = int(sort_order)

        created = self._make_request(
            "POST",
            "/api/Category/Add",
            json_body=payload,
            unwrap_operation_result=True,
        )
        if isinstance(created, int):
            return created
        # Some tenants may return a raw number as a string
        if isinstance(created, str) and created.strip().isdigit():
            return int(created.strip())
        raise OrderEaseOperationError(f"Unexpected Category/Add response: {created}")

    def ensure_category(self, *, name: str, parent_category_id: Optional[int] = None) -> int:
        """Find a category by name (case-insensitive), or create it."""
        target = name.strip().lower()
        for c in self.get_categories():
            if (_clean_str(c.get("name")) or _clean_str(c.get("Name"))).strip().lower() == target:
                cid = c.get("id") if c.get("id") is not None else c.get("Id")
                if isinstance(cid, int):
                    return cid
                if isinstance(cid, str) and cid.isdigit():
                    return int(cid)
        return self.create_category(
            name=name,
            parent_category_id=parent_category_id,
            external_reference=name,
            sort_order=0,
        )

    def find_category_id_by_name(self, name: str) -> Optional[int]:
        """Find a category id by name (case-insensitive). Returns None if not found."""
        target = name.strip().lower()
        for c in self.get_categories():
            if (_clean_str(c.get("name")) or _clean_str(c.get("Name"))).strip().lower() == target:
                cid = c.get("id") if c.get("id") is not None else c.get("Id")
                if isinstance(cid, int):
                    return cid
                if isinstance(cid, str) and cid.isdigit():
                    return int(cid)
        return None


def _chunked(items: List[Any], chunk_size: int) -> Iterable[List[Any]]:
    for i in range(0, len(items), chunk_size):
        yield items[i:i + chunk_size]


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _build_plant_description(p: Dict[str, Any]) -> str:
    """
    Build a compact description (<= 250 chars) from a plants.json entry.
    """
    group = _clean_str(p.get("Group"))
    name = _clean_str(p.get("Name"))
    size = _clean_str(p.get("Size"))
    branded = _clean_str(p.get("BrandedVarietyDisplayName"))
    branded_name = _clean_str(p.get("BrandedVarietyName"))

    base = " - ".join([part for part in [group, name] if part])
    if size:
        base = f"{base} ({size})" if base else f"({size})"

    extras: List[str] = []
    if branded_name:
        extras.append(branded_name)
    if branded and branded not in extras:
        extras.append(branded)

    if extras:
        base = f"{base} | " + " | ".join(extras)

    # Ensure max length 250 (per SupplierInventoryItem description constraint)
    return base[:250]


class InventoryManager:
    """High-level inventory management operations"""
    
    def __init__(self, api: OrderEaseAPI):
        self.api = api
    
    def list_inventories(self):
        """List all inventories"""
        print("\n📦 Fetching inventories...")
        inventories = self.api.get_all_inventories()
        
        if not inventories:
            print("No inventories found.")
            return
        
        print(f"\n✅ Found {len(inventories)} inventories:\n")
        for inv in inventories:
            print(f"  ID: {inv.get('id')}")
            print(f"  Name: {inv.get('name')}")
            print(f"  Description: {inv.get('description', 'N/A')}")
            print(f"  Company ID: {inv.get('companyId', 'N/A')}")
            if inv.get('startDate'):
                print(f"  Start Date: {inv.get('startDate')}")
            if inv.get('endDate'):
                print(f"  End Date: {inv.get('endDate')}")
            print()
    
    def list_catalogs(self, supplier_id: int):
        """List all catalogs for a supplier"""
        print(f"\n📚 Fetching catalogs for supplier {supplier_id}...")
        catalogs = self.api.get_all_catalogs(supplier_id)
        
        if not catalogs:
            print("No catalogs found.")
            return
        
        print(f"\n✅ Found {len(catalogs)} catalogs:\n")
        for cat in catalogs:
            print(f"  ID: {cat.get('id')}")
            print(f"  Name: {cat.get('name', 'N/A')}")
            print(f"  Description: {cat.get('description', 'N/A')}")
            print()

    def list_categories(self):
        """List all categories (id + fullPath/name)"""
        print("\n🗂️  Fetching categories...")
        categories = self.api.get_categories()
        if not categories:
            print("No categories found.")
            return

        def sort_key(c: Dict[str, Any]) -> str:
            return (
                _clean_str(c.get("fullPath"))
                or _clean_str(c.get("FullPath"))
                or _clean_str(c.get("dropDownDisplay"))
                or _clean_str(c.get("DropDownDisplay"))
                or _clean_str(c.get("name"))
                or _clean_str(c.get("Name"))
            ).lower()

        categories = sorted(categories, key=sort_key)
        print(f"\n✅ Found {len(categories)} categories:\n")
        for c in categories[:200]:
            cid = c.get("id") if c.get("id") is not None else c.get("Id")
            name = _clean_str(c.get("name")) or _clean_str(c.get("Name"))
            full_path = _clean_str(c.get("fullPath")) or _clean_str(c.get("FullPath"))
            drop = _clean_str(c.get("dropDownDisplay")) or _clean_str(c.get("DropDownDisplay"))
            label = full_path or drop or name or "(no name)"
            print(f"  {cid}: {label}")
        if len(categories) > 200:
            print(f"\n  ... and {len(categories) - 200} more (showing first 200)")

    def create_category(self, name: str, parent_category_id: Optional[int] = None) -> Optional[int]:
        """Create a category and return its id."""
        name = name.strip()
        if not name:
            print("❌ Category name cannot be empty.")
            return None

        print(f"\n🆕 Creating category: {name}")
        try:
            cid = self.api.create_category(name=name, parent_category_id=parent_category_id)
        except OrderEaseAPIError as e:
            print(f"❌ Failed to create category: {e}")
            return None

        print(f"✅ Category created with ID: {cid}")
        return cid
    
    def create_inventory_from_csv(self, csv_file: str, supplier_id: int, 
                                  inventory_name: str, inventory_description: str = ""):
        """
        Create an inventory and populate it from a CSV file
        
        CSV format should have columns: sku, description, quantity, price
        """
        print(f"\n📄 Reading CSV file: {csv_file}")
        
        if not os.path.exists(csv_file):
            print(f"❌ File not found: {csv_file}")
            return
        
        # Read CSV
        items = []
        try:
            with open(csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                items = list(reader)
        except Exception as e:
            print(f"❌ Error reading CSV: {e}")
            return
        
        if not items:
            print("❌ No items found in CSV")
            return
        
        print(f"✅ Found {len(items)} items in CSV")
        
        # Create inventory
        print(f"\n📦 Creating inventory: {inventory_name}")
        result = self.api.create_inventory(
            name=inventory_name,
            description=inventory_description,
            start_date=datetime.now().isoformat()
        )
        
        if result.get('success') or result.get('result'):
            inventory_id = result.get('result', {}).get('id')
            print(f"✅ Inventory created with ID: {inventory_id}")
            
            # Note: Actual product assignment would require getting item IDs
            # from the catalog first using the SKUs
            print("\n💡 Next steps:")
            print("1. Use the catalog API to get item IDs for your SKUs")
            print("2. Assign those item IDs to this inventory using AssignProductsToInventory")
        else:
            print(f"❌ Failed to create inventory: {result}")
    
    def show_supplier_inventory(self, supplier_id: int):
        """Show all inventory items for a supplier"""
        print(f"\n📦 Exporting supplier products for supplier {supplier_id}...")
        rows = self.api.export_supplier_products(supplier_id)

        if not rows:
            print("No products found.")
            return

        print(f"\n✅ Found {len(rows)} products:\n")
        for i, row in enumerate(rows[:10], 1):
            sku = row.get("privateSKU") or "N/A"
            desc = row.get("description") or "N/A"
            qty = row.get("quantityAvailable") or "N/A"
            price = row.get("netPrice") or "N/A"
            print(f"  {i}. SKU: {sku}")
            print(f"     Description: {desc}")
            print(f"     Quantity: {qty}")
            print(f"     Net Price: {price}")
            print()

        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")

    def update_pricing_from_csv(
        self,
        csv_file: str,
        *,
        supplier_id: int,
        batch_size: int = 200,
        dry_run: bool = False,
    ) -> None:
        """
        Update pricing using /api/SupplierInventory/UpdatePricing.

        CSV columns:
          - description/name (preferred) OR sku/privateSKU
          - netPrice (or price)
          - suggestedRetailPrice (optional)
        """
        if not os.path.exists(csv_file):
            print(f"❌ File not found: {csv_file}")
            return

        rows: List[Dict[str, Any]] = []
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        if not rows:
            print("❌ No rows found in CSV")
            return

        def get_any(d: Dict[str, Any], keys: List[str]) -> Optional[str]:
            for k in keys:
                if k in d and str(d.get(k) or "").strip():
                    return str(d.get(k)).strip()
            return None

        def norm_name(s: str) -> str:
            # Basic normalization for name matching
            s = (s or "").strip().lower()
            s = " ".join(s.split())
            return s

        # Parse desired price updates from CSV.
        # We store either a direct SKU, or a name/description to be resolved to a SKU.
        sku_to_prices: Dict[str, Dict[str, float]] = {}
        name_to_prices: Dict[str, Dict[str, float]] = {}
        invalid = 0
        for r in rows:
            sku = get_any(r, ["sku", "privateSKU", "privateSku", "SKU"])
            name = get_any(r, ["description", "Description", "name", "Name", "productName", "ProductName"])

            net_raw = get_any(r, ["netPrice", "price", "NetPrice", "Price"])
            srp_raw = get_any(r, ["suggestedRetailPrice", "srp", "SuggestedRetailPrice", "SRP"])

            if net_raw is None and srp_raw is None:
                invalid += 1
                continue

            price_entry: Dict[str, float] = {}
            try:
                if net_raw is not None:
                    price_entry["netPrice"] = float(net_raw)
                if srp_raw is not None:
                    price_entry["suggestedRetailPrice"] = float(srp_raw)
            except ValueError:
                invalid += 1
                continue

            if sku:
                sku_to_prices[sku] = price_entry
            elif name:
                name_to_prices[norm_name(name)] = price_entry
            else:
                invalid += 1

        if not sku_to_prices and not name_to_prices:
            print("❌ No valid pricing rows found (expected description/name OR sku + netPrice and/or suggestedRetailPrice)")
            return

        if invalid:
            print(f"⚠️  Skipped {invalid} invalid rows")

        # Resolve names/descriptions -> SKUs using the current supplier product export.
        if name_to_prices:
            print(f"\nResolving {len(name_to_prices)} names/descriptions to SKUs via supplier export ...")
            exported = self.api.export_supplier_products(supplier_id)
            desc_to_skus: Dict[str, List[str]] = {}
            for row in exported or []:
                sku = _clean_str(row.get("privateSKU") or row.get("PrivateSKU"))
                desc = _clean_str(row.get("description") or row.get("Description"))
                if not sku or not desc:
                    continue
                desc_to_skus.setdefault(norm_name(desc), []).append(sku)

            unresolved: List[str] = []
            ambiguous: List[str] = []
            for nm, prices in name_to_prices.items():
                candidates = desc_to_skus.get(nm) or []
                candidates = list(dict.fromkeys(candidates))
                if not candidates:
                    unresolved.append(nm)
                    continue
                if len(candidates) > 1:
                    ambiguous.append(nm)
                    continue
                sku_to_prices[candidates[0]] = prices

            if ambiguous:
                print(f"⚠️  {len(ambiguous)} rows were ambiguous (same description matched multiple SKUs) and were skipped.")
                print("   First few ambiguous names:", ", ".join(ambiguous[:10]))
            if unresolved:
                print(f"⚠️  {len(unresolved)} rows could not be matched to any OrderEase product description and were skipped.")
                print("   First few unmatched names:", ", ".join(unresolved[:10]))

        skus = list(sku_to_prices.keys())
        print(f"\n💲 Updating pricing for {len(skus)} SKUs")
        if dry_run:
            print("DRY RUN enabled: no API writes will be performed")

        # Lookup itemIds by SKU
        sku_to_item_id: Dict[str, int] = {}
        for chunk in _chunked(skus, 200):
            pairs = self.api.lookup_ids_by_sku(chunk)
            for p in pairs or []:
                pid = p.get("id") if p.get("id") is not None else p.get("Id")
                psku = _clean_str(p.get("privateSKU") or p.get("PrivateSKU"))
                if pid is None or not psku:
                    continue
                sku_to_item_id[psku] = int(pid)

        missing = [s for s in skus if s not in sku_to_item_id]
        if missing:
            print(f"⚠️  {len(missing)} SKUs were not found in OrderEase and will be skipped.")
            print("   First few missing:", ", ".join(missing[:10]))

        updates: List[Dict[str, Any]] = []
        for sku, prices in sku_to_prices.items():
            item_id = sku_to_item_id.get(sku)
            if not item_id:
                continue
            payload = {"itemId": item_id}
            payload.update(prices)
            updates.append(payload)

        if not updates:
            print("Nothing to update (no matching SKUs found).")
            return

        batch_size = max(1, int(batch_size))
        total_batches = (len(updates) + batch_size - 1) // batch_size
        print(f"\nSending {len(updates)} updates in {total_batches} batches (batch_size={batch_size}) ...")

        if dry_run:
            print("DRY RUN: sample payload:")
            print(json.dumps(updates[:3], indent=2))
            return

        ok = 0
        failed = 0
        for i, batch in enumerate(_chunked(updates, batch_size), 1):
            try:
                self.api.update_pricing(batch)
                ok += len(batch)
            except OrderEaseAPIError as e:
                failed += len(batch)
                print(f"❌ Batch {i}/{total_batches} failed: {e}")
            if i <= 3 or i % 10 == 0 or i == total_batches:
                print(f"  Progress: {i}/{total_batches} (ok={ok}, failed={failed})")

        print(f"\n✅ Pricing update complete. ok={ok}, failed={failed}")

    def import_plants_from_url(
        self,
        plants_url: str,
        *,
        default_unit_price: float = 0.0,
        catalog_id: Optional[int] = None,
        catalog_ref: Optional[str] = None,
        plant_id: Optional[str] = None,
        category_id: Optional[int] = None,
        category_name: str = "Plants",
        limit: Optional[int] = None,
        dry_run: bool = False,
        add_to_catalog: bool = True,
        upload_images: bool = False,
    ) -> None:
        """
        Import products from a plants.json feed:
        - Creates/updates products via SupplierInventory/AddOrUpdate
        - Optionally uploads product images from S3
        - Optionally adds SKUs to a catalog using bulk byRef endpoint
        """
        print(f"\nFetching plants feed: {plants_url}")
        resp = requests.get(plants_url, timeout=30)
        resp.raise_for_status()
        plants = resp.json()

        if not isinstance(plants, list):
            raise ValueError("plants feed must be a JSON array")

        if plant_id:
            wanted = plant_id.strip().lower()
            all_ids = [
                _clean_str(p.get("Id") or p.get("id2")).lower()
                for p in plants
                if _clean_str(p.get("Id") or p.get("id2"))
            ]
            all_ids_unique = sorted(set(all_ids))
            plants = [
                p for p in plants
                if _clean_str(p.get("Id")).lower() == wanted
                or _clean_str(p.get("id2")).lower() == wanted
            ]
            if not plants:
                suggestions: List[str] = []
                # Common typo: trailing "l" instead of "1"
                if wanted.endswith("l"):
                    alt = wanted[:-1] + "1"
                    if alt in set(all_ids_unique):
                        suggestions.append(alt)
                # Close matches
                suggestions.extend(difflib.get_close_matches(wanted, all_ids_unique, n=8, cutoff=0.4))
                suggestions = list(dict.fromkeys(suggestions))  # de-dupe, keep order

                msg = f"Plant Id not found in feed: {plant_id}"
                if suggestions:
                    msg += "\nClosest matches:\n  - " + "\n  - ".join(suggestions)
                msg += "\nTip: the feed uses IDs like 'mnva1' (number one), not 'mnval' (letter l)."
                raise ValueError(msg)
            # Force single-item behavior
            limit = 1

        if limit is not None:
            plants = plants[:limit]

        print(f"Found {len(plants)} plant records")
        if dry_run:
            print("DRY RUN enabled: no API writes will be performed")

        resolved_category_id: Optional[int] = category_id
        if not resolved_category_id and not dry_run:
            # Ensure we have a category to satisfy SupplierInventory requirements
            resolved_category_id = self.api.ensure_category(name=category_name)
            print(f"Using category '{category_name}' (id={resolved_category_id})")

        created_or_found = 0
        failed = 0
        skus_for_catalog: List[str] = []
        verbose = bool(plant_id) or (limit is not None and limit <= 5) or len(plants) <= 5

        for idx, p in enumerate(plants, 1):
            sku = _clean_str(p.get("Id") or p.get("id2"))
            if not sku:
                failed += 1
                continue

            desc = _build_plant_description(p)
            pack_desc = _clean_str(p.get("Size")) or None
            group = _clean_str(p.get("Group")) or None
            id_no_size = _clean_str(p.get("IdNoSize")) or None
            if verbose:
                print(f"\n[{idx}] Preparing product")
                print(f"  SKU: {sku}")
                print(f"  Description: {desc}")
                print(f"  Pack/Size: {pack_desc or 'N/A'}")
                if group:
                    print(f"  Group: {group}")
                print(f"  Unit Price: {default_unit_price}")

            if dry_run:
                created_or_found += 1
                skus_for_catalog.append(sku)
                continue

            try:
                # Create/update product in supplier inventory
                result = self.api.upsert_supplier_inventory_item(
                    private_sku=sku,
                    description=desc,
                    category_id=int(resolved_category_id) if resolved_category_id is not None else 0,
                    net_price=default_unit_price,
                    open_size_description=pack_desc,
                    comments=group,
                    external_source="michaelswholesalenursery/plants.json",
                    external_reference=(id_no_size or sku),
                )
                if verbose:
                    print("  ✅ Upserted in OrderEase (SupplierInventory/AddOrUpdate)")
                    # Try to resolve an ID via lookup
                    try:
                        ids = self.api.lookup_ids_by_sku([sku])
                        if ids:
                            first = ids[0]
                            pid = first.get("id") if first.get("id") is not None else first.get("Id")
                            print(f"  Product ID: {pid}")
                    except Exception:
                        pass
                
                # Upload product image if enabled
                if upload_images:
                    image_url = f"{PLANT_IMAGE_BUCKET_URL}/{sku}-Single-large.jpg"
                    if verbose:
                        print(f"  📷 Uploading image from: {image_url}")
                    img_ok = self.api.add_product_image_from_url(sku, image_url)
                    if img_ok:
                        if verbose:
                            print("  ✅ Image uploaded")
                    else:
                        if verbose:
                            print("  ⚠️  No image found or upload failed")
                
                created_or_found += 1
                skus_for_catalog.append(sku)
            except OrderEaseAPIError as e:
                failed += 1
                print(f"  Failed SKU={sku}: {e}")

            if idx % 200 == 0:
                print(f"  Progress: {idx}/{len(plants)} processed (ok={created_or_found}, failed={failed})")
                time.sleep(0.1)

        print(f"\nImport done. ok={created_or_found}, failed={failed}")

        if not add_to_catalog or dry_run or not skus_for_catalog:
            return

        # Resolve catalog_ref if only catalog_id was provided
        resolved_catalog_ref = catalog_ref
        if not resolved_catalog_ref and catalog_id is not None:
            cat = self.api.get_catalog_by_id(catalog_id)
            resolved_catalog_ref = _clean_str(cat.get("integrationReference")) or None
            if not resolved_catalog_ref:
                raise OrderEaseAPIError(
                    "Could not resolve catalogRef (integrationReference) from catalogId. "
                    "Provide catalog_ref explicitly."
                )

        if not resolved_catalog_ref:
            print("No catalog specified; skipping catalog assignment.")
            return

        print(f"\nAdding {len(skus_for_catalog)} SKUs to catalogRef={resolved_catalog_ref} ...")
        for chunk in _chunked(skus_for_catalog, 500):
            self.api.add_skus_to_catalog_by_ref(resolved_catalog_ref, chunk)
        print("Catalog assignment complete.")

    def _wait_for_product_media(
        self,
        product_ids: List[int],
        *,
        timeout_seconds: int = 45,
        poll_interval_seconds: float = 1.0,
    ) -> Dict[int, List[Dict[str, Any]]]:
        """
        Poll OrderEase until media records appear for each product id (or timeout).
        Returns a dict mapping productId -> list[ProductMediaModel] (empty list if not found).
        """
        remaining = {int(pid) for pid in (product_ids or []) if pid is not None}
        found: Dict[int, List[Dict[str, Any]]] = {}
        deadline = time.time() + max(0.0, float(timeout_seconds))

        while remaining and time.time() < deadline:
            try:
                items = self.api.get_product_media(list(remaining))
            except OrderEaseAPIError:
                items = []

            by_pid: Dict[int, List[Dict[str, Any]]] = {}
            for it in items or []:
                pid = it.get("productId") if it.get("productId") is not None else it.get("ProductId")
                if pid is None:
                    continue
                pid_int = int(pid)
                by_pid.setdefault(pid_int, []).append(it)

            for pid_int, lst in by_pid.items():
                if lst:
                    found[pid_int] = lst
                    remaining.discard(pid_int)

            if remaining:
                time.sleep(poll_interval_seconds)

        # Ensure every requested id is present (empty list if not found)
        for pid_int in {int(pid) for pid in (product_ids or []) if pid is not None}:
            found.setdefault(pid_int, [])
        return found

    def upload_plant_images_from_feed(
        self,
        plants_url: str,
        *,
        plant_id: Optional[str] = None,
        limit: Optional[int] = None,
        batch_size: int = 10,
        skip_existing: bool = True,
        clear_existing: bool = False,
        set_as_primary: bool = True,
        poll_timeout_seconds: int = 45,
        dry_run: bool = False,
    ) -> None:
        """
        Upload and attach product images for existing products from the plants feed.

        This is an images-only job: it does NOT modify product prices, descriptions, categories, etc.
        """
        print(f"\nFetching plants feed: {plants_url}")
        resp = requests.get(plants_url, timeout=30)
        resp.raise_for_status()
        plants = resp.json()

        if not isinstance(plants, list):
            raise ValueError("plants feed must be a JSON array")

        if plant_id:
            wanted = plant_id.strip().lower()
            all_ids = [
                _clean_str(p.get("Id") or p.get("id2")).lower()
                for p in plants
                if _clean_str(p.get("Id") or p.get("id2"))
            ]
            all_ids_unique = list(dict.fromkeys(all_ids))
            if wanted not in set(all_ids_unique):
                suggestions: List[str] = []
                if wanted.endswith("l") and (wanted[:-1] + "1") in set(all_ids_unique):
                    suggestions.append(wanted[:-1] + "1")
                for alt in [wanted + "1", wanted + "2", wanted + "3"]:
                    if alt in set(all_ids_unique):
                        suggestions.append(alt)
                suggestions.extend(difflib.get_close_matches(wanted, all_ids_unique, n=8, cutoff=0.4))
                suggestions = list(dict.fromkeys(suggestions))

                msg = f"Plant Id not found in feed: {plant_id}"
                if suggestions:
                    msg += "\nClosest matches:\n  - " + "\n  - ".join(suggestions)
                msg += "\nTip: the feed uses IDs like 'mnva1' (number one), not 'mnval' (letter l)."
                raise ValueError(msg)

            plants = [
                p for p in plants
                if _clean_str(p.get("Id") or p.get("id2")).lower() == wanted
            ]
            limit = 1

        if limit is not None:
            plants = plants[:limit]

        skus: List[str] = []
        for p in plants:
            sku = _clean_str(p.get("Id") or p.get("id2"))
            if sku:
                skus.append(sku)
        skus = list(dict.fromkeys(skus))  # de-dupe, keep order

        print(f"Found {len(skus)} plant records / SKUs")
        if dry_run:
            print("DRY RUN enabled: no API writes will be performed")

        print("\nLooking up product IDs in OrderEase...")
        sku_to_product_id: Dict[str, int] = {}
        for chunk in _chunked(skus, 200):
            rows = self.api.lookup_ids_by_sku(chunk)
            for r in rows or []:
                pid = r.get("id") if r.get("id") is not None else r.get("Id")
                rsku = _clean_str(r.get("privateSKU") or r.get("PrivateSKU") or r.get("sku") or r.get("SKU"))
                if pid is None or not rsku:
                    continue
                sku_to_product_id[rsku] = int(pid)

        missing_skus = [s for s in skus if s not in sku_to_product_id]
        if missing_skus:
            print(f"⚠️  {len(missing_skus)} SKUs were not found in OrderEase and will be skipped.")
            print("   First few missing:", ", ".join(missing_skus[:10]))

        target_skus = [s for s in skus if s in sku_to_product_id]
        if not target_skus:
            print("Nothing to do (no matching SKUs found in OrderEase).")
            return

        batch_size = max(1, int(batch_size))
        total_batches = (len(target_skus) + batch_size - 1) // batch_size
        print(
            f"\nUploading images for {len(target_skus)} products "
            f"(batch_size={batch_size}, skip_existing={skip_existing}, clear_existing={clear_existing}) ..."
        )

        attempted = 0
        fetch_failed = 0
        uploaded = 0
        attached = 0
        attach_failed = 0
        skipped_existing_count = 0
        primary_set_failed = 0

        # Build IdNoSize -> sibling SKUs map (used to source an image when a size-SKU has none)
        id_no_size_to_skus: Dict[str, List[str]] = {}
        for p in plants:
            sku = _clean_str(p.get("Id") or p.get("id2"))
            id_no_size = _clean_str(p.get("IdNoSize"))
            if sku and id_no_size:
                id_no_size_to_skus.setdefault(id_no_size, []).append(sku)
        for k in list(id_no_size_to_skus.keys()):
            # de-dupe, keep order
            id_no_size_to_skus[k] = list(dict.fromkeys(id_no_size_to_skus[k]))

        def _source_sku_candidates(target_sku: str) -> List[str]:
            # Prefer itself, then siblings of same IdNoSize (prefer larger sizes like 7/3 then 2 then 1)
            target_sku = _clean_str(target_sku)
            # Find its IdNoSize from the feed
            id_no_size = None
            for p in plants:
                if _clean_str(p.get("Id") or p.get("id2")) == target_sku:
                    id_no_size = _clean_str(p.get("IdNoSize")) or None
                    break
            sibs = id_no_size_to_skus.get(id_no_size, []) if id_no_size else []

            def rank(s: str) -> int:
                s = _clean_str(s)
                if s.endswith("f"):
                    return 4
                if s and s[-1].isdigit():
                    return int(s[-1])
                return 0

            ordered_sibs = sorted([x for x in sibs if x != target_sku], key=rank, reverse=True)
            return [target_sku] + ordered_sibs

        for batch_idx, batch_skus in enumerate(_chunked(target_skus, batch_size), 1):
            should_log = batch_idx <= 5 or batch_idx % 10 == 0 or batch_idx == total_batches
            to_upload_skus = list(batch_skus)
            product_ids = [sku_to_product_id[s] for s in to_upload_skus]

            # Optional: skip products that already have media attached.
            if skip_existing and not dry_run:
                existing_media = self.api.get_product_media(product_ids)
                has_media: set = set()
                for m in existing_media or []:
                    pid = m.get("productId") if m.get("productId") is not None else m.get("ProductId")
                    if pid is not None:
                        has_media.add(int(pid))

                if has_media:
                    filtered_skus: List[str] = []
                    filtered_pids: List[int] = []
                    for s in to_upload_skus:
                        pid = sku_to_product_id[s]
                        if pid in has_media:
                            skipped_existing_count += 1
                            continue
                        filtered_skus.append(s)
                        filtered_pids.append(pid)
                    to_upload_skus = filtered_skus
                    product_ids = filtered_pids

            if not to_upload_skus:
                if should_log:
                    print(f"  Batch {batch_idx}/{total_batches}: skipped (all already had media)")
                continue

            attempted += len(to_upload_skus)

            if dry_run:
                if should_log:
                    print(f"  Batch {batch_idx}/{total_batches}: DRY RUN would upload {len(to_upload_skus)} images")
                continue

            if should_log:
                print(f"  Batch {batch_idx}/{total_batches}: fetching+uploading {len(to_upload_skus)} images ...")

            # Build base64 payload by fetching first-available image for each SKU (with sibling fallback)
            fetch_failed_before = fetch_failed
            product_images_payload: List[Dict[str, str]] = []
            ok_skus: List[str] = []
            for s in to_upload_skus:
                img_bytes, chosen_url = self.api._fetch_first_available_image(
                    _source_sku_candidates(s),
                    timeout_seconds=20,
                )
                if not img_bytes:
                    fetch_failed += 1
                    continue
                product_images_payload.append({
                    "productSKU": s,
                    "imageData": base64.b64encode(img_bytes).decode("utf-8"),
                })
                ok_skus.append(s)
            fetch_failed_batch = fetch_failed - fetch_failed_before

            if product_images_payload:
                try:
                    self.api.upload_product_images_base64(
                        product_images_payload,
                        set_as_primary=set_as_primary,
                        skip_existing=skip_existing,
                        clear_existing=clear_existing,
                        is_fuzzy_match_mode=True,
                    )
                    uploaded += len(product_images_payload)
                except OrderEaseAPIError:
                    # Count as failed upload (treat as attach failures later)
                    pass

            if should_log:
                print(
                    f"  Batch {batch_idx}/{total_batches}: uploaded={len(ok_skus)}, fetch_failed={fetch_failed_batch}"
                )

            # Confirm attachments (and explicitly set primary where needed) for SKUs we actually uploaded
            ok_product_ids = [sku_to_product_id[s] for s in ok_skus if s in sku_to_product_id]
            if ok_product_ids:
                if should_log:
                    print(f"  Batch {batch_idx}/{total_batches}: waiting for attachment (up to {poll_timeout_seconds}s) ...")
                media_by_pid = self._wait_for_product_media(
                    ok_product_ids,
                    timeout_seconds=poll_timeout_seconds,
                    poll_interval_seconds=1.0,
                )
                for pid, items in media_by_pid.items():
                    if not items:
                        attach_failed += 1
                        continue
                    attached += 1

                    if set_as_primary:
                        chosen = max(
                            items,
                            key=lambda x: int(x.get("id") if x.get("id") is not None else x.get("Id") or 0),
                        )
                        pmid = chosen.get("id") if chosen.get("id") is not None else chosen.get("Id")
                        if pmid is None:
                            continue
                        try:
                            self.api.set_primary_product_media(product_id=int(pid), product_media_id=int(pmid))
                        except OrderEaseAPIError:
                            primary_set_failed += 1
            elif should_log and ok_count > 0:
                print(f"  Batch {batch_idx}/{total_batches}: uploaded images but had no resolvable product IDs to poll")

            if should_log:
                print(
                    f"  Progress: attempted={attempted} uploaded={uploaded} fetch_failed={fetch_failed} "
                    f"attached={attached} attach_failed={attach_failed}"
                )

        print("\nImage upload done.")
        print(f"  Attempted: {attempted}")
        print(f"  Uploaded (fetched+sent): {uploaded}")
        print(f"  Image fetch failed (missing/blocked): {fetch_failed}")
        if skipped_existing_count:
            print(f"  Skipped (already had media): {skipped_existing_count}")
        if not dry_run:
            print(f"  Attached (observed via GetProductMedia): {attached}")
            print(f"  Not attached (timed out): {attach_failed}")
            if set_as_primary and primary_set_failed:
                print(f"  Failed to set primary: {primary_set_failed}")


def main():
    """Main entry point"""
    print("=" * 60)
    print("OrderEase Inventory Manager - v2 API")
    print("=" * 60)
    
    # Load environment variables
    load_dotenv()
    
    # Get configuration from .env
    base_url = os.getenv("ORDEREASE_BASE_URL")

    # Credentials provided by OrderEase (as-is)
    v2_api_key = os.getenv("V2_API_Key")
    client_key = os.getenv("ClientKey")
    client_secret = os.getenv("ClientSecret")

    # Backward compatible / optional overrides
    integration_key = os.getenv("ORDEREASE_INTEGRATION_KEY") or v2_api_key
    bearer_token = os.getenv("ORDEREASE_BEARER_TOKEN") or os.getenv("ORDEREASE_TOKEN")

    supplier_id = os.getenv("ORDEREASE_SUPPLIER_ID")
    company_id = os.getenv("ORDEREASE_COMPANY_ID")
    default_category_id_raw = os.getenv("ORDEREASE_DEFAULT_CATEGORY_ID") or os.getenv("ORDEREASE_CATEGORY_ID")
    default_category_id = int(default_category_id_raw) if (default_category_id_raw and default_category_id_raw.isdigit()) else None

    if not base_url or not (integration_key or bearer_token or v2_api_key):
        print("\n❌ Missing required environment variables!")
        print("\nPlease create a .env file with at least:")
        print("  ORDEREASE_BASE_URL=https://your-orderease-api-url.com")
        print("  V2_API_Key=your_v2_api_key_here")
        print("  ClientKey=your_client_key_here")
        print("  ClientSecret=your_client_secret_here")
        print("\nOptional:")
        print("  ORDEREASE_SUPPLIER_ID=your_supplier_id")
        print("  ORDEREASE_COMPANY_ID=your_company_id")
        sys.exit(1)

    # Initialize API client
    api = OrderEaseAPI(
        base_url,
        integration_key=os.getenv("ORDEREASE_INTEGRATION_KEY"),
        bearer_token=bearer_token,
        v2_api_key=v2_api_key,
        client_key=client_key,
        client_secret=client_secret,
    )
    manager = InventoryManager(api)
    
    # Interactive menu
    while True:
        print("\n" + "=" * 60)
        print("What would you like to do?")
        print("=" * 60)
        print("1. List all inventories")
        print("2. List all catalogs")
        print("3. Show supplier inventory items")
        print("4. Create new inventory")
        print("5. Create inventory from CSV")
        print("6. Import products from plants.json feed")
        print("7. List categories")
        print("8. Create category")
        print("9. Upload product images (attach to existing products)")
        print("10. Update pricing from CSV")
        print("11. Exit")
        print()
        
        choice = input("Enter your choice (1-11): ").strip()
        
        if choice == '1':
            manager.list_inventories()
        
        elif choice == '2':
            if supplier_id:
                manager.list_catalogs(int(supplier_id))
            else:
                sid = input("Enter Supplier ID: ").strip()
                if sid.isdigit():
                    manager.list_catalogs(int(sid))
        
        elif choice == '3':
            if supplier_id:
                manager.show_supplier_inventory(int(supplier_id))
            else:
                sid = input("Enter Supplier ID: ").strip()
                if sid.isdigit():
                    manager.show_supplier_inventory(int(sid))
        
        elif choice == '4':
            name = input("Inventory name: ").strip()
            description = input("Description (optional): ").strip()
            
            if name:
                result = api.create_inventory(
                    name=name,
                    description=description,
                    start_date=datetime.now().isoformat()
                )
                print(f"\n✅ Result: {json.dumps(result, indent=2)}")
        
        elif choice == '5':
            csv_file = input("CSV file path: ").strip()
            name = input("Inventory name: ").strip()
            description = input("Description (optional): ").strip()
            
            if csv_file and name and supplier_id:
                manager.create_inventory_from_csv(
                    csv_file, int(supplier_id), name, description
                )
            else:
                print("❌ Missing required information")

        elif choice == '6':
            default_url = "https://michaelswholesalenursery.com/data/plants.json"
            url = default_url

            plant_id = input("Import a single plant Id (e.g. mnva1), blank for first N: ").strip() or None

            limit_raw = input("Limit records (blank for all): ").strip()
            limit = int(limit_raw) if limit_raw.isdigit() else None
            if plant_id:
                limit = 1

            price_raw = input("Default unit price (e.g. 0 or 12.99) [0]: ").strip()
            default_price = float(price_raw) if price_raw else 0.0

            cat_id_prompt = f"Category ID (blank to auto-create) [{default_category_id if default_category_id else ''}]: "
            cat_id_raw = input(cat_id_prompt).strip()
            category_id = default_category_id
            if cat_id_raw.isdigit():
                category_id = int(cat_id_raw)

            category_name = input("Category name (used only if Category ID is blank) [Plants]: ").strip() or "Plants"

            catalog_id_raw = input("Catalog ID to add products to (blank to skip): ").strip()
            catalog_id_val = int(catalog_id_raw) if catalog_id_raw.isdigit() else None

            upload_images_raw = input("Upload product images from S3? (y/N): ").strip().lower()
            upload_images = upload_images_raw in ("y", "yes")

            dry_run_raw = input("Dry run? (y/N): ").strip().lower()
            dry_run = dry_run_raw in ("y", "yes")

            try:
                manager.import_plants_from_url(
                    url,
                    plant_id=plant_id,
                    default_unit_price=default_price,
                    category_id=category_id,
                    category_name=category_name,
                    catalog_id=catalog_id_val,
                    limit=limit,
                    dry_run=dry_run,
                    add_to_catalog=bool(catalog_id_val),
                    upload_images=upload_images,
                )
            except ValueError as e:
                print(f"\n❌ {e}")
            except OrderEaseAPIError as e:
                print(f"\n❌ OrderEase API error: {e}")
            except requests.exceptions.RequestException as e:
                print(f"\n❌ Network error fetching plants feed: {e}")

        elif choice == '7':
            manager.list_categories()

        elif choice == '8':
            name = input("New category name: ").strip()
            parent_raw = input("Parent category ID (blank for root): ").strip()
            parent_id = int(parent_raw) if parent_raw.isdigit() else None
            manager.create_category(name, parent_category_id=parent_id)

        elif choice == '9':
            default_url = "https://michaelswholesalenursery.com/data/plants.json"
            url = default_url

            plant_id = input("Upload images for a single plant Id (e.g. mnva1), blank for all: ").strip() or None

            limit_raw = input("Limit records (blank for all): ").strip()
            limit = int(limit_raw) if limit_raw.isdigit() else None
            if plant_id:
                limit = 1

            batch_raw = input("Batch size (recommended 5-20) [10]: ").strip()
            batch_size = int(batch_raw) if batch_raw.isdigit() else 10

            skip_existing_raw = input("Skip products that already have images? (Y/n): ").strip().lower()
            skip_existing = False if skip_existing_raw in ("n", "no") else True

            clear_existing_raw = input("Clear existing product images before uploading? (y/N): ").strip().lower()
            clear_existing = clear_existing_raw in ("y", "yes")

            set_primary_raw = input("Set uploaded image as primary? (Y/n): ").strip().lower()
            set_primary = False if set_primary_raw in ("n", "no") else True

            poll_raw = input("Poll timeout seconds for attachment [45]: ").strip()
            poll_timeout = int(poll_raw) if poll_raw.isdigit() else 45

            dry_run_raw = input("Dry run? (y/N): ").strip().lower()
            dry_run = dry_run_raw in ("y", "yes")

            try:
                manager.upload_plant_images_from_feed(
                    url,
                    plant_id=plant_id,
                    limit=limit,
                    batch_size=batch_size,
                    skip_existing=skip_existing,
                    clear_existing=clear_existing,
                    set_as_primary=set_primary,
                    poll_timeout_seconds=poll_timeout,
                    dry_run=dry_run,
                )
            except ValueError as e:
                print(f"\n❌ {e}")
            except OrderEaseAPIError as e:
                print(f"\n❌ OrderEase API error: {e}")
            except requests.exceptions.RequestException as e:
                print(f"\n❌ Network error fetching plants feed: {e}")

        elif choice == '10':
            csv_file = input("Pricing CSV file path: ").strip()
            batch_raw = input("Batch size [200]: ").strip()
            batch_size = int(batch_raw) if batch_raw.isdigit() else 200
            dry_run_raw = input("Dry run? (y/N): ").strip().lower()
            dry_run = dry_run_raw in ("y", "yes")
            try:
                sid = supplier_id
                if not sid:
                    sid_in = input("Supplier ID (required for name matching): ").strip()
                    sid = sid_in if sid_in.isdigit() else None
                if not sid:
                    print("❌ Supplier ID is required.")
                else:
                    manager.update_pricing_from_csv(csv_file, supplier_id=int(sid), batch_size=batch_size, dry_run=dry_run)
            except Exception as e:
                print(f"❌ Pricing update failed: {e}")

        elif choice == '11':
            print("\n👋 Goodbye!")
            break
        
        else:
            print("❌ Invalid choice")


if __name__ == "__main__":
    main()

