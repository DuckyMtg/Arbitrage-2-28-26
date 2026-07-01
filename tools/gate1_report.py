"""
Gate 1: Offline report generator.

Reads ``tools/out/gate1_snapshot.json`` (produced by ``tools/gate1_collect.py``)
and emits the same classification tables as the former online harness, with no
network calls.

Usage
-----
    python -m tools.gate1_report [--snapshot PATH] [--set CODE] [--csv PATH]

Classification rules (applied per pool record from the snapshot)
-----------------------------------------------------------------
FETCH_ERROR        primary_count == -1
FAIL_ZERO          primary_count == 0 AND (no fallback OR fallback_count == 0)
                   UNLESS expected_count == 0 in pool_expectations.yaml → PASS
FALLBACK_USED      primary_count == 0 AND fallback_count > 0
MISMATCH           primary_count != expected_count (when expectation exists)
INFLATION_SUSPECT  unique == "prints" AND count_prints / count_cards >= 1.5
UNVERIFIED         primary_count > 0 AND no expectation
PASS               primary_count matches expectation (or expected_count == 0 when
                   primary_count == 0)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

DEFAULT_SNAPSHOT = Path(__file__).parent / "out" / "gate1_snapshot.json"
INFLATION_RATIO_THRESHOLD = 1.5

STATUS_ORDER = [
    "FETCH_ERROR",
    "FAIL_ZERO",
    "FALLBACK_USED",
    "MISMATCH",
    "INFLATION_SUSPECT",
    "UNVERIFIED",
    "PASS",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PoolReport:
    set_code: str
    kind: str
    slot_name: str
    pool_label: str
    query: str
    unique: str
    price_field: str
    primary_count: int
    count_prints: int
    count_cards: int
    fallback_query: Optional[str]
    fallback_count: Optional[int]
    avg_prints: float
    avg_cards: float
    expected_count: Optional[int]
    inflation_ratio: Optional[float]
    status: str


# ---------------------------------------------------------------------------
# Expectations loader
# ---------------------------------------------------------------------------

def load_expectations(repo_root: Path = _REPO_ROOT) -> dict[str, dict]:
    path = repo_root / "app" / "data" / "pool_expectations.yaml"
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


# ---------------------------------------------------------------------------
# Snapshot loader
# ---------------------------------------------------------------------------

def load_snapshot(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(
    primary_count: int,
    fallback_count: Optional[int],
    unique: str,
    count_prints: int,
    count_cards: int,
    expected_count: Optional[int],
) -> str:
    if primary_count == -1:
        return "FETCH_ERROR"
    if primary_count == 0:
        if expected_count == 0:
            return "PASS"
        if fallback_count is None or fallback_count == 0:
            return "FAIL_ZERO"
        return "FALLBACK_USED"
    if expected_count is not None and primary_count != expected_count:
        return "MISMATCH"
    inflation_ratio = count_prints / count_cards if count_cards > 0 else None
    if unique == "prints" and inflation_ratio is not None and inflation_ratio >= INFLATION_RATIO_THRESHOLD:
        return "INFLATION_SUSPECT"
    if expected_count is None:
        return "UNVERIFIED"
    return "PASS"


# ---------------------------------------------------------------------------
# Core: build PoolReport list from snapshot + expectations
# ---------------------------------------------------------------------------

def build_reports(
    snapshot: dict,
    expectations: dict[str, dict],
    filter_set: Optional[str] = None,
) -> list[PoolReport]:
    reports: list[PoolReport] = []
    for rec in snapshot.get("pools", []):
        set_code: str = rec["set"]
        if filter_set and set_code != filter_set.upper():
            continue
        kind: str = rec["kind"]
        exp_key = f"{set_code}|{kind}|{rec['pool_label']}"
        exp_entry = expectations.get(exp_key)
        expected_count: Optional[int] = exp_entry.get("expected") if exp_entry else None

        primary_count: int = rec["primary_count"]
        count_prints: int = rec.get("count_prints", 0)
        count_cards: int = rec.get("count_cards", 0)
        unique: str = rec["unique"]

        inflation_ratio: Optional[float] = (
            count_prints / count_cards if count_cards > 0 else None
        )

        status = classify(
            primary_count,
            rec.get("fallback_count"),
            unique,
            count_prints,
            count_cards,
            expected_count,
        )

        reports.append(PoolReport(
            set_code=set_code,
            kind=kind,
            slot_name=rec.get("slot_name", ""),
            pool_label=rec["pool_label"],
            query=rec["query"],
            unique=unique,
            price_field=rec.get("price_field", "usd"),
            primary_count=primary_count,
            count_prints=count_prints,
            count_cards=count_cards,
            fallback_query=rec.get("fallback_query"),
            fallback_count=rec.get("fallback_count"),
            avg_prints=rec.get("avg_prints", 0.0),
            avg_cards=rec.get("avg_cards", 0.0),
            expected_count=expected_count,
            inflation_ratio=inflation_ratio,
            status=status,
        ))

    status_rank = {s: i for i, s in enumerate(STATUS_ORDER)}
    reports.sort(key=lambda r: (
        status_rank.get(r.status, 99), r.set_code, r.kind, r.pool_label
    ))
    return reports


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def write_csv(reports: list[PoolReport], path: Path) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "set_code", "kind", "slot_name", "pool_label", "query", "unique",
            "price_field", "primary_count", "fallback_count",
            "count_prints", "count_cards", "inflation_ratio",
            "avg_prints", "avg_cards", "expected_count", "status",
        ])
        for r in reports:
            writer.writerow([
                r.set_code, r.kind, r.slot_name, r.pool_label, r.query, r.unique,
                r.price_field,
                r.primary_count,
                "" if r.fallback_count is None else r.fallback_count,
                r.count_prints, r.count_cards,
                "" if r.inflation_ratio is None else f"{r.inflation_ratio:.3f}",
                f"{r.avg_prints:.4f}", f"{r.avg_cards:.4f}",
                "" if r.expected_count is None else r.expected_count,
                r.status,
            ])


def print_summary(reports: list[PoolReport], snapshot_path: Path, generated_at: str) -> None:
    status_counts = Counter(r.status for r in reports)
    total = len(reports)

    print(f"\n{'='*60}")
    print("Gate 1: Composition Report (offline)")
    print(f"Snapshot: {snapshot_path}")
    print(f"Generated: {generated_at}")
    print(f"{'='*60}")
    print(f"Total pools: {total}")
    print()
    for s in STATUS_ORDER:
        n = status_counts.get(s, 0)
        print(f"  {s:<22} {n:>4}")
    print()

    attention_statuses = {"FETCH_ERROR", "FAIL_ZERO", "FALLBACK_USED", "MISMATCH", "INFLATION_SUSPECT"}
    attention = [r for r in reports if r.status in attention_statuses]
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
            pc = "ERR" if r.primary_count == -1 else str(r.primary_count)
            row = [r.set_code, r.kind, r.status, r.pool_label, pc, ir]
            print("  ".join(v.ljust(w) for v, w in zip(row, col_w)))
        print()
    else:
        print("No pools requiring attention.")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Gate 1: offline composition report")
    parser.add_argument("--snapshot", metavar="PATH", type=Path,
                        default=DEFAULT_SNAPSHOT,
                        help=f"Snapshot JSON (default: {DEFAULT_SNAPSHOT})")
    parser.add_argument("--set", metavar="CODE", help="Filter to one set code")
    parser.add_argument("--csv", metavar="PATH", type=Path,
                        help="Write CSV to this path")
    args = parser.parse_args()

    if not args.snapshot.exists():
        log.error("Snapshot not found: %s — run tools/gate1_collect.py first", args.snapshot)
        sys.exit(1)

    snapshot = load_snapshot(args.snapshot)
    generated_at = snapshot.get("generated_at", "unknown")

    age_warning(generated_at)

    expectations = load_expectations()
    reports = build_reports(snapshot, expectations, filter_set=args.set)

    print_summary(reports, args.snapshot, generated_at)

    csv_path = args.csv
    if csv_path is None:
        out_dir = Path(__file__).parent / "out"
        out_dir.mkdir(exist_ok=True)
        csv_path = out_dir / "gate1_composition.csv"
    write_csv(reports, csv_path)
    log.info("CSV written to %s", csv_path)


def age_warning(generated_at: str) -> None:
    try:
        ts = datetime.fromisoformat(generated_at)
        age = datetime.now(timezone.utc) - ts
        if age.days > 30:
            log.warning("Snapshot is %d days old — consider re-running gate1_collect.py", age.days)
    except Exception:
        pass


if __name__ == "__main__":
    main()
