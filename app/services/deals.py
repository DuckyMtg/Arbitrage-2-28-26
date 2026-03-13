# app/services/deals.py
from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import HTTPException

from app.services.catalog import get_product
from app.services import ev_core, ev_cache
from app.services.ebay_browse import search_items_simplified


class DealsContext:
    """
    Resolved, pre-enriched context shared by /deals/box and /deals.

    Both endpoints call resolve_deals_context() to get this object, then
    apply their own filtering / sorting / response-model shaping on top.
    """

    def __init__(
        self,
        *,
        set_code: str,
        product_key: str,
        product: dict,
        ev_box: float,
        ebay_query: str,
        product_kind: str,
        items: list[dict],
        total: Optional[int],
        limit: Optional[int],
        offset: Optional[int],
    ) -> None:
        self.set_code = set_code
        self.product_key = product_key
        self.product = product
        self.ev_box = ev_box
        self.ebay_query = ebay_query
        self.product_kind = product_kind
        self.items = items
        self.total = total
        self.limit = limit
        self.offset = offset


def resolve_deals_context(
    *,
    set_code: str,
    product_key: str,
    limit: int,
    offset: int = 0,
    marketplace_id: str = "EBAY_US",
    use_cache: bool = True,
    search_limit_override: int | None = None,
) -> DealsContext:
    """
    Shared pipeline executed by both deals endpoints:
      1. Resolve catalog product and validate EV config
      2. Fetch EV (from Redis cache, computing on miss)
      3. Search eBay (with optional search-limit override for pre-filtering)

    Raises HTTPException on any validation or upstream failure.
    Spread computation and filtering are intentionally left to the caller.
    """

    # ── 1. Catalog ────────────────────────────────────────────────────────
    p = get_product(set_code, product_key)
    if not p:
        raise HTTPException(
            status_code=404, detail="Unknown set_code/product_key combo"
        )

    if not p.get("ev_kind") or not p.get("ev_set_code"):
        raise HTTPException(
            status_code=400,
            detail=(
                "This product is not configured for EV yet. "
                "Add ev_set_code + ev_kind to its entry in catalog.yaml."
            ),
        )

    ebay_query = p.get("ebay_query")
    if not ebay_query:
        raise HTTPException(
            status_code=500, detail="Catalog product missing ebay_query"
        )

    product_kind = str(p.get("product_kind") or p.get("key")
                       or product_key).strip()
    ev_set_code = str(p["ev_set_code"]).strip().upper()
    ev_kind = str(p.get("ev_kind", "box")).strip().lower()

    # ── 2. EV (cached) ────────────────────────────────────────────────────
    model = ev_core.model_for_code(ev_set_code, ev_kind)
    if not model:
        raise HTTPException(
            status_code=400,
            detail=f"No EV model for {ev_set_code}/{ev_kind}",
        )

    def _compute_ev() -> dict:
        return asdict(model.run())

    ev_data = ev_cache.get_or_compute_ev_report(
        ev_set_code, ev_kind, _compute_ev)
    try:
        ev_box = float(ev_data["box_ev"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=500, detail="EV report missing box_ev")

    # ── 3. eBay search ────────────────────────────────────────────────────
    effective_limit = search_limit_override if search_limit_override is not None else limit

    try:
        search = search_items_simplified(
            q=ebay_query,
            limit=effective_limit,
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

    raw_items = search.get("items") or []
    if not isinstance(raw_items, list):
        raw_items = []

    return DealsContext(
        set_code=set_code.strip().upper(),
        product_key=product_key,
        product=p,
        ev_box=ev_box,
        ebay_query=ebay_query,
        product_kind=product_kind,
        items=raw_items,
        total=search.get("total"),
        limit=search.get("limit"),
        offset=search.get("offset"),
    )
