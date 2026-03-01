# app/api/deals.py
from __future__ import annotations

from dataclasses import asdict
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.services.catalog import get_product
from app.services import ev_core, ev_cache
from app.services.ebay_browse import search_items_simplified

router = APIRouter(tags=["deals"])


class DealItemOut(BaseModel):
    title: Optional[str]
    itemId: Optional[str]
    price: Optional[float]
    shipping: Optional[float]
    shipping_known: bool
    shipType: Optional[str] = Field(
        None, description="Best-effort shipping type from eBay payload")
    normalized_price: Optional[float]
    currency: Optional[str]
    endDate: Optional[str]
    url: Optional[str]

    ev_box: float = Field(...,
                          description="Current EV box value used for spread calc")
    spread: Optional[float] = Field(
        None, description="ev_box - normalized_price (None if missing price)")


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
    dependencies=[Depends(require_api_key)],
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
    # 1) Resolve catalog product
    p = get_product(set_code, product_key)
    if not p:
        raise HTTPException(
            status_code=404, detail="Unknown set_code/product_key combo")

    if not p.get("ev_kind") or not p.get("ev_set_code"):
        raise HTTPException(
            status_code=400,
            detail="This product is not configured for box EV yet (add ev_set_code + ev_kind=box in catalog)",
        )

    ev_set_code = str(p["ev_set_code"]).strip().upper()
    ev_kind = str(p.get("ev_kind", "box")).strip().lower()

    # 2) Get EV (cached)
    model = ev_core.model_for_code(ev_set_code, ev_kind)
    if not model:
        raise HTTPException(
            status_code=400, detail=f"No EV model for set code {ev_set_code}")

    def _compute_ev() -> dict:
        report = model.run()
        return asdict(report)

    ev_data = ev_cache.get_or_compute_ev_report(
        ev_set_code, ev_kind, _compute_ev)

    try:
        ev_box = float(ev_data.get("box_ev"))
    except Exception:
        raise HTTPException(status_code=500, detail="EV report missing box_ev")

    # 3) Run eBay search (catalog query)
    ebay_query = p.get("ebay_query")
    if not ebay_query:
        raise HTTPException(
            status_code=500, detail="Catalog product missing ebay_query")

    try:
        search = search_items_simplified(
            q=ebay_query,
            limit=limit,
            offset=offset,
            marketplace_id=marketplace_id,
            use_cache=use_cache,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"eBay search failed: {e}")

    # 4) Enrich items with spread
    raw_items = search.get("items") or []
    if not isinstance(raw_items, list):
        raw_items = []

    enriched: list[dict] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        np = it.get("normalized_price")
        spread = None
        try:
            if np is not None:
                spread = ev_box - float(np)
        except (TypeError, ValueError):
            spread = None

        out = dict(it)
        out["ev_box"] = ev_box
        out["spread"] = spread
        enriched.append(out)

    # Sort: best spread first; items with None spread go last
    enriched.sort(key=lambda x: (x.get("spread")
                  is None, -(x.get("spread") or -10**18)))

    return DealsOut(
        set_code=set_code.strip().upper(),
        product_key=product_key,
        product_label=p.get("label", product_key),
        ebay_query=ebay_query,
        ev_box=ev_box,
        total=search.get("total"),
        limit=search.get("limit"),
        offset=search.get("offset"),
        items=enriched,
    )
