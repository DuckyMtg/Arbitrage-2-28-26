# app/api/ev.py
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Depends
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.services import ev_core, ev_cache

router = APIRouter(tags=["ev"])


class EVRequest(BaseModel):
    set_code: str = Field(..., examples=["OTJ", "WOE", "MH3", "TLA", "ECL"])


@router.post(
    "/ev",
    dependencies=[Depends(require_api_key)],
)
def compute_ev(req: EVRequest):
    code = req.set_code.strip().upper()

    model = ev_core.model_for_code(code)
    if not model:
        raise HTTPException(status_code=400, detail="Unsupported set code")

    def _compute() -> dict:
        report = model.run()
        return asdict(report)

    data = ev_cache.get_or_compute_ev_report(code, _compute)
    return jsonable_encoder(data)
