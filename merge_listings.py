"""Merge all listing-source JSONs into one deduped listings.json for the map.

Sources (same schema, in priority order — earlier wins on conflicts, but
non-null contact/utilities/pets fields are back-filled from any duplicate):
  listings.json          (zumper, contact-enriched)  [required]
  listings_zillow.json   (zil-)                      [optional]
  listings_aptscom.json  (apt-)                      [optional]
  listings_fb.json       (fb-)                       [optional]

Usage: python merge_listings.py  -> rewrites listings.json (backup kept once
as listings_zumper_only.json on first run).
"""
import json
import os
import re
import sys

DIR = os.path.dirname(os.path.abspath(__file__))

PRIORITY = [
    ("listings.json", None),
    ("listings_zillow.json", "zillow"),
    ("listings_aptscom.json", "apartments.com"),
    ("listings_fb.json", "facebook"),
]

MERGE_FIELDS = ["contact_phone", "contact_name", "contact_method",
                "utilities_note", "cats_ok", "sqft", "thumbnail_url", "laundry"]


def norm_addr(a):
    if not a:
        return ""
    a = a.upper()
    a = re.sub(r"[^A-Z0-9 ]", " ", a)
    a = re.sub(r"\b(APT|UNIT|STE|SUITE|#)\s*\S+", "", a)
    a = re.sub(r"\s+", " ", a).strip()
    # street-number + first two words is enough to match across sites
    return " ".join(a.split()[:4])


def keys_for(l):
    ks = []
    if l.get("lat") and l.get("lng") and l.get("price"):
        ks.append(("geo", round(l["lat"], 4), round(l["lng"], 4), l["price"]))
    na = norm_addr(l.get("address"))
    if na and l.get("price"):
        ks.append(("addr", na, l["price"]))
    return ks


def main():
    merged, index = [], {}
    stats = {}
    for fname, _tag in PRIORITY:
        path = os.path.join(DIR, fname)
        if not os.path.exists(path):
            stats[fname] = "missing"
            continue
        rows = json.load(open(path, encoding="utf-8"))
        kept = dupes = 0
        for l in rows:
            hit = None
            for k in keys_for(l):
                if k in index:
                    hit = index[k]
                    break
            if hit is not None:
                dupes += 1
                for f in MERGE_FIELDS:  # back-fill missing fields from the dupe
                    if hit.get(f) in (None, "") and l.get(f) not in (None, ""):
                        hit[f] = l[f]
                continue
            merged.append(l)
            kept += 1
            for k in keys_for(l):
                index[k] = l
        stats[fname] = f"{kept} kept, {dupes} dupes"

    backup = os.path.join(DIR, "listings_zumper_only.json")
    src = os.path.join(DIR, "listings.json")
    if not os.path.exists(backup):
        os.replace(src, backup)
    json.dump(merged, open(src, "w", encoding="utf-8"), ensure_ascii=False)

    print(f"TOTAL merged: {len(merged)}")
    for f, s in stats.items():
        print(f"  {f}: {s}")
    phones = sum(1 for l in merged if l.get("contact_phone"))
    utils = sum(1 for l in merged if l.get("utilities_note"))
    cats = sum(1 for l in merged if l.get("cats_ok") is True)
    print(f"  contact_phone: {phones}  utilities_note: {utils}  cats_ok=true: {cats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
