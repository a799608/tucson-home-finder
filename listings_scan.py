"""Tucson AZ rental-listings scanner.

Listings layer for a neighborhood-finder map: 1-3BR apartments/houses,
$700-$2,400/mo, cat-friendly, within 25 mi of the Raytheon airport campus
(32.1051, -110.9456). Source: Zumper JSON backend. Craigslist is EXCLUDED
by scope (2026-07-26) - its code is kept but gated behind --with-craigslist.
ApartmentList and Apartments.com return hard 403s (Akamai) - skipped.

Contact enrichment (2026-07-26): each listing gets contact_phone /
contact_name / contact_method / utilities_note / cats_ok. Zumper list rows
carry a `phone` for ~1/3 of listings; the rest come from the no-login detail
JSON endpoints /api/t/1/buildings/{building_id} (agents[].phone) and
/api/t/1/listings/{listing_id} (listing_agents[].phone, description text).
Detail pulls are cached permanently in zumper_detail_cache.json.
Phones are only ever copied from source data - never synthesized.

Read-only scraping, no accounts. Throttled ~1.5s between page requests,
1.05s Nominatim. Raw pulls cached (zumper_raw.json / cl_raw.json, 6h TTL)
so re-runs don't rehammer the sources. Run with --fresh to force refetch.

Output: listings.json in this directory.
"""
import json, os, re, sys, time, math
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from laundry_rules import classify as laundry_classify, classify_text as laundry_from_text

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "listings.json")
ZUMPER_CACHE = os.path.join(HERE, "zumper_raw.json")
ZDETAIL_CACHE = os.path.join(HERE, "zumper_detail_cache.json")   # building/listing detail JSON, no TTL
CL_CACHE = os.path.join(HERE, "cl_raw.json")
GEO_CACHE_PATH = os.path.join(HERE, "geo_cache.json")  # same pattern as H:/Claude/mp_geocode.py
CACHE_TTL_S = 6 * 3600
FRESH = "--fresh" in sys.argv
INCLUDE_CL = "--with-craigslist" in sys.argv   # Craigslist excluded by default (scope change 2026-07-26)

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

# ------------------------------------------------- contact / utilities helpers
PHONE_SEP_RE = re.compile(r"\(?\b([2-9]\d{2})\)?[\s.\-]{1,2}(\d{3})[\s.\-](\d{4})\b")
PHONE_BARE_RE = re.compile(r"\b([2-9]\d{2})(\d{3})(\d{4})\b")

def find_phone(text):
    """Extract a US phone from free text; normalized (AAA) PPP-SSSS or None."""
    for rx in (PHONE_SEP_RE, PHONE_BARE_RE):
        m = rx.search(text or "")
        if m:
            return f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
    return None

UTIL_WORD_RE = re.compile(r"utilit|water|sewer|trash|garbage|electric|\bgas\b|internet|cable|wifi|\bheat\b", re.I)
UTIL_STANCE_RE = re.compile(r"includ|tenant pays|owner pays|landlord pays|responsib|\bpaid\b|covered", re.I)
UTIL_APPLIANCE_RE = re.compile(r"range|stove|oven|dryer|washer|heater|softn?er|hookup|fireplace|grill|furniture|landscap", re.I)

def utilities_from_tags(tags):
    """Amenity tags with explicit included/tenant-pays semantics -> note, else None."""
    out, seen = [], set()
    for t in tags or []:
        t = str(t).strip()
        if not t or not UTIL_STANCE_RE.search(t):
            continue
        if not UTIL_WORD_RE.search(t):
            continue
        if UTIL_APPLIANCE_RE.search(t):
            continue
        k = t.lower()
        if k not in seen:
            seen.add(k); out.append(t)
    return "; ".join(out) if out else None

def utilities_from_desc(desc):
    """Explicit utility-payment phrases from detail description text, else None."""
    out = []
    for m in re.finditer(r"[^.;\n\r]*(?:utilit\w*|water|sewer|trash|garbage|electric\w*|\bgas\b|internet|cable|wifi)[^.;\n\r]*",
                         desc or "", re.I):
        s = " ".join(m.group(0).split()).strip(" ,-")
        if len(s) > 140 or not UTIL_STANCE_RE.search(s) or UTIL_APPLIANCE_RE.search(s):
            continue
        if s.lower() not in (o.lower() for o in out):
            out.append(s)
        if len(out) >= 2:
            break
    return "; ".join(out) if out else None

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
    # contact + utilities + cat flag (list-level; detail enrichment fills gaps later)
    phone = x.get("phone") or None
    cname = x.get("brokerage_name") or x.get("agent_name") or None
    cats_ok = (1 in pets) if isinstance(pets, list) else None   # non-cat lists were dropped above
    util = utilities_from_tags((x.get("amenity_tags") or []) + (x.get("building_amenity_tags") or []))
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
        "contact_phone": phone,
        "contact_name": cname,
        "contact_method": "phone" if phone else "listing_page",
        "utilities_note": util,
        "cats_ok": cats_ok,
        # in_unit | onsite | None - copied from amenity tags/text only (laundry_rules.py)
        "laundry": laundry_classify((x.get("amenity_tags") or []) + (x.get("building_amenity_tags") or []),
                                    " ".join(str(x.get(k) or "") for k in ("short_description", "title"))),
    }

_zdetail_cache = None
def _save_zdetail_cache():
    try: json.dump(_zdetail_cache, open(ZDETAIL_CACHE, "w", encoding="utf-8"))
    except Exception: pass

