"""
MTGJSON integration for rarity counts.

Fetches per-set card data from MTGJSON (https://mtgjson.com/api/v5/)
and counts booster-eligible cards by rarity. Used as the primary source
in rarity_counts() (ev_core.py), with Scryfall as fallback.
"""
from __future__ import annotations

import requests

from app.services import ev_cache

MTGJSON_SET_URL = "https://mtgjson.com/api/v5/{code}.json"
_TTL = 24 * 3600  # 1 day — refresh rarity data daily

# Maps MTGJSON rarity strings → canonical rarity keys used in ev_core.
# "bonus" / "special" are intentionally excluded — they represent bonus-sheet
# cards (Breaking News, Enchanting Tales, etc.) handled by dedicated slot
# functions in ev_core.py, not generic rarity distribution slots.
_RARITY_MAP: dict[str, str] = {
    "common":      "common",
    "uncommon":    "uncommon",
    "rare":        "rare",
    "mythic":      "mythic",
    "mythic rare": "mythic",  # alternate spelling guard
}


def fetch_set_data(set_code: str) -> dict | None:
    """
    Return the MTGJSON set data dict for *set_code*, Redis-cached for 1 day.
    Returns None if the set is not found or the request fails.
    Cache key: ``mtgjson:set:{SET_CODE}``
    """
    key = f"mtgjson:set:{set_code.upper()}"
    cached = ev_cache.cache_get_json(key)
    if cached is not None:
        return cached
    try:
        resp = requests.get(
            MTGJSON_SET_URL.format(code=set_code.upper()),
            timeout=30,
            headers={"User-Agent": "mtg-sealed-deals/0.5"},
        )
        resp.raise_for_status()
    except Exception:
        return None
    data = resp.json().get("data")
    if not data:
        return None
    ev_cache.cache_set_json(key, data, _TTL)
    return data


def rarity_counts_mtgjson(
    set_code: str,
    booster_type: str = "play",
) -> dict[str, int]:
    """
    Count booster-eligible cards by rarity for *set_code* using MTGJSON data.

    ``booster_type`` selects which booster sheet to include — cards are counted
    only when their ``boosterTypes`` list contains this value.  The default
    ``"play"`` covers the current Play Booster era; pass ``"draft"`` for older
    draft-booster sets.

    Returns ``{"common": int, "uncommon": int, "rare": int, "mythic": int}``.
    Returns an empty dict on failure so the caller can fall back to Scryfall.
    """
    data = fetch_set_data(set_code)
    if not data:
        return {}

    counts: dict[str, int] = {"common": 0, "uncommon": 0, "rare": 0, "mythic": 0}
    for card in data.get("cards", []):
        booster_types = card.get("boosterTypes") or []
        if booster_type not in booster_types:
            continue
        canonical = _RARITY_MAP.get((card.get("rarity") or "").lower())
        if canonical:
            counts[canonical] += 1

    return counts
