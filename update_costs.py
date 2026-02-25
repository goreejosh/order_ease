#!/usr/bin/env python3
"""
Update OrderEase product costs from Michael's Nursery pricing CSV.

Fetches all products via GetAllSimple, matches each plant to a row in the
pricing CSV by name + size, and bulk-updates netPrice via UpdatePricing.

Usage:
    python update_costs.py                     # dry run (default)
    python update_costs.py --apply             # push updates to OrderEase
    python update_costs.py --preview           # just show parsed products
    python update_costs.py --use-markup        # use markup columns instead of cost
"""

import os
import sys
import csv
import re
import json
import argparse
from typing import Dict, List, Optional, Any, Tuple
from difflib import SequenceMatcher
from dotenv import load_dotenv

from orderease_inventory_manager import (
    OrderEaseAPI,
    OrderEaseAPIError,
    _clean_str,
    _chunked,
)

CSV_PATH = "/Users/joshg/Downloads/11michaels_nursery_pricebook_prices_by_size.csv"
SIZE_COLUMNS = ["1g", "3g", "full", "7g", "15g"]
COST_COL_OFFSET = 1
MARKUP_COL_OFFSET = 7

SIZE_MAP = {
    "1 gal": "1g",
    "3 gal": "3g",
    "7 gal": "7g",
    "15 gal": "15g",
    "30 gal": None,
    "2 gal": None,
    "full gal": None,
    "quarts": None,
    "flats": "full",
    "flat(s) 4\"": "full",
    "flat(s) 4''": "full",
    "flats 4\" 18ct": "full",
    "flats 4\" 6ct": "full",
    "flats 4'' 18ct": "full",
}


