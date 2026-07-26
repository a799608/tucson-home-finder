"""
Facebook Marketplace rental scraper for Tucson AZ (headless, logged-out, isolated profile).
Adapted from H:/Claude/mp_scan.py (MVP Marketplace Scanner pattern).
Output: listings_fb.json in this directory, schema matching the home-finder pipeline.
"""
import asyncio, json, os, re, sys, time
from datetime import datetime
from playwright.async_api import async_playwright

from laundry_rules import classify_text as laundry_from_text

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCRATCH = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(SCRATCH, "listings_fb.json")
GEO_CACHE = os.path.join(SCRATCH, "geo_cache_sources.json")
PROFILE = r"H:\Claude\fb_scraper_profile"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PRICE_MIN, PRICE_MAX = 700, 2400
BEDS_MIN, BEDS_MAX = 1, 3
DETAIL_CAP = 60          # max listing pages to open for details
TUCSON = (32.2217, -110.9265)
MAX_MILES = 30           # ~40km around city center

# Category page + search sweeps (city slug overrides the profile's saved location)
URLS = [
    ("category", "https://www.facebook.com/marketplace/tucson/propertyrentals"
                 f"?minPrice={PRICE_MIN}&maxPrice={PRICE_MAX}&minBedrooms=1&exact=false"),
    ("search", "https://www.facebook.com/marketplace/tucson/search/?query=house%20for%20rent"
               f"&minPrice={PRICE_MIN}&maxPrice={PRICE_MAX}&exact=false"),
    ("search", "https://www.facebook.com/marketplace/tucson/search/?query=apartment%20for%20rent"
               f"&minPrice={PRICE_MIN}&maxPrice={PRICE_MAX}&exact=false"),
    ("search", "https://www.facebook.com/marketplace/tucson/search/?query=condo%20for%20rent"
               f"&minPrice={PRICE_MIN}&maxPrice={PRICE_MAX}&exact=false"),
    ("search", "https://www.facebook.com/marketplace/tucson/search/?query=casita%20for%20rent"
               f"&minPrice={PRICE_MIN}&maxPrice={PRICE_MAX}&exact=false"),
    ("search", "https://www.facebook.com/marketplace/tucson/search/?query=duplex%20for%20rent"
               f"&minPrice={PRICE_MIN}&maxPrice={PRICE_MAX}&exact=false"),
]

EXTRACT = r"""
() => {
  const out = []; const seen = new Set();
  document.querySelectorAll('a[href*="/marketplace/item/"]').forEach(a => {
    const href = 'https://www.facebook.com' + a.getAttribute('href').split('?')[0];
    if (seen.has(href)) return; seen.add(href);
    const txt = (a.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    const img = a.querySelector('img');
    out.push({ link: href, lines: txt, thumb: img ? (img.getAttribute('src') || null) : null });
  });
  return out;
}
"""

PRICE_RE = re.compile(r"\$([\d,]+)")
BEDS_RE = re.compile(r"(\d+)\s*(?:bd|bds|bed|beds|bedroom)", re.I)
BATHS_RE = re.compile(r"(\d+(?:\.\d)?)\s*(?:ba\b|bath|baths|bathroom)", re.I)
SQFT_RE = re.compile(r"([\d,]{3,6})\s*(?:sq\s?ft|square\s+feet|sqft)", re.I)
PHONE_RE = re.compile(r"\(?([2-9]\d{2})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})")
NO_PETS_RE = re.compile(r"no\s+pets|pets?\s+(?:are\s+)?not\s+allowed|no\s+cats|sorry,?\s+no\s+pets", re.I)
CATS_OK_RE = re.compile(r"cat\s*friendly|cats?\s+(?:are\s+)?(?:welcome|ok|allowed)", re.I)
DOGS_OK_RE = re.compile(r"dog\s*friendly|dogs?\s+(?:are\s+)?(?:welcome|ok|allowed)", re.I)
PETS_OK_RE = re.compile(r"pet\s*friendly|pets?\s+(?:are\s+)?(?:welcome|ok|allowed|negotiable)", re.I)
LISTED_RE = re.compile(r"Listed\s+(.{1,40}?)\s+(?:in|·)", re.I)
LISTED_IN_RE = re.compile(r"Listed\s+.{1,40}?\s+in\s+([A-Za-z .]+,\s*AZ)", re.I)
ADDR_RE = re.compile(r"\b\d{2,5}\s+(?:[NSEW]\.?\s+)?[A-Za-z0-9 .']{2,40}\b"
                     r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Way|Pl|Place|Ct|Court|Trail|Loop)\b", re.I)
