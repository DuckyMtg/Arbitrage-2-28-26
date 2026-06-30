"""
Gate 1: INFLATION_SUSPECT triage.

For each INFLATION_SUSPECT pool (excluding *_land_* pools) compute
avg_price_usd at unique="prints" vs unique="cards" and rank by absolute
difference. A large diff means extra printings carry real prices and the
slot is genuinely over-counted; near-zero means the duplicates are null-
priced (foils dropped by Scryfall) and harmless.

Usage:
    python -m tools.gate1_inflation_triage
    python -m tools.gate1_inflation_triage --csv path/to/gate1_composition.csv
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.services.ev_core import avg_price_usd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_CSV = _REPO_ROOT / "tools" / "out" / "gate1_composition.csv"


def _is_land(pool_label: str) -> bool:
    return "_land_" in pool_label


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=str(_DEFAULT_CSV),
                        help="Path to gate1_composition.csv")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        log.error("CSV not found: %s", csv_path)
        sys.exit(1)

    with open(csv_path) as f:
        rows = list(csv.DictReader(f))

    suspect = [
        r for r in rows
        if r["status"] == "INFLATION_SUSPECT" and not _is_land(r["pool_label"])
    ]

    # Deduplicate by (query, price_field) — many pools share the same query
    seen: dict[tuple[str, str], dict] = {}
    deduped: list[dict] = []
    for r in suspect:
        key = (r["query"], r.get("price_field", "usd"))
        if key not in seen:
            seen[key] = r
            deduped.append(r)

    log.info("Triaging %d unique INFLATION_SUSPECT queries (%d total rows, land excluded)",
             len(deduped), len(suspect))

    results = []
    for i, r in enumerate(deduped):
        query = r["query"]
        # All INFLATION_SUSPECT pools use unique=prints (that's why they're flagged)
        # price_field not in CSV — infer from unique column
        price_field = "usd_foil" if r.get("unique") == "cards" else "usd"

        log.info("[%d/%d] %s|%s|%s", i + 1, len(deduped),
                 r["set_code"], r["kind"], r["pool_label"])
        try:
            avg_p = avg_price_usd(query, unique="prints", price_field=price_field)
            avg_c = avg_price_usd(query, unique="cards",  price_field=price_field)
        except Exception as exc:
            log.warning("  SKIP — %s", exc)
            continue

        abs_diff = abs(avg_p - avg_c)
        pct_diff = (abs_diff / avg_c * 100) if avg_c > 0 else float("inf")
        results.append({
            "set":        r["set_code"],
            "kind":       r["kind"],
            "pool_label": r["pool_label"],
            "query":      query,
            "avg_prints": avg_p,
            "avg_cards":  avg_c,
            "abs_diff":   abs_diff,
            "pct_diff":   pct_diff,
        })

    results.sort(key=lambda x: x["abs_diff"], reverse=True)

    print(f"\n{'='*90}")
    print("Gate 1: INFLATION_SUSPECT Triage  (sorted by |avg_prints - avg_cards|, desc)")
    print(f"{'='*90}")
    hdr = f"{'SET':<6}  {'KIND':<15}  {'POOL_LABEL':<28}  {'avg_prints':>10}  {'avg_cards':>10}  {'abs_diff':>9}  {'pct_diff':>9}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        pct = f"{r['pct_diff']:.1f}%" if r["pct_diff"] != float("inf") else "  ∞"
        print(f"{r['set']:<6}  {r['kind']:<15}  {r['pool_label']:<28}  "
              f"{r['avg_prints']:>10.4f}  {r['avg_cards']:>10.4f}  "
              f"{r['abs_diff']:>9.4f}  {pct:>9}")

    out_path = _REPO_ROOT / "tools" / "out" / "gate1_inflation_triage.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "set", "kind", "pool_label", "query",
            "avg_prints", "avg_cards", "abs_diff", "pct_diff",
        ])
        writer.writeheader()
        writer.writerows(results)
    log.info("CSV written to %s", out_path)


if __name__ == "__main__":
    main()
