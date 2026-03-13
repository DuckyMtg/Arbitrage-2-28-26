# app/services/ebay_browse.py
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

from app.services import ev_cache
from app.services.ebay_auth import get_app_access_token

# FIX: EBAY_ENV was previously a module-level constant set at import time:
#   EBAY_ENV = os.getenv("EBAY_ENV", "production").lower()
# If the env var is injected by a secrets manager or config loader that runs
# after the module is first imported (common in containers), the frozen value
# would point to the wrong environment for the lifetime of the process.
# _ebay_env() reads os.getenv on every call so it always reflects the current
# environment. _base() and _stable_cache_key() both consume it.


def _ebay_env() -> str:
    return (os.getenv("EBAY_ENV", "production") or "production").strip().lower()


BASE_PROD = "https://api.ebay.com"
BASE_SANDBOX = "https://api.sandbox.ebay.com"
BROWSE_SEARCH_PATH = "/buy/browse/v1/item_summary/search"

DEFAULT_MARKETPLACE = os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")
SEARCH_TTL = int(os.getenv("EBAY_SEARCH_TTL", "30"))
DEFAULT_FILTER = os.getenv("EBAY_DEFAULT_FILTER", "conditionIds:{1000}")

# FIX: DEFAULT_CASE_QTY was a single global applied to every product kind when
# "case" appears in a title but no explicit count is present. Case sizes differ
# by product: collector boxes ship 4/case, draft boxes 10/case, everything
# else typically 6. Using 6 universally inflates per-box price on draft and
# deflates it on collector. product_kind is now threaded into _extract_box_qty.
_CASE_QTY_BY_KIND: dict[str, int] = {
    "play_box":      6,
    "set_box":       6,
    "draft_box":     10,
    "collector_box": 4,
}
_DEFAULT_CASE_QTY_FALLBACK = int(os.getenv("EBAY_DEFAULT_CASE_QTY", "6"))

# ---------------------------------------------------------------------------
# In-memory OAuth token cache
# ---------------------------------------------------------------------------
_TOKEN_REFRESH_BUFFER = 5 * 60  # seconds before expiry to proactively refresh


@dataclass
class _CachedToken:
    value:      str
    expires_at: float  # monotonic seconds


_cached_token: Optional[_CachedToken] = None


def _get_token() -> str:
    global _cached_token
    now = time.monotonic()
    if (
        _cached_token is not None
        and now < _cached_token.expires_at - _TOKEN_REFRESH_BUFFER
    ):
        return _cached_token.value
    token = get_app_access_token()
    _cached_token = _CachedToken(value=token, expires_at=now + 120 * 60)
    return token


# ---------------------------------------------------------------------------
# Title-filtering constants
# ---------------------------------------------------------------------------

_BOX_HINTS = (
    "booster box",
    "set booster box",
    "draft booster box",
    "play booster box",
    "collector booster box",
    "display box",
    "sealed box",
    "booster display",
)

_ALWAYS_REJECT = (
    "digital",
    "code",
    "preorder",
    "pre-order",
    "prerelease",
    "empty",
    "wrapper",
    "japanese",
    "korean",
    "french",
    "german",
    "italian",
    "spanish",
    "portuguese",
    "chinese",
    "deutsch",
    "francais",
    "espanol",
    "italiano",
    "jp edition",
)

_PACK_ONLY_HINTS = (
    "1 pack",
    "single pack",
    "loose pack",
    "individual pack",
    "3 pack",
    "6 pack",
    "pack lot",
    "packs lot",
    "1 booster",
    "single booster",
    "1x booster pack",
    "one booster",
    "- 1 count",
    "qty 1",
    "quantity 1",
    "x1 booster",
)

_CROSS_TYPE_REJECTS: dict[str, tuple[str, ...]] = {
    "draft_box": ("set booster",),
    "set_box":   ("draft booster",),
    "play_box":  ("draft booster", "set booster", "jumpstart"),
}

_LOT_HINTS = ("lot", "bundle", "bulk")


def _base() -> str:
    # FIX: calls _ebay_env() rather than reading the old module-level constant
    return BASE_SANDBOX if _ebay_env() == "sandbox" else BASE_PROD


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _has_any(t: str, phrases: tuple[str, ...]) -> bool:
    return any(p in t for p in phrases)


def _is_box_intent(title: str, product_kind: str | None = None) -> bool:
    t = _norm(title)

    if _has_any(t, _ALWAYS_REJECT):
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