def normalize_name(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[®™©#]", "", name)
    name = re.sub(r",?\s*pp\s*\d+", "", name, flags=re.IGNORECASE)
    name = re.sub(r",?\s*ppaf", "", name, flags=re.IGNORECASE)
    name = re.sub(r",?\s*uspp\s*\d+", "", name, flags=re.IGNORECASE)
    name = name.strip().rstrip(",").strip()
    return " ".join(name.split())


def map_size(size_str: str) -> Optional[str]:
    return SIZE_MAP.get(size_str.strip().lower())


def parse_pricing_csv(csv_path: str, *, use_markup: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Returns { normalized_name: { "original": str, "sizes": { "1g": float, ... } } }
    """
    offset = MARKUP_COL_OFFSET if use_markup else COST_COL_OFFSET
    prices: Dict[str, Dict[str, Any]] = {}

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if not row or not row[0].strip():
                continue
            plant_name = row[0].strip()
            key = normalize_name(plant_name)
            size_prices: Dict[str, float] = {}
            for i, size in enumerate(SIZE_COLUMNS):
                col = offset + i
                if col < len(row):
                    val = row[col].strip()
                    if val:
                        try:
                            price = float(val)
                            if price > 0:
                                size_prices[size] = price
                        except ValueError:
                            pass
            if size_prices:
                prices[key] = {"original": plant_name, "sizes": size_prices}
    return prices


def parse_description(desc: str) -> Tuple[str, str, List[str]]:
    """
    Parse an OrderEase description into (group, variety, branded_parts).
    Input: "Azalea - Encore Royalty (3 Gal) | Encore Azaleas | Autumn Royalty, PP10580"
    Output: ("Azalea", "Encore Royalty", ["Encore Azaleas", "Autumn Royalty, PP10580"])
    """
    d = desc.strip()
    branded_parts: List[str] = []
    if " | " in d:
        segments = d.split(" | ")
        d = segments[0].strip()
        branded_parts = [s.strip() for s in segments[1:] if s.strip()]

    m = re.search(r"\(([^)]+)\)\s*$", d)
    if m:
        d = d[: m.start()].strip()

    if " - " in d:
        group, variety = d.split(" - ", 1)
        return group.strip(), variety.strip(), branded_parts

    return "", d.strip(), branded_parts


def find_csv_match(
    group: str,
    variety: str,
    branded_parts: List[str],
    csv_prices: Dict[str, Dict[str, Any]],
    *,
    threshold: float = 0.80,
) -> Optional[Tuple[str, float]]:
    """
    Try to match an OrderEase product to a CSV plant name.
    Returns (csv_key, confidence) or None.
    """
    candidates = []
    candidates.append(variety)
    if group:
        candidates.append(f"{group} - {variety}")
    candidates.extend(branded_parts)

    for candidate in candidates:
        key = normalize_name(candidate)
        if key in csv_prices:
            return (key, 1.0)

    best_key: Optional[str] = None
    best_ratio = 0.0
    for candidate in candidates:
        norm = normalize_name(candidate)
        for csv_key in csv_prices:
            ratio = SequenceMatcher(None, norm, csv_key).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_key = csv_key

    if best_key and best_ratio >= threshold:
        return (best_key, best_ratio)
    return None


def build_api_client() -> OrderEaseAPI:
    load_dotenv()
    return OrderEaseAPI(
        os.getenv("ORDEREASE_BASE_URL"),
        integration_key=os.getenv("ORDEREASE_INTEGRATION_KEY"),
        bearer_token=os.getenv("ORDEREASE_BEARER_TOKEN") or os.getenv("ORDEREASE_TOKEN"),
        v2_api_key=os.getenv("V2_API_Key"),
        client_key=os.getenv("ClientKey"),
        client_secret=os.getenv("ClientSecret"),
    )


def fetch_plant_products(api: OrderEaseAPI) -> List[Dict[str, Any]]:
    """Fetch all plant products via GetAllSimple."""
    result = api._make_request(
        "GET", "/api/SupplierInventory/GetAllSimple", unwrap_operation_result=True
    )
    return [p for p in (result or []) if p.get("Category", {}).get("Name") == "Plants"]


def run_preview(api: OrderEaseAPI, limit: int = 25) -> None:
    print("Fetching products...")
    products = fetch_plant_products(api)
    print(f"Total plant products: {len(products)}\n")

    for p in products[:limit]:
        sku = p.get("SupplierSKU", "?")
        desc = p.get("Description", "")
        size = p.get("OpenSizeDescription", "")
        price = p.get("CatalogPrice", 0)
        group, variety, branded = parse_description(desc)
        mapped_size = map_size(size) if size else None

        print(f"  SKU: {sku}")
        print(f"  Description: {desc}")
        print(f"  Group: {group or 'N/A'}, Variety: {variety}")
        if branded:
            print(f"  Branded: {' | '.join(branded)}")
        print(f"  Size: {size} -> {mapped_size or 'UNMAPPED'}")
        print(f"  Current price: {price}")
        print()

    if len(products) > limit:
        print(f"  ... showing {limit} of {len(products)}")


def run_update(
    api: OrderEaseAPI,
    csv_path: str,
    *,
    use_markup: bool = False,
    dry_run: bool = True,
    threshold: float = 0.80,
    batch_size: int = 200,
) -> None:
    mode = "DRY RUN" if dry_run else "LIVE UPDATE"
    label = "markup" if use_markup else "cost"
    print(f"\n{'='*60}")
    print(f"  {mode} - using {label} prices")
    print(f"{'='*60}\n")

    # 1) Parse CSV
    print(f"Reading CSV: {csv_path}")
    csv_prices = parse_pricing_csv(csv_path, use_markup=use_markup)
    print(f"  {len(csv_prices)} plant names loaded\n")

    # 2) Fetch products
    print("Fetching plant products from OrderEase...")
    products = fetch_plant_products(api)
    print(f"  {len(products)} plant products found\n")

    if not products:
        print("Nothing to do.")
        return

    # 3) Match
    matched: List[Dict[str, Any]] = []
    no_size_map: List[str] = []
    no_csv_match: List[str] = []
    no_size_price: List[str] = []
    price_unchanged: List[str] = []
    fuzzy_matches: List[Tuple[str, str, str, float]] = []

    for p in products:
        sku = p.get("SupplierSKU", "?")
        desc = p.get("Description", "")
        size_raw = p.get("OpenSizeDescription", "")
        current_price = float(p.get("CatalogPrice", 0) or 0)
        item_id = p.get("ItemId")
        group, variety, branded = parse_description(desc)

        csv_size = map_size(size_raw) if size_raw else None
        if not csv_size:
            no_size_map.append(f"{sku} ({variety}, size='{size_raw}')")
            continue

        result = find_csv_match(group, variety, branded, csv_prices, threshold=threshold)
        if not result:
            no_csv_match.append(f"{sku} ({group} - {variety})" if group else f"{sku} ({variety})")
            continue

        csv_key, confidence = result
        new_price = csv_prices[csv_key]["sizes"].get(csv_size)
        if new_price is None:
            no_size_price.append(f"{sku} ({variety} {csv_size})")
            continue

        if confidence < 1.0:
            fuzzy_matches.append((sku, variety, csv_prices[csv_key]["original"], confidence))

        if abs(current_price - new_price) < 0.001:
            price_unchanged.append(f"{sku} ({variety} {csv_size}) = ${new_price:.2f}")
            continue

        matched.append({
            "sku": sku,
            "item_id": item_id,
            "variety": variety,
            "size": csv_size,
            "old_price": current_price,
            "new_price": new_price,
            "csv_name": csv_prices[csv_key]["original"],
            "confidence": confidence,
        })

    # 4) Report
    print("=" * 60)
    print("MATCHING RESULTS")
    print("=" * 60)
    print(f"  Total plant products:    {len(products)}")
    print(f"  Matched (price change):  {len(matched)}")
    print(f"  Price already correct:   {len(price_unchanged)}")
    print(f"  Unmapped size:           {len(no_size_map)}")
    print(f"  No CSV name match:       {len(no_csv_match)}")
    print(f"  No price for that size:  {len(no_size_price)}")
    print()

    if fuzzy_matches:
        print(f"Fuzzy matches ({len(fuzzy_matches)}):")
        for sku, pname, cname, conf in sorted(fuzzy_matches, key=lambda x: x[3]):
            print(f"  {sku:20s} '{pname}' -> CSV '{cname}' ({conf:.0%})")
        print()

    if no_csv_match:
        print(f"Unmatched products (first 25 of {len(no_csv_match)}):")
        for entry in no_csv_match[:25]:
            print(f"  {entry}")
        print()

    if no_size_map:
        print(f"Unmapped sizes (first 10 of {len(no_size_map)}):")
        for entry in no_size_map[:10]:
            print(f"  {entry}")
        print()

    if matched:
        print(f"Price changes ({len(matched)}):")
        for m in sorted(matched, key=lambda x: x["variety"]):
            arrow = "+" if m["new_price"] > m["old_price"] else "-"
            print(
                f"  {m['sku']:20s} {m['variety'][:28]:28s} {m['size']:5s}  "
                f"${m['old_price']:>8.2f} -> ${m['new_price']:>8.2f}  ({arrow})"
            )
        print()

    if not matched:
        print("No price changes needed.")
        return

    if dry_run:
        print(f"DRY RUN complete. Re-run with --apply to push {len(matched)} changes.\n")
        return

    # 5) Push updates
    updates = [{"itemId": int(m["item_id"]), "netPrice": m["new_price"]} for m in matched]
    total_batches = (len(updates) + batch_size - 1) // batch_size
    print(f"Pushing {len(updates)} price updates in {total_batches} batch(es)...")

    ok = 0
    failed = 0
    for i, batch in enumerate(_chunked(updates, batch_size), 1):
        try:
            api.update_pricing(batch)
            ok += len(batch)
        except OrderEaseAPIError as e:
            failed += len(batch)
            print(f"  Batch {i}/{total_batches} FAILED: {e}")
        print(f"  Batch {i}/{total_batches}: ok={ok}, failed={failed}")

    print(f"\nDone. Updated: {ok}, Failed: {failed}")


def main():
    parser = argparse.ArgumentParser(description="Update OrderEase product costs from pricing CSV")
    parser.add_argument("--csv", default=CSV_PATH, help="Path to pricing CSV")
    parser.add_argument("--apply", action="store_true", help="Push updates (default is dry run)")
    parser.add_argument("--preview", action="store_true", help="Show products with parsed names/sizes")
    parser.add_argument("--use-markup", action="store_true", help="Use markup price columns")
    parser.add_argument("--threshold", type=float, default=0.80, help="Fuzzy match threshold (0-1)")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--limit", type=int, default=25, help="Limit for --preview mode")
    args = parser.parse_args()

    api = build_api_client()

    if args.preview:
        run_preview(api, limit=args.limit)
    else:
        run_update(
            api,
            args.csv,
            use_markup=args.use_markup,
            dry_run=not args.apply,
            threshold=args.threshold,
            batch_size=args.batch_size,
        )


if __name__ == "__main__":
    main()
