#!/usr/bin/env python3
"""
Uber Rides Invoice Downloader
Downloads all Uber Rides receipts/invoices as PDFs for a specified date range.
Uses Playwright with a visible browser — you log in manually, then the script takes over.

Usage:
  python download_invoices.py

Configure DATE_FROM / DATE_TO below before running.
"""

import os
import re
import sys
import time
import random
import logging
from datetime import datetime, date
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Configuration — change these to your desired date range
# ---------------------------------------------------------------------------
DATE_FROM = date(2025, 4, 1)
DATE_TO = date(2026, 3, 31)
OUTPUT_DIR = Path("uber_invoices")
DEBUG_DIR = Path("debug_dumps")
LOG_FILE = "download_log.txt"
TRIPS_URL = "https://riders.uber.com/trips"
BASE_URL = "https://riders.uber.com"
SLOW_MO = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def human_delay(lo: float = 1.5, hi: float = 3.5) -> None:
    """Random delay to mimic human behavior and avoid bot detection."""
    time.sleep(random.uniform(lo, hi))


def short_delay(lo: float = 0.5, hi: float = 1.2) -> None:
    time.sleep(random.uniform(lo, hi))


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("uber_downloader")
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(message)s"))
    ch.setLevel(logging.INFO)
    logger.addHandler(ch)
    return logger


def parse_receipt_date(text: str):
    """
    Parse date from the receipt modal header.
    Examples: 'Apr 6, 2026', 'Dec 15, 2025', 'January 3, 2026'
    """
    patterns = [
        (r"(\w{3,9}\s+\d{1,2},?\s+\d{4})", ["%b %d, %Y", "%b %d %Y", "%B %d, %Y", "%B %d %Y"]),
        (r"(\d{1,2}\s+\w{3,9},?\s+\d{4})", ["%d %b, %Y", "%d %b %Y", "%d %B, %Y", "%d %B %Y"]),
    ]
    for pat, fmts in patterns:
        m = re.search(pat, text)
        if m:
            for fmt in fmts:
                try:
                    return datetime.strptime(m.group(1).strip(), fmt).date()
                except ValueError:
                    continue
    return None


def parse_full_date(text: str):
    """
    Parse full date from detail page header.
    Examples:
      '2:49 PM, Monday April 6 2026 with ANOOP'
      '10:15 AM, Tuesday 15 January 2025 with RAJ'
    """
    patterns = [
        (r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(\w+\s+\d{1,2}\s+\d{4})",
         ["%B %d %Y", "%b %d %Y"]),
        (r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(\d{1,2}\s+\w+\s+\d{4})",
         ["%d %B %Y", "%d %b %Y"]),
        (r"(\w+\s+\d{1,2},?\s+\d{4})", ["%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"]),
        (r"(\d{1,2}\s+\w+\s+\d{4})", ["%d %B %Y", "%d %b %Y"]),
    ]
    for pat, fmts in patterns:
        m = re.search(pat, text)
        if m:
            for fmt in fmts:
                try:
                    return datetime.strptime(m.group(1).strip(), fmt).date()
                except ValueError:
                    continue
    return None


def dump_page_debug(page, name: str) -> None:
    """Save screenshot + HTML for debugging failed extractions."""
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def wait_for_login(page) -> None:
    """Navigate to Uber and wait for user to log in manually."""
    page.goto(BASE_URL, wait_until="domcontentloaded")
    human_delay(2, 4)

    input(
        "\n╔══════════════════════════════════════════════════════════╗\n"
        "║  Please log in to Uber in the browser window.          ║\n"
        "║  After you are fully logged in, come back here and     ║\n"
        "║  press Enter to continue...                            ║\n"
        "╚══════════════════════════════════════════════════════════╝\n"
    )

    current = page.url
    if "auth" in current or "login" in current:
        print("⚠  Still on a login page — waiting 15 s for redirect…")
        try:
            page.wait_for_url(lambda u: "auth" not in u and "login" not in u, timeout=15_000)
        except PlaywrightTimeout:
            print("⚠  Could not confirm login, continuing anyway…")

    print("✓ Login detected. Proceeding…\n")


