"""
Gate 1: Snapshot collector.

This is the ONLY tool permitted to make Scryfall network calls.
Run it locally; commit the output ``tools/out/gate1_snapshot.json``.
Downstream offline tools (gate1_composition.py, gate1_inflation_triage.py,
future tests) read that file instead of hitting the network.

Usage
-----
    python -m tools.gate1_collect [--set CODE] [--out PATH]

Output schema (schema_version 1)
---------------------------------
Top-level object
    schema_version  int     Always 1 for this version.
    generated_at    str     ISO 8601 UTC timestamp (no trailing Z; ends in +00:00).
    pools           list    One record per distinct QueryPool across every model.

Each pool record
    set             str     Upper-cased set code, e.g. "BLB".
    kind            str     Product kind: "box", "collector_box", "draft_box".
    slot_name       str     Human-readable slot name from ProductModel.slots[i].name.
    pool_label      str     QueryPool.label — stable cross-tool identifier.
    query           str     QueryPool.primary — the Scryfall search string.
    unique          str     QueryPool.unique — "prints" or "cards".
    price_field     str     QueryPool.price_field — "usd" or "usd_foil".

    primary_count   int     total_cards returned by (query, unique).
                            -1  if the Scryfall call failed (network error / 5xx).
                             0  if Scryfall returned an error-object (no results).
    count_prints    int     total_cards returned by (query, "prints"); 0 on error.
    count_cards     int     total_cards returned by (query, "cards");  0 on error.

    fallback_query  str|null  QueryPool.fallback if present, else null.
    fallback_count  int|null  total_cards for fallback query (same unique); null if
                              no fallback or if primary_count != -1 but fetch failed.

    avg_prints      float   avg_price_usd(query, unique="prints",
                                          price_field=price_field).
                            0.0 on error or when primary_count == 0.
    avg_cards       float   avg_price_usd(query, unique="cards",
                                          price_field=price_field).
                            0.0 on error or when primary_count == 0.

Notes
-----
- A pool that appears in multiple slots (same label+query+unique) is fetched once
  and de-duplicated: the record's slot_name holds the FIRST slot that referenced it.
- Records are sorted by (set, kind, pool_label).
- Errors within a single pool are logged and skipped; the run never aborts.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.services.ev_core import (
    SCRYFALL_SEARCH_URL,
    QueryPool,
    ProductModel,
    model_for_code,
    scryfall_get,
    avg_price_usd,
    MODEL_REGISTRY,
)
from app.services.collector_ev import COLLECTOR_MODEL_REGISTRY
from app.services.set_registry import SET_REGISTRY, DRAFT_REGISTRY

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_FETCH_ERROR = -1
_count_cache: dict[tuple[str, str], int] = {}


# ---------------------------------------------------------------------------
# Scryfall helpers
# ---------------------------------------------------------------------------

def _scryfall_count(query: str, unique: str) -> int:
    """Return total_cards for (query, unique). Returns _FETCH_ERROR on network failure."""
    key = (query, unique)
    if key in _count_cache:
        return _count_cache[key]
    count: int
    try:
        r = scryfall_get(SCRYFALL_SEARCH_URL, params={"q": query, "unique": unique})
        data = r.json()
        if data.get("object") == "error":
            count = 0
        else:
            count = data.get("total_cards", 0) or 0
    except Exception as exc:
        resp = getattr(exc, "response", None)
        status_code = getattr(resp, "status_code", None)
        body: dict = {}
        try:
            if resp is not None:
                body = resp.json()
        except Exception:
            pass
        if status_code == 404 or (body.get("object") == "error" and body.get("status") == 404):
            count = 0
        else:
            log.debug("Scryfall unreachable for %r: %s", query, exc)
            return _FETCH_ERROR
    _count_cache[key] = count
    return count


def _safe_avg(query: str, unique: str, price_field: str) -> float:
    """avg_price_usd wrapper that never raises; returns 0.0 on any failure."""
    try:
        return avg_price_usd(query, unique=unique, price_field=price_field)
    except Exception as exc:
        log.debug("avg_price_usd failed for %r unique=%s: %s", query, unique, exc)
        return 0.0


# ---------------------------------------------------------------------------
# Model enumeration (shared with gate1_composition)
# ---------------------------------------------------------------------------

def enumerate_keys() -> list[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for k in MODEL_REGISTRY:
        keys.add(k)
    for k in COLLECTOR_MODEL_REGISTRY:
        keys.add(k)
    for sc in SET_REGISTRY:
        keys.add((sc.upper(), "box"))
    for sc in DRAFT_REGISTRY:
        keys.add((sc.upper(), "draft_box"))
    return sorted(keys)


def collect_pools(model: ProductModel) -> list[tuple[str, QueryPool]]:
    """Return (slot_name, pool) pairs in slot order."""
    results: list[tuple[str, QueryPool]] = []
    for slot in model.slots:
        for _prob, value in slot.outcomes:
            if isinstance(value, QueryPool):
                results.append((slot.name, value))
    return results


# ---------------------------------------------------------------------------
# Per-pool fetch
# ---------------------------------------------------------------------------

def fetch_pool_record(
    set_code: str,
    kind: str,
    slot_name: str,
    pool: QueryPool,
) -> dict:
    """Fetch all Scryfall data for one QueryPool and return a snapshot record."""
    primary_count = _scryfall_count(pool.primary, pool.unique)
    if primary_count == _FETCH_ERROR:
        log.warning("[%s|%s|%s] primary query unreachable", set_code, kind, pool.label)

    # fallback: only fetch if primary returned 0 (not error)
    fallback_count: Optional[int] = None
    if pool.fallback and primary_count == 0:
        fc = _scryfall_count(pool.fallback, pool.unique)
        fallback_count = None if fc == _FETCH_ERROR else fc
    elif pool.fallback and primary_count != _FETCH_ERROR:
        # still record count for informational purposes, but we used primary
        fc = _scryfall_count(pool.fallback, pool.unique)
        fallback_count = None if fc == _FETCH_ERROR else fc

    count_prints = 0
    count_cards = 0
    avg_prints = 0.0
    avg_cards = 0.0
    if primary_count not in (_FETCH_ERROR, 0):
        pc = _scryfall_count(pool.primary, "prints")
        cc = _scryfall_count(pool.primary, "cards")
        count_prints = pc if pc != _FETCH_ERROR else 0
        count_cards = cc if cc != _FETCH_ERROR else 0
        avg_prints = _safe_avg(pool.primary, "prints", pool.price_field)
        avg_cards = _safe_avg(pool.primary, "cards", pool.price_field)

    return {
        "set": set_code.upper(),
        "kind": kind,
        "slot_name": slot_name,
        "pool_label": pool.label,
        "query": pool.primary,
        "unique": pool.unique,
        "price_field": pool.price_field,
        "primary_count": primary_count,
        "count_prints": count_prints,
        "count_cards": count_cards,
        "fallback_query": pool.fallback,
        "fallback_count": fallback_count,
        "avg_prints": avg_prints,
        "avg_cards": avg_cards,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_collect(
    filter_set: Optional[str] = None,
    out_path: Optional[Path] = None,
) -> Path:
    all_keys = enumerate_keys()
    if filter_set:
        fc = filter_set.upper()
        all_keys = [k for k in all_keys if k[0] == fc]
        if not all_keys:
            log.error("No models found for set code %s", filter_set)
            sys.exit(1)

    log.info("Collecting %d model(s)…", len(all_keys))

    # De-duplicate pools: same (label, query, unique) → fetch once; record first slot
    seen: dict[tuple[str, str, str], dict] = {}
    records: list[dict] = []

    for i, (set_code, kind) in enumerate(all_keys):
        log.info("[%d/%d] %s %s", i + 1, len(all_keys), set_code, kind)
        try:
            model = model_for_code(set_code, kind)
        except Exception as exc:
            log.error("model_for_code(%s, %s) raised: %s", set_code, kind, exc)
            continue
        if model is None:
            log.debug("model_for_code(%s, %s) → None", set_code, kind)
            continue

        pool_pairs = collect_pools(model)
        for slot_name, pool in pool_pairs:
            dedup_key = (pool.label, pool.primary, pool.unique)
            if dedup_key in seen:
                continue
            try:
                rec = fetch_pool_record(set_code, kind, slot_name, pool)
            except Exception as exc:
                log.error("fetch_pool_record(%s, %s, %s) failed: %s",
                          set_code, kind, pool.label, exc)
                continue
            seen[dedup_key] = rec
            records.append(rec)

    records.sort(key=lambda r: (r["set"], r["kind"], r["pool_label"]))

    snapshot = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pools": records,
    }

    if out_path is None:
        out_dir = Path(__file__).parent / "out"
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / "gate1_snapshot.json"

    out_path.write_text(json.dumps(snapshot, indent=2))
    log.info("Snapshot written to %s  (%d pools)", out_path, len(records))
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Gate 1: snapshot collector")
    parser.add_argument("--set", metavar="CODE", help="Collect only this set code")
    parser.add_argument("--out", metavar="PATH", type=Path,
                        help="Output path (default: tools/out/gate1_snapshot.json)")
    args = parser.parse_args()
    run_collect(filter_set=args.set, out_path=args.out)


if __name__ == "__main__":
    main()