PTYPE_RE = re.compile(r"\b(apartment|house|townhouse|condo|duplex|casita|studio|room)\b", re.I)


def parse_price(text):
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


# ---------------- geocode (own cache; Nominatim 1 req/sec) ----------------
_cache = None
_geolocator = None

def _load_cache():
    global _cache
    if _cache is None:
        _cache = {}
        if os.path.exists(GEO_CACHE):
            try:
                _cache = json.load(open(GEO_CACHE, encoding="utf-8"))
            except Exception:
                _cache = {}
    return _cache

def _save_cache():
    try:
        json.dump(_cache, open(GEO_CACHE, "w", encoding="utf-8"))
    except Exception:
        pass

def geocode(loc):
    """Nominatim REST (no geopy dependency), 1 req/sec, cached."""
    import urllib.request, urllib.parse
    loc = (loc or "").strip()
    if not loc:
        return None
    c = _load_cache()
    if loc in c:
        v = c[loc]
        return tuple(v) if v else None
    try:
        time.sleep(1.05)
        q = loc if ("AZ" in loc or "Arizona" in loc) else loc + ", AZ"
        url = ("https://nominatim.openstreetmap.org/search?format=json&limit=1&q="
               + urllib.parse.quote(q + ", USA"))
        req = urllib.request.Request(url, headers={"User-Agent": "tucson_home_finder_sources/1.0"})
        data = json.load(urllib.request.urlopen(req, timeout=15))
        if data:
            c[loc] = [round(float(data[0]["lat"]), 5), round(float(data[0]["lon"]), 5)]
        else:
            c[loc] = None
    except Exception:
        c[loc] = None
    _save_cache()
    v = c[loc]
    return tuple(v) if v else None

def miles_from_tucson(coords):
    from math import radians, sin, cos, asin, sqrt
    lat1, lon1 = map(radians, coords)
    lat2, lon2 = map(radians, TUCSON)
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return round(3958.8 * 2 * asin(sqrt(a)), 1)


# ---------------- detail-page parsing ----------------
def parse_detail_text(body_text):
    """Pull rental fields out of a logged-out FB item page's visible text."""
    d = {}
    t = body_text or ""
    m = PRICE_RE.search(t)
    d["price"] = int(m.group(1).replace(",", "")) if m else None
    m = BEDS_RE.search(t)
    d["beds"] = int(m.group(1)) if m else None
    m = BATHS_RE.search(t)
    d["baths"] = float(m.group(1)) if m else None
    m = SQFT_RE.search(t)
    if m:
        v = int(m.group(1).replace(",", ""))
        d["sqft"] = v if 150 <= v <= 10000 else None
    else:
        d["sqft"] = None
    # pets
    if NO_PETS_RE.search(t):
        d["pets_note"] = "no pets"
    else:
        parts = []
        if CATS_OK_RE.search(t):
            parts.append("cats OK")
        if DOGS_OK_RE.search(t):
            parts.append("dogs OK")
        if not parts and PETS_OK_RE.search(t):
            parts.append("pets OK")
        d["pets_note"] = " + ".join(parts) if parts else None
    m = PTYPE_RE.search(t)
    d["property_type"] = m.group(1).lower() if m else None
    m = LISTED_RE.search(t)
    d["listed_date_or_age"] = ("Listed " + m.group(1)) if m else None
    m = LISTED_IN_RE.search(t)
    d["location_txt"] = m.group(1).strip() if m else None
    m = ADDR_RE.search(t)
    d["address_guess"] = m.group(0).strip() if m else None
    m = PHONE_RE.search(t)
    d["contact_phone"] = "({}) {}-{}".format(*m.groups()) if m else None
    # in_unit | onsite | None from the visible page text ("w/d in unit",
    # "washer and dryer included", "laundry on site", hookups) - never guessed
    d["laundry"] = laundry_from_text(t)
    return d


