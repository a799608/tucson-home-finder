"""
Zillow Tucson rentals -> listings_zillow.json
Data source: Apify actor maxcopell/zillow-scraper (run mB2R3IUurGN5ge4eN, dataset v7vrZHPfolv7Axhg5),
raw copy saved as zillow_raw.json by the run pipeline. Re-fetch with --fetch if needed.
Search: Tucson AZ map bounds (31.95..32.45, -111.15..-110.70), for-rent, $700-2400, 1-3 beds.
"""
import json, os, re, sys

from laundry_rules import classify_text as laundry_from_text

SCRATCH = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(SCRATCH, "zillow_raw.json")
OUT = os.path.join(SCRATCH, "listings_zillow.json")
DATASET_ID = "v7vrZHPfolv7Axhg5"

PRICE_MIN, PRICE_MAX = 700, 2400
BEDS_MIN, BEDS_MAX = 1, 3

TYPE_MAP = {
    "Apartment for rent": "apartment",
    "House for rent": "house",
    "Home for rent": "house",
    "Condo for rent": "condo",
    "Townhouse for rent": "townhouse",
}


def fetch_raw():
    import urllib.request
    tok = os.environ["APIFY_TOKEN"]
    u = f"https://api.apify.com/v2/datasets/{DATASET_ID}/items?token={tok}&format=json"
    raw = urllib.request.urlopen(u).read()
    open(RAW, "wb").write(raw)


def parse_unit_price(s):
    m = re.search(r"\$([\d,]+)", s or "")
    return int(m.group(1).replace(",", "")) if m else None


def laundry_for(it):
    """in_unit | onsite | None from the only laundry-bearing text the map-search
    dataset carries: homeInsight display strings + the address/title blob.
    factsAndFeatures has no laundry flag; null means the source said nothing."""
    bits = [it.get("address") or ""]
    lcr = it.get("listCardRecommendation") or {}
    for rec in (lcr.get("flexFieldRecommendations") or []):
        bits.append(str(rec.get("displayString") or ""))
    return laundry_from_text(" | ".join(bits))


def days_age(it):
    d = it.get("daysOnZillow")
    if d is None or d < 0:
        return None
    if d == 0:
        return "listed today"
    return f"{d} days on Zillow"


def main():
    if "--fetch" in sys.argv or not os.path.exists(RAW):
        fetch_raw()
    items = json.load(open(RAW, encoding="utf-8"))
    rows, seen = [], set()
    for it in items:
        lat = (it.get("latLong") or {}).get("latitude")
        lng = (it.get("latLong") or {}).get("longitude")
        addr = it.get("address")
        thumb = it.get("imgSrc")
        if it.get("isBuilding"):
            # one row per advertised unit tier within range
            lot = it.get("lotId") or it.get("providerListingId") or it.get("id")
            for u in it.get("units") or []:
                if u.get("roomForRent"):
                    continue
                price = parse_unit_price(u.get("price"))
                try:
                    beds = int(u.get("beds"))
                except (TypeError, ValueError):
                    beds = None
                if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
                    continue
                if beds is None or not (BEDS_MIN <= beds <= BEDS_MAX):
                    continue
                rid = f"zil-b{lot}-{beds}br"
                if rid in seen:
                    continue
                seen.add(rid)
                rows.append({
                    "id": rid, "source": "zillow",
                    "url": it.get("detailUrl"),
                    "address": addr, "lat": lat, "lng": lng,
                    "price": price, "beds": beds, "baths": None, "sqft": None,
                    "pets_note": None,
                    "property_type": "apartment",
                    "listed_date_or_age": days_age(it),
                    "title": (it.get("buildingName") or it.get("statusText") or None),
                    "thumbnail_url": thumb,
                    "contact_phone": None, "contact_name": None, "contact_method": None,
                    "laundry": laundry_for(it),
                })
        else:
            price = it.get("unformattedPrice") or parse_unit_price(it.get("price"))
            beds = it.get("beds")
            baths = it.get("baths")
            if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
                continue
            if beds is None or not (BEDS_MIN <= int(beds) <= BEDS_MAX):
                continue
            zpid = it.get("zpid") or it.get("id")
            rid = f"zil-{zpid}"
            if rid in seen:
                continue
            seen.add(rid)
            broker = it.get("broker") or it.get("brokerName")
            rows.append({
                "id": rid, "source": "zillow",
                "url": it.get("detailUrl"),
                "address": addr, "lat": lat, "lng": lng,
                "price": int(price), "beds": int(beds),
                "baths": float(baths) if baths is not None else None,
                "sqft": int(it["area"]) if it.get("area") else None,
                "pets_note": None,
                "property_type": TYPE_MAP.get(it.get("statusText"), None),
                "listed_date_or_age": days_age(it),
                "title": it.get("statusText"),
                "thumbnail_url": thumb,
                "contact_phone": None,
                "contact_name": broker if isinstance(broker, str) and broker else None,
                "contact_method": None,
                "laundry": laundry_for(it),
            })
    json.dump(rows, open(OUT, "w", encoding="utf-8"), indent=1)
    prices = sorted(r["price"] for r in rows)
    print(f"[OK] zillow: {len(rows)} rows -> {OUT}")
    if prices:
        print(f"     price min/median/max: ${prices[0]} / ${prices[len(prices)//2]} / ${prices[-1]}")
    from collections import Counter
    print("     types:", dict(Counter(r['property_type'] for r in rows)))


if __name__ == "__main__":
    main()
