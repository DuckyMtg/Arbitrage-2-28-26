# app/api/sniper.py
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.services.rate_limit import require_rate_limit
from app.services.catalog import get_product
from app.services.ebay_browse import search_items_simplified

router = APIRouter(tags=["sniper"])


class SniperItemOut(BaseModel):
    title: Optional[str]
    itemId: Optional[str]
    price: Optional[float]
    shipping: Optional[float]
    shipping_known: bool = Field(
        ..., description="True if eBay provided an explicit shipping cost"
    )
    shipType: Optional[str] = Field(
        None, description="Best-effort shipping type from eBay payload"
    )
    normalized_price: Optional[float] = Field(
        None, description="price + (shipping or 0)"
    )
    normalized_price_per_box: Optional[float] = Field(
        None, description="normalized_price / inferred box count"
    )
    boxes: int = Field(
        1, description="Inferred number of boxes in the listing")
    currency: Optional[str]
    endDate: Optional[str]
    url: Optional[str]


class SniperSearchOut(BaseModel):
    set_code: str
    product_key: str
    product_label: str
    ebay_query: str
    product_kind: Optional[str]
    total: Optional[int] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    items: List[SniperItemOut]


@router.get(
    "/sniper/search",
    response_model=SniperSearchOut,
    dependencies=[Depends(require_api_key), Depends(require_rate_limit)],
)
def sniper_search(
    set_code: str = Query(..., description="Set code like MH3, OTJ, WOE"),
    product_key: str = Query(...,
                             description="Product key from /v1/catalog/products"),
    sort: Optional[str] = Query(
        None, description="Override sort (e.g. price, newlyListed, bestMatch)"
    ),
    filter_: Optional[str] = Query(
        None, alias="filter", description="Override Browse API filter string"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=10_000),
    marketplace_id: str = Query("EBAY_US"),
    use_cache: bool = Query(True),
):
    p = get_product(set_code, product_key)
    if not p:
        raise HTTPException(
            status_code=404, detail="Unknown set_code/product_key combo"
        )

    ebay_query = p.get("ebay_query")
    if not ebay_query:
        raise HTTPException(
            status_code=500, detail="Catalog product missing ebay_query"
        )

    product_kind = str(p.get("product_kind") or p.get("key")
                       or product_key).strip()

    # Use catalog defaults unless the caller explicitly overrides them
    effective_filter = filter_ if filter_ is not None else p.get("ebay_filter")
    effective_sort = sort if sort is not None else p.get("default_sort")

    try:
        search = search_items_simplified(
            q=ebay_query,
            filter_=effective_filter,
            sort=effective_sort,
            limit=limit,
            offset=offset,
            marketplace_id=marketplace_id,
            use_cache=use_cache,
            product_kind=product_kind,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"eBay search failed: {exc}")

    items = search.get("items") or []
    if not isinstance(items, list):
        items = []

    return SniperSearchOut(
        set_code=set_code.strip().upper(),
        product_key=product_key,
        product_label=p.get("label", product_key),
        ebay_query=ebay_query,
        product_kind=product_kind,
        total=search.get("total"),
        limit=search.get("limit"),
        offset=search.get("offset"),
        items=items,
    )