def _extract_box_qty(title: str, product_kind: str | None = None) -> int:
    t = _norm(title)

    # Prefix: "2 booster boxes", "2x booster box"
    m = re.search(r"\b(\d+)\s*(?:x\s*)?(?:booster\s*)?box(?:es)?\b", t)
    if m:
        return max(1, int(m.group(1)))

    # FIX: Suffix quantity — "booster box x2", "booster box (x2)", "booster box - 2"
    # Sellers often append quantity at the end of the title after the product name.
    m = re.search(r"\bbox(?:es)?\s*[-–(]?\s*x?\s*(\d+)\s*[)]?\b", t)
    if m:
        return max(1, int(m.group(1)))

    m = re.search(r"\blot\s+of\s+(\d+)\b", t)
    if m and ("box" in t or "booster" in t or "display" in t):
        return max(1, int(m.group(1)))

    m = re.search(r"\bcase\s+(?:of\s+)?(\d+)\b", t)
    if m:
        return max(1, int(m.group(1)))

    # "case" with no count — use the per-product-kind default
    if "case" in t:
        return max(1, _CASE_QTY_BY_KIND.get(product_kind or "", _DEFAULT_CASE_QTY_FALLBACK))

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
    opts = summary.get("shippingOptions")
    if isinstance(opts, list) and opts:
        v, c = _extract_money(opts[0].get("shippingCost"))
        if v is not None:
            return v, c
    v, c = _extract_money(summary.get("shippingCost"))
    if v is not None:
        return v, c
    return None, None


def _extract_ship_type(summary: Dict[str, Any]) -> Optional[str]:
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
    normalized_price: Optional[float]
    normalized_price_per_box: Optional[float]
    boxes: int
    currency: Optional[str]
    endDate: Optional[str]
    url: Optional[str]


def simplify_item_summary(summary: Dict[str, Any], product_kind: str | None = None) -> Dict[str, Any]:
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

    boxes = _extract_box_qty(title, product_kind)

    normalized_per_box: Optional[float] = None
    if normalized_total is not None and boxes > 0:
        normalized_per_box = normalized_total / float(boxes)

    return {
        "title":                    title,
        "itemId":                   summary.get("itemId"),
        "price":                    price_v,
        "shipping":                 ship_v,
        "shipping_known":           shipping_known,
        "shipType":                 ship_type,
        "normalized_price":         normalized_total,
        "boxes":                    boxes,
        "normalized_price_per_box": normalized_per_box,
        "currency":                 currency,
        "endDate":                  summary.get("itemEndDate"),
        "url":                      summary.get("itemWebUrl"),
    }


def _stable_cache_key(params: Dict[str, Any], marketplace_id: str, product_kind: str | None) -> str:
    # FIX: calls _ebay_env() so the cache key reflects the environment at
    # request time, not the value frozen at import time.
    env = _ebay_env()
    blob = json.dumps(
        {"marketplace": marketplace_id, "product_kind": product_kind, "params": params},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"ebay:search:{env}:{marketplace_id}:{product_kind or 'none'}:{ev_cache._sha1(blob)}"


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

    filter_effective = filter_ if (
        filter_ and filter_.strip()) else (DEFAULT_FILTER or None)

    params: Dict[str, Any] = {"q": q, "limit": limit, "offset": offset}
    if filter_effective:
        params["filter"] = filter_effective
    if sort:
        params["sort"] = sort

    cache_key = _stable_cache_key(params, marketplace_id, product_kind)

    if use_cache and SEARCH_TTL > 0:
        cached = ev_cache.cache_get_json(cache_key)
        if isinstance(cached, dict):
            return cached

    lock = ev_cache.RedisLock(ev_cache.key_lock(cache_key), ttl_s=15)
    got_lock = lock.acquire()

    if not got_lock and use_cache and SEARCH_TTL > 0:
        waited = ev_cache.wait_for_key(cache_key, wait_s=2.0)
        if isinstance(waited, dict):
            return waited

    try:
        token = _get_token()
        headers = {
            "Authorization":           f"Bearer {token}",
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

        simplified = [simplify_item_summary(it, product_kind)
                      for it in items if isinstance(it, dict)]

        if product_kind in {"play_box", "set_box", "draft_box", "collector_box"}:
            simplified = [it for it in simplified if _is_box_intent(
                it.get("title") or "", product_kind)]

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
