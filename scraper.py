"""
Cornwall Beach Scraper
======================
Extracts beach data from visitcornwall.com by parsing the __NEXT_DATA__ JSON
embedded in each page's HTML — no JS rendering needed for listing pages.

Playwright is only used for detail pages to follow Google Maps links and
extract lat/long coordinates. The LLM extracts address/location from the
page text when no map link is present.

Usage:
    # Start your MLX model server first:
    #   mlx_lm.server --model prism-ml/Ternary-Bonsai-8B-mlx-2bit --port 8080

    python scraper.py [--model MODEL] [--api-url URL] [--output FILE] [--delay SECS]
"""

import asyncio
import csv
import json
import re
import argparse
from dataclasses import dataclass
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL    = "https://www.visitcornwall.com"
BEACHES_URL = "https://www.visitcornwall.com/things-to-do/beaches"

DEFAULT_MODEL   = "prism-ml/Ternary-Bonsai-8B-mlx-2bit"
DEFAULT_API_URL = "http://127.0.0.1:8080/v1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

LOCATION_SYSTEM_PROMPT = """\
You are a data extraction assistant for a beach information website.
Extract location and notes from the page text provided.

RULES:
- Respond with ONLY a valid JSON object — no markdown, no explanation, no other text.

- location: if coordinates are visible (e.g. prefixed '[COORDINATES FROM MAP: ...]'),
  return them as 'lat,lng'. Otherwise return the most specific address or postcode
  you can find in the text. Return null if no location information is present.

- notes: extract FULL SPECIFIC DETAIL — do not summarise or paraphrase.
  Always preserve: exact dates (e.g. '1 May to 30 September'), exact times
  (e.g. '10am to 6pm'), and any named areas the restriction applies to.
  If both dog restrictions and cafe information are present, include both.
  Return null only if genuinely no useful detail is present.

  GOOD: "Dogs banned 10am-6pm from 1 May-30 Sep on the main beach; dogs on leads
  permitted on the rocks year-round. Seasonal beach cafe and ice cream kiosk."

  BAD: "Seasonal dog ban applies. Cafe on site."

OUTPUT FORMAT:
{
  "location": string or null,
  "notes": string or null
}"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Beach:
    title:        str            = ""
    slug:         str            = ""
    location:     Optional[str]  = None
    dog_friendly: str            = "No"   # Yes | No | Seasonal
    cafe:         str            = "No"   # Yes | No
    notes:        Optional[str]  = None
    url:          str            = ""

    def csv_row(self) -> dict:
        return {
            "beach_name":   self.title,
            "location":     self.location or "",
            "dog_friendly": self.dog_friendly,
            "cafe":         self.cafe,
            "notes":        self.notes or "",
            "url":          self.url,
        }


# ---------------------------------------------------------------------------
# Step 1: Extract all beaches from __NEXT_DATA__ across all listing pages
# ---------------------------------------------------------------------------

def extract_next_data(html: str) -> dict:
    """Parse the __NEXT_DATA__ JSON from a page's HTML."""
    soup = BeautifulSoup(html, "lxml")
    tag  = soup.find("script", {"id": "__NEXT_DATA__"})
    if not tag:
        raise ValueError("No __NEXT_DATA__ script tag found")
    return json.loads(tag.string)


def parse_beach_item(item: dict) -> Beach:
    """
    Convert a raw beach item from __NEXT_DATA__ initialItems into a Beach.
    globalTags contains slugs like 'dog-friendly', 'seasonal-dog-ban', 'refreshments'.
    """
    tags = {t["slug"] for t in item.get("globalTags", [])}

    if "seasonal-dog-ban" in tags:
        dog = "Seasonal"
    elif "dog-friendly" in tags:
        dog = "Yes"
    else:
        dog = "No"

    cafe = "Yes" if "refreshments" in tags else "No"
    slug = item.get("slug", "")

    return Beach(
        title        = item.get("title", "").strip(),
        slug         = slug,
        dog_friendly = dog,
        cafe         = cafe,
        url          = f"{BASE_URL}/things-to-do/beaches/{slug}",
    )


async def fetch_all_listing_pages(base_url: str) -> list[Beach]:
    """
    Fetch all listing pages using plain HTTP (no JS rendering needed —
    all beach data lives in the __NEXT_DATA__ script tag).

    Page 1 is at base_url; subsequent pages are at base_url?page=N.
    The total page count comes from initialPageCount in the first page's JSON.
    """
    beaches: dict[str, Beach] = {}  # slug → Beach, deduplicates automatically

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as client:

        # Page 1
        print("  Fetching listing page 1...")
        r = await client.get(base_url)
        r.raise_for_status()
        data        = extract_next_data(r.text)
        page_data   = data["props"]["pageProps"]["pageData"]
        items       = page_data["initialItems"]
        page_count  = page_data["initialPageCount"]

        for item in items:
            b = parse_beach_item(item)
            if b.slug and b.slug not in beaches:
                beaches[b.slug] = b
        print(f"  Page 1/{page_count}: {len(items)} beaches")

        # Pages 2..N
        for page_num in range(2, page_count + 1):
            url = f"{base_url}?page={page_num}"
            print(f"  Fetching listing page {page_num}/{page_count}...")
            r = await client.get(url)
            r.raise_for_status()

            data  = extract_next_data(r.text)
            items = data["props"]["pageProps"]["pageData"]["initialItems"]

            new = 0
            for item in items:
                b = parse_beach_item(item)
                if b.slug and b.slug not in beaches:
                    beaches[b.slug] = b
                    new += 1
            print(f"  Page {page_num}/{page_count}: {len(items)} beaches ({new} new)")
            await asyncio.sleep(0.5)

    return list(beaches.values())


