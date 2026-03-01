# app/services/catalog.py
from __future__ import annotations

from typing import Dict, List, Optional, TypedDict


class ProductType(TypedDict, total=False):
    key: str
    label: str
    ebay_query: str

    # NEW: how to compute EV for this product (optional for now)
    ev_set_code: str      # e.g. "MH3", "OTJ", "WOE"
    ev_kind: str          # "box" (future: "bundle", "collector_box", etc.)

    ebay_filter: str
    default_sort: str


CATALOG: Dict[str, List[ProductType]] = {
    "MH3": [
        {
            "key": "play_box",
            "label": "Play Booster Box",
            "ebay_query": "Modern Horizons 3 play booster box",
            "ev_set_code": "MH3",
            "ev_kind": "box",
        },
        {"key": "bundle", "label": "Bundle",
            "ebay_query": "Modern Horizons 3 bundle"},
    ],
    "OTJ": [
        {
            "key": "play_box",
            "label": "Play Booster Box",
            "ebay_query": "Outlaws of Thunder Junction play booster box",
            "ev_set_code": "OTJ",
            "ev_kind": "box",
        },
        {"key": "bundle", "label": "Bundle",
            "ebay_query": "Outlaws of Thunder Junction bundle"},
    ],
    "WOE": [
        {
            "key": "set_box",
            "label": "Set Booster Box",
            "ebay_query": "Wilds of Eldraine set booster box",
            "ev_set_code": "WOE",
            "ev_kind": "box",
        },
        {
            "key": "draft_box",
            "label": "Draft Booster Box",
            "ebay_query": "Wilds of Eldraine draft booster box",
            "ev_set_code": "WOE",
            "ev_kind": "draft_box",
        },
        {"key": "bundle", "label": "Bundle",
            "ebay_query": "Wilds of Eldraine bundle"},
    ],
    "ECL": [
        {
            "key": "play_box",
            "label": "Play Booster Box",
            "ebay_query": "Lorwyn Eclipsed play booster box",
            "ev_set_code": "ECL",
            "ev_kind": "box",
        },
    ],
    "TLA": [
        {
            "key": "play_box",
            "label": "Play Booster Box",
            "ebay_query": "The Last Airbender play booster box MTG",
            "ev_set_code": "TLA",
            "ev_kind": "box",
        },
    ],
}


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
