# app/services/ebay_browse.py
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

import requests

from app.services import ev_cache
from app.services.ebay_auth import get_app_access_token

EBAY_ENV = os.getenv("EBAY_ENV", "production").lower()

BASE_PROD = "https://api.ebay.com"
BASE_SANDBOX = "https://api.sandbox.ebay.com"
BROWSE_SEARCH_PATH = "/buy/browse/v1/item_summary/search"

DEFAULT_MARKETPLACE = os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")
SEARCH_TTL = int(os.getenv("EBAY_SEARCH_TTL", "30"))

# Default "sealed-friendly" filter (override by passing filter_)
DEFAULT_FILTER = os.getenv("EBAY_DEFAULT_FILTER", "conditionIds:{1000}")  # New

# If title says "case" but no explicit count, assume typical case size (configurable)
DEFAULT_CASE_QTY = int(os.getenv("EBAY_DEFAULT_CASE_QTY", "6"))

# Phrases that strongly indicate "this is a box listing"
_BOX_HINTS = (
    "booster box",
    "set booster box",
    "draft booster box",
    "play booster box",
    "collector booster box",
    "display box",
    "booster display",
)

# Phrases that are almost always garbage for sealed box sniping
_ALWAYS_REJECT = (
    "digital",
    "code",
    "preorder",
    "pre-order",
    "pre order",
    "prerelease",
    "empty",
    "wrapper",
    "jumpstart",       # Jumpstart packs/products are never sealed booster boxes
)

# Phrases that indicate "packs only" / loose packs / pack lots (not a box)
_PACK_ONLY_HINTS = (
    "1 pack",
    "single pack",
    "loose pack",
    "individual pack",
    "3 pack",
    "6 pack",
    "pack lot",
    "packs lot",
    "single booster",  # e.g. "single booster pack"
    "1x booster",      # e.g. "1x booster pack"
    "loose booster",   # e.g. "loose booster pack"
    "play booster pack",  # single play booster pack (not a box)
)

# Non-English language signals — reject any listing containing these tokens.
# MTG arbitrage targets US English listings; foreign language editions have
# different print runs, EV profiles, and may be grey-market imports.
_LANGUAGE_REJECTS = (
    "japanese",
    "japan",
    "french",
    "francais",
    "german",
    "deutsch",
    "spanish",
    "espanol",
    "italian",
    "italiano",
    "korean",
    "chinese",
    "portuguese",
    "russian",
    "ITA",
    "ita",
    "JAP",
    "jap",
)


class ProductKind(str, Enum):
    PLAY_BOX = "play_box"
    SET_BOX = "set_box"
    DRAFT_BOX = "draft_box"
    COLLECTOR_BOX = "collector_box"


# Set boosters aren't returned when you call draft boosters and vice versa;
# bundles and theme boosters are never any of these box types.
_CROSS_TYPE_REJECTS: dict[str, tuple[str, ...]] = {
    ProductKind.DRAFT_BOX:      ("set booster", "bundle", "theme booster"),
    ProductKind.SET_BOX:        ("draft booster", "bundle", "theme booster"),
    ProductKind.PLAY_BOX:       ("draft booster", "set booster", "bundle", "theme booster"),
    ProductKind.COLLECTOR_BOX:  ("draft booster", "set booster", "bundle", "theme booster"),
}

# "lot/bundle/bulk" tends to be non-box; treat as reject unless "box" intent is present
_LOT_HINTS = ("lot", "bundle", "bulk")


# Generic MTG product terms that don't identify a specific set
_QUERY_STOPWORDS = frozenset({
    "mtg", "magic", "the", "gathering", "play", "booster", "box",
    "draft", "set", "collector", "display", "sealed", "and", "of",
    "in", "a", "an", "for", "from", "beyond", "new",
})


def _meaningful_query_words(query: str) -> tuple[str, ...]:
    """
    Extract set-identifying words from an eBay search query by stripping generic
    MTG booster terms. Used to reject listings that match product-kind but are
    for a completely different set (e.g., TMNT showing up in an MH3 search).
    """
    words = re.findall(r"[a-z0-9]+", (query or "").lower())
    return tuple(w for w in words if w not in _QUERY_STOPWORDS)


def _base() -> str:
    return BASE_SANDBOX if EBAY_ENV == "sandbox" else BASE_PROD


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _has_any(t: str, phrases: tuple[str, ...]) -> bool:
    return any(p in t for p in phrases)


