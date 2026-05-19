"""
geocode.py
==========
Reads cornwall_beaches.csv, geocodes every beach by searching Google Maps
in a headless browser, and writes beaches.json ready to embed in index.html.

Why Google Maps instead of Nominatim:
  - Far better coverage of named beaches and coves
  - Understands natural language queries like "Kynance Cove, Cornwall"
  - No API key, no rate-limit headers to worry about
  - Coordinates come directly from the URL after Google Maps resolves the search

Strategy (tried in order until one succeeds):
  1. Already a lat,lng in the CSV location column → use directly, no search
  2. Search Google Maps for the beach name + "Cornwall"
  3. If that URL doesn't contain coords, search the raw location string
  4. Mark as unresolved if all attempts fail

Progress is saved to the output file after every beach, so you can Ctrl-C
and resume without losing work (already-resolved beaches are skipped).

Usage:
    python geocode.py
    python geocode.py --input cornwall_beaches.csv --output beaches.json
    python geocode.py --delay 2.0   # seconds between searches (default 2.0)
    python geocode.py --headed      # show the browser window (useful for debugging)
"""

import asyncio
import csv
import json
import re
import argparse
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

# ── Constants ─────────────────────────────────────────────────────────────────

LATLON_RE = re.compile(r'@(-?\d+\.\d+),(-?\d+\.\d+)')

# Also catch the ll= param style that sometimes appears
LATLON_LL_RE = re.compile(r'[?&]ll=(-?\d+\.\d+),(-?\d+\.\d+)')

# Sanity bounds: Cornwall is roughly 49.9–50.9 N, 4.1–5.8 W
LAT_MIN, LAT_MAX =  49.5,  51.5
LNG_MIN, LNG_MAX = -6.5,  -3.5

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ── Coordinate helpers ────────────────────────────────────────────────────────

def is_latlon(s: str) -> Optional[tuple[float, float]]:
    """Return (lat, lng) if string already contains a valid Cornish coord pair."""
    if not s:
        return None
    for pattern in (LATLON_RE, LATLON_LL_RE):
        m = pattern.search(s.strip())
        if m:
            lat, lng = float(m.group(1)), float(m.group(2))
            if LAT_MIN < lat < LAT_MAX and LNG_MIN < lng < LNG_MAX:
                return lat, lng
    return None


def extract_coords_from_url(url: str) -> Optional[tuple[float, float]]:
    """Pull lat,lng from a Google Maps URL, with Cornwall sanity check."""
    for pattern in (LATLON_RE, LATLON_LL_RE):
        m = pattern.search(url)
        if m:
            lat, lng = float(m.group(1)), float(m.group(2))
            if LAT_MIN < lat < LAT_MAX and LNG_MIN < lng < LNG_MAX:
                return lat, lng
    return None


# ── Google Maps search ────────────────────────────────────────────────────────

