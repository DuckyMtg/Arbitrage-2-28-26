# app/api/deals_opinionated.py
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.services.rate_limit import require_rate_limit
from app.services.deals import resolve_deals_context

router = APIRouter(tags=["deals"])


class DealOut(BaseModel):
    title: Optional[str]
    itemId: Optional[str]
    normalized_price: Optional[float] = Field(
        None, description="item price + shipping (unknown shipping treated as $0)"
    )
    price: Optional[float]
    shipping: Optional[float]
    shipping_known: bool
    shipType: Optional[str] = None
    boxes: int = Field(
        1, description="Inferred number of boxes in the listing")
    normalized_price_per_box: Optional[float] = Field(
        None, description="normalized_price / boxes"
    )
    endDate: Optional[str]
    url: Optional[str]
    currency: Optional[str]
    spread: Optional[float] = Field(
        None, description="ev_box - normalized_price_per_box"
    )


class DealsResponse(BaseModel):
    set_code: str
    product_key: str
    product_label: str
    ebay_query: str
    product_kind: str
    ev_box: float
    min_spread: float
    max_price: Optional[float] = None
    returned: int
    items: List[DealOut]


@router.get(
    "/deals",
    response_model=DealsResponse,
    dependencies=[Depends(require_api_key), Depends(require_rate_limit)],
)
def deals(
    set_code: str = Query(...,
                          description="MTG set code (e.g. OTJ, WOE, MH3)"),
    product_key: str = Query(...,
                             description="Catalog product key (e.g. play_box, set_box)"),
    min_spread: float = Query(
        0.0, description="Only return items with spread >= this"),
    max_price: Optional[float] = Query(
        None, description="Only return items with normalized_price_per_box <= this"
    ),
    limit: int = Query(20, ge=1, le=200),
    marketplace_id: str = Query("EBAY_US"),
    use_cache: bool = Query(True),
):
    # Fetch more than `limit` up front so downstream filtering doesn't empty results
    search_limit = min(200, max(limit * 5, limit))

    ctx = resolve_deals_context(
        set_code=set_code,
        product_key=product_key,
        limit=limit,
        offset=0,
        marketplace_id=marketplace_id,
        use_cache=use_cache,
        search_limit_override=search_limit,
    )

    results: list[dict] = []
    for it in ctx.items:
        if not isinstance(it, dict):
            continue

        # Prefer the pre-computed per-box price; fall back to total price for
        # single-box listings that don't have it set.
        np_per_box = it.get("normalized_price_per_box")
        if np_per_box is None:
            np_total = it.get("normalized_price")
            if np_total is None:
                continue
            try:
                np_per_box = float(np_total)
            except (TypeError, ValueError):
                continue
            it = dict(it)
            it.setdefault("boxes", 1)
            it["normalized_price_per_box"] = np_per_box

        try:
            npb_f = float(np_per_box)
        except (TypeError, ValueError):
            continue

        spread = ctx.ev_box - npb_f

        if spread < float(min_spread):
            continue
        if max_price is not None and npb_f > float(max_price):
            continue

        out = dict(it)
        out["spread"] = spread
        results.append(out)

    results.sort(key=lambda x: x.get("spread", -(10**18)), reverse=True)
    results = results[:limit]

    return DealsResponse(
        set_code=ctx.set_code,
        product_key=ctx.product_key,
        product_label=ctx.product.get("label", ctx.product_key),
        ebay_query=ctx.ebay_query,
        product_kind=ctx.product_kind,
        ev_box=ctx.ev_box,
        min_spread=float(min_spread),
        max_price=float(max_price) if max_price is not None else None,
        returned=len(results),
        items=results,
    )