def extract_visible_trips(page) -> list:
    """Extract trip cards currently visible in the DOM."""
    return page.evaluate("""
        () => {
            const results = [];
            const cards = document.querySelectorAll('div[href*="/trips/"]');
            for (const card of cards) {
                const href = card.getAttribute('href') || '';
                const match = href.match(/\\/trips\\/([a-f0-9-]+)/i);
                if (!match) continue;
                results.push({
                    href: href,
                    tripId: match[1],
                    text: (card.innerText || '').trim().substring(0, 600)
                });
            }
            return results;
        }
    """)


def load_and_collect_all_trips(page, logger) -> list:
    """
    Navigate to trips page, collect trip IDs during pagination.
    Uber virtualizes the DOM — old cards disappear as new ones load,
    so we collect on every "More" click rather than at the end.
    """
    page.goto(TRIPS_URL, wait_until="domcontentloaded")
    human_delay(3, 5)

    all_trips = {}  # tripId -> trip dict
    iteration = 0

    while True:
        iteration += 1

        # Extract currently visible trips
        visible = extract_visible_trips(page)
        new_count = 0
        for t in visible:
            tid = t["tripId"]
            if tid not in all_trips:
                text = t["text"]
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                destination = lines[0] if lines else "Unknown"
                is_cancelled = "cancelled" in text.lower()

                fare = None
                for line in lines:
                    fare_match = re.search(r"[₹$€£]\s*[\d,.]+", line)
                    if fare_match:
                        fare = fare_match.group()

                all_trips[tid] = {
                    "href": t["href"],
                    "trip_id": tid,
                    "fare": fare,
                    "destination": destination,
                    "is_cancelled": is_cancelled,
                }
                new_count += 1

        logger.info(f"  Round {iteration}: {len(visible)} visible, {new_count} new (total: {len(all_trips)})")

        # Click "More" button to load next batch
        more_btn = page.locator('button[aria-label="More"], button:has-text("More")')
        try:
            if more_btn.first.is_visible(timeout=3000):
                more_btn.first.scroll_into_view_if_needed()
                short_delay()
                more_btn.first.click()
                human_delay(2, 4)
                continue
        except (PlaywrightTimeout, Exception):
            pass

        # Fallback: try scrolling
        prev_h = page.evaluate("document.body.scrollHeight")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        human_delay(2, 3)
        new_h = page.evaluate("document.body.scrollHeight")

        if new_h == prev_h and new_count == 0:
            logger.info("  No more trips to load.")
            break

        if iteration > 200:
            logger.info("  Safety limit. Stopping.")
            break

    logger.info(f"Total unique trips collected: {len(all_trips)}")
    return list(all_trips.values())