# ---------------------------------------------------------------------------
# Step 2: Enrich each beach with location/notes from its detail page
# ---------------------------------------------------------------------------

async def extract_coords_from_maps(pw_page: Page, maps_url: str) -> Optional[str]:
    """Follow a Google Maps link and extract lat,lng from the final redirected URL."""
    try:
        await pw_page.goto(maps_url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)  # let JS redirects settle
        final = pw_page.url

        for pattern in [
            r'@(-?\d+\.\d+),(-?\d+\.\d+)',
            r'[?&]ll=(-?\d+\.\d+),(-?\d+\.\d+)',
            r'query=(-?\d+\.\d+),(-?\d+\.\d+)',
        ]:
            m = re.search(pattern, final)
            if m:
                return f"{m.group(1)},{m.group(2)}"
    except Exception as e:
        print(f"    Maps error: {e}")
    return None


def clean_text(html: str, max_chars: int = 5000) -> str:
    """Strip boilerplate tags and return plain text capped for LLM context."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "head", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text[:max_chars]


async def enrich_beach(
    beach: Beach,
    pw_page: Page,
    http_client: httpx.AsyncClient,
    llm: OpenAI,
    model: str,
) -> Beach:
    """
    Fetch the detail page, look for a Google Maps link to get lat/lng,
    then call the LLM to extract location (if not already found) and notes.
    """
    try:
        r = await http_client.get(beach.url)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"    HTTP error fetching {beach.url}: {e}")
        return beach

    # --- Try to extract structured location from __NEXT_DATA__ on detail page ---
    try:
        data      = extract_next_data(html)
        item_data = data["props"]["pageProps"].get("data", {})
        for key in ("coordinates", "latLng", "address", "location", "postcode"):
            val = item_data.get(key)
            if val:
                beach.location = str(val).strip()
                break
    except Exception:
        item_data = {}

    # --- Scan HTML for Google Maps links ---
    maps_url = None
    if not beach.location:
        soup = BeautifulSoup(html, "lxml")
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            text = a.get_text(strip=True).lower()
            if (
                any(x in href for x in ["maps.google", "goo.gl/maps", "maps.app.goo.gl"])
                or text == "map"
            ):
                maps_url = href
                break

    # --- Follow map link with Playwright ---
    coords = None
    if maps_url:
        print(f"    Map link found, following...")
        coords = await extract_coords_from_maps(pw_page, maps_url)
        if coords:
            print(f"    Coordinates: {coords}")
            beach.location = coords

    # --- LLM: extract location (if still missing) and notes ---
    page_text = clean_text(html)
    if coords:
        page_text = f"[COORDINATES FROM MAP: {coords}]\n\n" + page_text

    try:
        resp = llm.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": LOCATION_SYSTEM_PROMPT},
                {"role": "user",   "content": page_text},
            ],
            temperature=0.1,
            max_tokens=400,
        )
        raw = resp.choices[0].message.content.strip()
        # Strip markdown fences just in case
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'\s*```$',    '', raw)
        extracted = json.loads(raw)

        if not beach.location and extracted.get("location"):
            beach.location = extracted["location"]
        if extracted.get("notes"):
            beach.notes = extracted["notes"]

    except Exception as e:
        print(f"    LLM error: {e}")

    return beach


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(model: str, api_base: str, output_file: str, delay: float):
    llm = OpenAI(base_url=api_base, api_key="local")

    # Phase 1: Collect all beaches from listing pages (plain HTTP, fast)
    print("\n[Phase 1] Collecting beaches from listing pages (no browser needed)...")
    beaches = await fetch_all_listing_pages(BEACHES_URL)
    print(f"\n  Total unique beaches found: {len(beaches)}")

    # Phase 2: Enrich with location + notes from detail pages
    print("\n[Phase 2] Enriching beaches with location/notes from detail pages...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx     = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
        )
        pw_page = await ctx.new_page()

        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True, timeout=30
        ) as http_client:
            for i, beach in enumerate(beaches, 1):
                print(f"\n[{i}/{len(beaches)}] {beach.title}")
                beaches[i - 1] = await enrich_beach(
                    beach, pw_page, http_client, llm, model
                )
                b = beaches[i - 1]
                print(f"  → dogs={b.dog_friendly} | cafe={b.cafe} | loc={b.location}")
                await asyncio.sleep(delay)

        await browser.close()

    # Phase 3: Write CSV
    fieldnames = ["beach_name", "location", "dog_friendly", "cafe", "notes", "url"]
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for b in beaches:
            writer.writerow(b.csv_row())

    print(f"\n✓ Written {len(beaches)} beaches to '{output_file}'")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cornwall beach scraper")
    parser.add_argument("--model",   default=DEFAULT_MODEL,          help="MLX model identifier")
    parser.add_argument("--api-url", default=DEFAULT_API_URL,        help="MLX-LM server base URL")
    parser.add_argument("--output",  default="cornwall_beaches.csv", help="Output CSV filename")
    parser.add_argument("--delay",   type=float, default=1.0,        help="Seconds between detail page requests")
    args = parser.parse_args()

    asyncio.run(run(
        model       = args.model,
        api_base    = args.api_url,
        output_file = args.output,
        delay       = args.delay,
    ))
