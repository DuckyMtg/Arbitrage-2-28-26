# app/api/deals_opinionated.py
from __future__ import annotations

from dataclasses import asdict
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.services import ev_core, ev_cache
from app.services.catalog import get_product
from app.services.ebay_browse import search_items_simplified

router = APIRouter(tags=["deals"])


class DealOut(BaseModel):
    title: Optional[str]
    itemId: Optional[str]

    # totals for the listing
    normalized_price: Optional[float] = Field(
        None, description="item price + shipping (shipping unknown treated as $0)"
    )
    price: Optional[float]
    shipping: Optional[float]
    shipping_known: bool
    shipType: Optional[str] = None

    # multi-box support
    boxes: int = Field(
        1, description="Inferred number of boxes in the listing")
    normalized_price_per_box: Optional[float] = Field(
        None, description="normalized_price / boxes"
    )

    endDate: Optional[str]
    url: Optional[str]
    currency: Optional[str]

    spread: Optional[float] = Field(
        None, description="ev_box - normalized_price_per_box")


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
    dependencies=[Depends(require_api_key)],
)
def deals(
    set_code: str = Query(
        ..., description="MTG set code used for catalog lookup (e.g. OTJ, WOE, MH3)"),
    product_key: str = Query(
        ..., description="Catalog product key for that set (e.g. play_box, set_box, etc.)"),
    min_spread: float = Query(
        0.0, description="Only return items with spread >= this"),
    max_price: Optional[float] = Query(
        None, description="Only return items with normalized_price_per_box <= this"),
    limit: int = Query(20, ge=1, le=200),
    marketplace_id: str = Query(
        "EBAY_US", description="X-EBAY-C-MARKETPLACE-ID"),
    use_cache: bool = Query(
        True, description="Cache eBay results briefly in Redis"),
):
    # 1) Resolve product from catalog
    p = get_product(set_code, product_key)
    if not p:
        raise HTTPException(
            status_code=404, detail="Unknown set_code/product_key combo")

    product_kind = str(p.get("product_kind") or product_key).strip()

    if not p.get("ev_kind") or not p.get("ev_set_code"):
        raise HTTPException(
            status_code=400,
            detail="This product is not configured for EV yet (set ev_set_code + ev_kind=box in catalog)",
        )

    ebay_query = p.get("ebay_query")
    if not ebay_query:
        raise HTTPException(
            status_code=500, detail="Catalog product missing ebay_query")

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

    # 3) Search eBay (fetch more than limit so filtering doesn't empty results)
    search_limit = min(200, max(limit * 5, limit))
    try:
        search = search_items_simplified(
            q=ebay_query,
            limit=search_limit,
            offset=0,
            marketplace_id=marketplace_id,
            use_cache=use_cache,
            product_kind=product_kind,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"eBay search failed: {e}")

    items = search.get("items") or []
    if not isinstance(items, list):
        items = []

    # 4) Compute spread + filter
    results: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue

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

        spread = ev_box - npb_f

        if spread < float(min_spread):
            continue
        if max_price is not None and npb_f > float(max_price):
            continue

        out = dict(it)
        out["spread"] = spread
        results.append(out)

    # 5) Sort by best spread and return top `limit`
    results.sort(key=lambda x: x.get("spread", -10**18), reverse=True)
    results = results[:limit]

    return DealsResponse(
        set_code=set_code.strip().upper(),
        product_key=product_key,
        product_label=p.get("label", product_key),
        ebay_query=ebay_query,
        product_kind=product_kind,
        ev_box=ev_box,
        min_spread=float(min_spread),
        max_price=float(max_price) if max_price is not None else None,
        returned=len(results),
        items=results,
    )
