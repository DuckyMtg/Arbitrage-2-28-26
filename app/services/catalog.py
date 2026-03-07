# app/services/catalog.py
from __future__ import annotations

from typing import Dict, List, Optional, TypedDict


class ProductType(TypedDict, total=False):
    key: str
    label: str
    ebay_query: str

    # How to compute EV for this product (optional)
    ev_set_code: str   # e.g. "MH3", "OTJ", "WOE"
    ev_kind: str       # "box" (future: "bundle", "collector_box", etc.)

    # Optional eBay browse filters / sorting (not required)
    ebay_filter: str
    default_sort: str


# ---------------------------------------------------------------------------
# Catalog
# - Keys are set codes (upper).
# - Each set has one or more product "types" users can query.
# ---------------------------------------------------------------------------
CATALOG: Dict[str, List[ProductType]] = {
    # Existing
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

    # Newly appended sets (from the uploaded example listings)
    "BLB": [
        {
            "key": "play_box",
            "label": "Play Booster Box",
            "ebay_query": "Bloomburrow play booster box",
            "ev_set_code": "BLB",
            "ev_kind": "box",
        },
    ],
    "DSK": [
        {
            "key": "play_box",
            "label": "Play Booster Box",
            "ebay_query": "Duskmourn House of Horror play booster box",
            "ev_set_code": "DSK",
            "ev_kind": "box",
        },
    ],
    "FDN": [
        {
            "key": "play_box",
            "label": "Play Booster Box",
            "ebay_query": "Foundations play booster box MTG",
            "ev_set_code": "FDN",
            "ev_kind": "box",
        },
    ],
    "DFT": [
        {
            "key": "play_box",
            "label": "Play Booster Box",
            "ebay_query": "Aetherdrift play booster box",
            "ev_set_code": "DFT",
            "ev_kind": "box",
        },
    ],
    "INR": [
        {
            "key": "play_box",
            "label": "Play Booster Box",
            "ebay_query": "Innistrad Remastered play booster box",
            "ev_set_code": "INR",
            "ev_kind": "box",
        },
    ],
    "TDM": [
        {
            "key": "play_box",
            "label": "Play Booster Box",
            "ebay_query": "Tarkir Dragonstorm play booster box",
            "ev_set_code": "TDM",
            "ev_kind": "box",
        },
    ],
    "FIN": [
        {
            "key": "play_box",
            "label": "Play Booster Box",
            "ebay_query": "Final Fantasy play booster box MTG",
            "ev_set_code": "FIN",
            "ev_kind": "box",
        },
    ],
    "EOE": [
        {
            "key": "play_box",
            "label": "Play Booster Box",
            "ebay_query": "Edge of Eternities play booster box MTG",
            "ev_set_code": "EOE",
            "ev_kind": "box",
        },
    ],
    "SPM": [
        {
            "key": "play_box",
            "label": "Play Booster Box",
            "ebay_query": "Marvel's Spider-Man play booster box MTG",
            "ev_set_code": "SPM",
            "ev_kind": "box",
        },
    ],

    # MB2 is intentionally omitted for EV for now because its pack collation
    # uses large PLST sheets with non-numeric collector numbers (e.g. PCY-1),
    # which needs a different query strategy than the current config builder.
    "MB2": [
        {
            "key": "display",
            "label": "Mystery Booster 2 Display (24 packs)",
            "ebay_query": "Mystery Booster 2 display 24 packs",
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
