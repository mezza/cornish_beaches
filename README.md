# Cornwall Beach Scraper

Scrapes beach listings from [visitcornwall.com](https://www.visitcornwall.com/beaches) and produces a more user-friendly map with the name, location, dog-friendliness, café presence, notes, and URL for each beach. Each location can be clicked to provide directions via Apple Maps or Google Maps.

## Requirements

```bash
pip install playwright beautifulsoup4 lxml openai
playwright install chromium
```

You also need **mlx-lm** running as a local OpenAI-compatible server:

```bash
pip install mlx-lm
mlx_lm.server --model prism-ml/Ternary-Bonsai-8B-mlx-2bit --port 8080
# or swap in gemma4 / granite-4.1-8b-4bit
```

### Usage

```bash
python scraper.py
python geocode.py
```

### Detail pages
For each unique beach URL the scraper:
1. Renders the page with Playwright (handles JS-heavy content)
2. Looks for a Google Maps link; if found, follows it and extracts lat/long from the final redirected URL
3. Sends cleaned page text to the local LLM with a strict JSON extraction prompt
4. Merges LLM output with the listing-derived flags (listing flags take precedence for dog/café status, avoiding LLM under-detection)

### CSV output

| Column | Values |
|---|---|
| `beach_name` | Name as it appears on the page |
| `location` | `lat,lng` string if map link found, else address text |
| `dog_friendly` | `Yes` / `No` / `Seasonal` |
| `cafe` | `Yes` / `No` |
| `notes` | Dog restriction dates or café details (if any) |
| `url` | Source page URL |

### Geolocation and JSON data preparation

Once the CSV file is generated, the final `geocode.py` script uses GoogleMaps to geocode the location and add `lat`/`lng` columns to the output. The resulting JSON file is then read into the `leafletjs` map visualization.

## Notes

- The `--delay` flag (default 1.5 s) adds a polite crawl pause between detail page requests. Don't set it below 1.0.
- The scraper uses a realistic browser user-agent to avoid bot detection.
- If the MLX server is slow, increase `max_tokens` timeout in the OpenAI client or reduce `--delay`.
- Google Maps coordinate extraction works by following redirects and parsing `@lat,lng` from the final URL — no Maps API key needed.
