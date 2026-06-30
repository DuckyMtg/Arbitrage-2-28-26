"""
Gate 1: Composition validation harness.

Checks that every QueryPool in every EV model returns a sane number of cards
from Scryfall, without computing or comparing prices. Catches zero-result
queries and unique=prints inflation bugs.

Usage:
    python -m tools.gate1_composition
    python -m tools.gate1_composition --set BLB
    python -m tools.gate1_composition --limit 5
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter
from dataclasses import dataclass
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
    MODEL_REGISTRY,
)
from app.services.collector_ev import COLLECTOR_MODEL_REGISTRY
from app.services.set_registry import SET_REGISTRY, DRAFT_REGISTRY

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

STATUS_ORDER = [
    "FETCH_ERROR",
    "FAIL_ZERO",
    "FALLBACK_USED",
    "MISMATCH",
    "INFLATION_SUSPECT",
    "UNVERIFIED",
    "PASS",
]

_count_cache: dict[tuple[str, str], int] = {}
_FETCH_ERROR = -1


def _scryfall_count(query: str, unique: str) -> int:
    """Return card count. Returns 0 for empty results. Returns _FETCH_ERROR on network error."""
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
        if status_code == 404:
            count = 0
        else:
            body: dict = {}
            try:
                if resp is not None:
                    body = resp.json()
            except Exception:
                pass
            if body.get("object") == "error" and body.get("status") == 404:
                count = 0
            else:
                log.debug("Scryfall unreachable for %r: %s", query, exc)
                return _FETCH_ERROR

    _count_cache[key] = count
    return count


def collect_pools(model: ProductModel) -> list[tuple[str, QueryPool]]:
    results: list[tuple[str, QueryPool]] = []
    for slot in model.slots:
        for _prob, value in slot.outcomes:
            if isinstance(value, QueryPool):
                results.append((slot.name, value))
    return results


@dataclass
class PoolResult:
    set_code: str
    kind: str
    slot_name: str
    pool_label: str
    query: str
    unique: str
    primary_count: int
    fallback_count: Optional[int]
    prints_count: int
    cards_count: int
    inflation_ratio: Optional[float]
    expected_count: Optional[int]
    status: str


def load_expectations() -> dict[str, dict]:
    path = _REPO_ROOT / "app" / "data" / "pool_expectations.yaml"
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception as exc:
        log.warning("Could not load pool_expectations.yaml: %s", exc)
        return {}


def classify(
    primary_count: int,
    fallback_count: Optional[int],
    prints_count: int,
    cards_count: int,
    inflation_ratio: Optional[float],
    unique: str,
    expected_count: Optional[int],
) -> str:
    if primary_count == _FETCH_ERROR:
        return "FETCH_ERROR"
    if primary_count == 0:
        if expected_count == 0:
            return "PASS"
        if fallback_count is None or fallback_count == 0:
            return "FAIL_ZERO"
        return "FALLBACK_USED"
    if expected_count is not None and primary_count != expected_count:
        return "MISMATCH"
    if unique == "prints" and inflation_ratio is not None and inflation_ratio >= 1.5:
        return "INFLATION_SUSPECT"
    if expected_count is None:
        return "UNVERIFIED"
    return "PASS"


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


def check_model(
    set_code: str,
    kind: str,
    expectations: dict[str, dict],
) -> list[PoolResult]:
    try:
        model = model_for_code(set_code, kind)
    except Exception as exc:
        log.error("model_for_code(%s, %s) raised: %s", set_code, kind, exc)
        return []
    if model is None:
        log.debug("model_for_code(%s, %s) returned None", set_code, kind)
        return []

    pool_pairs = collect_pools(model)
    proto_cache: dict[tuple[str, str, str], PoolResult] = {}
    results: list[PoolResult] = []

    for slot_name, pool in pool_pairs:
        exp_key = f"{set_code.upper()}|{kind}|{pool.label}"
        exp_entry = expectations.get(exp_key)
        expected_count: Optional[int] = exp_entry.get("expected") if exp_entry else None

        cache_key = (pool.label, pool.primary, pool.unique)
        if cache_key in proto_cache:
            proto = proto_cache[cache_key]
            results.append(PoolResult(
                set_code=set_code.upper(), kind=kind,
                slot_name=slot_name, pool_label=pool.label,
                query=pool.primary, unique=pool.unique,
                primary_count=proto.primary_count,
                fallback_count=proto.fallback_count,
                prints_count=proto.prints_count,
                cards_count=proto.cards_count,
                inflation_ratio=proto.inflation_ratio,
                expected_count=expected_count,
                status=classify(
                    proto.primary_count, proto.fallback_count,
                    proto.prints_count, proto.cards_count,
                    proto.inflation_ratio, pool.unique, expected_count,
                ),
            ))
            continue

        primary_count = _scryfall_count(pool.primary, pool.unique)
        if primary_count == _FETCH_ERROR:
            log.warning("[%s|%s|%s] Scryfall unreachable", set_code, kind, pool.label)

        fallback_count: Optional[int] = None
        if pool.fallback and primary_count != _FETCH_ERROR:
            fb = _scryfall_count(pool.fallback, pool.unique)
            fallback_count = fb if fb != _FETCH_ERROR else None

        prints_count = 0
        cards_count = 0
        if primary_count != _FETCH_ERROR:
            pc = _scryfall_count(pool.primary, "prints")
            cc = _scryfall_count(pool.primary, "cards")
            prints_count = pc if pc != _FETCH_ERROR else 0
            cards_count = cc if cc != _FETCH_ERROR else 0

        inflation_ratio: Optional[float] = None
        if cards_count > 0:
            inflation_ratio = prints_count / cards_count

        status = classify(
            primary_count, fallback_count, prints_count, cards_count,
            inflation_ratio, pool.unique, expected_count,
        )

        result = PoolResult(
            set_code=set_code.upper(), kind=kind,
            slot_name=slot_name, pool_label=pool.label,
            query=pool.primary, unique=pool.unique,
            primary_count=primary_count,
            fallback_count=fallback_count,
            prints_count=prints_count,
            cards_count=cards_count,
            inflation_ratio=inflation_ratio,
            expected_count=expected_count,
            status=status,
        )
        proto_cache[cache_key] = result
        results.append(result)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Gate 1: Composition validation harness")
    parser.add_argument("--set", metavar="CODE", help="Run only this set code")
    parser.add_argument("--limit", metavar="N", type=int, help="Cap number of models checked")
    args = parser.parse_args()

    expectations = load_expectations()

    all_keys = enumerate_keys()
    if args.set:
        filter_code = args.set.upper()
        all_keys = [k for k in all_keys if k[0] == filter_code]
        if not all_keys:
            log.error("No models found for set code %s", filter_code)
            sys.exit(1)
    if args.limit:
        all_keys = all_keys[: args.limit]

    log.info("Checking %d model(s)...", len(all_keys))

    all_results: list[PoolResult] = []
    for i, (set_code, kind) in enumerate(all_keys):
        log.info("[%d/%d] %s %s", i + 1, len(all_keys), set_code, kind)
        try:
            results = check_model(set_code, kind, expectations)
            all_results.extend(results)
        except Exception as exc:
            log.error("check_model(%s, %s) failed: %s", set_code, kind, exc)

    status_rank = {s: i for i, s in enumerate(STATUS_ORDER)}
    all_results.sort(key=lambda r: (
        status_rank.get(r.status, 99), r.set_code, r.kind, r.pool_label
    ))

    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / "gate1_composition.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "set_code", "kind", "slot_name", "pool_label", "query", "unique",
            "primary_count", "fallback_count", "prints_count", "cards_count",
            "inflation_ratio", "expected_count", "status",
        ])
        for r in all_results:
            writer.writerow([
                r.set_code, r.kind, r.slot_name, r.pool_label, r.query, r.unique,
                r.primary_count,
                "" if r.fallback_count is None else r.fallback_count,
                r.prints_count, r.cards_count,
                "" if r.inflation_ratio is None else f"{r.inflation_ratio:.3f}",
                "" if r.expected_count is None else r.expected_count,
                r.status,
            ])
    log.info("CSV written to %s", csv_path)

    status_counts = Counter(r.status for r in all_results)
    total = len(all_results)

    print(f"\n{'='*60}")
    print("Gate 1: Composition Summary")
    print(f"{'='*60}")
    print(f"Total pools checked: {total}")
    print()
    for s in STATUS_ORDER:
        n = status_counts.get(s, 0)
        print(f"  {s:<22} {n:>4}")
    print()

    attention_statuses = {"FETCH_ERROR", "FAIL_ZERO", "FALLBACK_USED", "MISMATCH", "INFLATION_SUSPECT"}
    attention = [r for r in all_results if r.status in attention_statuses]
    if attention:
        print(f"{'─'*72}")
        print("Pools requiring attention:")
        print(f"{'─'*72}")
        col_w = [6, 15, 18, 24, 8, 10]
        headers = ["SET", "KIND", "STATUS", "POOL_LABEL", "PRIMARY", "INFLATION"]
        print("  ".join(h.ljust(w) for h, w in zip(headers, col_w)))
        print("  ".join("-" * w for w in col_w))
        for r in attention:
            ir = f"{r.inflation_ratio:.2f}" if r.inflation_ratio is not None else "n/a"
            pc = "ERR" if r.primary_count == _FETCH_ERROR else str(r.primary_count)
            row = [r.set_code, r.kind, r.status, r.pool_label, pc, ir]
            print("  ".join(v.ljust(w) for v, w in zip(row, col_w)))
        print()
    else:
        print("No pools with FETCH_ERROR/FAIL_ZERO/FALLBACK_USED/MISMATCH/INFLATION_SUSPECT.")
        print()

    print(f"Full results: {csv_path}")


if __name__ == "__main__":
    main()
