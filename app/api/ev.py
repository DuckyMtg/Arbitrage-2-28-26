# app/api/ev.py
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.services.rate_limit import require_rate_limit
from app.services import ev_core, ev_cache

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
    "/ev/supported",
    dependencies=[Depends(require_api_key)],
    summary="List all set/kind combinations that have an EV model",
)
def ev_supported():
    return [
        {"set_code": code, "kind": kind}
        for code, kind in sorted(ev_core.MODEL_REGISTRY.keys())
    ]
