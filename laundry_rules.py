"""Shared laundry classifier for the Tucson home-finder pipeline.

classify_tags(tags)  -> "in_unit" | "onsite" | None   (structured amenity strings)
classify_text(text)  -> "in_unit" | "onsite" | None   (free description text)
classify(tags, text) -> tags first, then text fallback.

Semantics (2026-07-26 spec):
  in_unit = washer/dryer (or W/D hookups) in the unit itself
  onsite  = shared laundry room/facility on the property
  None    = source says nothing - NEVER guessed
in_unit outranks onsite when both are present.
Copied from source data only; "Dishwasher" is explicitly excluded.
"""
import re

_DISH_RE = re.compile(r"dish\s*washers?(?:/disposal)?", re.I)
_LAUNDRY_WORD_RE = re.compile(r"laundry|washer|dryer|\bw\s*/\s*d\b|\bw&d\b", re.I)
# shared/onsite markers inside an amenity tag
_SHARED_TAG_RE = re.compile(r"on-?\s*site|community|shared|coin|facilit|laundry\s+room|"
                            r"pay\s+laundry|laundry\s+(?:center|cards?)", re.I)
# in-unit markers inside an amenity tag (dishwasher already stripped)
_INUNIT_TAG_RE = re.compile(r"washer|dryer|\bw\s*/\s*d\b|\bw&d\b|hook-?\s*up|in[- ]?unit|in[- ]?home", re.I)

# free-text patterns (description prose). Deliberately explicit - bare "washer"
# in prose is NOT enough; pairs/hookups/in-unit phrasing only.
_INUNIT_TEXT_RE = re.compile(
    r"\bin[- ]?unit (?:laundry|washer)|\blaundry in (?:the )?(?:unit|home|house)\b|"
    r"\bwashers?\s*(?:and|&|\+|/|-)\s*dryers?\b|\bw\s*/\s*d\b|\bw&d\b|"
    r"\b(?:washer|dryer|laundry|w/?d)\s*(?:and dryer\s*)?hook-?\s*ups?\b|"
    r"\bwasher\s*(?:and|&|/)?\s*dryer (?:included|units?|set)\b", re.I)
_ONSITE_TEXT_RE = re.compile(
    r"\bon-?\s?site laundry\b|\blaundry (?:on-?\s?site|room|facilit\w*|center)\b|"
    r"\bcommunity laundry\b|\bshared laundry\b|\bcoin(?:-?op\w*)? laundry\b|"
    r"\blaundry (?:is |are )?(?:available )?on (?:the )?(?:site|premises|property)\b", re.I)
_NEG_RE = re.compile(r"\b(?:no|without|not)\b[^.;,]{0,15}$", re.I)


def _strip_dish(s):
    return _DISH_RE.sub(" ", s or "")


def _tag_class(tag):
    t = _strip_dish(str(tag))
    if not _LAUNDRY_WORD_RE.search(t):
        return None
    if _SHARED_TAG_RE.search(t):
        return "onsite"
    if _INUNIT_TAG_RE.search(t):
        return "in_unit"
    return None            # bare "Laundry" etc. - too vague, never guess


def classify_tags(tags):
    got = {_tag_class(t) for t in (tags or [])}
    if "in_unit" in got:
        return "in_unit"
    if "onsite" in got:
        return "onsite"
    return None


def _text_hits(rx, text):
    for m in rx.finditer(text):
        if _NEG_RE.search(text[max(0, m.start() - 24):m.start()]):
            continue       # "no washer/dryer", "without laundry hookups"
        return True
    return False


def classify_text(text):
    t = _strip_dish(text)
    if not t:
        return None
    if _text_hits(_INUNIT_TEXT_RE, t):
        return "in_unit"
    if _text_hits(_ONSITE_TEXT_RE, t):
        return "onsite"
    return None


def classify(tags=None, text=None):
    return classify_tags(tags) or classify_text(text)