def process_trip(page, context, trip: dict, index: int, total: int, logger) -> str:
    """
    Navigate to trip detail page:
    1. Click 'View Receipt' to open receipt modal
    2. Read the date from the modal
    3. Check if trip falls within date range
    4. Click 'Download PDF' to get the actual Uber receipt
    5. Save into month-wise folder: uber_invoices/YYYY-MM/

    Returns: 'success', 'no_invoice', 'out_of_range', 'too_old', or 'failed'.
    """
    trip_id = trip["trip_id"]
    dest = trip["destination"]

    detail_url = trip["href"]
    if not detail_url.startswith("http"):
        detail_url = BASE_URL + detail_url

    for attempt in range(2):
        try:
            page.goto(detail_url, wait_until="domcontentloaded")
            human_delay(2, 4)

            # ── Step 1: Click "View Receipt" ──
            view_receipt = page.locator(
                'a:has-text("View Receipt"), '
                'button:has-text("View Receipt"), '
                'a:has-text("View receipt"), '
                'button:has-text("View receipt")'
            ).first

            try:
                view_receipt.wait_for(state="visible", timeout=6000)
            except PlaywrightTimeout:
                if attempt == 0:
                    logger.debug(f"  No 'View Receipt' button (attempt 1), retrying…")
                    human_delay(1, 2)
                    continue
                logger.info(f"  [{index}/{total}] SKIP — no receipt for {trip_id[:8]} | {dest}")
                return "no_invoice"

            logger.debug(f"  Clicking 'View Receipt'…")
            short_delay()
            view_receipt.click()
            human_delay(2, 4)

            # ── Step 2: Read date from receipt modal ──
            receipt_text = ""
            try:
                modal = page.locator(
                    'div[role="dialog"], '
                    'div[aria-modal="true"], '
                    'div:has(>> text="Receipt")'
                ).first
                receipt_text = modal.inner_text(timeout=5000)
            except Exception:
                try:
                    receipt_text = page.evaluate("() => document.body.innerText.substring(0, 3000)")
                except Exception:
                    pass

            trip_date = parse_receipt_date(receipt_text)

            # Fallback: try detail page header date
            if not trip_date:
                try:
                    page_text = page.evaluate("() => document.body.innerText.substring(0, 2000)")
                    trip_date = parse_full_date(page_text)
                except Exception:
                    pass

            if not trip_date:
                logger.info(f"  [{index}/{total}] ⚠ No date found for {trip_id[:8]} | {dest}")
                dump_page_debug(page, f"no_date_{trip_id[:12]}")
            else:
                logger.debug(f"  Receipt date: {trip_date}")
                if trip_date > DATE_TO:
                    logger.info(f"  [{index}/{total}] SKIP (after range) {trip_date} | {dest}")
                    page.keyboard.press("Escape")
                    return "out_of_range"
                if trip_date < DATE_FROM:
                    logger.info(f"  [{index}/{total}] STOP — {trip_date} is before {DATE_FROM}")
                    page.keyboard.press("Escape")
                    return "too_old"

            # Build filename & month folder
            if trip_date:
                date_str = trip_date.strftime("%Y-%m-%d")
                month_folder = OUTPUT_DIR / trip_date.strftime("%Y-%m")
            else:
                date_str = "unknown-date"
                month_folder = OUTPUT_DIR / "unknown"

            month_folder.mkdir(parents=True, exist_ok=True)
            safe_dest = re.sub(r'[^\w\s-]', '', dest)[:30].strip().replace(' ', '_')
            filename = f"{date_str}_{safe_dest}_{trip_id[:8]}.pdf"
            filepath = month_folder / filename

            if filepath.exists():
                logger.info(f"  [{index}/{total}] Already exists: {filename}")
                page.keyboard.press("Escape")
                return "success"

            logger.info(f"  [{index}/{total}] {date_str} | {dest} | {trip.get('fare', '?')}")

            # ── Step 3: Click "Download PDF" ──
            download_pdf_btn = page.locator(
                'a:has-text("Download PDF"), '
                'button:has-text("Download PDF"), '
                'a:has-text("Download pdf"), '
                'button:has-text("Download pdf")'
            ).first

            try:
                download_pdf_btn.wait_for(state="visible", timeout=8000)
            except PlaywrightTimeout:
                logger.debug("  'Download PDF' not found, dumping page…")
                dump_page_debug(page, f"no_download_btn_{trip_id[:12]}")
                if attempt == 0:
                    page.keyboard.press("Escape")
                    human_delay(1, 2)
                    continue
                logger.info(f"  SKIP — no 'Download PDF' button")
                return "no_invoice"

            logger.debug(f"  Clicking 'Download PDF'…")
            short_delay()

            try:
                with page.expect_download(timeout=30_000) as download_info:
                    download_pdf_btn.click()

                download = download_info.value
                download.save_as(str(filepath))
                logger.info(f"  ✓ Saved: {month_folder.name}/{filename}")

                page.keyboard.press("Escape")
                short_delay()
                return "success"

            except PlaywrightTimeout:
                logger.debug("  Download not triggered, checking for new tab…")
                pages = context.pages
                if len(pages) > 1:
                    new_page = pages[-1]
                    new_page.wait_for_load_state("domcontentloaded")
                    human_delay(1, 2)
                    try:
                        dl_btn = new_page.locator(
                            'a:has-text("Download PDF"), button:has-text("Download PDF")'
                        ).first
                        if dl_btn.is_visible(timeout=3000):
                            with new_page.expect_download(timeout=30_000) as dl_info:
                                dl_btn.click()
                            dl = dl_info.value
                            dl.save_as(str(filepath))
                            logger.info(f"  ✓ Saved (new tab): {month_folder.name}/{filename}")
                            new_page.close()
                            return "success"
                    except Exception:
                        pass
                    new_page.close()

                if attempt == 0:
                    logger.debug("  Retrying…")
                    continue
                logger.info(f"  FAIL — download not triggered")
                return "failed"

        except Exception as e:
            if attempt == 0:
                logger.debug(f"  Error: {e}. Retrying…")
                human_delay(1, 2)
                continue
            logger.info(f"  FAIL — {e}")
            return "failed"

    return "failed"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    DEBUG_DIR.mkdir(exist_ok=True)
    logger = setup_logging()

    logger.info("=" * 60)
    logger.info("Uber Rides Invoice Downloader")
    logger.info(f"Date range: {DATE_FROM} → {DATE_TO}")
    logger.info("=" * 60)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            slow_mo=SLOW_MO,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # Step 1 — Manual login
        wait_for_login(page)

        # Step 2 — Load ALL trips (no date filtering — dates read from detail page)
        logger.info("Loading trips…")
        all_trips = load_and_collect_all_trips(page, logger)

        if not all_trips:
            logger.info("No trips found on page.")
            input("\nPress Enter to close…")
            browser.close()
            return

        # Skip cancelled trips
        trips = [t for t in all_trips if not t["is_cancelled"]]
        cancelled = len(all_trips) - len(trips)
        logger.info(f"Total: {len(all_trips)} | Skipping {cancelled} cancelled | Processing: {len(trips)}")

        # Step 3 — Visit each trip detail, read date, download if in range
        logger.info(f"\nProcessing {len(trips)} trips (date checked on each detail page)…\n")
        results = {"success": 0, "no_invoice": 0, "failed": 0, "out_of_range": 0, "too_old": 0}
        total = len(trips)
        visited = 0
        failed_trips = []
        skipped_too_old = 0

        for i, trip in enumerate(trips, start=1):
            visited += 1
            status = process_trip(page, context, trip, i, total, logger)
            results[status] = results.get(status, 0) + 1

            if status == "failed":
                failed_trips.append(trip)

            if status == "too_old":
                skipped_too_old = total - i
                logger.info(f"  Remaining {skipped_too_old} trips are before {DATE_FROM}. Stopping.")
                break

            human_delay()

        # ── RETRY all failed trips ──
        if failed_trips:
            logger.info("")
            logger.info("=" * 60)
            logger.info(f"RETRYING {len(failed_trips)} failed trips…")
            logger.info("=" * 60)
            retry_success = 0
            retry_still_failed = 0

            for j, trip in enumerate(failed_trips, start=1):
                logger.info(f"  Retry {j}/{len(failed_trips)}: {trip['trip_id'][:8]} | {trip['destination']}")
                human_delay(2, 4)
                status = process_trip(page, context, trip, j, len(failed_trips), logger)
                if status == "success":
                    retry_success += 1
                    results["success"] += 1
                    results["failed"] -= 1
                else:
                    retry_still_failed += 1
                human_delay()

            logger.info(f"  Retry results: {retry_success} recovered, {retry_still_failed} still failed")

        # Summary
        total_accounted = (results['success'] + results['no_invoice'] +
                          results['out_of_range'] + results['too_old'] +
                          results['failed'])
        logger.info("")
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(f"  Total non-cancelled  : {total}")
        logger.info(f"  Visited              : {visited}")
        logger.info(f"  Downloaded           : {results['success']}")
        logger.info(f"  No receipt (skip)    : {results['no_invoice']}")
        logger.info(f"  Out of range (skip)  : {results['out_of_range']}")
        logger.info(f"  Too old (stopped)    : {results['too_old']}")
        logger.info(f"  Skipped (too old)    : {skipped_too_old}")
        logger.info(f"  Failed (after retry) : {results['failed']}")
        logger.info(f"  Cancelled (skipped)  : {cancelled}")
        logger.info(f"  Accounted total      : {total_accounted + skipped_too_old + cancelled}")
        logger.info(f"  Files in             : {OUTPUT_DIR.resolve()}")
        logger.info("=" * 60)

        input("\nPress Enter to close the browser…")
        browser.close()

    print("\nDone. Check uber_invoices/ and download_log.txt.")


if __name__ == "__main__":
    main()
