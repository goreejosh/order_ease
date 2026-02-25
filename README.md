# OrderEase Inventory Manager

A Python tool for managing your inventory in OrderEase using their v2 API.

## Features

- ✅ List all inventories
- ✅ List all catalogs
- ✅ View supplier inventory items
- ✅ Create new inventories
- ✅ Bulk import from CSV files
- ✅ Interactive command-line interface

## Prerequisites

- Python 3.7+ (use `python3` on macOS)
- OrderEase v2 API credentials (`V2_API_Key`, `ClientKey`, `ClientSecret`)
- Your OrderEase Supplier ID

## Setup

### 0. Create a virtual environment (recommended)

If you’re using Homebrew Python on macOS, installing packages system-wide can fail with “externally-managed-environment” (PEP 668). Use a venv:

```bash
cd /Users/joshg/OrderEase
python3 -m venv .venv
source .venv/bin/activate
```

### 1. Install Dependencies

```bash
python -m pip install -r requirements.txt
```

### 2. Configure Environment

Create a `.env` file and add your credentials (use `env_template.txt` as a starting point):

```bash
cat env_template.txt > .env
```

Then edit `.env` and add your actual values:

```env
V2_API_Key=your_v2_api_key_here
ClientKey=your_client_key_here
ClientSecret=your_client_secret_here
ORDEREASE_BASE_URL=https://api.orderease.com
ORDEREASE_SUPPLIER_ID=your_supplier_id
```

### 3. Getting Your Credentials

You'll need to obtain the following from OrderEase:

- **Base URL**: The API endpoint (usually `https://api.orderease.com` or your specific instance URL)
- **V2_API_Key / ClientKey / ClientSecret**: v2 credentials for API access
- **Supplier ID**: Your supplier/company ID in OrderEase

Contact OrderEase support if you don't have these credentials yet.

## Usage

### Interactive Mode

Run the script to access the interactive menu:

```bash
python orderease_inventory_manager.py
```

You'll see a menu with options:

```
1. List all inventories
2. List all catalogs
3. Show supplier inventory items
4. Create new inventory
5. Create inventory from CSV
6. Import products from plants.json feed
7. Exit
```

### Option 1: List All Inventories

View all existing inventories in your OrderEase account.

### Option 2: List All Catalogs

View all catalogs for your supplier. This helps you understand what product catalogs are available.

### Option 3: Show Supplier Inventory Items

Display all inventory items for your supplier, including:
- SKU
- Description
- Quantity available
- Price

### Option 4: Create New Inventory

Create a new inventory by providing:
- Inventory name
- Description (optional)

The inventory will be created with the current date as the start date.

### Option 5: Create Inventory from CSV

Bulk import inventory from a CSV file. See `example_inventory.csv` for the required format:

```csv
sku,description,quantity,price,category
ITEM-001,"Premium Widget - Blue",100,29.99,Widgets
ITEM-002,"Premium Widget - Red",150,29.99,Widgets
```

**Note**: This feature creates the inventory structure. You'll need to separately assign actual product IDs to the inventory using the catalog API.

### Option 6: Import products from the plants feed

This pulls products from the Michaels Wholesale Nursery JSON feed and creates/updates items by SKU, then (optionally) bulk-adds them to a catalog:

- Feed: `https://michaelswholesalenursery.com/data/plants.json`
- Uses: `POST /api/SupplierOrder/LookupOrCreateItem` for create/update
- Optionally uses: `POST /api/SupplierInventory/AddToCatalog/byRef/{catalogRef}/sku` to add SKUs to a catalog

## CSV File Format

Your CSV file should include these columns:

- `sku`: Product SKU/Item number
- `description`: Product description
- `quantity`: Available quantity
- `price`: Product price
- `category`: Product category (optional)

See `example_inventory.csv` for a complete example.

## API Integration

This tool uses the OrderEase API v2.0 and supports the following endpoints:

### Inventory Endpoints
- `GET /api/Inventory/GetAllInventories` - List all inventories
- `GET /api/Inventory/GetInventory/{id}` - Get specific inventory
- `POST /api/Inventory/AddOrUpdateInventory` - Create/update inventory
- `POST /api/Inventory/AssignProductsToInventory/{inventoryId}` - Assign products
- `GET /api/Inventory/GetSupplierInventory/{supplierId}` - Get supplier items

### Catalog Endpoints
- `GET /api/Catalog/GetAll/{supplierId}` - List all catalogs
- `GET /api/Catalog/GetItem` - Get catalog items by SKU
- `POST /api/Catalog/AssignCatalogToInventories/{catalogId}` - Link catalog to inventories

## Workflow for Loading Inventory

Here's a recommended workflow for getting your inventory into OrderEase:

### Step 1: Verify Connection
```bash
python orderease_inventory_manager.py
# Choose option 1 to list inventories and verify API connection
```

### Step 2: Check Existing Catalogs
```bash
# Choose option 2 to see what catalogs already exist
```

### Step 3: Review Existing Inventory Items
```bash
# Choose option 3 to see what items are already in your supplier inventory
```

### Step 4: Create New Inventory
```bash
# Choose option 4 to create a new inventory
# Or choose option 5 to create from a CSV file
```

### Step 5: Assign Products
After creating an inventory, you'll need to:
1. Get the item IDs for your products from the catalog
2. Use the `AssignProductsToInventory` API to link them to your inventory

## Troubleshooting

### Authentication Errors
- Verify your `V2_API_Key`, `ClientKey`, `ClientSecret` values are correct
- Check that your integration key hasn't expired
- Ensure you're using the correct base URL

### Missing Data
- Confirm your `ORDEREASE_SUPPLIER_ID` is correct
- Verify you have permission to access the requested resources
- Check if you're looking at the right company/supplier

### API Errors
- Check the error message returned by the API
- Verify the API version (2.0) is supported by your OrderEase instance
- Look for rate limiting or quota issues

## Understanding OrderEase Structure

OrderEase has several key concepts:

- **Supplier**: Your company/organization in OrderEase
- **Catalog**: A collection of products available for sale
- **Inventory**: A snapshot of available products at a specific time/location
- **Items**: Individual products with SKUs, prices, and quantities
- **Company ID**: Unique identifier for your organization

## Advanced Usage

### Using the API Client Directly

You can also use the `OrderEaseAPI` class in your own scripts:

```python
from orderease_inventory_manager import OrderEaseAPI

api = OrderEaseAPI(
    base_url="https://api.orderease.com",
    integration_key="your_key_here"
)

# Get all inventories
inventories = api.get_all_inventories()

# Create an inventory
result = api.create_inventory(
    name="Spring 2024 Inventory",
    description="Spring product line"
)

# Get supplier inventory
items = api.get_supplier_inventory(supplier_id=12345)
```

## Support

For issues with:
- **This tool**: Check the error messages and troubleshooting section
- **OrderEase API**: Contact OrderEase support
- **Your credentials**: Contact your OrderEase administrator

## API Version

This tool is designed for OrderEase API **v2.0**. If you need to use a different version, you can modify the `api_version` parameter when initializing the `OrderEaseAPI` class.

## Security Notes

- Never commit your `.env` file to version control
- Keep your integration key secure
- Rotate your integration keys periodically
- Use environment variables for sensitive data

## License

This tool is provided as-is for use with OrderEase API integration.

