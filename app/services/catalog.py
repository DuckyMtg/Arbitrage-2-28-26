# app/services/catalog.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, TypedDict

import yaml


class ProductType(TypedDict, total=False):
    key: str
    label: str
    ebay_query: str
    ev_set_code: str
    ev_kind: str
    product_kind: str
    ebay_filter: str
    default_sort: str


# ---------------------------------------------------------------------------
# Catalog path resolution
#   1. CATALOG_PATH env var (absolute or relative to cwd)
#   2. catalog.yaml at the project root (two levels above this file)
# ---------------------------------------------------------------------------
_env_path = os.getenv("CATALOG_PATH", "")
_CATALOG_PATH: Path = (
    Path(_env_path)
    if _env_path
    else Path(__file__).resolve().parent.parent.parent / "catalog.yaml"
)

_catalog_cache: Dict[str, List[ProductType]] | None = None


def _load_catalog() -> Dict[str, List[ProductType]]:
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache

    if not _CATALOG_PATH.exists():
        raise FileNotFoundError(
            f"Catalog file not found at {_CATALOG_PATH}. "
            "Set the CATALOG_PATH env var or place catalog.yaml at the project root."
        )

    with _CATALOG_PATH.open("r", encoding="utf-8") as f:
        raw: dict = yaml.safe_load(f) or {}

    catalog: Dict[str, List[ProductType]] = {}
    for set_code, products in raw.items():
        if not isinstance(products, list):
            continue
        code = str(set_code).strip().upper()
        catalog[code] = [p for p in products if isinstance(
            p, dict) and p.get("key")]

    _catalog_cache = catalog
    return catalog


def reload_catalog() -> None:
    """Force a full reload from disk. Useful after editing catalog.yaml without restarting."""
    global _catalog_cache
    _catalog_cache = None


def list_set_codes() -> List[str]:
    return sorted(_load_catalog().keys())


def list_products_for_set(set_code: str) -> List[ProductType]:
    return _load_catalog().get(set_code.strip().upper(), [])


def get_product(set_code: str, product_key: str) -> Optional[ProductType]:
    pk = product_key.strip()
    for p in list_products_for_set(set_code):
        if p.get("key") == pk:
            return p
    return None