def _is_box_intent(title: str, product_kind: str | None = None) -> bool:
    t = _norm(title)

    if _has_any(t, _ALWAYS_REJECT):
        return False

    if _has_any(t, _LANGUAGE_REJECTS):
        return False

    if _has_any(t, _PACK_ONLY_HINTS):
        return False

    if product_kind:
        cross_rejects = _CROSS_TYPE_REJECTS.get(product_kind, ())
        if _has_any(t, cross_rejects):
            return False

    if _has_any(t, _BOX_HINTS):
        return True

    if re.search(r"\bcase\s+of\s+\d+|\bcase\s+\d+|\b\d+\s*(?:x\s*)?\bcase\b", t):
        if "booster" in t or "display" in t or "sealed" in t or "box" in t:
            return True

    if _has_any(t, _LOT_HINTS) and "box" not in t and "booster" not in t and "display" not in t:
        return False

    return False


def _extract_box_qty(title: str) -> int:
    """
    Heuristic: infer number of boxes in the listing title.

    Examples:
      - "2 booster boxes" -> 2
      - "lot of 3 booster boxes" -> 3
      - "sealed case of 6" -> 6
      - "case" (no number) -> DEFAULT_CASE_QTY
    """
    t = _norm(title)

    # e.g. "2 booster boxes", "3 boxes", "2x booster box"
    m = re.search(r"\b(\d+)\s*(?:x\s*)?(?:booster\s*)?box(?:es)?\b", t)
    if m:
        return max(1, int(m.group(1)))

    # e.g. "lot of 2 booster boxes"
    m = re.search(r"\blot\s+of\s+(\d+)\b", t)
    if m and ("box" in t or "booster" in t or "display" in t):
        return max(1, int(m.group(1)))

    # e.g. "case of 6", "case 6"
    m = re.search(r"\bcase\s+(?:of\s+)?(\d+)\b", t)
    if m:
        return max(1, int(m.group(1)))

    # If it says "case" but no number, assume a typical case size
    if "case" in t:
        return max(1, DEFAULT_CASE_QTY)

    return 1


def _to_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _extract_money(m: Any) -> tuple[Optional[float], Optional[str]]:
    if not isinstance(m, dict):
        return None, None
    return _to_float(m.get("value")), m.get("currency")


def _extract_shipping(summary: Dict[str, Any]) -> tuple[Optional[float], Optional[str]]:
    # preferred: shippingOptions[0].shippingCost
    opts = summary.get("shippingOptions")
    if isinstance(opts, list) and opts:
        v, c = _extract_money(opts[0].get("shippingCost"))
        if v is not None:
            return v, c

    # fallback: top-level shippingCost
    v, c = _extract_money(summary.get("shippingCost"))
    if v is not None:
        return v, c

    return None, None


def _extract_ship_type(summary: Dict[str, Any]) -> Optional[str]:
    """
    Best-effort shipType extraction.
    eBay sometimes exposes shipping type fields on shippingOptions.
    """
    opts = summary.get("shippingOptions")
    if isinstance(opts, list) and opts:
        opt0 = opts[0]
        if isinstance(opt0, dict):
            for k in ("shippingCostType", "shipType", "shippingType", "shippingOptionType"):
                v = opt0.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()

    for k in ("shippingCostType", "shipType", "shippingType"):
        v = summary.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    return None


@dataclass(frozen=True)
class SimplifiedItem:
    title: Optional[str]
    itemId: Optional[str]
    price: Optional[float]
    shipping: Optional[float]
    shipping_known: bool
    shipType: Optional[str]
    normalized_price: Optional[float]  # total listing (item + shipping)
    normalized_price_per_box: Optional[float]
    boxes: int
    currency: Optional[str]
    endDate: Optional[str]
    url: Optional[str]


def simplify_item_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    title = summary.get("title") or ""

    price_v, price_ccy = _extract_money(summary.get("price"))
    ship_v, ship_ccy = _extract_shipping(summary)
    ship_type = _extract_ship_type(summary)

    currency = price_ccy or ship_ccy

    shipping_known = ship_v is not None
    ship_for_calc = ship_v if ship_v is not None else 0.0

    normalized_total: Optional[float] = None
    if price_v is not None:
        normalized_total = price_v + ship_for_calc

    boxes = _extract_box_qty(title)

    normalized_per_box: Optional[float] = None
    if normalized_total is not None and boxes > 0:
        normalized_per_box = normalized_total / float(boxes)

    return {
        "title":                   title,
        "itemId":                  summary.get("itemId"),
        "price":                   price_v,
        "shipping":                ship_v,
        "shipping_known":          shipping_known,
        "shipType":                ship_type,
        "normalized_price":        normalized_total,
        "boxes":                   boxes,
        "normalized_price_per_box": normalized_per_box,
        "currency":                currency,
        "endDate":                 summary.get("itemEndDate"),
        "url":                     summary.get("itemWebUrl"),
    }


