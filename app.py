"""
OrderEase Catalog Manager - Web UI

A FastAPI web application for managing the OrderEase product catalog.
Provides product browsing, search, inline price editing, and bulk CSV pricing.
"""

import os
import io
import re
import csv
import json
from typing import Any, Dict, List, Optional, Tuple
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from orderease_inventory_manager import (
    OrderEaseAPI,
    OrderEaseAPIError,
    _clean_str,
    _chunked,
)
from update_costs import (
    parse_pricing_csv,
    parse_description,
    find_csv_match,
    map_size,
    normalize_name,
    SIZE_COLUMNS,
)

load_dotenv()

# ---------------------------------------------------------------------------
# API client singleton
# ---------------------------------------------------------------------------

_api: Optional[OrderEaseAPI] = None


def get_api() -> OrderEaseAPI:
    global _api
    if _api is None:
        _api = OrderEaseAPI(
            os.getenv("ORDEREASE_BASE_URL", ""),
            integration_key=os.getenv("ORDEREASE_INTEGRATION_KEY"),
            bearer_token=os.getenv("ORDEREASE_BEARER_TOKEN") or os.getenv("ORDEREASE_TOKEN"),
            v2_api_key=os.getenv("V2_API_Key"),
            client_key=os.getenv("ClientKey"),
            client_secret=os.getenv("ClientSecret"),
        )
    return _api


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    get_api()
    yield

app = FastAPI(title="OrderEase Catalog Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def _product_stats(products: List[Dict]) -> Dict[str, Any]:
    total = len(products)
    priced = sum(1 for p in products if float(p.get("CatalogPrice", 0) or 0) > 0)
    unpriced = total - priced
    sizes = {}
    groups = {}
    for p in products:
        s = p.get("OpenSizeDescription") or "Unknown"
        sizes[s] = sizes.get(s, 0) + 1
        g = p.get("Comments") or "Uncategorized"
        groups[g] = groups.get(g, 0) + 1
    return {
        "total": total,
        "priced": priced,
        "unpriced": unpriced,
        "sizes": dict(sorted(sizes.items(), key=lambda x: -x[1])),
        "groups": dict(sorted(groups.items(), key=lambda x: -x[1])),
    }


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    api = get_api()
    try:
        raw = api._make_request("GET", "/api/SupplierInventory/GetAllSimple", unwrap_operation_result=True)
        plants = [p for p in (raw or []) if p.get("Category", {}).get("Name") == "Plants"]
    except OrderEaseAPIError as e:
        plants = []
    stats = _product_stats(plants)
    return templates.TemplateResponse("dashboard.html", {"request": request, "stats": stats})


@app.get("/products", response_class=HTMLResponse)
async def products_page(request: Request, q: str = "", group: str = "", size: str = "", pricing: str = ""):
    api = get_api()
    try:
        raw = api._make_request("GET", "/api/SupplierInventory/GetAllSimple", unwrap_operation_result=True)
        plants = [p for p in (raw or []) if p.get("Category", {}).get("Name") == "Plants"]
    except OrderEaseAPIError:
        plants = []

    all_groups = sorted(set(p.get("Comments") or "Uncategorized" for p in plants))
    all_sizes = sorted(set(p.get("OpenSizeDescription") or "Unknown" for p in plants))

    filtered = plants
    if q:
        ql = q.lower()
        filtered = [p for p in filtered if ql in (p.get("Description") or "").lower() or ql in (p.get("SupplierSKU") or "").lower()]
    if group:
        filtered = [p for p in filtered if (p.get("Comments") or "Uncategorized") == group]
    if size:
        filtered = [p for p in filtered if (p.get("OpenSizeDescription") or "Unknown") == size]
    if pricing == "priced":
        filtered = [p for p in filtered if float(p.get("CatalogPrice", 0) or 0) > 0]
    elif pricing == "unpriced":
        filtered = [p for p in filtered if float(p.get("CatalogPrice", 0) or 0) <= 0]

    return templates.TemplateResponse("products.html", {
        "request": request,
        "products": filtered,
        "products_json": json.dumps(filtered, default=str),
        "total": len(plants),
        "q": q,
        "group": group,
        "size": size,
        "pricing": pricing,
        "all_groups": all_groups,
        "all_sizes": all_sizes,
    })


@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request):
    return templates.TemplateResponse("pricing.html", {"request": request, "results": None})


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.post("/api/update-price")
async def update_single_price(request: Request):
    """Update a single product's price inline."""
    body = await request.json()
    item_id = body.get("itemId")
    new_price = body.get("netPrice")
    if item_id is None or new_price is None:
        raise HTTPException(400, "itemId and netPrice are required")
    try:
        api = get_api()
        api.update_pricing([{"itemId": int(item_id), "netPrice": float(new_price)}])
        return {"ok": True}
    except OrderEaseAPIError as e:
        raise HTTPException(500, str(e))


@app.post("/api/bulk-update-prices")
async def bulk_update_prices(request: Request):
    """Receive a list of {itemId, netPrice} and push them."""
    body = await request.json()
    updates = body.get("updates", [])
    if not updates:
        raise HTTPException(400, "No updates provided")
    try:
        api = get_api()
        ok = 0
        failed = 0
        for chunk in _chunked(updates, 200):
            try:
                api.update_pricing(chunk)
                ok += len(chunk)
            except OrderEaseAPIError:
                failed += len(chunk)
        return {"ok": True, "updated": ok, "failed": failed}
    except OrderEaseAPIError as e:
        raise HTTPException(500, str(e))


@app.post("/api/upload-csv")
async def upload_csv(file: UploadFile = File(...), use_markup: bool = Form(False)):
    """Upload a pricing CSV file and return the matched preview."""
    api = get_api()
    contents = await file.read()
    text = contents.decode("utf-8")

    tmp_path = "/tmp/_orderease_upload.csv"
    with open(tmp_path, "w") as f:
        f.write(text)

    try:
        csv_prices = parse_pricing_csv(tmp_path, use_markup=use_markup)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse CSV: {e}")

    try:
        raw = api._make_request("GET", "/api/SupplierInventory/GetAllSimple", unwrap_operation_result=True)
        plants = [p for p in (raw or []) if p.get("Category", {}).get("Name") == "Plants"]
    except OrderEaseAPIError as e:
        raise HTTPException(500, f"Failed to fetch products: {e}")

    matched = []
    unmatched_count = 0
    for p in plants:
        sku = p.get("SupplierSKU", "?")
        desc = p.get("Description", "")
        size_raw = p.get("OpenSizeDescription", "")
        current_price = float(p.get("CatalogPrice", 0) or 0)
        item_id = p.get("ItemId")
        group, variety, branded = parse_description(desc)

        csv_size = map_size(size_raw) if size_raw else None
        if not csv_size:
            continue

        result = find_csv_match(group, variety, branded, csv_prices, threshold=0.80)
        if not result:
            unmatched_count += 1
            continue

        csv_key, confidence = result
        new_price = csv_prices[csv_key]["sizes"].get(csv_size)
        if new_price is None:
            continue

        matched.append({
            "itemId": item_id,
            "sku": sku,
            "variety": variety,
            "size": csv_size,
            "oldPrice": current_price,
            "newPrice": new_price,
            "csvName": csv_prices[csv_key]["original"],
            "confidence": round(confidence * 100),
            "changed": abs(current_price - new_price) > 0.001,
        })

    changed = [m for m in matched if m["changed"]]
    return {
        "totalMatched": len(matched),
        "totalChanged": len(changed),
        "unmatched": unmatched_count,
        "matches": changed,
    }


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
