# app/api/catalog.py
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.catalog import list_products_for_set, list_set_codes

router = APIRouter(tags=["catalog"])


class SetOut(BaseModel):
    set_code: str = Field(..., examples=["MH3", "OTJ", "WOE", "TLA", "ECL"])


class ProductOut(BaseModel):
    key: str = Field(..., examples=["play_box", "bundle", "draft_box"])
    label: str = Field(..., examples=["Play Booster Box", "Bundle"])

    # ✅ drives box-only filtering in eBay browse
    product_kind: Optional[str] = Field(
        None, examples=["play_box", "set_box", "draft_box", "collector_box"]
    )

    # optional, but useful for UI and sanity checks
    ev_kind: Optional[str] = Field(None, examples=["box"])
    ev_set_code: Optional[str] = Field(
        None, examples=["MH3", "OTJ", "WOE", "TLA", "ECL"])
    ebay_query: Optional[str] = Field(
        None, examples=["Modern Horizons 3 play booster box"])


@router.get("/catalog/sets", response_model=List[SetOut])
def catalog_sets():
    return [{"set_code": c} for c in list_set_codes()]


@router.get("/catalog/products", response_model=List[ProductOut])
def catalog_products(
    set_code: str = Query(..., description="Set code like MH3, OTJ, WOE"),
):
    products = list_products_for_set(set_code)
    if not products:
        raise HTTPException(
            status_code=404,
            detail="Unknown set code (or no products configured)",
        )

    out: list[dict] = []
    for p in products:
        if not isinstance(p, dict):
            continue

        out.append(
            {
                "key": p.get("key"),
                "label": p.get("label") or p.get("key"),

                # prefer explicit product_kind; fall back to key if it matches your naming
                "product_kind": (p.get("product_kind") or p.get("key")),

                # optional fields
                "ev_kind": p.get("ev_kind"),
                "ev_set_code": p.get("ev_set_code"),
                "ebay_query": p.get("ebay_query"),
            }
        )

    # basic cleanup: drop items missing key
    out = [x for x in out if x.get("key")]

    return out