def zumper_detail_get(kind, id_):
    """GET /api/t/1/{buildings|listings}/{id_} (no login needed). Permanent local
    cache - contact data is stable; delete zumper_detail_cache.json to refresh."""
    global _zdetail_cache
    if _zdetail_cache is None:
        _zdetail_cache = {}
        if os.path.exists(ZDETAIL_CACHE):
            try: _zdetail_cache = json.load(open(ZDETAIL_CACHE, encoding="utf-8"))
            except Exception: _zdetail_cache = {}
    key = f"{kind}:{id_}"
    if key in _zdetail_cache:
        return _zdetail_cache[key]
    try:
        time.sleep(1.5)
        r = S.get(f"https://www.zumper.com/api/t/1/{kind}/{id_}", timeout=25)
        _zdetail_cache[key] = r.json() if r.status_code == 200 else None
    except Exception:
        _zdetail_cache[key] = None
    if len(_zdetail_cache) % 25 == 0:
        _save_zdetail_cache()
    return _zdetail_cache[key]

def enrich_zumper_contacts(listings, raw_rows):
    """Second pass over final zumper rows lacking a list-level phone or a
    laundry classification. Building pages expose agents[].phone, listing pages
    listing_agents[].phone; detail amenity tags + description also fill the
    laundry field. Copies only - a phone is never synthesized; null beats
    guessed."""
    raw_by_id = {r["listing_id"]: r for r in raw_rows}
    need = [l for l in listings if l["source"] == "zumper"
            and (not l["contact_phone"] or not l.get("laundry"))]
    to_fetch = {}          # cache-key -> (kind, id_) so shared buildings fetch once
    for l in need:
        r = raw_by_id.get(int(l["id"].split("-", 1)[1]), {})
        if r.get("building_id"):
            to_fetch[f"buildings:{r['building_id']}"] = None
        else:
            to_fetch[f"listings:{r.get('listing_id')}"] = None
    print(f"[zumper] contact enrichment: {len(need)} rows lack list-level phone; "
          f"{len(to_fetch)} detail fetches (~{len(to_fetch) * 1.6 / 60:.0f} min)")
    got = 0
    for i, l in enumerate(need):
        lid = int(l["id"].split("-", 1)[1])
        r = raw_by_id.get(lid, {})
        if r.get("building_id"):
            d = zumper_detail_get("buildings", r["building_id"]) or {}
            agents = d.get("agents") or []
        else:
            d = zumper_detail_get("listings", lid) or {}
            agents = d.get("listing_agents") or []
        phone = name = None
        for a in agents:
            if not isinstance(a, dict):
                continue
            phone = phone or a.get("phone")
            name = name or a.get("company_name") or a.get("brokerage") or a.get("long_name")
        desc = d.get("description") or ""
        if not phone:
            phone = find_phone(desc)                 # poster's own text, copied verbatim
        if phone:
            l["contact_phone"] = phone
            l["contact_method"] = "phone"
            got += 1
        if name and not l["contact_name"]:
            l["contact_name"] = name
        tags = (d.get("amenity_tags") or []) + (d.get("building_amenity_tags") or [])
        if isinstance(d.get("amenity_groups"), dict):
            tags += list(d["amenity_groups"])       # detail JSON keys tag names here
        if not l["utilities_note"]:
            l["utilities_note"] = utilities_from_tags(tags) or utilities_from_desc(desc)
        if not l.get("laundry"):
            l["laundry"] = laundry_classify(tags, desc)
        if i % 50 == 49:
            print(f"[zumper] enrichment {i + 1}/{len(need)} (+{got} phones so far)")
    _save_zdetail_cache()
    print(f"[zumper] enrichment done: +{got} phones from detail JSON")

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
        # schema parity with zumper rows (CL contact enrichment excluded by scope)
        "contact_phone": None,
        "contact_name": None,
        "contact_method": "listing_page",
        "utilities_note": None,
        "cats_ok": True if x.get("cats_ok") else None,
        "laundry": laundry_from_text(" ".join([x.get("body") or "", x.get("title") or ""]
                                              + [str(a) for a in (x.get("attrs") or [])])),
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
    if not INCLUDE_CL:
        failed["craigslist"] = "excluded by scope 2026-07-26 (--with-craigslist to re-enable)"
    per_source_kept, drops_by_src = {}, {}
    listings, zumper_raw_rows = [], []

    sources = [("zumper", fetch_zumper_raw, norm_zumper)]
    if INCLUDE_CL:
        sources.append(("craigslist", fetch_cl_raw, norm_cl))
    for name, fetch, norm in sources:
        drops = {k: 0 for k in ("no_coords", "too_far", "excluded_city", "no_price",
                                "price_band", "no_beds", "beds_band", "pets")}
        kept = []
        try:
            raws = fetch()
            if name == "zumper":
                zumper_raw_rows = raws
            for raw in raws:
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

    if zumper_raw_rows:                      # after dedupe so dupes don't cost fetches
        enrich_zumper_contacts(out, zumper_raw_rows)

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
    ph = {}
    for l in out:
        s = l["source"]
        ph.setdefault(s, [0, 0])[1] += 1
        if l["contact_phone"]: ph[s][0] += 1
    print("contact_phone coverage:", {s: f"{v[0]}/{v[1]}" for s, v in ph.items()})
    print("contact_name:", sum(1 for l in out if l["contact_name"]),
          "| utilities_note:", sum(1 for l in out if l["utilities_note"]),
          "| cats_ok true/null:", sum(1 for l in out if l["cats_ok"] is True),
          "/", sum(1 for l in out if l["cats_ok"] is None))
    print("drop reasons:", {s: {k: v for k, v in d.items() if v} for s, d in drops_by_src.items()})
    print("failed/blocked sources:", failed)
    print("output:", OUT_PATH)

if __name__ == "__main__":
    main()