def _stable_cache_key(
    params: Dict[str, Any], marketplace_id: str, product_kind: str | None
) -> str:
    """
    Include product_kind in the cache key so box and generic searches don't
    collide, and future product types can share the same query params safely.
    """
    blob = json.dumps(
        {"marketplace": marketplace_id, "product_kind": product_kind, "params": params},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"ebay:search:{EBAY_ENV}:{ev_cache._sha1(blob)}"


def search_items_simplified(
    *,
    q: str,
    filter_: str | None = None,
    sort: str | None = None,
    limit: int = 50,
    offset: int = 0,
    marketplace_id: str = DEFAULT_MARKETPLACE,
    use_cache: bool = True,
    product_kind: str | None = None,
) -> Dict[str, Any]:
    if not q or not q.strip():
        raise ValueError("q is required")

    limit = max(1, min(int(limit), 200))
    offset = max(0, min(int(offset), 10_000))

    # default sealed-friendly filter if caller doesn't provide one
    filter_effective = filter_ if (
        filter_ and filter_.strip()) else (DEFAULT_FILTER or None)

    params: Dict[str, Any] = {"q": q, "limit": limit, "offset": offset}
    if filter_effective:
        params["filter"] = filter_effective
    if sort:
        params["sort"] = sort

    cache_key = _stable_cache_key(params, marketplace_id, product_kind)

    # 1) Cache hit
    if use_cache and SEARCH_TTL > 0:
        cached = ev_cache.cache_get_json(cache_key)
        if isinstance(cached, dict):
            return cached

    # 2) Stampede lock: only one request should hit eBay on a cold cache
    lock = ev_cache.RedisLock(ev_cache.key_lock(cache_key), ttl_s=15)
    got_lock = lock.acquire()

    if not got_lock and use_cache and SEARCH_TTL > 0:
        waited = ev_cache.wait_for_key(cache_key, wait_s=2.0)
        if isinstance(waited, dict):
            return waited
        # best-effort fallback: still do the request

    try:
        token = get_app_access_token()
        headers = {
            "Authorization":          f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace_id,
            "Accept":                  "application/json",
        }

        url = _base() + BROWSE_SEARCH_PATH
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        raw = r.json()

        items = raw.get("itemSummaries") or []
        if not isinstance(items, list):
            items = []

        simplified = [simplify_item_summary(it)
                      for it in items if isinstance(it, dict)]

        # Box-only mode: keep ONLY box/case/multi-box listings
        if product_kind in {pk.value for pk in ProductKind}:
            simplified = [
                it for it in simplified
                if _is_box_intent(it.get("title") or "", product_kind)
            ]
            # Set-identity guard: require that the title contains at least one
            # meaningful (set-identifying) word from the search query. This
            # rejects listings that match product-kind but are for an entirely
            # different set (e.g., a TMNT box returned by an MH3 search).
            set_words = _meaningful_query_words(q)
            if set_words:
                simplified = [
                    it for it in simplified
                    if any(w in _norm(it.get("title") or "") for w in set_words)
                ]

        # Sort by per-box price (best for sniping); fallback to total; None last
        simplified.sort(
            key=lambda x: (
                x.get("normalized_price_per_box") is None,
                x.get("normalized_price_per_box", float("inf")),
                x.get("normalized_price") is None,
                x.get("normalized_price", float("inf")),
            )
        )

        out: Dict[str, Any] = {
            "q":              q,
            "filter":         filter_effective,
            "sort":           sort,
            "product_kind":   product_kind,
            "marketplace_id": marketplace_id,
            "total":          raw.get("total"),
            "limit":          raw.get("limit"),
            "offset":         raw.get("offset"),
            "items":          simplified,
        }

        if use_cache and SEARCH_TTL > 0:
            ev_cache.cache_set_json(cache_key, out, SEARCH_TTL)

        return out

    finally:
        if got_lock:
            lock.release()
