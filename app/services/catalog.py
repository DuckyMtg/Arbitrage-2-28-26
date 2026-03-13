# app/services/catalog.py
from __future__ import annotations

from typing import Dict, List, Optional, TypedDict

from app.services.set_registry import SET_REGISTRY, to_catalog_product


class ProductType(TypedDict, total=False):
    key:          str
    label:        str
    ebay_query:   str
    ev_set_code:  str
    ev_kind:      str
    product_kind: str
    ebay_filter:  str
    default_sort: str


# ---------------------------------------------------------------------------
# CATALOG
#
# Standard sets (those in SET_REGISTRY) are auto-generated — do not add them
# here manually.
#
# Only add entries below for:
#   • Sets with multiple product types (set box + draft box, play box + bundle)
#   • Sets with no EV model (displays, bundles)
#   • Products needing fields beyond what to_catalog_product() produces
# ---------------------------------------------------------------------------

_MANUAL_ENTRIES: Dict[str, List[ProductType]] = {
    "MH3": [
        {
            "key":          "play_box",
            "label":        "Play Booster Box",
            "ebay_query":   "Modern Horizons 3 play booster box",
            "ev_set_code":  "MH3",
            "ev_kind":      "box",
            "product_kind": "play_box",
        },
        {
            "key":        "bundle",
            "label":      "Bundle",
            "ebay_query": "Modern Horizons 3 bundle",
        },
    ],
    "OTJ": [
        {
            "key":          "play_box",
            "label":        "Play Booster Box",
            "ebay_query":   "Outlaws of Thunder Junction play booster box",
            "ev_set_code":  "OTJ",
            "ev_kind":      "box",
            "product_kind": "play_box",
        },
        {
            "key":        "bundle",
            "label":      "Bundle",
            "ebay_query": "Outlaws of Thunder Junction bundle",
        },
    ],
    "WOE": [
        {
            "key":          "set_box",
            "label":        "Set Booster Box",
            "ebay_query":   "Wilds of Eldraine set booster box",
            "ev_set_code":  "WOE",
            "ev_kind":      "box",
            "product_kind": "set_box",
        },
        {
            "key":          "draft_box",
            "label":        "Draft Booster Box",
            "ebay_query":   "Wilds of Eldraine draft booster box",
            "ev_set_code":  "WOE",
            "ev_kind":      "draft_box",
            "product_kind": "draft_box",
        },
        {
            "key":        "bundle",
            "label":      "Bundle",
            "ebay_query": "Wilds of Eldraine bundle",
        },
    ],
    "ECL": [
        {
            "key":          "play_box",
            "label":        "Play Booster Box",
            "ebay_query":   "Lorwyn Eclipsed play booster box",
            "ev_set_code":  "ECL",
            "ev_kind":      "box",
            "product_kind": "play_box",
        },
    ],
    "TLA": [
        {
            "key":          "play_box",
            "label":        "Play Booster Box",
            "ebay_query":   "The Last Airbender play booster box MTG",
            "ev_set_code":  "TLA",
            "ev_kind":      "box",
            "product_kind": "play_box",
        },
    ],
    "MB2": [
        {
            "key":        "display",
            "label":      "Mystery Booster 2 Display (24 packs)",
            "ebay_query": "Mystery Booster 2 display 24 packs",
        },
    ],
}


def _build_catalog() -> Dict[str, List[ProductType]]:
    """
    Merge auto-generated entries (SET_REGISTRY) with manual entries.
    Manual entries take precedence — any set_code in _MANUAL_ENTRIES is used
    as-is and skipped in the auto-generation pass.
    """
    catalog: Dict[str, List[ProductType]] = {}

    for set_code, defn in SET_REGISTRY.items():
        if set_code not in _MANUAL_ENTRIES:
            # type: ignore[list-item]
            catalog[set_code] = [to_catalog_product(defn)]

    catalog.update(_MANUAL_ENTRIES)
    return catalog


CATALOG: Dict[str, List[ProductType]] = _build_catalog()


def list_set_codes() -> List[str]:
    return sorted(CATALOG.keys())


def list_products_for_set(set_code: str) -> List[ProductType]:
    return CATALOG.get(set_code.strip().upper(), [])


def get_product(set_code: str, product_key: str) -> Optional[ProductType]:
    code = set_code.strip().upper()
    pk = product_key.strip()
    for p in CATALOG.get(code, []):
        if p.get("key") == pk:
            return p
    return None
