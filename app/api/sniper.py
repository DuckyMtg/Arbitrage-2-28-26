# app/api/sniper.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.services.catalog import get_product
from app.services.ebay_browse import search_items_simplified

router = APIRouter(tags=["sniper"])


class SniperItemOut(BaseModel):
    title: Optional[str]
    itemId: Optional[str]
    price: Optional[float]
    shipping: Optional[float]
    shipping_known: bool
    shipType: Optional[str] = None
    normalized_price: Optional[float]
    normalized_price_per_box: Optional[float]
    boxes: int = 1
    currency: Optional[str]
    endDate: Optional[str]
    url: Optional[str]


class SniperDataOut(BaseModel):
    q: str
    filter: Optional[str] = None
    sort: Optional[str] = None
    product_kind: Optional[str] = None
    marketplace_id: Optional[str] = None
    total: Optional[int] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    items: List[SniperItemOut]


class SniperSearchOut(BaseModel):
    set_code: str
    product_key: str
    product_label: str
    ebay_query: str
    # FIX: was `data: dict` — completely untyped, bypassing all Pydantic
    # validation and OpenAPI schema generation for the response body.
    # Replaced with SniperDataOut which mirrors the shape returned by
    # search_items_simplified().
    data: SniperDataOut


@router.get(
    "/sniper/search",
    response_model=SniperSearchOut,
    dependencies=[Depends(require_api_key)],
)
def sniper_search(
    set_code: str = Query(..., description="Set code like MH3, OTJ, WOE"),
    product_key: str = Query(...,
                             description="Product key from /v1/catalog/products"),
    sort: Optional[str] = Query(
        None, description="Override sort (e.g. price, newlyListed, bestMatch)"),
    filter_: Optional[str] = Query(
        None, alias="filter", description="Override Browse filter string"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=9_800),
    marketplace_id: str = Query(
        "EBAY_US", description="X-EBAY-C-MARKETPLACE-ID"),
    use_cache: bool = Query(True),
):
    p = get_product(set_code, product_key)
    if not p:
        raise HTTPException(
            status_code=404, detail="Unknown set_code/product_key combo")

    ebay_query = p.get("ebay_query")
    if not ebay_query:
        raise HTTPException(
            status_code=500, detail="Catalog product missing ebay_query")

    if offset + limit > 10_000:
        raise HTTPException(
            status_code=422,
            detail=(
                f"offset ({offset}) + limit ({limit}) = {offset + limit} "
                "exceeds the eBay Browse API cap of 10,000."
            ),
        )

    effective_filter = filter_ if filter_ is not None else p.get("ebay_filter")
    effective_sort = sort if sort is not None else p.get("default_sort")

    try:
        data = search_items_simplified(
            q=ebay_query,
            filter_=effective_filter,
            sort=effective_sort,
            limit=limit,
            offset=offset,
            marketplace_id=marketplace_id,
            use_cache=use_cache,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"eBay search failed: {e}")

    return SniperSearchOut(
        set_code=set_code.strip().upper(),
        product_key=product_key,
        product_label=p.get("label", product_key),
        ebay_query=ebay_query,
        data=data,
    )