async def run():
    found = {}
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE, executable_path=CHROME, headless=True,
            args=["--no-first-run", "--no-default-browser-check"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # --- sweep category + search pages ---
        for kind, url in URLS:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                print(f"    [{kind}] nav error: {type(e).__name__}")
                continue
            await page.wait_for_timeout(3500)
            for _ in range(10):
                await page.evaluate("window.scrollBy(0, window.innerHeight*1.6)")
                await page.wait_for_timeout(900)
            cards = await page.evaluate(EXTRACT)
            kept = 0
            for c in cards:
                link = c["link"]
                if link in found:
                    continue
                lines = c["lines"]
                joined = " | ".join(lines)
                price = parse_price(joined)
                if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
                    continue
                # location = usually last line ending in ", AZ"
                loc = None
                for ln in reversed(lines):
                    if re.search(r",\s*AZ$", ln):
                        loc = ln
                        break
                nonprice = [ln for ln in lines if not ln.startswith("$")]
                title = nonprice[0] if nonprice else ""
                found[link] = {"link": link, "title": title, "price": price,
                               "location": loc, "raw": joined, "thumb": c["thumb"]}
                kept += 1
            print(f"    [{kind}] {len(cards):3} cards -> +{kept} in range (total {len(found)})")

        print(f"[*] {len(found)} unique in-range cards; opening up to {DETAIL_CAP} detail pages ...")

        # --- open detail pages for full fields ---
        rows = []
        for i, c in enumerate(list(found.values())[:DETAIL_CAP]):
            try:
                await page.goto(c["link"], wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2500)
                body = await page.evaluate("() => document.body.innerText")
                # try to expand 'See more' description (best effort)
                d = parse_detail_text(body)
            except Exception as e:
                print(f"    detail {i}: error {type(e).__name__}")
                d = {}
            item_id = re.search(r"/item/(\d+)", c["link"])
            item_id = item_id.group(1) if item_id else str(abs(hash(c["link"])))
            price = d.get("price") or c["price"]
            if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
                continue
            beds = d.get("beds")
            if beds is None:
                m = BEDS_RE.search(c["title"] or "") or BEDS_RE.search(c["raw"] or "")
                beds = int(m.group(1)) if m else None
            if beds is not None and not (BEDS_MIN <= beds <= BEDS_MAX):
                continue
            if d.get("pets_note") == "no pets":
                continue  # cat-hostile
            loc_txt = d.get("location_txt") or c["location"] or "Tucson, AZ"
            rows.append({
                "id": f"fb-{item_id}",
                "source": "facebook",
                "url": c["link"],
                "address": d.get("address_guess") or loc_txt,
                "lat": None, "lng": None,
                "price": price,
                "beds": beds,
                "baths": d.get("baths"),
                "sqft": d.get("sqft"),
                "pets_note": d.get("pets_note"),
                "property_type": d.get("property_type"),
                "listed_date_or_age": d.get("listed_date_or_age"),
                "title": c["title"] or None,
                "thumbnail_url": c["thumb"],
                "contact_phone": d.get("contact_phone"),
                "contact_name": None,
                "contact_method": "facebook_messenger",
                "laundry": d.get("laundry") or laundry_from_text(
                    (c["title"] or "") + " " + (c["raw"] or "")),
                "_loc_for_geo": d.get("address_guess") or loc_txt,
            })
            if (i + 1) % 10 == 0:
                print(f"    details: {i+1} done, {len(rows)} kept")
        await ctx.close()

    # checkpoint: never lose the scrape to a geocode failure
    with open(os.path.join(SCRATCH, "fb_rows_pre_geo.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1)

    # --- geocode + radius filter ---
    print(f"[*] geocoding {len(rows)} rows ...")
    final = []
    for r in rows:
        cc = geocode(r.pop("_loc_for_geo"))
        if cc:
            mi = miles_from_tucson(cc)
            if mi > MAX_MILES:
                continue
            r["lat"], r["lng"] = cc
        final.append(r)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(final, f, indent=1)
    prices = [r["price"] for r in final if r["price"]]
    if prices:
        print(f"[OK] facebook: {len(final)} listings -> {OUT_PATH}")
        print(f"     price min/median/max: ${min(prices)} / ${sorted(prices)[len(prices)//2]} / ${max(prices)}")
    else:
        print(f"[OK] facebook: 0 listings -> {OUT_PATH}")
    phones = sum(1 for r in final if r["contact_phone"])
    print(f"     phone coverage: {phones}/{len(final)}")


if __name__ == "__main__":
    asyncio.run(run())
