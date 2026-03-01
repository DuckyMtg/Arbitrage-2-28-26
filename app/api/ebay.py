# app/api/ebay.py
from __future__ import annotations

from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.services.ebay_browse import search_items_simplified

router = APIRouter(tags=["ebay"])


class EbayItemOut(BaseModel):
    title: Optional[str]
    itemId: Optional[str]
    price: Optional[float]
    shipping: Optional[float]
    shipping_known: bool = Field(...,
                                 description="True if eBay provided shipping cost")
    shipType: Optional[str] = Field(
        None, description="Best-effort shipping type from eBay payload (may be missing)"
    )
    normalized_price: Optional[float] = Field(
        None, description="price + (shipping or 0)"
    )
    currency: Optional[str]
    endDate: Optional[str]
    url: Optional[str]


class EbaySearchOut(BaseModel):
    q: str
    filter: Optional[str] = None
    sort: Optional[str] = None
    total: Optional[int] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    items: List[EbayItemOut]


@router.get(
    "/ebay/search",
    response_model=EbaySearchOut,
    dependencies=[Depends(require_api_key)],
)
def ebay_search(
    q: str = Query(..., description="Keywords to search (required)"),
    filter_: str | None = Query(
        None, alias="filter", description="Browse API filter string"),
    sort: str | None = Query(
        None, description="e.g. price, newlyListed, bestMatch, etc."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=10_000),
    marketplace_id: str = Query(
        "EBAY_US", description="X-EBAY-C-MARKETPLACE-ID"),
    use_cache: bool = Query(
        True, description="Cache results briefly in Redis"),
):
    try:
        return search_items_simplified(
            q=q,
            filter_=filter_,
            sort=sort,
            limit=limit,
            offset=offset,
            marketplace_id=marketplace_id,
            use_cache=use_cache,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"eBay search failed: {e}")
