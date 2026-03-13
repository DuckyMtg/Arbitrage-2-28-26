# app/api/deals.py
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.services.rate_limit import require_rate_limit
from app.services.deals import resolve_deals_context

router = APIRouter(tags=["deals"])


class DealItemOut(BaseModel):
    title: Optional[str]
    itemId: Optional[str]
    price: Optional[float]
    shipping: Optional[float]
    shipping_known: bool
    shipType: Optional[str] = Field(
        None, description="Best-effort shipping type from eBay payload"
    )
    normalized_price: Optional[float]
    currency: Optional[str]
    endDate: Optional[str]
    url: Optional[str]
    ev_box: float = Field(...,
                          description="Current EV box value used for spread calc")
    spread: Optional[float] = Field(
        None, description="ev_box - normalized_price (None if price is missing)"
    )


class DealsOut(BaseModel):
    set_code: str
    product_key: str
    product_label: str
    ebay_query: str
    ev_box: float
    total: Optional[int] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    items: List[DealItemOut]


@router.get(
    "/deals/box",
    response_model=DealsOut,
    dependencies=[Depends(require_api_key), Depends(require_rate_limit)],
)
def deals_box(
    set_code: str = Query(..., description="Set code like MH3, OTJ, WOE"),
    product_key: str = Query(...,
                             description="Product key from /v1/catalog/products"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=10_000),
    marketplace_id: str = Query("EBAY_US"),
    use_cache: bool = Query(True),
):
    ctx = resolve_deals_context(
        set_code=set_code,
        product_key=product_key,
        limit=limit,
        offset=offset,
        marketplace_id=marketplace_id,
        use_cache=use_cache,
    )

    enriched: list[dict] = []
    for it in ctx.items:
        if not isinstance(it, dict):
            continue
        np_ = it.get("normalized_price")
        spread = None
        try:
            if np_ is not None:
                spread = ctx.ev_box - float(np_)
        except (TypeError, ValueError):
            pass
        out = dict(it)
        out["ev_box"] = ctx.ev_box
        out["spread"] = spread
        enriched.append(out)

    # Best spread first; items with no price go last
    enriched.sort(
        key=lambda x: (x.get("spread") is None, -
                       (x.get("spread") or -(10**18)))
    )

    return DealsOut(
        set_code=ctx.set_code,
        product_key=ctx.product_key,
        product_label=ctx.product.get("label", ctx.product_key),
        ebay_query=ctx.ebay_query,
        ev_box=ctx.ev_box,
        total=ctx.total,
        limit=ctx.limit,
        offset=ctx.offset,
        items=enriched,
    )
