# Tucson Home Finder

Interactive neighborhood + rental-listing finder for a move to Tucson, AZ, anchored on the
Raytheon (RTX) airport campus at 1151 E Hermans Rd (32.1051, -110.9456).

**Live site:** https://a799608.github.io/tucson-home-finder/

## What it does
- Leaflet map of 16 Tucson neighborhoods, each scored 0–5 on ~20 attributes
  (live jazz, live music, nightlife, rock climbing, hiking, walkability,
  Sun Link streetcar access, safety, cat-friendly rental stock, etc.)
- Interest toggles + max-rent slider + commute-importance slider recolor the
  map and re-rank all neighborhoods live (green / blue / orange / gray)
- "Son's preset" button loads the default profile (jazz + live music + climbing
  + hiking + young-professional + restaurants + gym, nightlife off)
- Listings layer (`listings.json`): current rental listings in the $700–$2,400
  band, cat-friendly, plotted as pins — refreshed by a local scan script and
  pushed here (GitHub Pages serves the update in ~1 min)
- Feedback loop: rejecting a listing (with a reason) posts to an Apps Script
  endpoint → Google Sheet; the next refresh hides rejected listings and shifts
  preference weights based on recurring rejection reasons

## Data pipeline (runs on Will's PC)
- `listings_scan.py` → regenerates `listings.json` (Zumper / ApartmentList /
  Craigslist, deduped, geocoded via Nominatim with cache)
- `drive_times.json` → measured weekday rush-hour drive times (7:15 AM / 4:45 PM)
  per neighborhood to the Raytheon campus; merged into commute scores
- Push model: data files are committed only when changed (same pattern as
  mvp-rentals-website `publish_availability.py`)

## Status
Work in progress — control panel v2 (sliders + full toggle set) and the
listings layer land next. Rush-hour drive-time calibration is the final pass.
