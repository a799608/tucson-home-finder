"""Tucson AZ rental-listings scanner.

Listings layer for a neighborhood-finder map: 1-3BR apartments/houses,
$700-$2,400/mo, cat-friendly, within 25 mi of the Raytheon airport campus
(32.1051, -110.9456). Sources: Zumper JSON backend + Craigslist static pages.
ApartmentList and Apartments.com return hard 403s (Akamai) - skipped.

Read-only scraping, no accounts. Throttled ~1.5s between page requests,
1.05s Nominatim. Raw pulls cached (zumper_raw.json / cl_raw.json, 6h TTL)
so re-runs don't rehammer the sources. Run with --fresh to force refetch.

Output: listings.json in this directory.
"""
import json, os, re, sys, time, math
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "listings.json")
ZUMPER_CACHE = os.path.join(HERE, "zumper_raw.json")
CL_CACHE = os.path.join(HERE, "cl_raw.json")
GEO_CACHE_PATH = os.path.join(HERE, "geo_cache.json")  # same pattern as H:/Claude/mp_geocode.py
CACHE_TTL_S = 6 * 3600
FRESH = "--fresh" in sys.argv

CENTER = (32.1051, -110.9456)          # Raytheon airport campus
RADIUS_MI = 25.0
PRICE_MIN, PRICE_MAX = 700, 2400
BEDS_MIN, BEDS_MAX = 1, 3
EXCLUDE_CITIES = {"green valley", "oro valley"}   # trivial name-based exclusion

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}
S = requests.Session(); S.headers.update(UA)

NO_PETS_RE = re.compile(r"\bno pets\b|\bpets? (?:are )?not allowed\b|sorry,? no pets", re.I)
NO_CATS_RE = re.compile(r"\bdogs? only\b|\bno cats\b", re.I)

