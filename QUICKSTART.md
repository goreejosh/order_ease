# Quick Start Guide - OrderEase Inventory Manager

Get your inventory loaded into OrderEase in 5 minutes!

## Step 1: Install Dependencies

```bash
cd /Users/joshg/OrderEase

# Create and activate a virtual environment (recommended on macOS/Homebrew Python)
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies into the venv
python -m pip install -r requirements.txt
```

## Step 2: Configure Your Credentials

Create a `.env` file with your OrderEase credentials:

```bash
# Copy the template content
cat env_template.txt > .env

# Edit the .env file with your actual credentials
# You can use any text editor, for example:
nano .env
# or
open -e .env
```

Your `.env` file should look like:

```env
V2_API_Key=YOUR_V2_API_KEY_HERE
ClientKey=YOUR_CLIENT_KEY_HERE
ClientSecret=YOUR_CLIENT_SECRET_HERE
ORDEREASE_BASE_URL=https://api.orderease.com
ORDEREASE_SUPPLIER_ID=12345
```

**Where to get these values:**
- Contact OrderEase support or check your OrderEase admin panel
- OrderEase should provide you `V2_API_Key`, `ClientKey`, and `ClientSecret`
- Supplier ID is your company/organization ID in OrderEase

## Step 3: Test Your Connection

Run the script:

```bash
python orderease_inventory_manager.py
```

Choose option `1` to list all inventories and verify your API connection works.

## Step 4: Explore Your Current Setup

Before adding new inventory, see what you already have:

- **Option 2**: List all catalogs
- **Option 3**: Show supplier inventory items

This helps you understand your existing structure.

## Step 5: Load Your Inventory

### Option A: Create from CSV (Recommended for bulk import)

1. Prepare your CSV file using `example_inventory.csv` as a template
2. Run the script and choose option `5`
3. Enter the path to your CSV file
4. Enter a name for your inventory

### Option B: Create manually

1. Run the script and choose option `4`
2. Enter inventory name and description
3. The inventory will be created with today's date

### Option C: Import from the plants feed (Michaels Wholesale Nursery)

This pulls products from the public JSON feed and creates/updates SKUs in OrderEase:

- Source: `https://michaelswholesalenursery.com/data/plants.json`
- Run the script and choose option `6`
- Use a small limit first (e.g. 25) to validate your setup

## Common Issues

### "Missing required environment variables"
- Make sure you created the `.env` file
- Verify the file is in the same directory as the script
- Check that all required fields are filled in

### "API Error: 401 Unauthorized"
- Your integration key is incorrect or expired
- Contact OrderEase to verify your credentials

### "API Error: 404 Not Found"
- Your base URL might be wrong
- Verify the URL with OrderEase support
- Make sure you're using the correct API endpoint for your instance

## Next Steps

After loading your inventory:

1. **Assign to Catalogs**: Link your inventory to specific catalogs
2. **Set Quantities**: Update available quantities for each item
3. **Configure Sales Channels**: Set up where your inventory is available
4. **Review and Test**: Verify your inventory is showing correctly in OrderEase

## Need Help?

- Check the full `README.md` for detailed documentation
- Review the `orderease_api.json` for complete API reference
- Contact OrderEase support for account-specific questions

## Understanding the Workflow

```
Your Data (CSV/Manual)
         ↓
   Create Inventory
         ↓
  Get Catalog Item IDs
         ↓
Assign Products to Inventory
         ↓
   Link to Sales Channels
         ↓
    Your inventory is live! 🎉
```

## Pro Tips

✅ **Start small**: Test with a few items first before bulk importing
✅ **Verify SKUs**: Make sure your SKUs match what's in OrderEase catalogs
✅ **Keep backups**: Save your CSV files as backups
✅ **Check regularly**: Monitor inventory sync status in OrderEase admin panel

## Example Session

```bash
$ python orderease_inventory_manager.py

============================================================
OrderEase Inventory Manager - v2 API
============================================================

============================================================
What would you like to do?
============================================================
1. List all inventories
2. List all catalogs
3. Show supplier inventory items
4. Create new inventory
5. Create inventory from CSV
6. Import products from plants.json feed
7. Exit

Enter your choice (1-7): 3

📦 Exporting supplier products for supplier 12345...

✅ Found 150 inventory items:

  1. SKU: WIDGET-001
     Description: Premium Widget Blue
     Quantity: 100
     Net Price: 29.99

  2. SKU: WIDGET-002
     Description: Premium Widget Red
     Quantity: 150
     Net Price: 29.99
     
  ... and 148 more items
```

Happy inventory managing! 🚀

