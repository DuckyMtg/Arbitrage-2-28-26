# app/api/ev.py
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.services.rate_limit import require_rate_limit
from app.services import ev_core, ev_cache
from app.services.catalog import list_set_codes, list_products_for_set

router = APIRouter(tags=["ev"])


class EVRequest(BaseModel):
    set_code: str = Field(..., examples=["OTJ", "WOE", "MH3", "TLA", "ECL"])
    kind: str = Field(
        "box",
        description="Product kind — must match a registered EV model.",
        examples=["box", "draft_box"],
    )


@router.post(
    "/ev",
    dependencies=[Depends(require_api_key), Depends(require_rate_limit)],
    summary="Compute (or return cached) EV for a set/kind combination",
)
def compute_ev(req: EVRequest):
    code = req.set_code.strip().upper()
    kind = req.kind.strip().lower()

    model = ev_core.model_for_code(code, kind)
    if not model:
        supported = sorted(f"{c}/{k}" for c, k in ev_core.MODEL_REGISTRY)
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported set/kind: {code}/{kind}. Supported: {supported}",
        )

    def _compute() -> dict:
        return asdict(model.run())

    data = ev_cache.get_or_compute_ev_report(code, kind, _compute)
    return jsonable_encoder(data)


@router.get(
    "/ev/all",
    dependencies=[Depends(require_api_key)],
    summary="Return cached/computed EV for every set+product in the catalog",
)
def ev_all():
    tasks: list[tuple[str, str, str, str]] = []
    for set_code in list_set_codes():
        for p in list_products_for_set(set_code):
            if not isinstance(p, dict):
                continue
            ev_sc = p.get("ev_set_code")
            ev_k = p.get("ev_kind")
            if ev_sc and ev_k:
                tasks.append((ev_sc.upper(), ev_k.lower(), p["key"], p.get("label") or p["key"]))

    def fetch(args: tuple[str, str, str, str]) -> dict:
        sc, kind, key, label = args
        model = ev_core.model_for_code(sc, kind)
        if not model:
            return {"set_code": sc, "product_key": key, "label": label, "pack_ev": None, "box_ev": None, "error": "no model"}
        try:
            data = ev_cache.get_or_compute_ev_report(sc, kind, lambda: asdict(model.run()))
            return {
                "set_code": sc,
                "product_key": key,
                "label": label,
                "pack_ev": data.get("pack_ev"),
                "box_ev": data.get("box_ev"),
                "warnings": data.get("warnings", []),
            }
        except Exception as exc:
            return {"set_code": sc, "product_key": key, "label": label, "pack_ev": None, "box_ev": None, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, tasks))

    return jsonable_encoder(sorted(results, key=lambda r: (r["set_code"], r["product_key"])))


@router.get(
    "/ev/supported",
    dependencies=[Depends(require_api_key)],
    summary="List all set/kind combinations that have an EV model",
)
def ev_supported():
    return [
        {"set_code": code, "kind": kind}
        for code, kind in sorted(ev_core.MODEL_REGISTRY.keys())
    ]