# ---------------------------------------------------------------- utilities
def haversine_mi(lat1, lng1, lat2, lng2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

def cache_fresh(path):
    return (not FRESH) and os.path.exists(path) and (time.time() - os.path.getmtime(path) < CACHE_TTL_S)

_geo_cache = None
def geocode(addr):
    """Nominatim with local JSON cache + 1 req/sec throttle (mp_geocode.py pattern)."""
    global _geo_cache
    if _geo_cache is None:
        _geo_cache = {}
        if os.path.exists(GEO_CACHE_PATH):
            try: _geo_cache = json.load(open(GEO_CACHE_PATH, encoding="utf-8"))
            except Exception: _geo_cache = {}
    key = addr.strip().upper()
    if key in _geo_cache:
        v = _geo_cache[key]; return tuple(v) if v else None
    try:
        time.sleep(1.05)
        r = S.get("https://nominatim.openstreetmap.org/search",
                  params={"q": addr + ", USA", "format": "json", "limit": 1},
                  headers={"User-Agent": "tucson_listings_scanner"}, timeout=15)
        j = r.json()
        _geo_cache[key] = [round(float(j[0]["lat"]), 5), round(float(j[0]["lon"]), 5)] if j else None
    except Exception:
        _geo_cache[key] = None
    try: json.dump(_geo_cache, open(GEO_CACHE_PATH, "w", encoding="utf-8"))
    except Exception: pass
    v = _geo_cache[key]; return tuple(v) if v else None

# ---------------------------------------------------------------- Zumper
ZUMPER_API = "https://www.zumper.com/api/t/1/pages/listables"
# property_type enum mapped empirically (titles/urls/feeds of 1521 Tucson rows):
# 4=apartment building, 1=condo, 2=townhome, 0=casita, 7=manufactured,
# 9/13=single-family pads (PM feeds: listhub/appfolio/showmojo/rently), 14=duplex
ZPT = {4: "apartment", 1: "condo", 2: "house", 0: "house", 7: "house",
       9: "house", 13: "house", 14: "house"}
# pets codes verified against listing text: 1=cats, 2=dogs, 3=small dogs
# ("no pets" text <-> pets==[], "dogs only/no cats" text <-> lists without 1)

def fetch_zumper_raw():
    if cache_fresh(ZUMPER_CACHE):
        rows = json.load(open(ZUMPER_CACHE, encoding="utf-8"))
        print(f"[zumper] using cached raw pull ({len(rows)} rows)")
        return rows
    rows, off = [], 0
    while True:
        d = S.post(ZUMPER_API, json={"url": "tucson-az", "limit": 100, "offset": off},
                   timeout=30).json()
        page = d.get("listables", [])
        rows += page
        if len(page) < 100 or off >= 3000:
            break
        off += 100
        time.sleep(1.5)
    json.dump(rows, open(ZUMPER_CACHE, "w", encoding="utf-8"))
    print(f"[zumper] fetched {len(rows)} raw rows")
    return rows

def zumper_text(x):
    parts = [str(x.get(k) or "") for k in ("short_description", "title")]
    for k in ("amenity_tags", "building_amenity_tags"):
        parts += [str(v) for v in (x.get(k) or [])]
    return " ".join(parts)

def norm_zumper(x, drops):
    lat, lng = x.get("lat"), x.get("lng")
    if lat is None or lng is None:
        drops["no_coords"] += 1; return None
    if haversine_mi(lat, lng, *CENTER) > RADIUS_MI:
        drops["too_far"] += 1; return None
    if (x.get("city") or "").strip().lower() in EXCLUDE_CITIES:
        drops["excluded_city"] += 1; return None

    pmin = x.get("min_price"); pmax = x.get("max_price") or pmin
    if not pmin and not pmax:
        drops["no_price"] += 1; return None
    pmin = pmin or pmax
    if pmax < PRICE_MIN or pmin > PRICE_MAX:
        drops["price_band"] += 1; return None

    bmin = x.get("min_bedrooms"); bmax = x.get("max_bedrooms")
    if x.get("bedrooms") is not None:
        bmin = bmax = x["bedrooms"]
    if bmin is None and bmax is None:
        drops["no_beds"] += 1; return None
    bmin = bmin if bmin is not None else bmax
    bmax = bmax if bmax is not None else bmin
    if bmax < BEDS_MIN or bmin > BEDS_MAX:
        drops["beds_band"] += 1; return None
    beds = bmin if bmin >= BEDS_MIN else BEDS_MIN  # smallest in-band unit offered

    pets = x.get("pets")
    txt = zumper_text(x)
    if pets == [] or (isinstance(pets, list) and 1 not in pets):
        drops["pets"] += 1; return None          # explicit no-pets, or cats not allowed
    if pets is None and (NO_PETS_RE.search(txt) or NO_CATS_RE.search(txt)):
        drops["pets"] += 1; return None
    if isinstance(pets, list):
        note = "cats OK" + (" + dogs OK" if 2 in pets or 3 in pets else " (dogs not listed)")
    else:
        note = "pet policy not stated"

    ptype = ZPT.get(x.get("property_type"), "apartment")
    tl = txt.lower()
    if x.get("property_type") in (9, 13, 0):       # individual pads: refine via text
        if re.search(r"\bcondo(minium)?\b", tl): ptype = "condo"
        elif re.search(r"\bapartment\b", tl):    ptype = "apartment"

    addr = ", ".join(p for p in [x.get("address"), x.get("city"),
                                 x.get("state"), x.get("zipcode")] if p)
    img = (x.get("image_ids") or [None])[0]
    listed = x.get("listed_on") or x.get("created_on")
    return {
        "id": f"zmp-{x['listing_id']}",
        "source": "zumper",
        "url": "https://www.zumper.com" + (x.get("url") or ""),
        "address": addr,
        "lat": round(lat, 6), "lng": round(lng, 6),
        "price": int(pmin),
        "beds": int(beds),
        "baths": x.get("min_bathrooms") or x.get("bathrooms"),
        "sqft": x.get("square_feet") or x.get("min_square_feet"),
        "pets_note": note,
        "property_type": ptype,
        "listed_date_or_age": datetime.fromtimestamp(listed, tz=timezone.utc).strftime("%Y-%m-%d") if listed else None,
        "title": x.get("title") or x.get("building_name") or f"{beds}BR in {x.get('city') or 'Tucson'}",
        "thumbnail_url": f"https://img.zumpercdn.com/{img}/640x480" if img else None,
    }

# ---------------------------------------------------------------- Craigslist
CL_SEARCH = "https://tucson.craigslist.org/search/apa"
CL_SKIP_LOC = ("phoenix", "sierra vista", "benson", "nogales", "douglas", "casa grande",
               "green valley", "oro valley", "safford", "willcox", "globe", "mesa",
               "tempe", "chandler", "scottsdale", "glendale")
CL_TYPE_MAP = {"apartment": "apartment", "condo": "condo", "house": "house",
               "duplex": "house", "townhouse": "house", "cottage/cabin": "house",
               "manufactured": "house", "in-law": "house", "flat": "apartment",
               "loft": "apartment"}

def parse_cl_detail(html, url):
    ds = BeautifulSoup(html, "html.parser")
    out = {"url": url}
    t = ds.select_one("#titletextonly")
    out["title"] = t.get_text(strip=True) if t else None
    p = ds.select_one(".price")
    out["price"] = int(re.sub(r"[^\d]", "", p.get_text())) if p and re.search(r"\d", p.get_text()) else None
    mp = ds.select_one("#map")
    out["lat"] = float(mp["data-latitude"]) if mp and mp.get("data-latitude") else None
    out["lng"] = float(mp["data-longitude"]) if mp and mp.get("data-longitude") else None
    ma = ds.select_one(".mapaddress")
    out["mapaddress"] = ma.get_text(strip=True) if ma else None
    attrs = [a.get_text(" ", strip=True) for a in ds.select("div.attrgroup .attr, div.attrgroup span.attr")]
    out["attrs"] = attrs
    blob = " ".join(attrs) + " " + (ds.select_one(".housing").get_text() if ds.select_one(".housing") else "")
    m = re.search(r"(\d+)\s*BR", blob, re.I); out["beds"] = int(m.group(1)) if m else None
    m = re.search(r"(\d+(?:\.\d+)?)\s*Ba", blob, re.I); out["baths"] = float(m.group(1)) if m else None
    m = re.search(r"(\d{3,5})\s*ft2", blob, re.I); out["sqft"] = int(m.group(1)) if m else None
    low = html.lower()
    out["cats_ok"] = "cats are ok" in low
    out["dogs_ok"] = "dogs are ok" in low
    body = ds.select_one("#postingbody")
    out["body"] = body.get_text(" ", strip=True)[:2000] if body else ""
    out["housing_type"] = next((h for h in CL_TYPE_MAP if re.search(r"housing_type=\d+\">" + re.escape(h), html)), None)
    tm = ds.select_one("time.date.timeago") or ds.select_one("time")
    out["posted"] = (tm.get("datetime") or "")[:10] if tm else None
    og = ds.find("meta", property="og:image")
    out["thumb"] = og["content"] if og and og.get("content") else None
    return out

def fetch_cl_raw():
    if cache_fresh(CL_CACHE):
        rows = json.load(open(CL_CACHE, encoding="utf-8"))
        print(f"[craigslist] using cached raw pull ({len(rows)} rows)")
        return rows
    h = S.get(CL_SEARCH, timeout=25).text
    soup = BeautifulSoup(h, "html.parser")
    cards = soup.select("li.cl-static-search-result")
    cands = []
    for li in cards:
        a = li.find("a")
        if not a or not a.get("href"):
            continue
        loc = li.select_one(".location")
        loc = loc.get_text(strip=True).lower() if loc else ""
        if any(s in loc for s in CL_SKIP_LOC):
            continue
        pr = li.select_one(".price")
        price = int(re.sub(r"[^\d]", "", pr.get_text())) if pr and re.search(r"\d", pr.get_text()) else None
        if price is not None and not (PRICE_MIN - 50 <= price <= PRICE_MAX + 100):
            continue
        cands.append(a["href"])
    print(f"[craigslist] {len(cards)} cards -> {len(cands)} candidates; fetching details "
          f"(~{len(cands) * 1.6 / 60:.0f} min)")
    rows = []
    for i, u in enumerate(cands):
        try:
            r = S.get(u, timeout=25)
            if r.status_code == 200:
                rows.append(parse_cl_detail(r.text, u))
            else:
                print(f"[craigslist] {r.status_code} on {u[:60]}")
        except Exception as e:
            print(f"[craigslist] ERR {u[:60]}: {e!r:.80}")
        if i % 25 == 24:
            print(f"[craigslist] {i + 1}/{len(cands)}")
        time.sleep(1.5)
    json.dump(rows, open(CL_CACHE, "w", encoding="utf-8"))
    print(f"[craigslist] fetched {len(rows)} detail pages")
    return rows

def norm_cl(x, drops):
    price = x.get("price")
    if price is None:
        drops["no_price"] += 1; return None
    if not (PRICE_MIN <= price <= PRICE_MAX):
        drops["price_band"] += 1; return None
    beds = x.get("beds")
    if beds is None:
        drops["no_beds"] += 1; return None
    if not (BEDS_MIN <= beds <= BEDS_MAX):
        drops["beds_band"] += 1; return None

    text = (x.get("body") or "") + " " + (x.get("title") or "")
    if NO_PETS_RE.search(text) or NO_CATS_RE.search(text):
        drops["pets"] += 1; return None
    if x.get("cats_ok"):
        note = "cats OK" + (" + dogs OK" if x.get("dogs_ok") else "")
    elif x.get("dogs_ok"):
        note = "dogs OK; cats not stated"
    else:
        note = "pet policy not stated"

    lat, lng = x.get("lat"), x.get("lng")
    addr = x.get("mapaddress")
    if (lat is None or lng is None) and addr:
        g = geocode(f"{addr}, Tucson, AZ")
        if g: lat, lng = g
    if lat is None or lng is None:
        drops["no_coords"] += 1; return None
    if haversine_mi(lat, lng, *CENTER) > RADIUS_MI:
        drops["too_far"] += 1; return None
    if addr and any(c in addr.lower() for c in EXCLUDE_CITIES):
        drops["excluded_city"] += 1; return None

    ptype = CL_TYPE_MAP.get(x.get("housing_type") or "", "apartment")
    slug = x["url"].rstrip("/").split("/")[-1]
    return {
        "id": f"cl-{slug}",
        "source": "craigslist",
        "url": x["url"],
        "address": (addr + ", Tucson, AZ") if addr and "tucson" not in addr.lower() else (addr or "Tucson, AZ (approx map point)"),
        "lat": round(lat, 6), "lng": round(lng, 6),
        "price": price,
        "beds": beds,
        "baths": x.get("baths"),
        "sqft": x.get("sqft"),
        "pets_note": note,
        "property_type": ptype,
        "listed_date_or_age": x.get("posted"),
        "title": x.get("title") or f"{beds}BR in Tucson",
        "thumbnail_url": x.get("thumb"),
    }

# ---------------------------------------------------------------- dedupe + main
def dedupe_keys(l):
    keys = []
    if l.get("address"):
        street = re.sub(r"[^A-Z0-9]", "", l["address"].upper().split(",")[0])
        if street:
            keys.append(f"A|{street}|{l['price'] // 50}")
    keys.append(f"G|{round(l['lat'], 4)},{round(l['lng'], 4)}|{l['price'] // 50}")
    return keys

def main():
    failed = {"apartmentlist": "HTTP 403 (bot-blocked)", "apartments.com": "HTTP 403 Access Denied (Akamai)"}
    per_source_kept, drops_by_src = {}, {}
    listings = []

    for name, fetch, norm in (("zumper", fetch_zumper_raw, norm_zumper),
                              ("craigslist", fetch_cl_raw, norm_cl)):
        drops = {k: 0 for k in ("no_coords", "too_far", "excluded_city", "no_price",
                                "price_band", "no_beds", "beds_band", "pets")}
        kept = []
        try:
            for raw in fetch():
                l = norm(raw, drops)
                if l: kept.append(l)
        except Exception as e:
            failed[name] = repr(e)[:200]
        per_source_kept[name] = len(kept)
        drops_by_src[name] = drops
        listings += kept

    seen, out = set(), []
    for l in listings:                       # zumper first = richer records win
        ks = dedupe_keys(l)
        if any(k in seen for k in ks):
            continue
        seen.update(ks)
        out.append(l)

    json.dump(out, open(OUT_PATH, "w", encoding="utf-8"), indent=1)

    prices = sorted(l["price"] for l in out)
    med = prices[len(prices) // 2] if prices else None
    types = {}
    for l in out:
        types[l["property_type"]] = types.get(l["property_type"], 0) + 1
    print("\n===== SUMMARY =====")
    print("kept per source:", per_source_kept)
    print("total after dedupe:", len(out), f"(removed {len(listings) - len(out)} dupes)")
    if prices:
        print(f"price: min ${prices[0]}  median ${med}  max ${prices[-1]}")
    print("property types:", types)
    print("drop reasons:", {s: {k: v for k, v in d.items() if v} for s, d in drops_by_src.items()})
    print("failed/blocked sources:", failed)
    print("output:", OUT_PATH)

if __name__ == "__main__":
    main()
