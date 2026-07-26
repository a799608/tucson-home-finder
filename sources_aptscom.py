"""
Apartments.com Tucson rentals -> listings_aptscom.json
Data source: Apify actor memo23/apartments-cheerio-ppe run 3ZiiYjXkkWutOuj36 (dataset KTp63DwFefJBNFD8C),
saved as aptscom_raw.json. PARTIAL: run was killed at 139/~497 items when the Apify account hit its
$5 monthly usage hard limit. Search URL:
https://www.apartments.com/apartments-condos-houses-townhomes/tucson-az/1-to-3-bedrooms-700-to-2400/
One output row per floorplan (model) in range, like the zumper pipeline.
"""
import json, os, re, time

from laundry_rules import classify_tags as laundry_from_tags, classify_text as laundry_from_text

SCRATCH = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(SCRATCH, "aptscom_raw.json")
OUT = os.path.join(SCRATCH, "listings_aptscom.json")
GEO_CACHE = os.path.join(SCRATCH, "geo_cache_sources.json")

PRICE_MIN, PRICE_MAX = 700, 2400
BEDS_MIN, BEDS_MAX = 1, 3

LATLNG_RE = re.compile(r'"latitude":\s*"?(-?[\d.]+)"?.{0,80}?"longitude":\s*"?(-?[\d.]+)"?', re.S)
PRICE_RE = re.compile(r"\$([\d,]+)")
NO_PETS_RE = re.compile(r"(?:no|not)\s+(?:allow(?:ed)?\s+)?pets|pets?\s+(?:are\s+)?not\s+allowed|does\s+not\s+(?:allow|welcome)\s+pets", re.I)
WELCOMES_RE = re.compile(r"welcomes\s+pets|pet[- ]friendly|pets?\s+(?:are\s+)?(?:welcome|allowed)", re.I)

_cache = None

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

def geocode(loc):
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
        url = ("https://nominatim.openstreetmap.org/search?format=json&limit=1&q="
               + urllib.parse.quote(loc + ", USA"))
        req = urllib.request.Request(url, headers={"User-Agent": "tucson_home_finder_sources/1.0"})
        data = json.load(urllib.request.urlopen(req, timeout=15))
        c[loc] = [round(float(data[0]["lat"]), 5), round(float(data[0]["lon"]), 5)] if data else None
    except Exception:
        c[loc] = None
    try:
        json.dump(c, open(GEO_CACHE, "w", encoding="utf-8"))
    except Exception:
        pass
    v = c[loc]
    return tuple(v) if v else None


def pets_note_for(item):
    """FAQ 'Is X pet-friendly?' + amenity lists. Returns note or 'no pets'."""
    sd = item.get("structuredData") or {}
    blob = json.dumps(sd.get("faq") or {})
    amen = json.dumps(item.get("amenities") or [])
    cats = bool(re.search(r"Cats?\s+Allowed", amen, re.I))
    dogs = bool(re.search(r"Dogs?\s+Allowed", amen, re.I))
    if cats or dogs:
        parts = (["cats OK"] if cats else []) + (["dogs OK"] if dogs else [])
        return " + ".join(parts)
    m = re.search(r'pet-friendly\?".{0,400}', blob, re.I | re.S)
    ctx = m.group(0) if m else blob
    if NO_PETS_RE.search(ctx):
        return "no pets"
    if WELCOMES_RE.search(ctx):
        return "pets OK"
    return None


def laundry_for(item):
    """in_unit | onsite | None. Canonical amenity strings first ("Washer/Dryer",
    "Washer/Dryer Hookup" -> in_unit; "Laundry Facilities" -> onsite), then
    free-text fallback over the detail blobs. Never guessed."""
    tags = []
    for grp in item.get("amenities") or []:
        tags += [str(x) for x in (grp.get("value") or [])]
    v = laundry_from_tags(tags)
    if not v:
        blob = json.dumps({k: item.get(k) for k in
                           ("moreDetails", "propertyInformation", "details",
                            "structuredData", "description")}, ensure_ascii=False)
        v = laundry_from_text(blob)
    return v


