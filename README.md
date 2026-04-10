# Uber Rides Toolkit

Two Python scripts to **download** all your Uber ride receipts as PDFs and **extract** trip data into a structured Excel summary.

## What It Does

| Script | Purpose |
|--------|---------|
| `download_invoices.py` | Opens a browser, lets you log in to Uber, then automatically downloads all ride receipt PDFs for a date range |
| `extract_to_excel.py` | Parses the downloaded PDFs and generates an Excel file with 16 fields per trip |

### Extracted Fields

| Field | Description |
|-------|-------------|
| Date | Trip date (normalized to DD/MM/YYYY) |
| Departure Time | Pickup time |
| Driver | Driver name |
| License Plate | Vehicle registration |
| From / To | Pickup and dropoff addresses |
| Distance (km) | Trip distance |
| Duration (min) | Trip duration |
| Suggested Fare | Base fare before adjustments |
| Discount | Promotions applied |
| Amount Paid | Final amount charged |
| GST | Tax amount (or N/A if not applicable) |
| Payment Method | Cash / UPI / Card etc. |
| Payment Status | Success / Failed |
| Driver Rating | Rating shown on receipt |

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/uber-rides-toolkit.git
cd uber-rides-toolkit

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install Playwright browser (first time only)
playwright install chromium
```

## Usage

### Step 1: Download Receipts

Edit the date range in `download_invoices.py` (lines 22-23) if needed:

```python
DATE_FROM = date(2025, 4, 1)
DATE_TO = date(2026, 3, 31)
```

Then run:

```bash
python download_invoices.py
```

1. A Chrome window opens → log in to your Uber account
2. Switch back to terminal → press **Enter**
3. The script downloads all receipts into `uber_invoices/YYYY-MM/` folders
4. Failed downloads are auto-retried once

### Step 2: Extract to Excel

```bash
python extract_to_excel.py --folder ./uber_invoices --output uber_trips_summary.xlsx
```

This generates an Excel file with:
- One row per trip, sorted by date
- Auto-formatted columns with currency formatting
- A TOTAL row with SUM formulas
- Bold/highlighted header row

## How It Works

### Downloader (`download_invoices.py`)

- Uses [Playwright](https://playwright.dev/python/) in headful mode (visible browser)
- You log in manually — the script never touches your credentials
- Collects trip IDs during pagination (handles Uber's virtualized DOM)
- For each trip: opens detail page → clicks "View Receipt" → clicks "Download PDF"
- Reads the actual date from each receipt to filter by date range
- Random delays between actions to avoid bot detection
- Auto-retries all failed downloads

### Extractor (`extract_to_excel.py`)

- Handles two major Uber receipt PDF formats (Format A: older, Format B: newer)
- Parses dates in 4+ different formats (day-first, month-first, abbreviated, full)
- Detects multiple payment methods including failed UPI attempts
- Deduplicates by (date + amount + license plate)
- Falls back gracefully for missing fields

## Requirements

- Python 3.8+
- macOS / Linux / Windows
- A valid Uber account with ride history

## License

MIT
