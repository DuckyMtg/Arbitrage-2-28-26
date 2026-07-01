"""
Seed app/data/pool_expectations.yaml from the Gate 1 snapshot.

Writes entries for every pool that is "clean" — not FAIL_ZERO, not
FALLBACK_USED, and not a real-price-divergence inflation pool.

A pool is excluded from seeding if ANY of the following are true:
    1. primary_count == 0     (FAIL_ZERO or would become so)
    2. primary_count == -1    (FETCH_ERROR — no reliable count)
    3. fallback_count > 0 and primary_count == 0  (FALLBACK_USED)
    4. unique == "prints" AND count_prints / count_cards >= INFLATION_RATIO
       AND abs(avg_prints - avg_cards) >= PRICE_DIFF_THRESHOLD
       (real inflation with a material price impact; fix the query first)

For all other pools, the seeded count is:
    - count_prints  when pool.unique == "prints"
    - count_cards   when pool.unique == "cards"
    - primary_count otherwise (should not occur in practice)

Existing entries are preserved; only new keys are added (never overwritten).

Usage
-----
    python -m tools.seed_expectations [--snapshot PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

DEFAULT_SNAPSHOT = Path(__file__).parent / "out" / "gate1_snapshot.json"
EXPECTATIONS_PATH = _REPO_ROOT / "app" / "data" / "pool_expectations.yaml"

# Pools with count_prints/count_cards >= this AND price diff >= PRICE_DIFF_THRESHOLD
# are considered "real inflation" and are excluded from seeding.
INFLATION_RATIO = 1.5
PRICE_DIFF_THRESHOLD = 1.00  # USD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_snapshot(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_existing(path: Path) -> dict[str, dict]:
    """Load existing YAML expectations; returns {} if file absent."""
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception as exc:
        log.warning("Could not load %s: %s", path, exc)
        return {}


def _select_count(pool: dict) -> int:
    unique = pool.get("unique", "prints")
    if unique == "prints":
        return pool.get("count_prints", pool["primary_count"])
    if unique == "cards":
        return pool.get("count_cards", pool["primary_count"])
    return pool["primary_count"]


def _is_excluded(pool: dict) -> tuple[bool, str]:
    """Return (excluded, reason). reason is '' when not excluded."""
    primary = pool["primary_count"]
    fb = pool.get("fallback_count")

    if primary == -1:
        return True, "FETCH_ERROR"
    if primary == 0 and (fb is None or fb == 0):
        return True, "FAIL_ZERO"
    if primary == 0 and fb and fb > 0:
        return True, "FALLBACK_USED"

    unique = pool.get("unique", "prints")
    cp = pool.get("count_prints", 0)
    cc = pool.get("count_cards", 0)
    ap = pool.get("avg_prints", 0.0)
    ac = pool.get("avg_cards", 0.0)

    if unique == "prints" and cc > 0:
        ratio = cp / cc
        if ratio >= INFLATION_RATIO and abs(ap - ac) >= PRICE_DIFF_THRESHOLD:
            return True, f"INFLATION (ratio={ratio:.2f}, Δprice={abs(ap-ac):.2f})"

    return False, ""


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def seed(snapshot_path: Path, dry_run: bool = False) -> None:
    if not snapshot_path.exists():
        log.error("Snapshot not found: %s — run tools/gate1_collect.py first", snapshot_path)
        sys.exit(1)

    snapshot = _load_snapshot(snapshot_path)
    generated_at: str = snapshot.get("generated_at", "unknown")
    pools: list[dict] = snapshot.get("pools", [])

    existing = _load_existing(EXPECTATIONS_PATH)

    new_entries: dict[str, dict] = {}
    skipped_excluded = 0
    skipped_existing = 0

    for pool in pools:
        key = f"{pool['set']}|{pool['kind']}|{pool['pool_label']}"

        if key in existing:
            skipped_existing += 1
            continue

        excluded, reason = _is_excluded(pool)
        if excluded:
            log.debug("Skipping %s (%s)", key, reason)
            skipped_excluded += 1
            continue

        count = _select_count(pool)
        new_entries[key] = {
            "expected": count,
            "unique": pool.get("unique", "prints"),
            "source": f"baseline-seed {generated_at}",
        }

    log.info(
        "Snapshot pools: %d  |  existing: %d  |  excluded: %d  |  new: %d",
        len(pools), skipped_existing, skipped_excluded, len(new_entries),
    )

    if not new_entries:
        log.info("Nothing new to seed.")
        return

    if dry_run:
        log.info("DRY RUN — would write %d new entries:", len(new_entries))
        for key, entry in sorted(new_entries.items()):
            log.info("  %s: expected=%d unique=%s", key, entry["expected"], entry["unique"])
        return

    # Merge: preserve existing, append new (hand-edited entries survive)
    merged = dict(existing)
    merged.update(new_entries)
    _write_yaml(EXPECTATIONS_PATH, merged)
    log.info("Wrote %d total entries to %s  (%d new)", len(merged), EXPECTATIONS_PATH, len(new_entries))


def _write_yaml(path: Path, data: dict[str, dict]) -> None:
    """Write YAML manually to preserve the expected format (no aliases, compact)."""
    lines = [
        "# Gate 1 composition expectations — ground-truth oracle for QueryPool card counts.",
        "#",
        "# Format: each key is \"{SET_CODE}|{kind}|{pool_label}\", value has:",
        "#   expected: <int>   # expected primary_count from Scryfall",
        "#   unique:   <str>   # unique mode used (prints or cards)",
        "#   source: <str>     # where this count was verified",
        "#",
        "# Status classification uses this file:",
        "#   PASS    — count at recorded unique mode matches expected",
        "#   MISMATCH — count != expected",
        "#   Pools with no entry here are UNVERIFIED.",
        "",
    ]
    for key in sorted(data.keys()):
        entry = data[key]
        lines.append(f"{key}:")
        lines.append(f"  expected: {entry['expected']}")
        if "unique" in entry:
            lines.append(f"  unique: {entry['unique']!r}")
        lines.append(f"  source: {entry['source']!r}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Seed pool_expectations.yaml from snapshot")
    parser.add_argument("--snapshot", metavar="PATH", type=Path, default=DEFAULT_SNAPSHOT,
                        help=f"Snapshot JSON (default: {DEFAULT_SNAPSHOT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written without modifying the file")
    args = parser.parse_args()
    seed(args.snapshot, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