def parse_model(model):
    """-> (price, beds, baths, sqft, availability) or None"""
    details = model.get("details") or []
    beds = baths = sqft = None
    for d in details:
        dm = re.match(r"([\d.]+)\s+Bed", d)
        if dm:
            beds = int(float(dm.group(1)))
        elif re.match(r"Studio", d, re.I):
            beds = 0
        dm = re.match(r"([\d.]+)\s+Bath", d)
        if dm:
            baths = float(dm.group(1))
        dm = re.match(r"([\d,]+)\s+Sq\s*Ft", d, re.I)
        if dm:
            sqft = int(dm.group(1).replace(",", ""))
    prices = []
    for u in model.get("units") or []:
        pm = PRICE_RE.search(u.get("price") or "")
        if pm:
            prices.append(int(pm.group(1).replace(",", "")))
    if not prices:
        pm = PRICE_RE.search(model.get("rentLabel") or "")
        if pm:
            prices.append(int(pm.group(1).replace(",", "")))
    if not prices:
        return None
    avail = None
    for u in model.get("units") or []:
        if u.get("availability"):
            avail = u["availability"]
            break
    return min(prices), beds, baths, sqft, avail


def main():
    items = json.load(open(RAW, encoding="utf-8"))
    rows, seen = [], set()
    for it in items:
        lid = it.get("listingId")
        sd = json.dumps((it.get("structuredData") or {}).get("allJsonLd") or "")
        m = LATLNG_RE.search(sd)
        lat, lng = (float(m.group(1)), float(m.group(2))) if m else (None, None)
        addr_parts = [it.get("streetAddress"), it.get("listingCity"), it.get("listingState")]
        addr = ", ".join(p for p in addr_parts if p)
        if it.get("listingZip"):
            addr += " " + str(it["listingZip"])
        if lat is None:
            cc = geocode(addr)
            if cc:
                lat, lng = cc
        pets = pets_note_for(it)
        if pets == "no pets":
            continue  # cat-hostile property
        phone = it.get("phoneNumber") or it.get("phone")
        base = {
            "laundry": laundry_for(it),
            "source": "apartments_com",
            "url": it.get("url"),
            "address": addr or (it.get("fullAddress") or None),
            "lat": lat, "lng": lng,
            "pets_note": pets,
            "property_type": (it.get("propertyType") or "apartment").lower(),
            "listed_date_or_age": None,
            "thumbnail_url": it.get("imageUrl") or None,
            "contact_phone": phone or None,
            "contact_name": it.get("propertyName") or it.get("listingName") or None,
            "contact_method": "phone" if phone else None,
        }
        emitted = 0
        for mi, model in enumerate(it.get("models") or []):
            parsed = parse_model(model)
            if not parsed:
                continue
            price, beds, baths, sqft, avail = parsed
            if not (PRICE_MIN <= price <= PRICE_MAX):
                continue
            if beds is None or not (BEDS_MIN <= beds <= BEDS_MAX):
                continue
            rid = f"apt-{lid}-m{mi}"
            if rid in seen:
                continue
            seen.add(rid)
            name = model.get("modelName") or ""
            title = f"{base['contact_name']}: {name}".strip(": ")
            rows.append({"id": rid, **base, "price": price, "beds": beds,
                         "baths": baths, "sqft": sqft,
                         "listed_date_or_age": f"available {avail}" if avail else None,
                         "title": title})
            emitted += 1
        if emitted == 0:
            # fallback: property-level row from listingMinRent
            price = it.get("listingMinRent")
            beds = it.get("minBeds")
            try:
                beds = int(beds)
            except (TypeError, ValueError):
                beds = None
            if price and PRICE_MIN <= price <= PRICE_MAX and beds is not None and BEDS_MIN <= beds <= BEDS_MAX:
                rid = f"apt-{lid}"
                if rid not in seen:
                    seen.add(rid)
                    rows.append({"id": rid, **base, "price": int(price), "beds": beds,
                                 "baths": None, "sqft": None,
                                 "title": base["contact_name"]})
    # normalize key order to schema
    order = ["id", "source", "url", "address", "lat", "lng", "price", "beds", "baths",
             "sqft", "pets_note", "property_type", "listed_date_or_age", "title",
             "thumbnail_url", "contact_phone", "contact_name", "contact_method", "laundry"]
    rows = [{k: r.get(k) for k in order} for r in rows]
    json.dump(rows, open(OUT, "w", encoding="utf-8"), indent=1)
    prices = sorted(r["price"] for r in rows)
    print(f"[OK] apartments_com: {len(rows)} rows from {len(items)} properties -> {OUT}")
    if prices:
        print(f"     price min/median/max: ${prices[0]} / ${prices[len(prices)//2]} / ${prices[-1]}")
    print(f"     phones: {sum(1 for r in rows if r['contact_phone'])}/{len(rows)}")
    print(f"     with coords: {sum(1 for r in rows if r['lat'] is not None)}/{len(rows)}")


if __name__ == "__main__":
    main()
