# app/main.py
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

from app.api.ev import router as ev_router
from app.api.ebay import router as ebay_router
from app.api.catalog import router as catalog_router
from app.api.sniper import router as sniper_router
from app.api.deals import router as deals_router
from app.api.deals_opinionated import router as deals_opinionated_router

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

app = FastAPI(
    title="MTG EV API",
    description=(
        "Sealed MTG product arbitrage: EV modeling via Scryfall + "
        "live eBay sniping. All routes require X-API-Key."
    ),
    version="1.0.0",
)

app.include_router(ev_router,                  prefix="/v1")
app.include_router(ebay_router,                prefix="/v1")
app.include_router(catalog_router,             prefix="/v1")
app.include_router(sniper_router,              prefix="/v1")
app.include_router(deals_router,               prefix="/v1")
app.include_router(deals_opinionated_router,   prefix="/v1")


@app.get("/health", tags=["meta"])
def health():
    return {"ok": True}