async def search_google_maps(page: Page, query: str, delay: float) -> Optional[tuple[float, float]]:
    """
    Search Google Maps for `query`, wait for the URL to settle on a result
    page, and extract coordinates from the URL.

    Google Maps search URLs look like:
      https://www.google.com/maps/search/Kynance+Cove+Cornwall/
    After resolving, the URL becomes:
      https://www.google.com/maps/place/Kynance+Cove/@49.976,-5.231,15z/...
    The @lat,lng is what we want.
    """
    search_url = (
        "https://www.google.com/maps/search/"
        + query.replace(" ", "+")
    )

    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)

        # Accept cookies if the consent dialog appears (EU/UK)
        try:
            btn = page.locator('button:has-text("Accept all"), button:has-text("Reject all")')
            if await btn.first.is_visible(timeout=2000):
                # Accept so the map loads fully
                await page.locator('button:has-text("Accept all")').first.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        # Wait for the URL to contain coordinates — Google Maps always puts
        # @lat,lng in the URL once it resolves a place
        for _ in range(20):
            current_url = page.url
            coords = extract_coords_from_url(current_url)
            if coords:
                return coords
            await page.wait_for_timeout(500)

        # If URL never got coords, try reading from the page's canonical link
        # (sometimes Maps uses a different URL structure on first load)
        canonical = await page.evaluate(
            "() => document.querySelector('link[rel=canonical]')?.href || ''"
        )
        if canonical:
            coords = extract_coords_from_url(canonical)
            if coords:
                return coords

    except PWTimeout:
        print(f"    Timeout searching for: {query}")
    except Exception as e:
        print(f"    Error searching for '{query}': {e}")

    await asyncio.sleep(delay)
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(input_csv: str, output_json: str, delay: float, headed: bool):

    # Load existing output if present (for resuming interrupted runs)
    existing: dict[str, dict] = {}
    if Path(output_json).exists():
        with open(output_json, encoding="utf-8") as f:
            try:
                for b in json.load(f):
                    existing[b["name"]] = b
                print(f"Loaded {len(existing)} existing results from {output_json}")
            except json.JSONDecodeError:
                pass

    # Read input CSV
    with open(input_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"Read {len(rows)} beaches from {input_csv}\n")

    beaches: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        ctx     = await browser.new_context(
            user_agent=USER_AGENT,
            locale="en-GB",
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()

        for i, row in enumerate(rows, 1):
            name     = row.get("beach_name", "").strip()
            location = row.get("location",   "").strip()

            print(f"[{i}/{len(rows)}] {name}")

            # ── Already resolved in a previous run? ──
            if name in existing and existing[name].get("lat") is not None:
                b = existing[name]
                print(f"  ✓ Already resolved: {b['lat']},{b['lng']}")
                beaches.append(b)
                continue

            lat, lng = None, None

            # ── Strategy 1: location column is already lat,lng ──
            coords = is_latlon(location)
            if coords:
                lat, lng = coords
                print(f"  ✓ Already have coords: {lat},{lng}")

            # ── Strategy 2: search by beach name + Cornwall ──
            if lat is None:
                query  = f"{name} Cornwall"
                print(f"  Searching Maps: '{query}'")
                coords = await search_google_maps(page, query, delay)
                if coords:
                    lat, lng = coords
                    print(f"  ✓ {lat},{lng}")
                    await asyncio.sleep(delay)

            # ── Strategy 3: search by raw location string ──
            if lat is None and location and not is_latlon(location):
                query  = location if "cornwall" in location.lower() else f"{location}, Cornwall"
                print(f"  Trying location string: '{query}'")
                coords = await search_google_maps(page, query, delay)
                if coords:
                    lat, lng = coords
                    print(f"  ✓ {lat},{lng}")
                    await asyncio.sleep(delay)

            if lat is None:
                print(f"  ✗ Could not resolve coordinates")

            beach = {
                "name":         name,
                "lat":          lat,
                "lng":          lng,
                "dog_friendly": row.get("dog_friendly", "No"),
                "cafe":         row.get("cafe",         "No"),
                "notes":        row.get("notes",        "").strip() or None,
                "url":          row.get("url",          "").strip(),
            }
            beaches.append(beach)

            # Save after every beach so progress isn't lost on interruption
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(beaches, f, indent=2, ensure_ascii=False)

        await browser.close()

    # Final summary
    resolved = sum(1 for b in beaches if b["lat"] is not None)
    missing  = [b["name"] for b in beaches if b["lat"] is None]

    print(f"\n{'─'*50}")
    print(f"✓ {resolved}/{len(beaches)} beaches geocoded")

    if missing:
        print(f"\n✗ {len(missing)} unresolved (will need manual coordinates):")
        for name in missing:
            print(f"    - {name}")
        print(
            "\nFor each, find it on Google Maps, right-click the pin, "
            "and copy the coordinates into beaches.json manually."
        )

    print(f"\n✓ Written to {output_json}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Geocode Cornwall beaches via Google Maps")
    parser.add_argument("--input",  default="cornwall_beaches.csv", help="Input CSV")
    parser.add_argument("--output", default="beaches.json",         help="Output JSON")
    parser.add_argument("--delay",  type=float, default=2.0,        help="Seconds between searches")
    parser.add_argument("--headed", action="store_true",            help="Show browser window")
    args = parser.parse_args()

    asyncio.run(run(args.input, args.output, args.delay, args.headed))
